# -*- coding: utf-8 -*-
"""响应分类器测试：五态覆盖 + 边界。"""
from core.classifier import Outcome, classify_error
from core.client import BiliApiError


def E(code=None, msg=""):
    return BiliApiError(f"接口报错 code={code} message={msg} url=x",
                        code=code, response_text="")


def test_credential_expired_by_code():
    assert classify_error(E(-101, "账号未登录")) is Outcome.CREDENTIAL_EXPIRED
    assert classify_error(E(-111, "csrf校验失败")) is Outcome.CREDENTIAL_EXPIRED


def test_credential_expired_by_message():
    assert classify_error(E(1, "access_key 已失效")) is Outcome.CREDENTIAL_EXPIRED
    assert classify_error(E(-400, "请先登录")) is Outcome.CREDENTIAL_EXPIRED


def test_already_done():
    assert classify_error(E(1, "您已购买该套餐")) is Outcome.ALREADY_DONE
    assert classify_error(E(1, "重复下单")) is Outcome.ALREADY_DONE
    assert classify_error(E(1, "限购：每个用户限购一件")) is Outcome.ALREADY_DONE


def test_sold_out():
    assert classify_error(E(1, "今日已售罄")) is Outcome.SOLD_OUT
    assert classify_error(E(1, "手慢了，已抢完")) is Outcome.SOLD_OUT


def test_system_error():
    assert classify_error(E(None, "网络失败: timeout")) is Outcome.SYSTEM_ERROR
    assert classify_error(E(502, "HTTP 502")) is Outcome.SYSTEM_ERROR
    assert classify_error(E(503, "HTTP 503")) is Outcome.SYSTEM_ERROR


def test_retry_default():
    assert classify_error(E(1, "活动未开始")) is Outcome.RETRY
    assert classify_error(E(-400, "参数错误")) is Outcome.RETRY
    assert classify_error(E(429, "请求过于频繁")) is Outcome.RETRY


def test_priority_credential_over_soldout():
    # 凭证失效优先级最高（即使是"未登录+售罄"字样混合）
    ex = BiliApiError("code=-101 message=未登录且今日售罄", code=-101)
    assert classify_error(ex) is Outcome.CREDENTIAL_EXPIRED


def test_bilibili_business_codes_retry():
    """B 站六位数业务码(未开售/售罄态返回)必须 RETRY,不能因 >=500 误判。

    09-30 演练实证:69422(暂时无法购买)被旧规则误判 SYSTEM_ERROR,
    每 5 发冷却 2s,90 秒仅打出 172 发,拖垮开售节奏。
    """
    assert classify_error(E(69422, "暂时无法购买此商品")) is Outcome.RETRY
    assert classify_error(E(43055, "活动状态")) is Outcome.RETRY
    assert classify_error(E(-702, "请求过于频繁")) is Outcome.RETRY


def test_http_codes_split():
    assert classify_error(E(500, "HTTP 500")) is Outcome.SYSTEM_ERROR
    assert classify_error(E(503, "HTTP 503")) is Outcome.SYSTEM_ERROR
    assert classify_error(E(412, "HTTP 412")) is Outcome.SYSTEM_ERROR
