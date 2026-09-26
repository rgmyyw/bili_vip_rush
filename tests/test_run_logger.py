# -*- coding: utf-8 -*-
"""RunLogger 与 HTTP 审计日志测试。"""
import json
from unittest import mock

from core.client import BiliClient
from core.run_logger import NullRunLogger, RunLogger, mask_mapping, mask_value


def test_mask_value():
    assert mask_value("0123456789abcdefghij") == "012345...ghij"
    assert mask_value("1234567890") == "***"      # 短值不透露任何片段
    assert mask_value("short") == "***"


def test_mask_mapping_covers_credentials():
    out = mask_mapping({
        "access_key": "AK1234567890XYZW", "sign": "abcdef0123456789",
        "csrf": "0123456789abcdef", "build": "9110400",
    })
    assert out["build"] == "9110400"                       # 非敏感原样
    assert out["access_key"] == "AK1234...XYZW"
    assert out["sign"].startswith("abcdef") and "..." in out["sign"]
    full = "AK1234567890XYZW"
    assert full not in json.dumps(out)                     # 明文不落盘


def test_run_logger_writes_jsonl(tmp_path):
    logs = RunLogger(tmp_path, mode="test")
    logs.phase = "precheck"
    logs.log_event("attract_card", drainage_status="SOLD_OUT_TODAY")
    logs.log_http(method="GET", url="https://api.bilibili.com/x/vip/a/b",
                  params={"access_key": "AK123456789"},
                  status_code=200, api_code=0, response_text='{"code":0}',
                  elapsed_ms=88.4)
    path = tmp_path / f"run_{logs.run_id}.jsonl"
    lines = [json.loads(x) for x in
             path.read_text(encoding="utf-8").strip().splitlines()]
    types = [r["type"] for r in lines]
    assert types[0] == "meta" and "run_start" in lines[0]["event"]
    assert types[1] == "event" and lines[1]["drainage_status"] == "SOLD_OUT_TODAY"
    http = lines[2]
    assert http["api"] == "x/vip/a/b"
    assert http["request"]["params"]["access_key"] != "AK123456789"
    assert http["response"]["text"] == '{"code":0}'
    assert [r["seq"] for r in lines] == [1, 2, 3]          # 调用链序号


def test_client_http_audit_on_success_and_failure(tmp_path):
    """client 全路径写审计：成功、接口报错、网络异常都要落盘。"""
    import requests as _rq

    logs = RunLogger(tmp_path, mode="test")
    c = BiliClient(run_logger=logs)
    c.phase = "rush"

    ok = mock.Mock(status_code=200, text='{"code":0,"data":{"x":1}}')
    bad = mock.Mock(status_code=200,
                    text='{"code":-400,"message":"ext_params缺少必要字段"}')
    with mock.patch.object(c.session, "request", side_effect=[ok, bad]):
        assert c.get_attract_card() == {"x": 1}
        try:
            c.get_attract_card()
            raise AssertionError("应抛 BiliApiError")
        except Exception:
            pass
    # 网络异常路径
    with mock.patch.object(c.session, "request",
                           side_effect=_rq.ConnectionError("boom")):
        try:
            c.get_attract_card()
        except Exception:
            pass

    path = tmp_path / f"run_{logs.run_id}.jsonl"
    rows = [json.loads(x) for x in
            path.read_text(encoding="utf-8").strip().splitlines()]
    https = [r for r in rows if r["type"] == "http"]
    assert len(https) == 3
    assert https[1]["response"]["api_code"] == -400
    assert "ext_params" in https[1]["response"]["text"]
    assert https[2]["error"] and https[2]["response"]["status_code"] is None


def test_null_logger_is_noop():
    c = BiliClient(run_logger=NullRunLogger())
    assert c.run_logger.path is None
    c.run_logger.log_http(method="GET")  # 不抛异常即可
