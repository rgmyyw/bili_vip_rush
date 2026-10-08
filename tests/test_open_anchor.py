# -*- coding: utf-8 -*-
"""开闸锚定与 43055 开闸信号测试(10-02 复盘优化)。

复盘实证:B 站开闸时刻逐日漂移(10-01 +0.2s / 10-02 +0.35s),钟点锚定
的黄金窗打在未开闸的 69422 上;43055(拥挤)是最早的放行实证,却被排除
在开闸信号外,导致 burst 全程未拉响、余量路 +1.0s 才进场全程吃 -702。
"""
import json
import time
from unittest import mock

from core.client import BiliApiError
from flows.rush import effective_sale_s, phase_pacing, RushFlow
import flows.rush as rush_mod


def _flow(order_outcomes, tmp_path):
    from tests.test_rush import FakeClient
    client = FakeClient(order_outcomes)
    flow = RushFlow(client, plans=[{
        "name": "p1", "act_token": "T1", "app_id": "241",
        "app_sub_id": "26moe_fhc", "panel_type": "26moe_cdd178",
        "months": 12, "order_type": 1, "product_type": "1"}],
        log_dir=tmp_path)
    return flow, client


def _events(tmp_path):
    f = sorted(tmp_path.glob("run_*.jsonl"))[0]
    return [json.loads(x) for x in
            f.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------ 开闸锚定平移
def test_effective_sale_shifts_by_anchor():
    """可信锚点整体平移;缺失/超上限/非正数退化钟点锚定。"""
    assert effective_sale_s(1.0, 0.35) == 0.65      # 平移
    assert effective_sale_s(1.0, None) == 1.0       # 无锚点:原样
    assert effective_sale_s(1.0, 0.0) == 1.0        # 0 锚(准点)不平移
    assert effective_sale_s(1.0, -0.2) == 1.2       # 负锚:开售早于整点
                                                   # (10-08 实测首个 43055
                                                   #  在 -0.073s)
    assert effective_sale_s(1.0, -0.4) == 1.0       # 越下界:不可信不锚
    assert effective_sale_s(1.0, 2.0) == 1.0        # 越上界:不可信不锚


def test_anchor_combo_pacing(monkeypatch):
    """锚定组合 phase_pacing:锚点前退探测密度,锚点后才进黄金窗。"""
    monkeypatch.setattr(rush_mod.settings, "RUSH_OPEN_ANCHOR_MAX_S", 1.5)
    # 无锚点:sale_s=1.0 在拥挤冲刺窗(锚点缺失=钟点计) -> 冲刺豁免
    d, e, st = phase_pacing(effective_sale_s(1.0, None))
    assert e is True and st is False
    # 锚点 0.35:钟点 1.0 即开闸后 0.65s -> 仍在冲刺窗
    d, e, st = phase_pacing(effective_sale_s(1.0, 0.35))
    assert e is True and st is False
    # 锚点 0.35:钟点 0.2 实为开闸前 -> 探测段低密度
    d, e, st = phase_pacing(effective_sale_s(0.2, 0.35))
    assert d > 5 and e is False
    # 锚定后收兵点整体后移(钟点 10.4 = 锚定 10.05),仍在 duration 兜底内
    assert phase_pacing(effective_sale_s(10.4, 0.35))[2] is True
    # 负锚(开售早于整点):钟点 -0.1 实为开闸后 0.1s -> 冲刺窗豁免
    d, e, st = phase_pacing(effective_sale_s(-0.1, -0.2))
    assert e is True and st is False


def test_volley_layout_20():
    """20 路三段梯队:哨兵1+一波7+二波7+余量5,门控边界自洽。"""
    st = rush_mod.settings
    n = int(st.RUSH_CONCURRENCY)
    v1, v2 = int(st.RUSH_VOLLEY_1), int(st.RUSH_VOLLEY_2)
    assert n == 20
    assert v1 == 8 and v2 == 7
    assert v1 + v2 < n          # 余量路存在(锁单回流扫尾)
    assert 0 < st.RUSH_VOLLEY2_FALLBACK < 1.0
    assert st.RUSH_TARGET_RPS <= n   # 贴线,不超路数


# ------------------------------------------------ 43055 开闸信号
def test_43055_fires_burst_and_anchor(tmp_path, monkeypatch):
    """43055(拥挤)=放行实证:拉响 burst 并设开闸锚点。"""
    errs = [BiliApiError("活动太火爆", code=43055)] * 500
    flow, _ = _flow(errs, tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       interval=0.01, duration=0.8)
    assert result is None
    evs = _events(tmp_path)
    bursts = [e for e in evs if e.get("event") == "burst_signal"]
    assert bursts and bursts[0]["code"] == 43055
    summary = [e for e in evs if e.get("event") == "run_summary"][0]
    assert summary["open_obs_s"] is not None
    assert summary["burst_fired"] is True


def test_702_neither_burst_nor_anchor(tmp_path, monkeypatch):
    """-702 只证明频控:不设锚点、不拉响 burst。"""
    errs = [BiliApiError("请求频率过高", code=-702)] * 500
    flow, _ = _flow(errs, tmp_path)
    result = flow.rush(target_ts=time.time() - 1, early_seconds=0,
                       interval=0.01, duration=0.8)
    assert result is None
    summary = [e for e in _events(tmp_path)
               if e.get("event") == "run_summary"][0]
    assert summary["open_obs_s"] is None
    assert summary["burst_fired"] is False


def test_69422_neither_burst_nor_anchor(tmp_path, monkeypatch):
    """69422(未开售)既不放行也不设锚:锚点必须等首个放行码。"""
    errs = [BiliApiError("暂时无法购买", code=69422)] * 500
    flow, _ = _flow(errs, tmp_path)
    flow.rush(target_ts=time.time() - 1, early_seconds=0,
              interval=0.01, duration=0.8)
    summary = [e for e in _events(tmp_path)
               if e.get("event") == "run_summary"][0]
    assert summary["open_obs_s"] is None
    assert summary["burst_fired"] is False


def test_anchor_is_earliest_observation(tmp_path, monkeypatch):
    """多路先后观测放行码:锚点取最早一次(首写即定,后写不覆盖)。"""
    errs = [BiliApiError("活动太火爆", code=43055)] * 500
    flow, _ = _flow(errs, tmp_path)
    flow.rush(target_ts=time.time() - 1, early_seconds=0,
              interval=0.01, duration=0.8)
    summary = [e for e in _events(tmp_path)
               if e.get("event") == "run_summary"][0]
    bursts = [e for e in _events(tmp_path)
              if e.get("event") == "burst_signal"]
    # 锚点=首个放行码观测,不得晚于任一 burst 观测时刻
    assert summary["open_obs_s"] <= min(b["sale_s"] for b in bursts) + 1e-6
