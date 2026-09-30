# -*- coding: utf-8 -*-
"""BiliClient 测试：mock 掉网络层，验证参数组装与错误处理。"""
import json
from unittest import mock

import pytest

from core.client import BiliApiError, BiliClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.text = json.dumps(payload, ensure_ascii=False)
        self.status_code = status_code


def make_client():
    return BiliClient(timeout=1)


def test_get_attract_card_ok():
    c = make_client()
    payload = {"code": 0, "message": "OK", "data": {
        "drainage_status": "SOLD_OUT_TODAY", "next_open_at": 1790481600,
        "current_time": 1790423331, "isReserved": True, "has_buy": False}}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload)) as req:
        data = c.get_attract_card()
    assert data["drainage_status"] == "SOLD_OUT_TODAY"
    # 请求方法与签名参数检查
    kwargs = req.call_args.kwargs
    assert req.call_args.args[0] == "GET"
    params = kwargs["params"]
    assert params["appkey"] == "1d8b6e7d45233436"
    assert len(params["sign"]) == 32
    assert params["access_key"]
    assert json.loads(params["ext_params"])["component_id"] == "iiVsSuDEJK"
    assert kwargs["headers"]["Cookie"].startswith("SESSDATA=")


def test_api_error_keeps_response_text():
    c = make_client()
    payload = {"code": -400, "message": "ext_params缺少必要字段"}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload)):
        with pytest.raises(BiliApiError) as ei:
            c.get_attract_card()
    assert ei.value.code == -400
    assert "ext_params" in ei.value.response_text


def test_create_order_posts_required_fields():
    c = make_client()
    plan = {
        "name": "t", "act_token": "T", "app_id": "241",
        "app_sub_id": "26moe_fhc", "panel_type": "26moe_cdd178",
        "months": 12, "order_type": 1, "product_type": "1",
    }
    payload = {"code": 0, "data": {"order_no": "X1"}}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload)) as req:
        order = c.create_order(plan)
    assert order == {"order_no": "X1"}
    kwargs = req.call_args.kwargs
    assert kwargs["data"]["act_token"] == "T"
    assert kwargs["data"]["panel_type"] == "26moe_cdd178"
    assert kwargs["data"]["months"] == 12
    assert kwargs["data"]["orderType"] == 1
    assert kwargs["data"]["csrf"]
    assert kwargs["params"]["sign"]


def test_extract_order_no_variants():
    assert BiliClient.extract_order_no({"order_no": "A"}) == "A"
    assert BiliClient.extract_order_no({"orderNo": "B"}) == "B"
    assert BiliClient.extract_order_no(
        {"payParams": {"orderId": "C"}}) == "C"
    assert BiliClient.extract_order_no({"foo": 1}) == ""
    assert BiliClient.extract_order_no(None) == ""


def test_get_order_status_queries_order_no():
    c = make_client()
    payload = {"code": 0, "data": {"status": 1}}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload)) as req:
        data = c.get_order_status({"order_no": "ON9"})
    assert data == {"status": 1}
    params = req.call_args.kwargs["params"]
    assert params["order_no"] == "ON9"
    assert params["app_id"] == "241"


# ------------------------------------------------------------------ 凭证体检
def test_check_login_valid():
    c = make_client()
    payload = {"code": 0, "message": "0", "data": {"mid": 123}}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload)):
        assert c.check_login() is True


@pytest.mark.parametrize("code", [-101, -400, 61000])
def test_check_login_invalid(code):
    """接口业务码（未登录/请求错误/凭证缺失）判为凭证失效。"""
    c = make_client()
    payload = {"code": code, "message": "err"}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload)):
        assert c.check_login() is False


def test_check_login_http_layer_unknown():
    """HTTP 层错误（412 风控等）不能断定凭证失效。"""
    c = make_client()
    payload = {"code": 0}
    with mock.patch.object(c.session, "request",
                           return_value=FakeResponse(payload, status_code=412)):
        assert c.check_login() is None


def test_check_login_network_error_unknown():
    import requests as _requests
    c = make_client()
    with mock.patch.object(c.session, "request",
                           side_effect=_requests.RequestException("boom")):
        assert c.check_login() is None
