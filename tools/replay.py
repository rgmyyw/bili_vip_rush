# -*- coding: utf-8 -*-
"""复盘报告：读取 run_*.jsonl，输出调用序列 / 失败分类 / 关键响应原文。

解析逻辑在 core/logstats.py，与仪表盘(tools/dashboard.py)共用。

用法：
    python tools/replay.py                     # 复盘最近一次运行
    python tools/replay.py logs/run_xxx.jsonl  # 复盘指定文件
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.logstats import (          # noqa: E402
    failure_breakdown,
    list_runs,
    load_rows,
)

LOGS_DIR = ROOT / "logs"

# 复盘时重点关注的接口（抢购链路关键环节）
KEY_APIS = ("create/activity", "reserve", "attract_card",
            "buyComponentEvo_info")


def fmt_row(no: int, r: dict) -> str:
    t = (r.get("t") or "")[11:23]
    if r["type"] == "http":
        resp = r.get("response", {})
        status = resp.get("status_code")
        api_code = resp.get("api_code")
        err = r.get("error")
        brief = resp.get("message") or err or ""
        if not brief and resp.get("text"):
            try:
                brief = json.loads(resp["text"]).get("message", "")
            except Exception:
                brief = resp["text"][:40]
        codes = f"{status}/{api_code if api_code is not None else '-'}"
        return (f"  {no:>3}  {t}  [{r.get('phase',''):<9}] "
                f"{r.get('method',''):<4} {r.get('api','')[:34]:<34} "
                f"{r.get('elapsed_ms',0):>7}ms  {codes:<9} {brief[:50]}")
    return (f"  {no:>3}  {t}  [{r.get('phase',''):<9}] "
            f"EVENT {r.get('event','')}: "
            f"{json.dumps({k: v for k, v in r.items() if k not in ('type','t','phase','event','seq','run_id')}, ensure_ascii=False)[:100]}")


def report(path: Path):
    rows = load_rows(path)
    http_rows = [r for r in rows if r["type"] == "http"]
    meta = [r for r in rows if r["type"] == "meta"]
    events = [r for r in rows if r["type"] == "event"]

    print("=" * 100)
    print(f"复盘报告: {path.name}  共 {len(rows)} 行 "
          f"(HTTP {len(http_rows)} / 事件 {len(events)} / meta {len(meta)})")
    if meta:
        start = meta[0]
        print(f"模式: {start.get('mode')}  开始: {start.get('t')}")

    print("\n---------- 调用序列 ----------")
    for i, r in enumerate(rows, 1):
        print(fmt_row(i, r))

    failures = failure_breakdown(rows)
    if failures:
        print("\n---------- 失败分类（接口/错误码/信息 -> 次数） ----------")
        for api, code, msg, cnt in failures:
            print(f"  {api[:34]:<34} code={code:<6} ×{cnt:<3} {msg}")

    # 关键接口响应原文（迭代脚本时看这里：新字段/新风控提示都在原文里）
    key_rows = [r for r in http_rows
                if any(k in (r.get("api") or "") for k in KEY_APIS)]
    if key_rows:
        print("\n---------- 关键接口响应原文（去重，最多各3条） ----------")
        from collections import Counter
        seen: Counter = Counter()
        for r in key_rows:
            api = r.get("api", "")
            if seen[api] >= 3:
                continue
            seen[api] += 1
            resp = r.get("response", {})
            text = resp.get("text") or f"<无响应 error={r.get('error')}>"
            print(f"\n  [{r.get('t','')[11:23]}] {api} "
                  f"(http={resp.get('status_code')} code={resp.get('api_code')})")
            print(f"  请求: {json.dumps(r.get('request', {}).get('data') or r.get('request', {}).get('params') or {}, ensure_ascii=False)[:200]}")
            print(f"  响应: {text[:600]}")

    print("\n" + "=" * 100)
    return 0


def latest_run() -> Path | None:
    files = sorted(LOGS_DIR.glob("run_*.jsonl"))
    return files[-1] if files else None


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = latest_run()
        if path is None:
            print(f"{LOGS_DIR} 下没有 run_*.jsonl")
            return 1
        print(f"(未指定文件，复盘最近一次: {path.name})\n")
    return report(path)


if __name__ == "__main__":
    sys.exit(main())
