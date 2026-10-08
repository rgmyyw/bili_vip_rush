# -*- coding: utf-8 -*-
"""-702 频控熔断与 30 发预算上限测试(10-08 复盘定案)。

七日铁律:频控墙后发数 >130 穿透 0 次(10-08 当日 43 发全拒);账号级
额度 ~30 发(10-02 瞬时 51/10-07 匀速 30/10-08 恰 30),第 31 发起必吃
-702——两条熔断把每天的发数从 73 收到 ~30,墙后一发不烧。
"""
import json
import time

from flows.rush import RushFlow

PLAN = {"name": "p1", "act_token": "T1", "app_id": "241",
        "app_sub_id": "26moe_fhc", "panel_type": "26moe_cdd178",
        "months": 12, "order_type": 1, "product_type": "1"}


class FuseClient:
    """恒定失败码客户端(全 mock,不发网络)。"""

    def __init__(self, fail_code):
        self.fail_code = fail_code
        self.phase = ""
        self.server_time = int(time.time())
        self.card = {"drainage_status": "ON_SALE",
                     "next_open_at": time.time() + 1,
                     "current_time": self.server_time,
                     "isReserved": True, "has_buy": False}

    def get_attract_card(self):
        return dict(self.card)

    def get_server_time(self):
        return self.server_time

    def get_buy_component(self):
        return {"buySets": []}

    def create_order(self, plan):
        from core.client import BiliApiError
        raise BiliApiError(f"code={self.fail_code}", code=self.fail_code)

    def pay_link(self, plan):
        return "http://pay"

    def get_order_status(self, order, app_id="241"):
        return {"status": 1}

    def prewarm(self, connections=1):
        return 0.01


def _events(tmp_path):
    f = sorted(tmp_path.glob("run_*.jsonl"))[0]
    return [json.loads(x) for x in
            f.read_text(encoding="utf-8").splitlines()]


def test_freq_wall_stops_immediately(tmp_path):
    """连续 3 发 -702 即全局收兵:不吃满时长,墙后一发不烧。"""
    from datetime import datetime
    flow = RushFlow(FuseClient(-702), plans=[dict(PLAN)], log_dir=tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       duration=5.0)
    assert result is None
    evs = _events(tmp_path)
    stops = [e for e in evs if e.get("event") == "stop_reason"
             and e["reason"] == "freq_wall"]
    assert stops                            # 留痕 reason=freq_wall
    # 抢购相位耗时(剔除校时):起跑到熔断必须秒级,不吃满 5s
    t_start = next(datetime.fromisoformat(e["t"]).timestamp()
                   for e in evs if e.get("event") == "rush_start")
    t_stop = datetime.fromisoformat(stops[0]["t"]).timestamp()
    assert t_stop - t_start < 1.5           # 旧版会打满并烧 43 发
    summary = [e for e in evs if e.get("event") == "run_summary"][0]
    assert summary["attempts"] <= 12        # 8 路一波在途+熔断即收
    assert flow.last_result["result"] == "freq_wall"


def test_budget_cap_30(tmp_path):
    """43055 永远挤不进:全局发数到 30 即收(31 发起必吃 -702)。"""
    from datetime import datetime
    flow = RushFlow(FuseClient(43055), plans=[dict(PLAN)], log_dir=tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       duration=5.0)
    assert result is None
    evs = _events(tmp_path)
    stops = [e for e in evs if e.get("event") == "stop_reason"
             and e["reason"] == "budget_cap"]
    assert stops and stops[0]["attempts"] == 30
    # 抢购相位耗时(剔除校时):二轮 0.3s 间隔,30 发在 ~1s 内投完即收
    t_start = next(datetime.fromisoformat(e["t"]).timestamp()
                   for e in evs if e.get("event") == "rush_start")
    t_stop = datetime.fromisoformat(stops[0]["t"]).timestamp()
    assert t_stop - t_start < 3
    summary = [e for e in evs if e.get("event") == "run_summary"][0]
    # 20 路并发下最多数发在途越过阈值,但必须 << 旧版 73 发
    assert 30 <= summary["attempts"] <= 40
    assert flow.last_result["result"] == "budget_cap"


def test_notify_reason_text_covers_new_stops():
    """通知文案覆盖新终态,推送标题不出现裸 result 串。"""
    from core.notify import REASON_TEXT
    assert "freq_wall" in REASON_TEXT and "budget_cap" in REASON_TEXT


def test_explicit_max_attempts_reason(tmp_path):
    """显式 max_attempts(回流路径)仍记 max_attempts,与预算语义区分。"""
    import threading
    flow = RushFlow(FuseClient(43055), plans=[dict(PLAN)], log_dir=tmp_path)
    flow._lead_s = 0.0
    counter = {"lock": threading.Lock(), "n": 0, "timed_out": False,
               "risk": 0, "crashed": 0, "win_total": 0, "win_702": 0,
               "win_rate": 0.0, "open_obs_s": None,
               "burst": threading.Event()}
    counter["burst"].set()      # 跳过波门,直接进场
    stop = threading.Event()
    r = flow._rush_attempts(time.monotonic() + 2, 2.0, time.monotonic(),
                            stop, counter, flow.client, 0, interval=0.01,
                            pacing_off=True, max_attempts=3)
    assert r is None and stop.is_set()
    evs = _events(tmp_path)
    assert any(e.get("event") == "stop_reason"
               and e["reason"] == "max_attempts" for e in evs)
