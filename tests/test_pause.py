# -*- coding: utf-8 -*-
"""服务暂停开关测试：pause.json 读写 + daemon/rush 检查点行为。"""
import json

import pytest

from core import pause as pause_mod
from core.pause import RushPausedError, is_paused, pause_state, set_paused


def test_pause_roundtrip(pause_file):
    assert is_paused() is False
    set_paused(True, "测试暂停")
    assert is_paused() is True
    st = pause_state()
    assert st["paused"] is True
    assert st["reason"] == "测试暂停"
    set_paused(False)
    assert is_paused() is False
    assert pause_state()["reason"] == ""


def test_missing_file_means_not_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(pause_mod.settings, "PAUSE_JSON_PATH",
                        tmp_path / "nonexistent.json")
    assert is_paused() is False   # 文件缺失不阻塞抢购


def test_corrupt_file_means_not_paused(pause_file):
    pause_file.write_text("{broken json", encoding="utf-8")
    assert is_paused() is False


def test_rush_aborts_when_paused_during_wait(pause_file):
    """倒计时等待期间暂停 → rush() 抛 RushPausedError 中止本轮。"""
    from unittest import mock
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger

    import time as _t
    flow = RushFlow.__new__(RushFlow)   # 绕过 __init__（避免真实 client）
    flow.client = mock.MagicMock()
    flow.client.get_server_time.side_effect = lambda: int(_t.time())
    flow.run_logger = NullRunLogger()
    flow._record = lambda *a, **k: None
    flow._set_phase = lambda *a, **k: None

    set_paused(True, "倒计时中暂停")
    import time as _time
    with pytest.raises(RushPausedError):
        flow.rush(target_ts=_time.time() + 60)   # 等待期首秒即应中止


def test_rush_aborts_right_before_fire(pause_file):
    """开抢前一刻才暂停（T-3s 内无检查点的窗口）→ 开抢前最后一道闸拦截。"""
    from unittest import mock
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger

    import time as _t
    flow = RushFlow.__new__(RushFlow)
    flow.client = mock.MagicMock()
    flow.client.get_server_time.side_effect = lambda: int(_t.time())
    flow.run_logger = NullRunLogger()
    flow._record = lambda *a, **k: None
    flow._set_phase = lambda *a, **k: None

    set_paused(False)
    import time as _time
    # 目标已过（remain<=0 直接 break 出等待循环），随后开抢前闸门检查
    set_paused(True, "临开抢暂停")
    with pytest.raises(RushPausedError):
        flow.rush(target_ts=_time.time() - 1)


# ---------------------------------------------------------------- 下单白名单
def test_rush_blocks_plan_outside_whitelist(pause_file, monkeypatch):
    """白名单外的套餐绝不发起 create_order（根治备选落单买错）。"""
    from unittest import mock
    import time as _t
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger
    from config import settings as st

    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    set_paused(False)

    flow = RushFlow.__new__(RushFlow)
    flow.client = mock.MagicMock()
    flow.client.get_server_time.side_effect = lambda: int(_t.time())
    flow.client.create_order.return_value = {"order_no": "X"}
    flow.run_logger = NullRunLogger()
    recorded = []
    flow._record = lambda ev, **kw: recorded.append(ev)
    flow._set_phase = lambda *a, **k: None
    flow.plans = [
        {"name": "错套餐", "panel_type": "26moe_xny178", "act_token": "t",
         "app_id": "1", "app_sub_id": "s", "months": 12, "order_type": 0},
    ]
    # 目标时刻已过 -> 直接进抢购循环
    flow.rush(target_ts=_t.time() - 1, duration=0.2)
    flow.client.create_order.assert_not_called()
    assert "panel_blocked" in recorded


def test_rush_allows_whitelisted_plan(pause_file, monkeypatch):
    from unittest import mock
    import time as _t
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger
    from config import settings as st

    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    set_paused(False)

    flow = RushFlow.__new__(RushFlow)
    flow.client = mock.MagicMock()
    flow.client.get_server_time.side_effect = lambda: int(_t.time())
    flow.client.create_order.return_value = {
        "order_no": "OK1", "orderId": "OK1"}
    flow.run_logger = NullRunLogger()
    flow._record = lambda ev, **kw: None
    flow._set_phase = lambda *a, **k: None
    flow.plans = [
        {"name": "正确套餐", "panel_type": "26moe_cdd178", "act_token": "t",
         "app_id": "1", "app_sub_id": "s", "months": 12, "order_type": 1},
    ]
    order = flow.rush(target_ts=_t.time() - 1, duration=0.3)
    flow.client.create_order.assert_called_once()
    assert order and order["order_no"] == "OK1"


