# -*- coding: utf-8 -*-
"""RushFlow 流程测试：mock 客户端，验证抢购状态机。"""
import json
import time
from unittest import mock

from core.client import BiliApiError
from flows.rush import RushFlow


class FakeClient:
    def __init__(self, order_outcomes):
        self.order_outcomes = list(order_outcomes)  # 每次下单依次弹出一个结果
        self.server_time = int(time.time())
        self.card = {"drainage_status": "ON_SALE", "next_open_at": time.time() + 1,
                     "current_time": self.server_time, "isReserved": True,
                     "has_buy": False}

    def get_attract_card(self):
        return dict(self.card)

    def get_server_time(self):
        return self.server_time

    def get_buy_component(self):
        return {"buySets": []}

    def create_order(self, plan):
        result = self.order_outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def pay_link(self, plan):
        return "https://big.bilibili.com/mobile/activityPay?act_token=T1"

    def get_order_status(self, order, app_id="241"):
        return {"status": 1, "order_no": order.get("order_no")}


def make_flow(order_outcomes, tmp_path, **rush_kwargs):
    client = FakeClient(order_outcomes)
    flow = RushFlow(client, plans=[{
        "name": "p1", "act_token": "T1", "app_id": "241",
        "app_sub_id": "26moe_fhc", "panel_type": "26moe_cdd178",
        "months": 12, "order_type": 1, "product_type": "1"}],
        log_dir=tmp_path)
    return flow, client


def test_rush_success_stops_immediately(tmp_path):
    order = {"order_no": "OK123"}
    flow, _ = make_flow([order], tmp_path)
    result = flow.rush(target_ts=time.time() - 1,   # 已开售
                       early_seconds=0, interval=0.01, duration=5)
    assert result == {"order_no": "OK123"}


def test_rush_retries_then_success(tmp_path):
    err1 = BiliApiError("code=1 活动未开始", code=1, response_text="raw1")
    order = {"order_no": "OK456"}
    flow, _ = make_flow([err1, err1, order], tmp_path)
    result = flow.rush(target_ts=time.time() - 1, interval=0.01,
                       early_seconds=0, duration=5)
    assert result["order_no"] == "OK456"


def test_rush_sold_out_stops_after_linger(tmp_path):
    import flows.rush as rush_mod
    errs = [BiliApiError("code=1 今日已售罄", code=1)] * 100
    flow, _ = make_flow(errs, tmp_path)
    with mock.patch.object(rush_mod.settings, "SOLD_OUT_LINGER_SECONDS", 0):
        result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                           interval=0.01, duration=5)
    assert result is None


def test_rush_writes_scene_log(tmp_path):
    order = {"order_no": "OK789"}
    flow, _ = make_flow([order], tmp_path)
    flow.rush(target_ts=time.time() - 1, early_seconds=0, interval=0.01,
              duration=5)
    log_files = list(tmp_path.glob("run_*.jsonl"))
    assert log_files, "应生成运行日志"
    content = log_files[0].read_text(encoding="utf-8")
    assert "order_ok" in content and "OK789" in content


def test_rush_unexpected_error_recorded_and_continues(tmp_path):
    class Boom(Exception):
        pass

    order = {"order_no": "OK000"}
    flow, _ = make_flow([Boom("bug"), order], tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       interval=0.01, duration=5)
    assert result["order_no"] == "OK000"
    content = list(tmp_path.glob("run_*.jsonl"))[0].read_text(encoding="utf-8")
    assert "unexpected_error" in content and "Boom" in content


# ---------------------------------------------------------------- 节奏与熔断
import flows.rush as rush_mod


