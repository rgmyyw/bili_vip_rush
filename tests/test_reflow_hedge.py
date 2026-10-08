# -*- coding: utf-8 -*-
"""回流捡漏窗与形态对冲测试(10-08)。

回流:主轮失败后等到支付时效点低密度值守(默认关——用户定调该套餐
抢到即必付,弃单率≈0;代码保留,REFLOW_ENABLED=True 启用)。
对冲:二波末尾 2 路换变体 BUILD+UA 下单,主形态保持冻结。
"""
import json
import time
from unittest import mock

from core.client import BiliApiError
from flows.rush import RushFlow, hedge_worker_ids
import flows.rush as rush_mod
from config import settings as st

PLAN = {"name": "p1", "act_token": "T1", "app_id": "241",
        "app_sub_id": "26moe_fhc", "panel_type": "26moe_cdd178",
        "months": 12, "order_type": 1, "product_type": "1"}


class ReflowClient:
    """按 phase 切换行为:非 reflow 一律 43055(挤门),reflow 首发成交。"""

    def __init__(self, order=None, fail_code=43055):
        self.order = order or {"order_no": "RF1"}
        self.fail_code = fail_code
        self.phase = ""
        self.server_time = int(time.time())
        self.card = {"drainage_status": "ON_SALE",
                     "next_open_at": time.time() + 1,
                     "current_time": self.server_time,
                     "isReserved": True, "has_buy": False}
        self.buy_sets = []

    def get_attract_card(self):
        return dict(self.card)

    def get_server_time(self):
        return self.server_time

    def get_buy_component(self):
        return {"buySets": [dict(b) for b in self.buy_sets]}

    def create_order(self, plan):
        if getattr(self, "phase", "") == "reflow":
            return dict(self.order)
        raise BiliApiError(f"code={self.fail_code} 活动太火爆",
                           code=self.fail_code)

    def pay_link(self, plan):
        return "http://pay"

    def get_order_status(self, order, app_id="241"):
        return {"status": 1}

    def prewarm(self, connections=1):
        return 0.01


def _flow(client, tmp_path):
    return RushFlow(client, plans=[dict(PLAN)], log_dir=tmp_path)


def _events(tmp_path):
    f = sorted(tmp_path.glob("run_*.jsonl"))[0]
    return [json.loads(x) for x in
            f.read_text(encoding="utf-8").splitlines()]


def _reflow_on(monkeypatch, **kw):
    cfg = {"REFLOW_ENABLED": True, "REFLOW_DELAY_S": 0.05,
           "REFLOW_WINDOW_S": 1.0, "REFLOW_INTERVAL": 0.02,
           "REFLOW_WORKERS": 2, "REFLOW_MAX_ATTEMPTS": 10}
    cfg.update(kw)
    for k, v in cfg.items():
        monkeypatch.setattr(st, k, v)


# ------------------------------------------------ 回流:默认关闭
def test_reflow_disabled_by_default(tmp_path):
    """默认关:主轮失败后不等待不值守,rush 立即返回。"""
    assert st.REFLOW_ENABLED is False
    t0 = time.time()
    flow = _flow(ReflowClient(), tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       duration=0.4)
    assert result is None
    # 校时+主轮本身约 1~3s;回流若被误启用会等 REFLOW_DELAY_S=600s
    assert time.time() - t0 < 5
    assert not [e for e in _events(tmp_path)
                if e.get("event", "").startswith("reflow")]


# ------------------------------------------------ 回流:启用态行为
def test_reflow_picks_up_after_timeout(tmp_path, monkeypatch):
    """主轮 timeout 后值守回流窗,首发命中即返回订单并置成功终态。"""
    _reflow_on(monkeypatch)
    flow = _flow(ReflowClient(), tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       duration=0.4)
    assert result == {"order_no": "RF1"}
    evs = _events(tmp_path)
    assert [e for e in evs if e.get("event") == "reflow_wait"]
    ok = [e for e in evs if e.get("event") == "order_ok"]
    assert ok and ok[0]["worker"] >= 100       # 回流 worker 独立编号
    assert flow.last_result["result"] == "success"
    assert flow.last_result["pay_link"]        # 通知链路带支付链接
    done = [e for e in evs if e.get("event") == "reflow_done"]
    assert done and done[0]["result"] == "success"
    assert done[0]["entered"] >= 1 and done[0]["attempts"] >= 1


def test_reflow_skips_when_credential_expired(tmp_path, monkeypatch):
    """凭证失效终态不做回流(重试无意义,别烧账号)。"""
    _reflow_on(monkeypatch)
    flow = _flow(ReflowClient(fail_code=-101), tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       duration=0.3)
    assert result is None
    skips = [e for e in _events(tmp_path) if e.get("event") == "reflow_skip"]
    assert skips and skips[0]["reason"] == "credential_expired"