# ---------------------------------------------------------------- 并发抢购
def test_rush_concurrent_workers_first_success_stops_all(pause_file, monkeypatch):
    """3 路并发:任一路成功即全局停,总下单不超预期,结果为首个成功单。"""
    from unittest import mock
    import time as _t
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger
    from config import settings as st

    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 3)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    set_paused(False)

    calls = []
    calls_lock = __import__("threading").Lock()

    class FakeWorkerClient:
        def __init__(self, name):
            self.name = name
            self.phase = "rush"
            self.run_logger = NullRunLogger()
            self.timeout = 1
        def get_server_time(self):
            return int(_t.time())
        def get_attract_card(self):
            return {"next_open_at": int(_t.time()) + 3600}
        def create_order(self, plan):
            with calls_lock:
                calls.append(self.name)
                n = len(calls)
            # 第 3 次调用起成功(模拟几轮拒绝后某路命中)
            if n >= 3:
                return {"order_no": f"OK-{self.name}", "orderId": f"OK-{self.name}"}
            raise __import__("core.client", fromlist=["BiliApiError"]).BiliApiError(
                "接口报错 code=-400 message=未开售")
        def pay_link(self, plan): return "http://pay"
        def get_order_status(self, order): return {"status": 1}
        def prewarm(self, connections=1): return 0.01

    main_client = FakeWorkerClient("main")
    flow = RushFlow.__new__(RushFlow)
    flow.client = main_client
    flow.plans = [{"name": "p", "panel_type": "26moe_cdd178", "act_token": "T",
                   "app_id": "1", "app_sub_id": "s", "months": 12,
                   "order_type": 1}]
    flow.logs = NullRunLogger()
    flow._record = lambda ev, **kw: None
    flow._set_phase = lambda *a, **k: None
    flow.last_result = {}
    flow._result_lock = __import__("threading").Lock()
    flow._worker_clients = [main_client, FakeWorkerClient("w1"), FakeWorkerClient("w2")]
    flow._spawn_client = lambda: FakeWorkerClient("wx")

    import flows.rush as rush_mod
    orig_spawn = rush_mod.RushFlow._spawn_client
    rush_mod.RushFlow._spawn_client = lambda self: FakeWorkerClient("wx")

    order = flow.rush(target_ts=_t.time() - 1, duration=1.5)
    rush_mod.RushFlow._spawn_client = orig_spawn
    assert order and order["order_no"].startswith("OK-")
    assert flow.last_result["result"] == "success"
    # 成功后全局停:总请求数有限(不会打满整个 duration)
    assert len(calls) < 30, f"成功后未及时停: {len(calls)} 次调用"


def test_rush_serial_path_still_works(pause_file, monkeypatch):
    """并发=1 时走原串行路径不受影响。"""
    from unittest import mock
    import time as _t
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger
    from config import settings as st

    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 1)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    set_paused(False)

    flow = RushFlow.__new__(RushFlow)
    flow.client = mock.MagicMock()
    flow.client.get_server_time.side_effect = lambda: int(_t.time())
    flow.client.create_order.return_value = {"order_no": "S1", "orderId": "S1"}
    flow.plans = [{"name": "p", "panel_type": "26moe_cdd178", "act_token": "T",
                   "app_id": "1", "app_sub_id": "s", "months": 12,
                   "order_type": 1}]
    flow.logs = NullRunLogger()
    flow._record = lambda ev, **kw: None
    flow._set_phase = lambda *a, **k: None
    flow.last_result = {}
    flow._result_lock = __import__("threading").Lock()
    flow._worker_clients = []
    order = flow.rush(target_ts=_t.time() - 1, duration=0.3)
    flow.client.create_order.assert_called_once()
    assert order["order_no"] == "S1"


# ---------------------------------------------------------------- 风控熔断
def test_rush_risk_control_stops_all(pause_file, monkeypatch):
    """412/403 连续达到阈值即全局停抢,防止持续轰炸被拉黑。"""
    from unittest import mock
    import time as _t
    import threading
    from flows.rush import RushFlow
    from core.run_logger import NullRunLogger
    from core.client import BiliApiError
    from config import settings as st

    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 2)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    monkeypatch.setattr(st, "RUSH_RISK_PAUSE", 5)
    set_paused(False)

    calls = {"n": 0}
    lock = threading.Lock()

    def fake_create(plan):
        with lock:
            calls["n"] += 1
        raise BiliApiError("接口报错 code=412", code=412)

    flow = RushFlow.__new__(RushFlow)
    flow.client = mock.MagicMock()
    flow.client.get_server_time.side_effect = lambda: int(_t.time())
    flow.client.create_order.side_effect = fake_create
    flow.logs = NullRunLogger()
    flow._record = lambda ev, **kw: None
    flow._set_phase = lambda *a, **k: None
    flow.last_result = {}
    flow._result_lock = threading.Lock()
    flow.plans = [{"name": "p", "panel_type": "26moe_cdd178",
                   "act_token": "T", "app_id": "1", "app_sub_id": "s",
                   "months": 12, "order_type": 1}]
    flow._worker_clients = [flow.client, mock.MagicMock()]
    flow._worker_clients[1].get_server_time.side_effect = lambda: int(_t.time())
    flow._worker_clients[1].create_order.side_effect = fake_create
    flow._worker_clients[1].pay_link = lambda p: ""
    flow._worker_clients[1].get_order_status = lambda o: {}
    flow.client.pay_link = lambda p: ""
    flow.client.get_order_status = lambda o: {}

    flow.rush(target_ts=_t.time() - 1, duration=2.0)
    assert flow.last_result["result"] == "risk_control"
    assert calls["n"] <= 12, f"风控熔断未及时停: {calls['n']} 次调用"