def test_next_interval_three_stages():
    # 爆发段：前 BURST_COUNT 次，值域 [burst*(1-j), burst*(1+j)]
    for attempt in range(1, rush_mod.settings.RUSH_BURST_COUNT + 1):
        v = rush_mod.next_interval(attempt, elapsed=0.0)
        b, j = rush_mod.settings.RUSH_BURST_INTERVAL, rush_mod.settings.RUSH_JITTER
        assert b * (1 - j) <= v <= b * (1 + j)
    # 快速段：attempt 已过爆发、elapsed 在窗口内
    v = rush_mod.next_interval(rush_mod.settings.RUSH_BURST_COUNT + 1,
                               elapsed=1.0)
    b, j = rush_mod.settings.RUSH_FAST_INTERVAL, rush_mod.settings.RUSH_JITTER
    assert b * (1 - j) <= v <= b * (1 + j)
    # 慢速段
    v = rush_mod.next_interval(999, elapsed=rush_mod.settings.RUSH_FAST_WINDOW + 1)
    b = rush_mod.settings.RUSH_SLOW_INTERVAL
    assert b * (1 - j) <= v <= b * (1 + j)
    # override：不抖动
    assert rush_mod.next_interval(1, 0.0, override=0.05) == 0.05


def test_is_system_error():
    from core.client import BiliApiError
    assert rush_mod.is_system_error(BiliApiError("网络失败", code=None))
    assert rush_mod.is_system_error(BiliApiError("HTTP 502", code=502))
    assert not rush_mod.is_system_error(BiliApiError("code=-400", code=-400))
    assert not rush_mod.is_system_error(BiliApiError("code=1", code=1))


def test_rush_max_attempts_stops(tmp_path):
    """发数上限熔断即停——主轮实际上限是 30 发预算(budget_cap,
    10-08 定案:第 31 发起必吃 -702,墙后一发都不值)。"""
    errs = [BiliApiError("code=1 还没开售", code=1)] * 50
    flow, _ = make_flow(errs, tmp_path)
    with mock.patch.object(rush_mod.settings, "RUSH_ATTEMPT_BUDGET", 3):
        result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                           interval=0.0, duration=5)
    assert result is None
    content = list(tmp_path.glob("run_*.jsonl"))[0].read_text(encoding="utf-8")
    assert '"reason": "budget_cap"' in content


def test_rush_syserr_cooldown_recorded(tmp_path):
    from core.client import BiliApiError
    errs = [BiliApiError("网络失败: timeout", code=None)] * 10
    order = {"order_no": "OK111"}
    errs.append(order)
    flow, _ = make_flow(errs, tmp_path)
    with mock.patch.object(rush_mod.settings, "RUSH_SYSERR_PAUSE", 2), \
         mock.patch.object(rush_mod.settings, "RUSH_SYSERR_COOLDOWN", 0.0):
        result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                           interval=0.0, duration=5)
    assert result == {"order_no": "OK111"}
    content = list(tmp_path.glob("run_*.jsonl"))[0].read_text(encoding="utf-8")
    assert "syserr_cooldown" in content


def test_rush_success_records_order_status(tmp_path):
    order = {"order_no": "OK222"}
    flow, _ = make_flow([order], tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       interval=0.01, duration=5)
    assert result["order_no"] == "OK222"
    rows = [json.loads(x) for x in
            list(tmp_path.glob("run_*.jsonl"))[0].read_text(encoding="utf-8").splitlines()]
    ok = [r for r in rows if r.get("event") == "order_ok"][0]
    assert ok["order_status"] == {"status": 1, "order_no": "OK222"}


def test_rush_credential_expired_stops_immediately(tmp_path):
    """凭证失效(-101)必须立即停止，不烧完重试预算。"""
    from core.client import BiliApiError
    order = {"order_no": "NEVER"}
    flow, client = make_flow(
        [BiliApiError("code=-101 message=账号未登录", code=-101), order], tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       interval=0.0, duration=5)
    assert result is None
    content = list(tmp_path.glob("run_*.jsonl"))[0].read_text(encoding="utf-8")
    assert '"reason": "credential_expired"' in content
    # 第二个候选订单未被消费（第一击即停）
    assert client.order_outcomes == [order]
