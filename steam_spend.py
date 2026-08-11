#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam Spend Calculator —— 统计你在 Steam 上总共花了多少钱。

为什么需要这个程序:
    Steam 没有公开的"购买历史"API, 只能抓登录后的网页
    https://store.steampowered.com/account/history/ 来统计。
    本程序替你自动登录、翻页、解析、汇总。

两种使用模式:
    1. 自动登录模式 (推荐, 适合所有人):
           python steam_spend.py
       会弹出一个真实的浏览器窗口, 你在里面登录 Steam (密码只输入到
       Steam 官网), 登录后程序自动抓取并统计。全程不需要输入密码给程序。

    2. Cookie 模式 (适合熟悉浏览器开发者工具的人):
           python steam_spend.py --cookies-file cookies.json
       从你自己已登录 Steam 的浏览器里导出 cookie 存成 JSON 文件。

依赖安装:
    pip install -r requirements.txt
    自动登录模式额外需要:  python -m playwright install chromium

可选参数:
    --cookies-file <文件>   使用 cookie 文件 (JSON), 跳过浏览器登录
    --export <文件.csv>     导出逐条明细为 CSV
    --proxy <URL>           指定代理, 如 http://127.0.0.1:7890 (也可用环境变量 HTTPS_PROXY)
    --debug                 打印调试信息 (解析失败时定位问题)
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import List, Optional

HISTORY_URL = "https://store.steampowered.com/account/history/"
AJAX_MORE_URL = "https://store.steampowered.com/account/AjaxLoadMoreHistory/"

# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    raw: str                  # 该行的原始文本
    date: str = ""            # 日期
    item: str = ""            # 条目描述 (游戏名 / 市场物品 / 充值 等)
    amount: Optional[float] = None   # 金额数值 (负数=支出, 正数=收入)
    currency: str = ""        # 货币符号 (如 $ ¥ €)
    category: str = "other"   # purchase / market / refund / wallet / gift / other


@dataclass
class Report:
    transactions: List[Transaction] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)   # 未能解析的行
    ignored_currency: int = 0   # 因非 CNY/HKD/USD 币种被忽略的交易数

    @property
    def expense(self) -> float:
        """总支出 (所有负金额之和的绝对值)"""
        return abs(sum(t.amount for t in self.transactions
                       if t.amount is not None and t.amount < 0))

    @property
    def income(self) -> float:
        """总收入 (充值 + 退款 + 市场卖出等所有正金额之和)"""
        return sum(t.amount for t in self.transactions
                   if t.amount is not None and t.amount > 0)

    def by_category(self, negative_only=True):
        """按分类汇总金额。negative_only=True 只统计支出类。"""
        out = {}
        for t in self.transactions:
            if t.amount is None:
                continue
            if negative_only and t.amount >= 0:
                continue
            out[t.category] = out.get(t.category, 0.0) + t.amount
        return out

    def positive_by_category(self):
        """按分类汇总正金额 (充值/退款/市场卖出/货币转换)。"""
        out = {}
        for t in self.transactions:
            if t.amount is not None and t.amount > 0:
                out[t.category] = out.get(t.category, 0.0) + t.amount
        return out

    def expense_by_currency(self):
        """按币种汇总支出 (负金额绝对值)。"""
        out = {}
        for t in self.transactions:
            if t.amount is not None and t.amount < 0:
                c = t.currency or "UNKNOWN"
                out[c] = out.get(c, 0.0) + abs(t.amount)
        return out


# ---------------------------------------------------------------------------
# 解析器: 把购买历史页面的 HTML 解析成交易列表
# ---------------------------------------------------------------------------
# 注意: Steam 页面的 HTML 结构可能随改版变化。解析器做两层兜底:
#   1. 按已知的 wallet_history_row 结构解析 (HTMLParser 按 div 深度提取)
#   2. 兜底: 按行文本 + 正则提取金额
# 若失败行太多, 会提示用户把 --debug 的输出发给开发者调整。

