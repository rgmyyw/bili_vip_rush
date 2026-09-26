# -*- coding: utf-8 -*-
"""签名算法测试。

黄金用例：期望 sign 值是用 hashlib/urllib 独立计算后写死的常量（与被测
代码无共享逻辑），用于锁定"输入参数 -> sign"的映射，防止算法被误改。

真实 HAR 抓包数据的对拍用例在 tests/test_signer_har.py（含真实凭证，
已被 .gitignore 排除，仅存在于本地；缺失时自动跳过）。
"""
from core.signer import app_sign, signed_params

# 固定假参数（不含任何真实凭证）
GOLDEN_PARAMS = {
    "access_key": "TEST_ACCESS_KEY_0123456789ABCDEF",
    "appkey": "1d8b6e7d45233436",
    "build": "9110400",
    "csrf": "testcsrf0123456789abcdef",
    "disable_rcmd": "0",
    "ext_params": '{"activity_id":"TESTACT","component_id":"TESTCMP","is_preview":false,"creative_id":0,"new_act_panel":1}',
    "mobi_app": "android",
    "new_act_panel": "1",
    "platform": "android",
    "scene": "activity",
    "statistics": '{"appId":1,"platform":3,"version":"9.11.0","abtest":""}',
    "ts": "1790423331",
    "web_location": "888.153775",
}
# md5(urlencode(sorted(params)) + appsec) 的独立计算结果
GOLDEN_SIGN = "c17b739d789e8400c591f5c8e49d3c5e"
APP_SEC = "560c52ccd288fed045859ed18bffd973"


def test_app_sign_golden():
    assert app_sign(GOLDEN_PARAMS, APP_SEC) == GOLDEN_SIGN


def test_sign_ignored_in_params():
    params = dict(GOLDEN_PARAMS)
    params["sign"] = "deadbeef"
    assert app_sign(params, APP_SEC) == GOLDEN_SIGN


def test_signed_params_adds_appkey_ts():
    out = signed_params({"build": "9110400"}, "1d8b6e7d45233436", APP_SEC, ts=100)
    assert out["ts"] == 100
    assert out["appkey"] == "1d8b6e7d45233436"
    assert app_sign(out, APP_SEC) == out["sign"]


def test_sign_sensitive_to_value_change():
    tampered = dict(GOLDEN_PARAMS)
    tampered["ts"] = "1790423332"
    assert app_sign(tampered, APP_SEC) != GOLDEN_SIGN
