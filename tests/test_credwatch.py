# -*- coding: utf-8 -*-
"""钉钉推送与凭证巡检状态机测试。"""
from unittest import mock

from config import settings as st
from core.credwatch import CredWatchState


# ---------------------------------------------------------------- 钉钉推送
def _set_ding(monkeypatch, webhook="https://oapi.test/send?token=x",
              secret="SECxxx"):
    monkeypatch.setattr(st, "DINGTALK_WEBHOOK", webhook)
    monkeypatch.setattr(st, "DINGTALK_SECRET", secret)


def test_send_dingtalk_signed_ok(monkeypatch):
    from core import notify
    _set_ding(monkeypatch)
    with mock.patch.object(notify.requests, "post",
                           return_value=mock.MagicMock(
                               json=lambda: {"errcode": 0})) as p:
        assert notify.send_dingtalk("hello") is True
    url = p.call_args.args[0]
    assert "timestamp=" in url and "sign=" in url   # 加签参数已拼


def test_send_dingtalk_no_webhook(monkeypatch):
    from core import notify
    monkeypatch.setattr(st, "DINGTALK_WEBHOOK", "")
    assert notify.send_dingtalk("x") is False


def test_send_dingtalk_api_fail(monkeypatch):
    from core import notify
    _set_ding(monkeypatch)
    with mock.patch.object(notify.requests, "post",
                           return_value=mock.MagicMock(
                               json=lambda: {"errcode": 310000,
                                             "errmsg": "sign error"})):
        assert notify.send_dingtalk("x") is False


# ---------------------------------------------------------------- 巡检状态机
def test_flip_to_invalid_alerts_once_then_daily():
    m = CredWatchState(realert_every_s=86400)
    assert m.step(True, 1000) == []
    assert m.step(False, 2000) == ["alert_invalid"]      # 翻转立即告警
    assert m.step(False, 3000) == []                     # 持续失效 24h 内静默
    assert m.step(False, 2000 + 86400 + 1) == ["alert_invalid"]  # 超 24h 重发


def test_recovered_sends_notice():
    m = CredWatchState()
    assert m.step(False, 1000) == ["alert_invalid"]      # 首检即失效也告警
    assert m.step(True, 2000) == ["alert_recovered"]     # 恢复通知
    assert m.step(True, 3000) == []                      # 稳定有效静默


def test_unknown_never_alerts():
    m = CredWatchState()
    assert m.step(None, 1000) == []
    assert m.step(None, 2000) == []
    assert m.last_state is None      # 无法判定不污染状态
    assert m.step(False, 3000) == ["alert_invalid"]