from html.parser import HTMLParser

# 强制 UTF-8 输出: Windows 下默认 GBK 编码无法表示 ¥ 等符号, 会导致
# UnicodeEncodeError 崩溃 (表现为窗口闪退)。errors=replace 兜底。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

AMOUNT_RE = re.compile(
    r"(?<![0-9])(?P<sign>[+-])?\s*(?P<cur>[$¥￥€£₩]|R\$|USD|CNY|EUR|GBP|KRW)?"
    r"\s*(?P<num>\d{1,3}(?:,\d{3})*(?:\.\d{1,2})|\.\d{1,2})(?![0-9])"
)

# 日期: 支持 "1 Oct, 2021" / "Oct 1, 2021" / "2021-10-1" / "2021年10月1日" / "2021 年 10 月 1 日"
# 注意: 中文与日期可能无空格分隔, 不能用 \b (Python re 的 \w 含汉字)
DATE_RE = re.compile(
    r"(?<![0-9A-Za-z年])("
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{1,2}\s+[A-Za-z]{3},?\s+\d{4}"
    r"|[A-Za-z]{3}\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}[-\/]\d{1,2}[-\/]\d{1,2}"
    r")(?![0-9])"
)


class _HistoryRowExtractor(HTMLParser):
    """按 class 含 wallet_table_row / wallet_history_row 的 div/td 提取完整文本。

    Steam 改版前交易行是 <div class="wallet_history_row">, 改版后是
    <td class="wallet_table_row"> (行内还有嵌套 td)。对 div 和 td 都支持,
    深度对所有非自闭合标签计数, 保证嵌套正确配对。
    """

    _VOID_TAGS = {"br", "img", "input", "hr", "meta", "link", "area", "base", "col",
                  "embed", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: List[str] = []
        self._depth = 0      # 当前标签嵌套深度
        self._target = None  # 命中目标行时的 [起始深度, 文本片段列表]

    def handle_starttag(self, tag, attrs):
        if self._target is None:
            cls = dict(attrs).get("class", "") or ""
            # 旧版: <div class="wallet_history_row">; 新版: <tr class="wallet_table_row">
            if tag in ("div", "td", "tr") and ("wallet_table_row" in cls or "wallet_history_row" in cls):
                self._target = [self._depth, []]
        if tag not in self._VOID_TAGS:
            self._depth += 1

    def handle_startendtag(self, tag, attrs):
        pass  # 自闭合标签不增减深度

    def handle_endtag(self, tag):
        if tag in self._VOID_TAGS:
            return
        self._depth -= 1
        if self._target is not None and self._depth == self._target[0]:
            text = " ".join("".join(self._target[1]).split())
            self.rows.append(text)
            self._target = None

    def handle_data(self, data):
        if self._target is not None:
            self._target[1].append(data)

def _classify(text: str) -> str:
    """先按文本关键词定交易类型 (不依赖金额)。

    顺序很重要: 货币转换 -> 退款 -> 充值(钱包资金) -> 市场 -> 购买。
    充值行的文本也含"购买"("已购买 xx 钱包资金 购买"), 所以"钱包资金"
    判断必须先于购买。退款行也可能含"钱包资金"(退款回到钱包), 退款在前。
    """
    low = text.lower()
    if "货币转换" in text or "转换" in low or "conversion" in low:
        return "currency_convert"
    if "退款" in text or "refund" in low:
        return "refund"
    if ("钱包资金" in text or "wallet funds" in low or "wallet credit" in low
            or "充值" in text or "add funds" in low or "存入" in text):
        return "wallet"
    if "market" in low or "市场" in text:
        return "market"
    return "purchase"


def _amounts(text: str):
    """提取文本里所有金额。返回 [(数值, 符号, 货币), ...]"""
    out = []
    for m in AMOUNT_RE.finditer(text):
        num = m.group("num").replace(",", "")
        try:
            v = float(num)
        except ValueError:
            continue
        out.append((v, m.group("sign") or "", m.group("cur") or ""))
    return out


def _extract_amount_by_category(text: str, category: str):
    """按类型提取金额, 返回 (数值, 货币符号) 或 (None, "")。

    - purchase(购买): 支出。优先带负号的实付(钱包扣款); 没有负号时取
      最后一个价格(支付宝/微信支付的折后实付), 标为支出。
    - market: 按符号 (+卖出 / -买入); 无符号取第一个。
    - refund / wallet / currency_convert: 收入。优先 + 号, 否则取第一个。
    """
    amts = _amounts(text)
    if not amts:
        return None, ""

    def pick(sign):
        for v, s, c in amts:
            if s == sign:
                return v, c
        return None

    if category == "purchase":
        r = pick("-")
        if r:
            return -r[0], r[1]
        v, c = amts[-1][0], amts[-1][2]     # 无负号: 最后一个价格 = 折后实付
        return -v, c
    if category == "market":
        r = pick("+")
        if r:
            return r[0], r[1]
        r = pick("-")
        if r:
            return -r[0], r[1]
        return abs(amts[0][0]), amts[0][2]
    # refund / wallet / currency_convert: 收入
    r = pick("+")
    if r:
        return r[0], r[1]
    return abs(amts[0][0]), amts[0][2]


# 面向国内用户: 只支持人民币/美刀/港元, 其他币种一律忽略
SUPPORTED_CURRENCIES = ("CNY", "HKD", "USD")
CURRENCY_NAMES = {
    "CNY": "人民币",
    "HKD": "港元",
    "USD": "美刀",
}


def _detect_currency(text: str, fallback: str = "") -> str:
    """从行文本识别币种, 仅返回 CNY/HKD/USD, 其余返回 OTHER。

    优先级: 货币转换行目标币种 > 提取到的实际扣款符号 (如 -¥48.35 的 ¥,
    此时即使文本里同时出现 USD 定价也应算 CNY) > 文本明确标记。
    """
    upper = text.upper()
    # 货币转换行: 目标币种在文本里
    m = re.search(r"货币转换至\s*([A-Z]{3})", text)
    if m:
        return m.group(1) if m.group(1) in SUPPORTED_CURRENCIES else "OTHER"
    # 提取到的符号代表实际扣款币种, 最可靠
    if fallback in ("¥", "￥"):
        return "CNY"
    if fallback == "$":
        # $ 可能是 HKD (HK$) 或 USD
        if "HK$" in text or "HKD" in upper:
            return "HKD"
        return "USD"
    if fallback:
        # 兜底符号不在支持列表里 (如 RM) → 其他币种
        return fallback if fallback in SUPPORTED_CURRENCIES else "OTHER"
    # 无符号可循: 靠文本标记
    if "HK$" in text or "HKD" in upper:
        return "HKD"
    if "USD" in upper:
        return "USD"
    if "¥" in text or "￥" in text:
        return "CNY"
    return "OTHER"


def _split_row(row_div_html: str) -> Transaction:
    """从单行 div 的 HTML 提取交易信息。"""
    # 去掉标签只留文本
    text = re.sub(r"<[^>]+>", " ", row_div_html)
    text = re.sub(r"\s+", " ", text).strip()

    txn = Transaction(raw=text)

    m = DATE_RE.search(text)
    if m:
        txn.date = m.group(1)

    # 先按文本定类型, 再按类型提取金额 (金额方向由类型决定)
    txn.category = _classify(text)
    value, cur = _extract_amount_by_category(text, txn.category)
    txn.amount = value
    txn.currency = _detect_currency(text, cur)

    # 条目描述: 去掉日期、金额后的文本, 取第一段有意义的内容
    item_text = text
    if txn.date:
        item_text = item_text.replace(txn.date, "")
    if value is not None:
        item_text = AMOUNT_RE.sub(" ", item_text)
    item_text = re.sub(r"[^\w\u4e00-\u9fff\- ]+", " ", item_text)
    item_text = re.sub(r"\s+", " ", item_text).strip()
    # 常见前缀词 (购买/refund/market transaction 等) 保留, 但去掉纯前缀
    txn.item = item_text[:120]

    return txn


def parse_history(html: str, debug: bool = False) -> Report:
    """解析购买历史页面 HTML, 返回 Report。"""
    report = Report()

    # 检测是否为登录页 (未登录时跳转 /login/)
    if "account/history" not in html and ("login" in html.lower() or "sign in" in html.lower()):
        raise RuntimeError("未检测到登录态, 请先登录 Steam (或检查 cookie 是否有效)")

    # 按 wallet_history_row div 提取每行文本
    extractor = _HistoryRowExtractor()
    extractor.feed(html)
    rows = extractor.rows

    if not rows:
        # 页面可能为空 (新账号无交易) 或结构大改
        if "No purchases" in html or "没有" in html:
            return report
        raise RuntimeError(
            "无法在页面中找到交易记录行。\n"
            "可能原因: 登录信息无效或已过期 (Steam 登录态约 2 周有效), 请重新登录后再试;\n"
            "或 Steam 页面结构已改版, 请用 --debug 运行并反馈。"
        )

    for r in rows:
        try:
            txn = _split_row(r)
        except Exception as e:  # 单行解析失败不应中断整体
            report.skipped.append(f"[解析异常] {e} | 片段: {r[:200]}")
            continue
        if txn.amount is None:
            report.skipped.append(f"[无金额] {txn.raw[:160]}")
            continue
        if txn.currency not in SUPPORTED_CURRENCIES:
            # 面向国人: 只统计人民币/美刀/港元, 其他币种忽略
            report.ignored_currency += 1
            continue
        report.transactions.append(txn)

    if debug:
        print(f"[debug] 识别到 {len(rows)} 行, 成功解析 {len(report.transactions)} 行, "
              f"跳过 {len(report.skipped)} 行", file=sys.stderr)
        for s in report.skipped[:20]:
            print(f"[debug] 跳过: {s}", file=sys.stderr)

    return report


# ---------------------------------------------------------------------------
# 统计输出
# ---------------------------------------------------------------------------

def format_money(value: float, currency: str = "") -> str:
    sign = "-" if value < 0 else ""
    if currency:
        return f"{sign}{currency}{abs(value):,.2f}"
    return f"{sign}{abs(value):,.2f}"


def print_report(report: Report) -> None:
    print("\n" + "=" * 52)
    print("  Steam 花费统计")
    print("=" * 52)
    if not report.transactions:
        print("  没有解析到任何交易记录。")
        return

    print(f"  总交易笔数:      {len(report.transactions)}")

    # 总支出 = 所有余额减少的部分 (真金白银花出去的钱), 按币种分组展示
    print(f"  总支出 (余额减少): {format_money(-report.expense, '')}")
    for cur, val in sorted(report.expense_by_currency().items(), key=lambda x: -x[1]):
        name = CURRENCY_NAMES.get(cur, cur)
        print(f"    {name:<4} ({cur}): {format_money(val)}")

    # 真实支出 = 总支出 - 退款 (退掉的钱回到钱包, 不算实际花掉)
    pos = report.positive_by_category()
    refund_total = pos.get("refund", 0.0)
    if refund_total > 0:
        net = report.expense - refund_total
        print(f"  真实支出 (总支出-退款): {format_money(net, '')}")

    if report.ignored_currency:
        print(f"  (已忽略 {report.ignored_currency} 笔非人民币/美刀/港元的交易)")

    if report.skipped:
        print(f"\n  ⚠ 有 {len(report.skipped)} 行未能解析 (可能含金额格式变化), 明细见 --debug 输出。")


def export_csv(report: Report, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "分类", "金额", "货币", "描述", "原文"])
        for t in report.transactions:
            w.writerow([t.date, t.category, t.amount, t.currency, t.item, t.raw])
    print(f"\n明细已导出: {path}")