def test_reflow_hasbuy_safety_gate(tmp_path, monkeypatch):
    """值守前复查:服务端 hasBuy=true(疑似漏单)→ 不下单,紧急通知人工。"""
    _reflow_on(monkeypatch)
    client = ReflowClient()
    client.buy_sets = [{"token": "T1", "hasBuy": True}]
    flow = _flow(client, tmp_path)
    with mock.patch("core.notify.notify_all") as na:
        result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                           duration=0.4)
        na.assert_called_once()
    assert result is None
    skips = [e for e in _events(tmp_path) if e.get("event") == "reflow_skip"]
    assert skips and skips[0]["reason"] == "already_bought_server_side"
    # 安全闸后绝不发下单请求
    assert not [e for e in _events(tmp_path)
                if e.get("event") in ("order_ok", "order_fail")
                and e.get("worker", 0) >= 100]


def test_reflow_respects_pause(tmp_path, monkeypatch):
    """回流等待期间被暂停:立即放弃回流(宁可错过不误买)。

    暂停从 reflow_wait 记录后开始生效——主轮不受影响,只拦回流。
    """
    _reflow_on(monkeypatch, REFLOW_DELAY_S=3.0)   # 校时耗 ~1.5s,须留足余量
    flow = _flow(ReflowClient(), tmp_path)
    paused = {"v": False}
    _orig_record = flow._record

    def _record_or_pause(ev, **kw):
        if ev == "reflow_wait":
            paused["v"] = True
        return _orig_record(ev, **kw)

    flow._record = _record_or_pause
    monkeypatch.setattr(rush_mod, "is_paused", lambda: paused["v"])
    t0 = time.time()
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       duration=0.3)
    assert result is None
    assert time.time() - t0 < 5
    skips = [e for e in _events(tmp_path) if e.get("event") == "reflow_skip"]
    assert skips and skips[0]["reason"] == "paused"


# ------------------------------------------------ 形态对冲
def test_hedge_worker_ids_default():
    """默认配置:二波(8..14)末尾 2 路 = {13,14}。"""
    ids = hedge_worker_ids()
    assert ids == {13, 14}
    v1, v2 = int(st.RUSH_VOLLEY_1), int(st.RUSH_VOLLEY_2)
    assert ids and ids.issubset(set(range(v1, v1 + v2)))  # 只在二波段


def test_form_hedge_event_and_variant_tags(tmp_path):
    """复盘留痕:form_hedge 一行自描述 + 对冲路逐发 variant 标记。"""
    flow = _flow(ReflowClient(), tmp_path)
    flow.rush(target_ts=time.time() - 1, early_seconds=0, duration=0.6)
    evs = _events(tmp_path)
    hedge_ev = [e for e in evs if e.get("event") == "form_hedge"]
    assert hedge_ev and hedge_ev[0]["workers"] == [13, 14]
    assert hedge_ev[0]["build"] == "9130500"
    summary = [e for e in evs if e.get("event") == "run_summary"][0]
    assert summary["hedge_workers"] == [13, 14]
    fails = [e for e in evs if e.get("event") == "order_fail"]
    assert fails                                 # 主轮有逐发记录
    v_fails = [f for f in fails if f.get("variant")]
    assert v_fails and {f["worker"] for f in v_fails} == {13, 14}
    for f in fails:                              # 每发自带距开售秒数
        assert isinstance(f.get("sale_s"), float)


def test_hedge_worker_ids_off(monkeypatch):
    monkeypatch.setattr(st, "RUSH_HEDGE_WORKERS", 0)
    assert hedge_worker_ids() == set()


def test_create_order_variant_build_ua():
    """plan 带 _build/_ua:签名参数与 UA 请求级覆盖,不污染 session。"""
    from core.client import BiliClient

    class FakeResp:
        status_code = 200
        text = '{"code": 0, "data": {"order_no": "V1"}}'

        @staticmethod
        def json():
            return {"code": 0, "data": {"order_no": "V1"}}

    c = BiliClient(run_logger=None, timeout=1)
    with mock.patch.object(c.session, "request",
                           return_value=FakeResp()) as req:
        order = c.create_order(dict(PLAN, _build="9130500",
                                    _ua="UA-9130500"))
    assert order == {"order_no": "V1"}
    kwargs = req.call_args.kwargs
    assert kwargs["params"]["build"] == "9130500"       # 签名前已覆盖
    assert len(kwargs["params"]["sign"]) == 32           # 签名完整
    assert kwargs["headers"]["User-Agent"] == "UA-9130500"
    assert c.session.headers["User-Agent"] == st.APP_UA  # session 未被污染


def test_plain_plan_uses_frozen_build():
    """普通 plan 仍走冻结主形态(BUILD=9110400,09-27 实战成交组合)。"""
    from core.client import BiliClient

    class FakeResp:
        status_code = 200
        text = '{"code": 0, "data": {}}'

        @staticmethod
        def json():
            return {"code": 0, "data": {}}

    c = BiliClient(run_logger=None, timeout=1)
    with mock.patch.object(c.session, "request",
                           return_value=FakeResp()) as req:
        c.create_order(dict(PLAN))
    kwargs = req.call_args.kwargs
    assert kwargs["params"]["build"] == st.BUILD
    assert "User-Agent" not in kwargs["headers"]          # 无覆盖头
