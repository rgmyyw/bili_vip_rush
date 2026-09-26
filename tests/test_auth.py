# -*- coding: utf-8 -*-
"""TV 扫码登录与凭证管理测试（全部 mock 网络）。"""
import json
from unittest import mock

import core.auth as auth_mod
from core.auth import (
    POLL_EXPIRED,
    POLL_SCANNED,
    _extract_credentials,
    credentials_view,
    qrcode_poll,
    qrcode_start,
    reload_credentials_into_settings,
    save_credentials,
)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_qrcode_start_ok():
    with mock.patch.object(auth_mod._session, "post",
                           return_value=FakeResp({"code": 0, "data": {
                               "auth_code": "AC123",
                               "url": "https://passport.bilibili.com/h5/qrcode/auth?auth_code=AC123"}})):
        r = qrcode_start()
    assert r["auth_code"] == "AC123"
    assert "AC123" in r["url"]


def test_qrcode_start_error():
    with mock.patch.object(auth_mod._session, "post",
                           return_value=FakeResp({"code": -400, "message": "bad"})):
        try:
            qrcode_start()
            raise AssertionError("应抛 AuthError")
        except auth_mod.AuthError:
            pass


def _poll_setup(monkeypatch, tmp_path, payload):
    monkeypatch.setattr(auth_mod.settings, "CREDENTIALS_JSON_PATH",
                        tmp_path / "credentials.json")
    monkeypatch.setattr(auth_mod.settings, "ACCESS_KEY", "OLD")
    monkeypatch.setattr(auth_mod.settings, "CSRF", "")
    monkeypatch.setattr(auth_mod.settings, "SESSDATA", "")
    monkeypatch.setattr(auth_mod.settings, "BILI_JCT", "")
    monkeypatch.setattr(auth_mod.settings, "DEDE_USER_ID", "")
    return mock.patch.object(auth_mod._session, "post",
                             return_value=FakeResp(payload))


def test_qrcode_poll_states(monkeypatch, tmp_path):
    with _poll_setup(monkeypatch, tmp_path,
                     {"code": auth_mod.POLL_WAITING, "message": "二维码尚未确认"}):
        assert qrcode_poll("AC")["status"] == "waiting"
    with _poll_setup(monkeypatch, tmp_path, {"code": POLL_SCANNED}):
        assert qrcode_poll("AC")["status"] == "scanned"
    with _poll_setup(monkeypatch, tmp_path, {"code": POLL_EXPIRED}):
        assert qrcode_poll("AC")["status"] == "expired"


def test_qrcode_poll_success_saves_credentials(monkeypatch, tmp_path):
    payload = {"code": 0, "data": {
        "access_token": "NEWAK123456789",
        "cookie_info": {"cookies": [
            {"name": "SESSDATA", "value": "NEWSESS%2Cxyz"},
            {"name": "bili_jct", "value": "NEWJCTabcdef"},
            {"name": "DedeUserID", "value": "999888"},
        ]}}}
    with _poll_setup(monkeypatch, tmp_path, payload):
        r = qrcode_poll("AC")
    assert r["status"] == "success"
    # 全套凭证落盘且立即生效
    saved = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert saved["bili_access_key"] == "NEWAK123456789"
    assert saved["bili_sessdata"] == "NEWSESS%2Cxyz"
    assert saved["bili_csrf"] == "NEWJCTabcdef"       # csrf 取 bili_jct
    assert auth_mod.settings.ACCESS_KEY == "NEWAK123456789"
    # 视图脱敏：明文不回传
    assert "NEWAK123456789" not in json.dumps(r)


def test_extract_credentials_handles_empty():
    assert _extract_credentials({}) == {}
    assert _extract_credentials({"cookie_info": {"cookies": []}}) == {}


def test_save_credentials_field_merge(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_mod.settings, "CREDENTIALS_JSON_PATH",
                        tmp_path / "credentials.json")
    # "本地文件"源的权威位置是 secrets 对象，settings 属性仅是加载结果
    monkeypatch.setattr(auth_mod.settings._secrets, "ACCESS_KEY", "")
    monkeypatch.setattr(auth_mod.settings._secrets, "SESSDATA", "LOCALSESS")
    monkeypatch.setattr(auth_mod.settings, "ACCESS_KEY", "")
    monkeypatch.setattr(auth_mod.settings, "SESSDATA", "LOCALSESS")
    # 只更新 access_key，SESSDATA 未提交 -> 本地值继续生效
    save_credentials({"bili_access_key": "AK999"})
    data = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert data["bili_access_key"] == "AK999"
    assert "bili_sessdata" not in data
    assert auth_mod.settings.ACCESS_KEY == "AK999"
    assert auth_mod.settings.SESSDATA == "LOCALSESS"
    # 空串删除键回落
    monkeypatch.setattr(auth_mod.settings._secrets, "ACCESS_KEY", "SECRETS_AK")
    save_credentials({"bili_access_key": ""})
    assert auth_mod.settings.ACCESS_KEY == "SECRETS_AK"


def test_reload_credentials(monkeypatch, tmp_path):
    """reload: json 值覆盖旧 settings 属性（常驻进程每轮刷新）。"""
    monkeypatch.setattr(auth_mod.settings, "CREDENTIALS_JSON_PATH",
                        tmp_path / "credentials.json")
    (tmp_path / "credentials.json").write_text(
        json.dumps({"bili_access_key": "JSONAK111"}), encoding="utf-8")
    monkeypatch.setattr(auth_mod.settings, "ACCESS_KEY", "STALE")
    reload_credentials_into_settings()
    assert auth_mod.settings.ACCESS_KEY == "JSONAK111"


def test_credentials_view_masks(monkeypatch, tmp_path):
    monkeypatch.setattr(auth_mod.settings, "CREDENTIALS_JSON_PATH",
                        tmp_path / "credentials.json")
    (tmp_path / "credentials.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(auth_mod.settings, "ACCESS_KEY", "AK1234567890ABCDEFG")
    monkeypatch.setattr(auth_mod.settings, "SESSDATA", "SD1234567890")
    monkeypatch.setattr(auth_mod.settings, "CSRF", "C1")
    monkeypatch.setattr(auth_mod.settings, "BILI_JCT", "J1")
    monkeypatch.setattr(auth_mod.settings, "DEDE_USER_ID", "42")
    v = credentials_view()
    assert v["access_key"] == "AK1234...DEFG"
    assert v["uid"] == "42"
    assert v["sources"]["access_key"] == "本地文件"
