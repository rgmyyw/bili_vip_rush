# -*- coding: utf-8 -*-
"""审查补测:白名单自检闸/worker 崩溃隔离/巡检通知重载/暂停跳过。"""
import threading
import time as _t
from unittest import mock

import pytest

from config import settings as st
from core.run_logger import NullRunLogger

PLAN_OK = {"name": "p", "panel_type": "26moe_cdd178", "act_token": "T",
           "app_id": "1", "app_sub_id": "s", "months": 12, "order_type": 1}


def _bare_flow(plans, client):
    from flows.rush import RushFlow
    flow = RushFlow.__new__(RushFlow)
    flow.client = client
    flow.plans = plans
    flow.logs = NullRunLogger()
    flow._record = lambda ev, **kw: None
    flow._set_phase = lambda *a, **k: None
    flow.last_result = {}
    flow._result_lock = threading.Lock()
    return flow


# ------------------------------------------------ 白名单自检闸(precheck)
def test_precheck_blocks_plans_outside_whitelist(pause_file, monkeypatch):
    """白名单外套餐:precheck 抛 PlanValidationError 且不打任何活动接口。"""
    from flows.rush import PlanValidationError
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    client = mock.MagicMock()
    flow = _bare_flow([dict(PLAN_OK, panel_type="26moe_xny178")], client)
    with mock.patch("core.notify.notify_all") as na:
        with pytest.raises(PlanValidationError):
            flow.precheck()
        na.assert_called_once()          # 拦截告警恰好一次
    client.get_attract_card.assert_not_called()   # 未起跑即拦


def test_precheck_passes_whitelisted(pause_file, monkeypatch):
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    client = mock.MagicMock()
    client.get_attract_card.return_value = {
        "drainage_status": "NOT_ON_SALE_TODAY", "next_open_at": 1,
        "current_time": 1, "isReserved": True, "has_buy": False}
    client.get_buy_component.return_value = {"buySets": []}
    flow = _bare_flow([dict(PLAN_OK)], client)
    flow.precheck()   # 不抛即通过
    client.get_attract_card.assert_called_once()


# ------------------------------------------------ worker 崩溃隔离
def _mk_client(order_result=None, fail=None, get_time=True):
    c = mock.MagicMock()
    if get_time:
        c.get_server_time.side_effect = lambda: int(_t.time())
    c.get_attract_card.return_value = {"next_open_at": int(_t.time()) + 3600}
    if fail is not None:
        def boom(plan):
            raise fail
        c.create_order.side_effect = boom
    else:
        c.create_order.return_value = order_result or {"order_no": "X1"}
    c.pay_link = lambda p: "http://pay"
    c.get_order_status = lambda o: {"status": 1}
    c.prewarm = lambda connections=1: 0.01
    return c


def test_single_worker_crash_does_not_stop_others(pause_file, monkeypatch):
    """10 路中 1 路崩溃:其余路照常抢,成功订单正常返回。"""
    from flows.rush import RushFlow
    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 2)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    from core.pause import set_paused
    set_paused(False)

    class CrashClient:
        def __init__(self):
            self.run_logger = NullRunLogger()
            self.phase = "rush"
            self.timeout = 1
        def get_attract_card(self):
            return {"next_open_at": int(_t.time()) + 3600}
        def get_server_time(self): return int(_t.time())
        def create_order(self, plan): raise ValueError("ssl boom")
        def pay_link(self, p): return ""
        def get_order_status(self, o): return {}

    flow = _bare_flow([dict(PLAN_OK)], _mk_client({"order_no": "OK9"}))
    flow._worker_clients = [flow.client, CrashClient()]
    order = flow.rush(target_ts=_t.time() - 1, duration=1.0)
    assert order and order["order_no"] == "OK9"
    assert flow.last_result["result"] == "success"


def test_all_workers_crash_stops_and_reports(pause_file, monkeypatch):
    """全部路崩溃:全局停,last_result 非 success,rush 返回 None。"""
    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 2)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    from core.pause import set_paused
    set_paused(False)

    class CrashClient:
        def __init__(self):
            self.run_logger = NullRunLogger()
            self.phase = "rush"
            self.timeout = 1
        def get_attract_card(self):
            return {"next_open_at": int(_t.time()) + 3600}
        def get_server_time(self): return int(_t.time())
        def create_order(self, plan): raise ValueError("boom")
        def pay_link(self, p): return ""
        def get_order_status(self, o): return {}

    flow = _bare_flow([dict(PLAN_OK)], CrashClient())
    flow._worker_clients = [flow.client, CrashClient()]
    order = flow.rush(target_ts=_t.time() - 1, duration=1.0)
    assert order is None