# ---------------------------------------------------------------------------
# Cookie 模式: 纯 requests 抓取
# ---------------------------------------------------------------------------

def _resolve_proxy(explicit: Optional[str]) -> Optional[str]:
    """优先命令行参数, 其次环境变量 HTTPS_PROXY/HTTP_PROXY。"""
    if explicit:
        return explicit
    for env in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(env)
        if v:
            return v
    return None


def fetch_with_cookies(cookie_source, debug: bool = False, proxy: Optional[str] = None) -> str:
    """cookie_source: cookie 文件路径或 cookie dict。"""
    import requests

    if isinstance(cookie_source, str):
        with open(cookie_source, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = cookie_source

    s = requests.Session()
    proxy = _resolve_proxy(proxy)
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    # 支持两种格式: {"name": "value", ...} 或 [{"name":..., "value":..., "domain":...}, ...]
    if isinstance(data, list):
        for c in data:
            s.cookies.set(c.get("name"), c.get("value", ""),
                          domain=c.get("domain", "store.steampowered.com"))
    elif isinstance(data, dict):
        for k, v in data.items():
            s.cookies.set(k, v, domain="store.steampowered.com")
    else:
        raise ValueError("cookie 文件格式必须是 JSON 对象或数组")

    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "X-Requested-With": "XMLHttpRequest",
    })

    # 首屏 (新版页面每屏约 25 条; 完整历史需翻页, 见下方循环)
    url = HISTORY_URL
    if debug:
        print("[debug] GET " + url, file=sys.stderr)
    r = s.get(url, timeout=60)
    r.raise_for_status()
    html = r.text

    # 翻页: 首屏 HTML 里有初始游标 g_historyCursor, 循环 POST 翻页接口
    # 注意: 前端 jQuery 会把 cursor 对象编码成 cursor[字段]=值 的嵌套格式
    cursor = _extract_cursor(html)
    pages = 1
    while cursor:
        payload = {
            "sessionid": s.cookies.get("sessionid", ""),
            "cursor[wallet_txnid]": cursor.get("wallet_txnid", ""),
            "cursor[timestamp_newest]": cursor.get("timestamp_newest", ""),
            "cursor[balance]": cursor.get("balance", ""),
            "cursor[currency]": cursor.get("currency", ""),
        }
        if debug:
            print("[debug] POST " + AJAX_MORE_URL + " txnid=" + str(payload["cursor[wallet_txnid]"]),
                  file=sys.stderr)
        r = s.post(AJAX_MORE_URL, data=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        if data.get("html"):
            html += data["html"]
        cursor = data.get("cursor") if isinstance(data, dict) else None
        pages += 1
        if pages > 500:   # 安全上限, 防止异常死循环
            break

    if debug:
        print("[debug] 共抓取 " + str(pages) + " 屏", file=sys.stderr)
    return html


def _extract_cursor(html: str):
    """从首屏 HTML 提取 g_historyCursor (JS 变量值), 返回 dict 或 None。"""
    m = re.search(r"var\s+g_historyCursor\s*=\s*(\{[^}]*\})\s*;", html)
    if not m:
        return None
    try:
        cur = json.loads(m.group(1))
        return cur if isinstance(cur, dict) and cur else None
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Playwright 自动登录模式
# ---------------------------------------------------------------------------

def fetch_with_browser(debug: bool = False, timeout_minutes: int = 10,
                       proxy: Optional[str] = None) -> str:
    from playwright.sync_api import sync_playwright

    proxy = _resolve_proxy(proxy)
    with sync_playwright() as p:
        launch_kwargs = {"headless": False}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_kwargs)
        ctx = browser.new_context(locale="zh-CN")
        page = ctx.new_page()
        page.goto("https://store.steampowered.com/", wait_until="domcontentloaded")

        print("请在弹出的浏览器窗口中登录 Steam (密码只会输入到 Steam 官网)。")
        print("登录成功后程序会自动继续, 无需其他操作...")

        # 等待登录: 检测 store 页面是否出现登录后的特征 (账号下拉菜单)
        logged_in = False
        deadline = time.time() + timeout_minutes * 60
        while time.time() < deadline:
            # 已登录的 store 页面包含 #account_pulldown 或 nav 里的用户名
            try:
                if page.locator("#account_pulldown").count() > 0:
                    logged_in = True
                    break
                if page.locator(".account_pulldown").count() > 0:
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(1)

        if not logged_in:
            browser.close()
            raise RuntimeError(f"{timeout_minutes} 分钟内未检测到登录, 已退出。")

        print("检测到登录, 开始抓取购买历史...")
        page.goto(HISTORY_URL + "?maxchunks=9999", wait_until="domcontentloaded")
        time.sleep(2)

        # 无限滚动兜底: 滚到底直到高度不再变化
        last_h = 0
        stable = 0
        for _ in range(200):
            h = page.evaluate("document.body.scrollHeight")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.8)
            if h == last_h:
                stable += 1
                if stable >= 4:
                    break
            else:
                stable = 0
            last_h = h

        html = page.content()
        browser.close()
        return html


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _get_cookies_auto_or_manual() -> dict:
    """优先自动读取浏览器 cookie; 失败则引导用户手动输入。"""
    try:
        import steam_login
        cookies = steam_login.read_steam_cookies()
        if cookies:
            print("已自动获取登录信息, 开始抓取...", file=sys.stderr)
            return cookies
    except Exception as e:
        print(f"[提示] 自动读取失败: {e}", file=sys.stderr)

    print(file=sys.stderr)
    print("未能自动读取到 Steam 登录信息。", file=sys.stderr)
    print("请手动获取 cookie (Steam 登录态约 2 周有效):", file=sys.stderr)
    print("  1. 用浏览器打开 https://store.steampowered.com 并登录", file=sys.stderr)
    print("  2. 按 F12 打开开发者工具 → 应用(Application) → Cookie", file=sys.stderr)
    print("     → 选择 https://store.steampowered.com", file=sys.stderr)
    print("  3. 找到 steamLoginSecure 和 sessionid, 双击值并复制", file=sys.stderr)
    print()
    login = input("steamLoginSecure 的值: ").strip()
    session = input("sessionid 的值: ").strip()
    if not login:
        raise RuntimeError("未提供 steamLoginSecure, 无法继续。")
    return {"steamLoginSecure": login, "sessionid": session}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="统计你在 Steam 上总共花了多少钱",
        epilog="直接运行 = 自动读取浏览器登录信息; 或使用 --cookies-file 指定 cookie 文件。",
    )
    ap.add_argument("--cookies-file", metavar="PATH", help="使用已登录浏览器的 cookie JSON 文件")
    ap.add_argument("--export", metavar="PATH", help="导出逐条明细为 CSV")
    ap.add_argument("--proxy", metavar="URL", help="代理地址, 如 http://127.0.0.1:7890")
    ap.add_argument("--debug", action="store_true", help="打印调试信息")
    args = ap.parse_args()

    try:
        if args.cookies_file:
            html = fetch_with_cookies(args.cookies_file, args.debug, args.proxy)
        else:
            cookies = _get_cookies_auto_or_manual()
            html = fetch_with_cookies(cookies, args.debug, args.proxy)

        report = parse_history(html, args.debug)
        print_report(report)

        if args.export and report.transactions:
            export_csv(report, args.export)

        # 交互场景 (非脚本调用): 结束时停留, 避免窗口闪退看不到结果
        if not args.cookies_file:
            input("\n按回车键退出...")

        if report.skipped:
            return 2
        return 0
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as e:
        print(f"\n[错误] {e}", file=sys.stderr)
        if args.debug:
            import traceback
            traceback.print_exc()
        # 出错也停留, 让用户看到错误信息 (脚本模式不暂停)
        if not args.cookies_file:
            input("\n按回车键退出...")
        return 1


if __name__ == "__main__":
    sys.exit(main())
