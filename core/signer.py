# -*- coding: utf-8 -*-
"""B站 App API 签名。

规则（与安卓客户端一致）：
    1. 取出除 sign 外的所有参数，按 key 字典序排序；
    2. 拼接为 urlencode 的 query 串（键值都做 URL 转义）；
    3. 在末尾拼上 appsec；
    4. 取 MD5 小写。
"""
import hashlib
import time
import urllib.parse
from typing import Mapping


def app_sign(params: Mapping[str, object], app_sec: str) -> str:
    """对参数计算 B 站 App 签名。params 中若含 sign 会被忽略。"""
    items = {k: v for k, v in params.items() if k != "sign"}
    query = urllib.parse.urlencode(sorted(items.items()))
    return hashlib.md5((query + app_sec).encode("utf-8")).hexdigest()


def signed_params(params: Mapping[str, object], app_key: str, app_sec: str,
                  ts: int | None = None) -> dict:
    """补充 appkey/ts 并返回带 sign 的完整参数字典。"""
    merged = dict(params)
    merged["appkey"] = app_key
    merged["ts"] = int(ts if ts is not None else time.time())
    merged["sign"] = app_sign(merged, app_sec)
    return merged
