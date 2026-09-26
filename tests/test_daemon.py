# -*- coding: utf-8 -*-
"""常驻调度与凭证环境变量覆盖测试。"""
import importlib
from datetime import datetime

from main import next_run_ts, parse_hhmm


def test_parse_hhmm():
    assert parse_hhmm("11:50") == (11, 50, 0)
    assert parse_hhmm("12:00:30") == (12, 0, 30)


def test_next_run_ts_today_when_future():
    now = datetime(2026, 9, 27, 10, 0, 0)
    ts = next_run_ts("11:50", now=now)
    assert datetime.fromtimestamp(ts) == datetime(2026, 9, 27, 11, 50, 0)


def test_next_run_ts_tomorrow_when_passed():
    now = datetime(2026, 9, 27, 13, 0, 0)
    ts = next_run_ts("11:50", now=now)
    assert datetime.fromtimestamp(ts) == datetime(2026, 9, 28, 11, 50, 0)


def test_next_run_ts_same_minute_goes_tomorrow():
    # 恰好等于当前时刻视为已过（避免零延迟空转）
    now = datetime(2026, 9, 27, 11, 50, 0)
    ts = next_run_ts("11:50", now=now)
    assert datetime.fromtimestamp(ts) == datetime(2026, 9, 28, 11, 50, 0)


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("BILI_ACCESS_KEY", "env_ak_1234567890")
    monkeypatch.setenv("BILI_CSRF", "env_csrf")
    from config import settings
    importlib.reload(settings)
    try:
        assert settings.ACCESS_KEY == "env_ak_1234567890"
        assert settings.CSRF == "env_csrf"
        # 未设置的项回落默认值
        assert settings.APP_KEY == "1d8b6e7d45233436"
    finally:
        # 恢复默认模块状态，避免污染其他测试
        monkeypatch.delenv("BILI_ACCESS_KEY")
        monkeypatch.delenv("BILI_CSRF")
        importlib.reload(settings)