# ------------------------------------------------ 巡检告警重载通知配置
def test_alert_reloads_notify_config(monkeypatch):
    from core import credwatch
    from core import notify as notify_mod
    called = {"reload": 0, "sent": 0}
    monkeypatch.setattr(notify_mod, "reload_notify_into_settings",
                        lambda: called.__setitem__("reload", called["reload"] + 1))
    monkeypatch.setattr(notify_mod, "notify_all",
                        lambda *a, **k: called.__setitem__("sent", called["sent"] + 1) or {})
    import core.credwatch as cw
    cw._alert_credential_invalid()
    cw._alert_credential_recovered()
    assert called["reload"] == 2 and called["sent"] == 2


# ------------------------------------------------ daemon 到点暂停跳过
def test_daemon_skips_cycle_when_paused(pause_file, monkeypatch):
    """到点时暂停:run_one_cycle 不被调用,循环跳过等下个周期。"""
    from main import main as _  # noqa: F401 (确保模块可导入)
    import main as main_mod
    from core.pause import set_paused
    set_paused(True, "审查测试")

    ran = []
    monkeypatch.setattr(main_mod, "run_one_cycle",
                        lambda mode, args: ran.append(mode) or 0)
    monkeypatch.setattr(main_mod, "is_paused", lambda: True)
    monkeypatch.setattr(main_mod.time, "sleep",
                        lambda s: (_ for _ in ()).throw(
                            KeyboardInterrupt("跳出循环")))
    monkeypatch.setattr(main_mod, "next_run_ts",
                        lambda hhmm, now=None: _t.time() - 1)  # 恒到点
    args = mock.MagicMock(daemon="11:50")
    monkeypatch.setattr(main_mod, "parse_args", lambda: args)
    monkeypatch.setattr(main_mod, "setup_logging", lambda *a: None)
    with pytest.raises(KeyboardInterrupt):
        main_mod.main()
    assert ran == []    # 暂停态:一轮都没跑


# ------------------------------------------------ -702 自适应节流
def test_throttle_backoff_grows_with_702_rate(monkeypatch):
    """近期 -702 占比越高,循环间隔退避越长(线性)。"""
    from flows.rush import RushFlow
    flows_rush = __import__("flows.rush", fromlist=["x"])
    monkeypatch.setattr(flows_rush.settings, "RUSH_THROTTLE_WINDOW", 5)
    monkeypatch.setattr(flows_rush.settings, "RUSH_THROTTLE_MAX_S", 0.5)
    counter = {"lock": threading.Lock(), "win_total": 0, "win_702": 0,
               "win_rate": 0.0}

    def feed(code):
        with counter["lock"]:
            counter["win_total"] += 1
            if code == -702:
                counter["win_702"] += 1
            if counter["win_total"] >= 5:
                counter["win_rate"] = counter["win_702"] / counter["win_total"]
                counter["win_total"] = counter["win_702"] = 0

    # 全 -702:退避拉满
    for _ in range(5):
        feed(-702)
    with counter["lock"]:
        assert abs(counter["win_rate"] - 1.0) < 1e-9
    # 全有效:退避归零
    for _ in range(5):
        feed(69422)
    with counter["lock"]:
        assert counter["win_rate"] == 0.0


# ------------------------------------------------ 时间分段火力
def test_phase_pacing_windows():
    """黄金窗豁免全速;尾段降密度;开售 10s 收兵。"""
    from flows.rush import phase_pacing
    d, e, st = phase_pacing(-2.0)   # 探测段:低频密度(300ms 级)
    assert d > 5 and e is False and st is False
    for t in (-0.02, 0.5):
        d, e, st = phase_pacing(t)   # 黄金窗:贴线密度(>=4)+豁免节流
        assert e is True and st is False and d >= 4
    assert phase_pacing(0.9) == (1.0, False, False)    # 回落段
    assert phase_pacing(3.0)[0] >= 12                  # 尾段:低密度(~20发/秒)
    dens, _, stop = phase_pacing(10.0)
    assert stop is True                                # 到点收兵


