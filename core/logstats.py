# -*- coding: utf-8 -*-
"""运行日志统计：供 tools/replay.py（复盘）与 tools/dashboard.py（仪表盘）
共用的解析层。
"""
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


def load_rows(path: Path) -> list:
    """读取一个 run_*.jsonl，返回行列表（坏行保留为 corrupt）。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"type": "corrupt", "raw": line})
    return rows


def failure_breakdown(rows: list) -> list:
    """失败分类：[(api, code, message, count)]，按次数降序。"""
    fails = []
    for r in rows:
        if r.get("type") != "http":
            continue
        resp = r.get("response", {})
        if r.get("error"):
            fails.append((r.get("api", ""), "NETWORK", r["error"][:60]))
        elif resp.get("api_code") not in (None, 0):
            fails.append((r.get("api", ""), resp["api_code"],
                          (resp.get("message") or "")[:60]))
    return [(api, code, msg, cnt)
            for (api, code, msg), cnt in Counter(fails).most_common()]


def summarize_run(path: Path) -> dict:
    """把一次运行汇总成一行记录（仪表盘/列表用）。"""
    rows = load_rows(path)
    events = [r for r in rows if r.get("type") == "event"]
    metas = [r for r in rows if r.get("type") == "meta"]

    mode = ""
    for m in metas:
        if m.get("event") == "run_start":
            mode = m.get("mode", "")
            break
    started = rows[0].get("t", "") if rows else ""
    ended = rows[-1].get("t", "") if rows else ""

    order = None
    pay_link = ""
    order_status = None
    stop_reason = ""
    stop_msg = ""
    http_count = sum(1 for r in rows if r.get("type") == "http")
    fail_count = sum(1 for r in events if r.get("event") == "order_fail")
    ok_count = sum(1 for r in events if r.get("event") == "order_ok")
    exit_code = None
    for e in events:
        if e.get("event") == "order_ok":
            order = e.get("order")
            pay_link = e.get("pay_link", "")
            order_status = e.get("order_status")
        elif e.get("event") == "stop_reason":
            stop_reason = e.get("reason", "")
            stop_msg = e.get("message", "")[:80]
    for m in metas:
        if m.get("event") == "run_end":
            exit_code = m.get("exit_code")

    if order:
        result = "success"
    elif stop_reason:
        result = stop_reason          # sold_out / credential_expired / already_owned / max_attempts
    elif any(m.get("event") == "run_crashed" for m in metas):
        result = "crashed"
    elif mode in ("rush", "daemon") and ok_count == 0:
        result = "timeout" if not stop_reason else stop_reason
    else:
        result = "done"

    return {
        "file": path.name,
        "mode": mode,
        "started": started,
        "ended": ended,
        "http_calls": http_count,
        "attempts": fail_count + ok_count,
        "result": result,
        "stop_message": stop_msg,
        "order": order,
        "order_status": order_status,
        "pay_link": pay_link,
        "exit_code": exit_code,
    }


def list_runs(logs_dir: Path) -> list:
    """列出全部运行记录，新->旧。"""
    logs_dir = Path(logs_dir)
    files = sorted(logs_dir.glob("run_*.jsonl"), reverse=True)
    return [summarize_run(p) for p in files]
