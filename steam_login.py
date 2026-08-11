#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动从本机浏览器读取 Steam 登录 cookie (无需 F12, 无需手动复制)。

支持: Edge / Chrome (v10 加密) / Firefox (明文)。
依赖: 仅 Windows 自带 API (DPAPI + BCrypt AES-GCM), 无需第三方库。

用法 (被 steam_spend.py 调用):
    import steam_login
    cookies = steam_login.read_steam_cookies()   # {'steamLoginSecure': ..., 'sessionid': ...}
"""

import base64
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Windows DPAPI (解密浏览器主密钥)
# ---------------------------------------------------------------------------

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_decrypt(blob: bytes) -> bytes:
    """DPAPI 解密 (当前 Windows 用户加密的数据)。"""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]

    inblob = DATA_BLOB(
        len(blob),
        ctypes.cast(ctypes.c_char_p(blob), ctypes.POINTER(ctypes.c_char)),
    )
    outblob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(inblob), None, None, None, None, 0, ctypes.byref(outblob))
    if not ok:
        raise OSError("CryptUnprotectData 失败 (可能以其他 Windows 用户身份运行)")
    try:
        return ctypes.string_at(outblob.pbData, outblob.cbData)
    finally:
        kernel32.LocalFree(outblob.pbData)


# ---------------------------------------------------------------------------
# BCrypt AES-GCM (解密 cookie 值)
# ---------------------------------------------------------------------------

STATUS_AUTH_TAG_MISMATCH = 0xC000A002


class BCRYPT_AUTH_INFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.ULONG),
        ("dwInfoVersion", wintypes.ULONG),
        ("pbNonce", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbNonce", wintypes.ULONG),
        ("pbAuthData", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbAuthData", wintypes.ULONG),
        ("pbTag", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbTag", wintypes.ULONG),
        ("pbMacContext", ctypes.POINTER(ctypes.c_ubyte)),
        ("cbMacContext", wintypes.ULONG),
        ("cbAAD", wintypes.ULONG),
        ("cbData", ctypes.c_ulonglong),
        ("dwFlags", wintypes.ULONG),
    ]


def aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, tag: bytes) -> bytes:
    """AES-128-GCM 解密 (Windows BCrypt 实现, 无需第三方库)。"""
    bcrypt = ctypes.windll.bcrypt
    bcrypt.BCryptOpenAlgorithmProvider.restype = wintypes.LONG
    bcrypt.BCryptOpenAlgorithmProvider.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_wchar_p, ctypes.c_wchar_p, wintypes.ULONG]
    bcrypt.BCryptSetProperty.restype = wintypes.LONG
    bcrypt.BCryptSetProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptGenerateSymmetricKey.restype = wintypes.LONG
    bcrypt.BCryptGenerateSymmetricKey.argtypes = [
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG]
    bcrypt.BCryptDecrypt.restype = wintypes.LONG
    bcrypt.BCryptDecrypt.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p,
        ctypes.c_void_p, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG), wintypes.ULONG]

    h_alg = ctypes.c_void_p()
    status = bcrypt.BCryptOpenAlgorithmProvider(
        ctypes.byref(h_alg), "AES", None, 0)
    if status != 0:
        raise OSError(f"BCryptOpenAlgorithmProvider 失败: {status:#x}")

    # 设置为 GCM 链模式
    mode = "ChainingModeGCM"
    status = bcrypt.BCryptSetProperty(
        h_alg, "ChainingMode", mode.encode("utf-16-le") + b"\x00\x00",
        (len(mode) + 1) * 2, 0)
    if status != 0:
        raise OSError(f"设置 GCM 失败: {status:#x}")

    # 生成密钥句柄
    h_key = ctypes.c_void_p()
    key_obj = ctypes.create_string_buffer(1024)
    key_buf = ctypes.create_string_buffer(key)
    status = bcrypt.BCryptGenerateSymmetricKey(
        h_alg, ctypes.byref(h_key), key_obj, 1024, key_buf, len(key), 0)
    if status != 0:
        raise OSError(f"BCryptGenerateSymmetricKey 失败: {status:#x}")

    # 认证信息 (nonce + tag)
    nonce_buf = (ctypes.c_ubyte * len(nonce)).from_buffer_copy(nonce)
    tag_buf = (ctypes.c_ubyte * len(tag)).from_buffer_copy(tag)
    info = BCRYPT_AUTH_INFO()
    info.cbSize = ctypes.sizeof(BCRYPT_AUTH_INFO)
    info.dwInfoVersion = 1
    info.pbNonce = ctypes.cast(nonce_buf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbNonce = len(nonce)
    info.pbTag = ctypes.cast(tag_buf, ctypes.POINTER(ctypes.c_ubyte))
    info.cbTag = len(tag)

    out = ctypes.create_string_buffer(len(ciphertext))
    done = wintypes.ULONG(0)
    ct_buf = ctypes.create_string_buffer(ciphertext)
    status = bcrypt.BCryptDecrypt(
        h_key, ct_buf, len(ciphertext), ctypes.byref(info),
        None, 0, out, len(ciphertext), ctypes.byref(done), 0)
    if status == STATUS_AUTH_TAG_MISMATCH:
        raise ValueError("AES-GCM 校验失败 (tag mismatch)")
    if status != 0:
        raise OSError(f"BCryptDecrypt 失败: {status:#x}")
    return out.raw[:done.value]


# ---------------------------------------------------------------------------
# 浏览器 cookie 读取
# ---------------------------------------------------------------------------

BROWSERS = [
    {
        "name": "Edge",
        "cookie_db": r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Default\Network\Cookies",
        "local_state": r"%LOCALAPPDATA%\Microsoft\Edge\User Data\Local State",
    },
    {
        "name": "Chrome",
        "cookie_db": r"%LOCALAPPDATA%\Google\Chrome\User Data\Default\Network\Cookies",
        "local_state": r"%LOCALAPPDATA%\Google\Chrome\User Data\Local State",
    },
]


def _get_aes_key(local_state_path: str) -> bytes:
    """从 Local State 读取并解密浏览器主密钥 (v10)。"""
    with open(local_state_path, encoding="utf-8") as f:
        ls = json.load(f)
    encrypted = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    if encrypted.startswith(b"DPAPI"):
        return dpapi_decrypt(encrypted[5:])
    raise RuntimeError("未知的加密密钥格式")


def _decrypt_value(enc: bytes, key: bytes) -> str:
    """解密 cookie 值 (v10 / 明文)。"""
    if enc.startswith(b"v10"):
        nonce, tag = enc[3:15], enc[-16:]
        plain = aes_gcm_decrypt(key, nonce, enc[15:-16], tag)
        return plain.decode("utf-8", "replace")
    if enc.startswith(b"v20"):
        raise RuntimeError("v20 (Edge 新版本 app-bound 加密), 请改用 Chrome/Firefox 登录")
    return enc.decode("utf-8", "replace")


def _read_chromium(b: dict):
    """读取 Edge/Chrome 的 Steam cookie。返回 cookie dict 或 None。"""
    db_path = os.path.expandvars(b["cookie_db"])
    ls_path = os.path.expandvars(b["local_state"])
    if not (os.path.isfile(db_path) and os.path.isfile(ls_path)):
        return None

    tmp_db = os.path.join(tempfile.gettempdir(), "steam_ck_tmp.db")
    try:
        # 复制数据库文件再读, 避免浏览器运行时的文件锁
        shutil.copy2(db_path, tmp_db)
        key = _get_aes_key(ls_path)
        con = sqlite3.connect(tmp_db)
        rows = con.execute(
            "select name, encrypted_value from cookies "
            "where host_key like '%steampowered.com'"
        ).fetchall()
        con.close()
        cookies = {}
        for name, enc in rows:
            try:
                cookies[name] = _decrypt_value(bytes(enc), key)
            except Exception:
                pass
        return cookies if cookies.get("steamLoginSecure") else None
    finally:
        try:
            os.remove(tmp_db)
        except OSError:
            pass


def _read_firefox():
    """读取 Firefox 的 Steam cookie (cookies.sqlite 明文)。"""
    db = os.path.expandvars(
        r"%APPDATA%\Mozilla\Firefox\Profiles") if os.name == "nt" else \
        os.path.expanduser("~/.mozilla/firefox")
    if not os.path.isdir(db):
        return None
    for entry in os.listdir(db):
        path = os.path.join(db, entry, "cookies.sqlite")
        if not os.path.isfile(path):
            continue
        tmp_db = os.path.join(tempfile.gettempdir(), "steam_ff_tmp.db")
        try:
            shutil.copy2(path, tmp_db)
            con = sqlite3.connect(tmp_db)
            rows = con.execute(
                "select name, value from moz_cookies "
                "where host like '%steampowered%'"
            ).fetchall()
            con.close()
            cookies = {n: v for n, v in rows}
            if cookies.get("steamLoginSecure"):
                return cookies
        except Exception:
            pass
        finally:
            try:
                os.remove(tmp_db)
            except OSError:
                pass
    return None


def read_steam_cookies():
    """扫描 Edge/Chrome/Firefox, 返回 Steam 登录 cookie dict。找不到返回 None。"""
    for b in BROWSERS:
        try:
            ck = _read_chromium(b)
        except Exception as e:
            print(f"[{b['name']}] 读取失败: {e}", file=sys.stderr)
            ck = None
        if ck:
            print(f"[ok] 已从 {b['name']} 读取到 Steam 登录态", file=sys.stderr)
            return ck
    ck = _read_firefox()
    if ck:
        print("[ok] 已从 Firefox 读取到 Steam 登录态", file=sys.stderr)
        return ck
    return None
