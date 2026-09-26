# -*- coding: utf-8 -*-
"""仪表盘邮件配置测试：notify.json 优先级 / 保存 / 脱敏视图。"""
import json
from pathlib import Path

import core.notify as notify_mod
from core.notify import notify_config_view, save_notify_config


def test_save_and_view_roundtrip(monkeypatch, tmp_path):
    # 指向临时 json 路径，避免污染真实 config/notify.json
    monkeypatch.setattr(notify_mod.settings, "NOTIFY_JSON_PATH",
                        tmp_path / "notify.json")
    monkeypatch.setattr(notify_mod.settings, "SMTP_HOST", "")
    monkeypatch.setattr(notify_mod.settings, "SMTP_PORT", 465)
    monkeypatch.setattr(notify_mod.settings, "SMTP_USER", "")
    monkeypatch.setattr(notify_mod.settings, "SMTP_PASS", "")
    monkeypatch.setattr(notify_mod.settings, "NOTIFY_TO", "")

    view = save_notify_config({"host": "smtp.qq.com", "port": 465,
                               "user": "a@qq.com", "pass": "auth1",
                               "to": "b@qq.com"})
    assert view["enabled"] is True
    assert view["host"] == "smtp.qq.com" and view["user"] == "a@qq.com"
    assert view["to"] == "b@qq.com"
    assert view["pass_set"] is True
    # 授权码绝不回传
    assert "auth1" not in json.dumps(view)
    # 落盘内容正确
    saved = json.loads((tmp_path / "notify.json").read_text(encoding="utf-8"))
    assert saved["pass"] == "auth1" and saved["port"] == 465


def test_save_empty_pass_keeps_old(monkeypatch, tmp_path):
    """授权码留空=保留旧值（网页表单不回传密码的配套逻辑）。"""
    monkeypatch.setattr(notify_mod.settings, "NOTIFY_JSON_PATH",
                        tmp_path / "notify.json")
    monkeypatch.setattr(notify_mod.settings, "SMTP_HOST", "smtp.x.com")
    monkeypatch.setattr(notify_mod.settings, "SMTP_USER", "u@x.com")
    monkeypatch.setattr(notify_mod.settings, "SMTP_PASS", "OLDPASS")

    save_notify_config({"host": "smtp.qq.com", "port": 465,
                        "user": "a@qq.com", "pass": "", "to": ""})
    assert notify_mod.settings.SMTP_PASS == "OLDPASS"
    assert notify_mod.settings.SMTP_HOST == "smtp.qq.com"
    assert notify_mod.settings.NOTIFY_TO == "a@qq.com"   # to 空=发给自己


def test_settings_priority_json_over_secrets(monkeypatch):
    """settings 加载优先级：notify.json > secrets 占位值。

    reload 会重置模块属性，monkeypatch 路径无效；采用备份-写入-恢复。
    """
    import importlib
    from config import settings

    real = Path(settings.__file__).parent / "notify.json"
    backup = real.read_bytes() if real.exists() else None
    try:
        real.write_text(
            json.dumps({"host": "json.host", "port": 587, "user": "json@u",
                        "pass": "jsonpass", "to": "json@to"}),
            encoding="utf-8")
        monkeypatch.delenv("BILI_SMTP_HOST", raising=False)
        importlib.reload(settings)
        assert settings.SMTP_HOST == "json.host"
        assert settings.SMTP_PORT == 587
        assert settings.SMTP_USER == "json@u"
    finally:
        if backup is not None:
            real.write_bytes(backup)
        else:
            real.unlink(missing_ok=True)
        importlib.reload(settings)


def test_view_disabled_by_default(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "SMTP_HOST", "")
    monkeypatch.setattr(notify_mod.settings, "SMTP_USER", "")
    monkeypatch.setattr(notify_mod.settings, "SMTP_PASS", "")
    view = notify_config_view()
    assert view["enabled"] is False and view["pass_set"] is False
