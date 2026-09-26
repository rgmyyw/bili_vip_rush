# -*- coding: utf-8 -*-
"""logstats 汇总解析测试：构造小型 jsonl 验证汇总/失败分类。"""
import json
from pathlib import Path

from core.logstats import failure_breakdown, list_runs, summarize_run


def write_run(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    return p


def test_summarize_success_run(tmp_path):
    p = write_run(tmp_path, "run_20260927_120000_rush.jsonl", [
        {"type": "meta", "event": "run_start", "mode": "rush", "t": "2026-09-27T12:00:00.000"},
        {"type": "http", "api": "x/vip/order/create/activity", "phase": "rush",
         "response": {"api_code": 1, "message": "活动未开始"}, "t": "2026-09-27T12:00:00.100"},
        {"type": "event", "event": "order_fail", "plan": "p1",
         "t": "2026-09-27T12:00:00.101"},
        {"type": "http", "api": "x/vip/order/create/activity", "phase": "rush",
         "response": {"api_code": 0, "message": "OK"}, "t": "2026-09-27T12:00:00.400"},
        {"type": "event", "event": "order_ok", "attempt": 2,
         "order": {"order_no": "OK9"}, "order_status": {"status": 1},
         "pay_link": "https://pay.example/x",
         "t": "2026-09-27T12:00:00.401"},
        {"type": "meta", "event": "run_end", "exit_code": 0,
         "t": "2026-09-27T12:00:00.500"},
    ])
    s = summarize_run(p)
    assert s["mode"] == "rush"
    assert s["result"] == "success"
    assert s["attempts"] == 2          # 1 fail + 1 ok
    assert s["http_calls"] == 2
    assert s["order"] == {"order_no": "OK9"}
    assert s["pay_link"].startswith("https://pay.example")
    assert s["exit_code"] == 0
    assert s["started"].startswith("2026-09-27T12:00:00")


def test_summarize_stops(tmp_path):
    sold = write_run(tmp_path, "run_a_sold.jsonl", [
        {"type": "meta", "event": "run_start", "mode": "rush", "t": "t0"},
        {"type": "event", "event": "order_fail", "t": "t1"},
        {"type": "event", "event": "stop_reason", "reason": "sold_out",
         "message": "今日已售罄", "t": "t2"},
        {"type": "meta", "event": "run_end", "exit_code": 1, "t": "t3"},
    ])
    cred = write_run(tmp_path, "run_b_cred.jsonl", [
        {"type": "meta", "event": "run_start", "mode": "rush", "t": "t0"},
        {"type": "event", "event": "order_fail", "t": "t1"},
        {"type": "event", "event": "stop_reason", "reason": "credential_expired",
         "t": "t2"},
    ])
    assert summarize_run(sold)["result"] == "sold_out"
    assert summarize_run(sold)["stop_message"] == "今日已售罄"
    assert summarize_run(cred)["result"] == "credential_expired"


def test_failure_breakdown(tmp_path):
    p = write_run(tmp_path, "run_c.jsonl", [
        {"type": "http", "api": "create/activity",
         "response": {"api_code": 1, "message": "活动未开始"}},
        {"type": "http", "api": "create/activity",
         "response": {"api_code": 1, "message": "活动未开始"}},
        {"type": "http", "api": "create/activity", "error": "timeout"},
        {"type": "http", "api": "attract_card",
         "response": {"api_code": 0, "message": "OK"}},
    ])
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    fb = failure_breakdown(rows)
    assert fb[0] == ("create/activity", 1, "活动未开始", 2)
    assert any(f[1] == "NETWORK" for f in fb)
    assert not any(f[1] == 0 for f in fb)     # code=0 不算失败


def test_list_runs_newest_first(tmp_path):
    write_run(tmp_path, "run_1.jsonl",
              [{"type": "meta", "event": "run_start", "mode": "check", "t": "a"}])
    write_run(tmp_path, "run_2.jsonl",
              [{"type": "meta", "event": "run_start", "mode": "rush", "t": "b"}])
    runs = list_runs(tmp_path)
    assert [r["file"] for r in runs] == ["run_2.jsonl", "run_1.jsonl"]
