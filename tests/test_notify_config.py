# -*- coding: utf-8 -*-
"""仪表盘邮件配置测试：字段级合并 / 来源视图 / 脱敏。"""
import json
from pathlib import Path

import core.notify as notify_mod
from core.notify import notify_config_view, save_notify_config


def _setup(monkeypatch, tmp_path, *, host="", user="", passwd="", to="", port=465):
    """把生效配置与 json 路径重置到干净状态。"""
    monkeypatch.setattr(notify_mod.settings, "NOTIFY_JSON_PATH",
                        tmp_path / "notify.json")
    monkeypatch.setattr(notify_mod.settings, "SMTP_HOST", host)
    monkeypatch.setattr(notify_mod.settings, "SMTP_PORT", port)
    monkeypatch.setattr(notify_mod.settings, "SMTP_USER", user)
    monkeypatch.setattr(notify_mod.settings, "SMTP_PASS", passwd)
    monkeypatch.setattr(notify_mod.settings, "NOTIFY_TO", to or user)


def _json_of(tmp_path):
    return json.loads((tmp_path / "notify.json").read_text(encoding="utf-8"))


def test_save_full_and_view_masked(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    view = save_notify_config({"host": "smtp.qq.com", "port": 465,
                               "user": "a@qq.com", "pass": "auth1",
                               "to": "b@qq.com"})
    assert view["enabled"] is True
    assert view["host"] == "smtp.qq.com" and view["to"] == "b@qq.com"
    assert view["pass_set"] is True
    assert "auth1" not in json.dumps(view)          # 授权码绝不回传
    assert view["sources"]["host"] == "仪表盘"
    assert _json_of(tmp_path)["pass"] == "auth1"


def test_save_empty_pass_falls_back_to_local(monkeypatch, tmp_path):
    """授权码留空 -> 不写入 json，回落本地(secrets)值；两者共存生效。"""
    _setup(monkeypatch, tmp_path, host="smtp.x.com", user="u@x.com",
           passwd="LOCALPASS")
    # "本地文件"源权威位置是 secrets 对象（_setup 只改了 settings 属性）
    monkeypatch.setattr(notify_mod.settings._secrets, "SMTP_PASS", "LOCALPASS")
    save_notify_config({"host": "smtp.qq.com", "port": 465,
                        "user": "a@qq.com", "pass": "", "to": ""})
    data = _json_of(tmp_path)
    assert "pass" not in data                        # 未固化本地授权码
    assert notify_mod.settings.SMTP_HOST == "smtp.qq.com"   # 仪表盘值
    assert notify_mod.settings.SMTP_PASS == "LOCALPASS"     # 本地值生效
    view = notify_config_view()
    assert view["sources"]["host"] == "仪表盘"
    assert view["sources"]["pass"] == "本地文件"
    assert view["enabled"] is True


def test_save_clears_json_key(monkeypatch, tmp_path):
    """表单显式清空 -> 删除 json 键，之前仪表盘的值被撤销。"""
    _setup(monkeypatch, tmp_path)
    save_notify_config({"host": "a.com", "user": "u@a", "pass": "p",
                        "port": 465, "to": ""})
    save_notify_config({"host": "", "user": None, "pass": None,
                        "port": 465, "to": None})
    data = _json_of(tmp_path)
    assert "host" not in data and data["user"] == "u@a"  # None 不动，空串删除


def test_unset_field_keeps_json(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    save_notify_config({"host": "a.com", "port": 465})
    save_notify_config({"user": "u@a", "port": 465})
    assert _json_of(tmp_path) == {"host": "a.com", "port": 465, "user": "u@a"}


def test_settings_field_level_merge(monkeypatch, tmp_path):
    """settings 加载即字段级合并：json 只有 host 时 pass 取本地。"""
    import importlib
    from config import settings

    real = Path(settings.__file__).parent / "notify.json"
    backup = real.read_bytes() if real.exists() else None
    try:
        real.write_text(json.dumps({"host": "json.host", "port": 587}),
                        encoding="utf-8")
        for k in ("BILI_SMTP_HOST", "BILI_SMTP_PORT", "BILI_SMTP_USER",
                  "BILI_SMTP_PASS"):
            monkeypatch.delenv(k, raising=False)
        importlib.reload(settings)
        assert settings.SMTP_HOST == "json.host"     # json 字段生效
        assert settings.SMTP_PORT == 587
        # user/pass 未在 json -> 回落 secrets（测试环境 secrets.py 有值或空）
        assert settings.SMTP_USER == settings._secrets.SMTP_USER
    finally:
        if backup is not None:
            real.write_bytes(backup)
        else:
            real.unlink(missing_ok=True)
        importlib.reload(settings)


def test_view_disabled(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    view = notify_config_view()
    assert view["enabled"] is False and view["pass_set"] is False