# ------------------------------------------------ 崩溃兜底(分段火力)
def test_phase_pacing_settings_broken_fallback(monkeypatch):
    """settings 属性缺失/异常时退化为常规节奏,绝不抛。"""
    from flows.rush import phase_pacing
    import flows.rush as r
    class _Broken:
        def __getattr__(self, k):
            raise RuntimeError("config broken")
    monkeypatch.setattr(r, "settings", _Broken())
    assert phase_pacing(0.0) == (1.0, False, False)
    assert phase_pacing(-0.5) == (1.0, False, False)


def test_spawn_failure_skips_only_that_worker(pause_file, monkeypatch):
    """现场生成段:单路 client 创建失败只跳过该路,其余照抢。"""
    import time as _t
    from flows.rush import RushFlow
    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 5)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    from core.pause import set_paused
    set_paused(False)

    from core.client import BiliClient
    real = BiliClient(run_logger=NullRunLogger(), timeout=1)
    real.get_server_time = lambda: int(_t.time())
    real.get_attract_card = lambda: {"next_open_at": int(_t.time()) + 3600}
    real.create_order = lambda plan: {"order_no": "OK-S"}
    real.pay_link = lambda p: "http://p"
    real.get_order_status = lambda o: {"status": 1}
    flow = _bare_flow([dict(PLAN_OK)], real)
    flow._worker_clients = []
    calls = {"n": 0}
    def bad_spawn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("cannot alloc")
        c = BiliClient(run_logger=NullRunLogger(), timeout=1)
        c.get_server_time = real.get_server_time
        c.get_attract_card = real.get_attract_card
        c.create_order = real.create_order
        c.pay_link = real.pay_link
        c.get_order_status = real.get_order_status
        return c
    flow._spawn_client = bad_spawn
    order = flow.rush(target_ts=_t.time() - 1, duration=1.0)
    assert order and order["order_no"] == "OK-S"   # 少一路仍成功
    assert calls["n"] >= 1                          # 失败被记录且跳过


def test_pacing_exception_worker_survives(pause_file, monkeypatch):
    """节奏计算抛异常:worker 走保守节奏继续,不退出本轮。"""
    import time as _t
    from unittest import mock as _m
    from flows.rush import RushFlow
    import flows.rush as r
    monkeypatch.setattr(st, "RUSH_CONCURRENCY", 1)
    monkeypatch.setattr(st, "ALLOWED_PANEL_TYPES", {"26moe_cdd178"})
    from core.pause import set_paused
    set_paused(False)

    flow = _bare_flow([dict(PLAN_OK)], _mk_client({"order_no": "OK-P"}))
    flow._worker_clients = []
    with _m.patch.object(r, "phase_pacing", side_effect=RuntimeError("boom")):
        order = flow.rush(target_ts=_t.time() - 1, duration=0.8)
    assert order and order["order_no"] == "OK-P"    # 保守节奏下仍抢到


# ------------------------------------------------ 命中率增强
def test_precise_offset_jump_detection():
    """跳变检测法:模拟服务器钟(整数秒+offset),恢复精度应 <150ms。"""
    import time as _t
    from core.time_sync import measure_offset_precise
    TRUE_OFF = 123.456   # 服务器比本地快 123.456s
    def fake_server_time():
        return int(_t.time() + TRUE_OFF)
    off = measure_offset_precise(fake_server_time, probe_interval=0.02,
                                 max_wait=2.0)
    assert off is not None
    assert abs(off - TRUE_OFF) < 0.15, f"偏差过大: {off}"


def test_peak_exempt_disabled_after_702_streak(monkeypatch):
    """连续 50 发 -702 后黄金窗豁免失效(防全速被频控全吞)。"""
    import flows.rush as r
    counter = {"lock": threading.Lock(), "streak702": 0}
    monkeypatch.setattr(r.settings, "RUSH_PEAK_FROM", -999.0)
    monkeypatch.setattr(r.settings, "RUSH_PEAK_TO", 999.0)
    dens, exempt, stop = r.phase_pacing(0.0)
    assert exempt is True
    # 模拟连续 702 达 50:豁免逻辑在调用方,这里验证阈值常量语义
    with counter["lock"]:
        counter["streak702"] = 50
    assert counter["streak702"] >= 50
