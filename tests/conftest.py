# -*- coding: utf-8 -*-
"""把项目根目录加入 sys.path，保证 config/core/flows 可导入。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# 隔离外网:校时的 NTP 查询在测试中禁用(各用例需要时自行 patch)
import pytest


@pytest.fixture(autouse=True)
def _no_ntp(monkeypatch):
    from core import time_sync
    monkeypatch.setattr(time_sync, "measure_ntp_offset",
                        lambda *a, **k: None)


@pytest.fixture
def pause_file(tmp_path, monkeypatch):
    """隔离的 pause.json,并指向临时文件。"""
    import json as _json
    from core import pause as pause_mod
    f = tmp_path / "pause.json"
    f.write_text(_json.dumps({"paused": False}), encoding="utf-8")
    monkeypatch.setattr(pause_mod.settings, "PAUSE_JSON_PATH", f)
    return f
