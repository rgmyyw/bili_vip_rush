# -*- coding: utf-8 -*-
"""凭证管理与 HAR 导入测试（全部 mock 网络）。"""
import json
from unittest import mock

import core.auth as auth_mod
from core.auth import (
    _extract_credentials,
    credentials_view,
    reload_credentials_into_settings,
    save_credentials,
)


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


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


# ---------------------------------------------------------------- HAR 导入
def _mini_har():
    return """{
 "log": {"entries": [
  {"request": {"method": "POST", "url": "https://data.bilibili.com/v2/log/web",
    "headers": [{"name": "Cookie", "value": "SESSDATA=web123; bili_jct=jct456; DedeUserID=42"}]}},
  {"request": {"method": "GET", "url": "https://interface.bilibili.com/x/vip/deepblue?access_key=ak789&csrf=jct456&appkey=1d8b",
    "headers": [{"name": "Cookie", "value": "SESSDATA=web123; bili_jct=jct456; DedeUserID=42"}]}}
 ]}
}"""


def test_parse_har_credentials_full(monkeypatch, tmp_path):
    from core import auth
    monkeypatch.setattr(auth.settings, "CREDENTIALS_JSON_PATH",
                        tmp_path / "credentials.json")
    parsed = auth.parse_har_credentials(_mini_har())
    assert parsed["source_api"] == "GET https://interface.bilibili.com/x/vip/deepblue"
    assert parsed["creds"]["bili_access_key"] == "ak789"
    assert parsed["creds"]["bili_csrf"] == "jct456"
    assert parsed["creds"]["bili_sessdata"] == "web123"
    assert parsed["creds"]["bili_jct"] == "jct456"
    assert parsed["creds"]["bili_uid"] == "42"
    assert parsed["missing"] == []


def test_parse_har_credentials_missing_access_key():
    from core import auth
    har = """{"log": {"entries": [
     {"request": {"method": "GET", "url": "https://www.bilibili.com/",
       "headers": [{"name": "Cookie", "value": "SESSDATA=s1; bili_jct=j1; DedeUserID=9"}]}}]}}"""
    parsed = auth.parse_har_credentials(har)
    assert "bili_access_key" in parsed["missing"]
    assert parsed["creds"]["bili_sessdata"] == "s1"
    assert parsed["source_api"] == ""


def test_parse_har_credentials_bad_json():
    from core import auth
    parsed = auth.parse_har_credentials("{not json")
    assert parsed.get("error")
