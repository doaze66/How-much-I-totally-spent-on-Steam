# -*- coding: utf-8 -*-
"""解析器单元测试: 用模拟的 Steam 购买历史 HTML 验证解析逻辑。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from steam_spend import parse_history, Report, Transaction


def make_row(tid, date, item, amount_html, extra=""):
    """构造一行 wallet_history_row 的 HTML (模拟旧版 Steam 结构)。"""
    return (
        f'<div class="wallet_history_row" id="history_row_{tid}">{extra}'
        f'<div class="wallet_history_row_left"><div class="wallet_history_row_date">{date}</div></div>'
        f'<div class="wallet_history_row_middle"><div class="wallet_history_row_item">{item}</div></div>'
        f'<div class="wallet_history_row_right"><div class="wallet_history_row_balance">{amount_html}</div></div>'
        f"</div>"
    )


def make_new_row(tid, date, item, amt_display, paid):
    """构造新版 Steam 结构的一行 wallet_table_row (改版后: <tr> + 嵌套 <td>)。"""
    return (
        f'<tr class="wallet_table_row wallet_table_row_amt_change" id="tr_{tid}" '
        f'data-panel="{{&quot;focusable&quot;:true}}" onclick="location.href=...">'
        f'<td class="wallet_table_row_date">{date}</td>'
        f'<td class="wallet_table_row_item">{item}</td>'
        f'<td class="wallet_table_row_amount">{amt_display}</td>'
        f'<td class="wht_wallet_balance" data-tooltip-html="&lt;div&gt;先前的钱包余额&lt;/div&gt;">{paid}</td>'
        f"</tr>"
    )


class TestParseHistory(unittest.TestCase):
    def test_simple_purchase(self):
        rows = make_row(1, "1 Oct, 2021", "购买 Inscryption", "-$19.99")
        html = "<html><body>" + rows + "</body></html>"
        report = parse_history(html)
        self.assertEqual(len(report.transactions), 1)
        t = report.transactions[0]
        self.assertAlmostEqual(t.amount, -19.99)
        self.assertEqual(t.category, "purchase")
        self.assertEqual(t.date, "1 Oct, 2021")

    def test_multiple_types(self):
        rows = (
            make_row(1, "1 Oct, 2021", "购买 Inscryption", "-$19.99") +
            make_row(2, "2 Oct, 2021", "Market Transaction", "-$1.23") +
            make_row(3, "3 Oct, 2021", "Add Funds", "+$50.00") +
            make_row(4, "4 Oct, 2021", "Refund", "+$19.99") +
            make_row(5, "5 Oct, 2021", "购买 魔塔", "-¥18.00")
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        self.assertEqual(len(report.transactions), 5)

        # 总支出 = 19.99 + 1.23 + 18 = 39.22
        self.assertAlmostEqual(report.expense, 39.22)
        # 总收入 = 50 + 19.99 = 69.99
        self.assertAlmostEqual(report.income, 69.99)

        cats = report.by_category()
        self.assertIn("purchase", cats)
        self.assertIn("market", cats)

    def test_currency_symbols(self):
        # 人民币计入; 欧元行非支持币种, 被忽略
        rows = make_row(1, "1 Jan, 2022", "购买 测试", "-¥88.00") + \
               make_row(2, "2 Jan, 2022", "Purchase Test", "-€25.50")
        report = parse_history("<html><body>" + rows + "</body></html>")
        self.assertAlmostEqual(report.expense, 88.00)
        self.assertEqual(report.ignored_currency, 1)

    def test_empty_page(self):
        report = parse_history("<html><body>No purchases found</body></html>")
        self.assertEqual(len(report.transactions), 0)

    def test_not_logged_in(self):
        html = '<html><body><a href="/login">Sign in</a></body></html>'
        with self.assertRaises(RuntimeError):
            parse_history(html)

    def test_amount_formats(self):
        # 带逗号分隔的千位、小数点后两位、无符号
        rows = make_row(1, "1 Jan, 2022", "购买 大礼包", "-$1,234.56")
        report = parse_history("<html><body>" + rows + "</body></html>")
        self.assertAlmostEqual(report.transactions[0].amount, -1234.56)

    def test_new_structure(self):
        """新版页面: wallet_table_row + 中文日期带空格 + 多金额取负号。"""
        rows = make_new_row(
            1, "2026 年 3 月 4 日",
            "Limbus Company 每月狂气补给 游戏内购买 钱包",
            "$6.99 USD", "-¥ 48.35",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        self.assertEqual(len(report.transactions), 1)
        t = report.transactions[0]
        self.assertAlmostEqual(t.amount, -48.35)
        self.assertEqual(t.currency, "CNY")  # 实付 -¥48.35, 人民币
        self.assertEqual(t.date, "2026 年 3 月 4 日")
        self.assertEqual(t.category, "purchase")  # "游戏内购买" 含 "购买"

    def test_new_structure_income(self):
        """新版页面充值行: 无负号, 取第一个正金额。"""
        rows = make_new_row(2, "2026 年 1 月 1 日", "Add Funds 充值", "+$50.00", "$50.00")
        report = parse_history("<html><body>" + rows + "</body></html>")
        self.assertEqual(len(report.transactions), 1)
        t = report.transactions[0]
        self.assertGreater(t.amount, 0)
        self.assertEqual(t.category, "wallet")

    def test_wallet_funds_is_recharge(self):
        """'已购买 xx 钱包资金' = 充值, 不是退款。"""
        rows = make_new_row(
            3, "2026 年 3 月 4 日",
            "已购买 ¥ 48.35 钱包资金 购买 微信支付",
            "¥ 48.35 +¥ 48.35", "+¥ 48.35",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertGreater(t.amount, 0)
        self.assertEqual(t.category, "wallet")
        # 正金额不应计入支出
        self.assertAlmostEqual(report.expense, 0)

    def test_refund_with_wallet_funds_text(self):
        """含'退款'字样且带'钱包资金'的行, 应判为退款而非充值。"""
        rows = make_new_row(
            4, "2022 年 8 月 24 日",
            "Necrosmith Necrosmith Soundtrack 退款 退款 钱包资金",
            "¥ 16.19", "+¥ 16.19",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertGreater(t.amount, 0)
        self.assertEqual(t.category, "refund")

    def test_alipay_purchase_no_negative(self):
        """支付宝购买无负号金额: 取最后一个价格(折后实付)作为支出。"""
        rows = make_new_row(
            5, "2026 年 3 月 20 日",
            "RESIDENT EVIL 3 购买 支付宝 -90%",
            "¥ 198.00 ¥ 19.80 ¥ 19.80", "¥ 19.80",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertAlmostEqual(t.amount, -19.80)
        self.assertEqual(t.category, "purchase")

    def test_alipay_purchase_no_discount(self):
        """支付宝购买无折扣: 取最后一个价格(原价)作为支出。"""
        rows = make_new_row(
            6, "2025 年 9 月 12 日",
            "Hollow Knight: Silksong 购买 支付宝",
            "¥ 76.00 ¥ 76.00", "¥ 76.00",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertAlmostEqual(t.amount, -76.00)
        self.assertEqual(t.category, "purchase")

    def test_market_sell(self):
        """市场出售: 带 + 号, 正金额, 分类 market。"""
        rows = make_new_row(
            7, "2024 年 8 月 11 日",
            "Steam 社区市场 7 市场交易 钱包 资金",
            "+¥ 408.88", "+¥ 408.88",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertGreater(t.amount, 0)
        self.assertEqual(t.category, "market")

    def test_market_buy(self):
        """市场买入: 带 - 号, 负金额, 分类 market。"""
        rows = make_new_row(
            8, "2024 年 7 月 23 日",
            "Steam 社区市场 210 市场交易 钱包",
            "-¥ 55.82", "-¥ 55.82",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertLess(t.amount, 0)
        self.assertEqual(t.category, "market")

    def test_currency_convert(self):
        """货币转换: 正金额, 分类 currency_convert。"""
        rows = make_new_row(
            9, "2025 年 9 月 12 日",
            "货币转换至 CNY（中国（大陆）） 转换 钱包",
            "¥ 5.26", "¥ 5.26",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertGreater(t.amount, 0)
        self.assertEqual(t.category, "currency_convert")

    def test_currency_hkd(self):
        """港元购买: 币种识别为 HKD。"""
        rows = make_new_row(
            10, "2025 年 6 月 8 日",
            "Tales of the Black Forest 购买 支付宝 -70%",
            "HK$ 26.00 HK$ 7.80 HK$ 7.80", "HK$ 7.80",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertEqual(t.currency, "HKD")
        self.assertAlmostEqual(t.amount, -7.80)

    def test_currency_usd(self):
        """美刀购买: 币种识别为 USD。"""
        rows = make_new_row(
            11, "2024 年 4 月 20 日",
            "Mosaique Neko Waifus 购买 贝宝 -75%",
            "$29.89 USD $8.36 USD", "$8.36 USD",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        t = report.transactions[0]
        self.assertEqual(t.currency, "USD")
        self.assertAlmostEqual(t.amount, -8.36)

    def test_currency_myr_ignored(self):
        """马币行 (非 CNY/HKD/USD): 应被忽略, 不计入统计。"""
        rows = make_new_row(
            12, "2025 年 5 月 26 日",
            "已购买 RM16.00 钱包资金 购买 零售",
            "RM16.00 RM16.00", "RM16.00",
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        self.assertEqual(len(report.transactions), 0)
        self.assertEqual(report.ignored_currency, 1)

    def test_expense_by_currency(self):
        """按币种汇总支出。"""
        rows = (
            make_new_row(1, "2026 年 3 月 4 日", "A 游戏 购买 支付宝", "¥ 10.00 ¥ 10.00", "¥ 10.00") +
            make_new_row(2, "2026 年 3 月 4 日", "B 游戏 购买 支付宝", "-HK$ 8.00 HK$ 8.00", "-HK$ 8.00") +
            make_new_row(3, "2026 年 3 月 4 日", "C 游戏 购买 支付宝", "$5.00 USD $5.00 USD", "$5.00 USD")
        )
        report = parse_history("<html><body>" + rows + "</body></html>")
        by_cur = report.expense_by_currency()
        self.assertAlmostEqual(by_cur.get("CNY", 0), 10.00)
        self.assertAlmostEqual(by_cur.get("HKD", 0), 8.00)
        self.assertAlmostEqual(by_cur.get("USD", 0), 5.00)


class TestReportAggregation(unittest.TestCase):
    def test_empty_report(self):
        r = Report()
        self.assertEqual(r.expense, 0)
        self.assertEqual(r.income, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
