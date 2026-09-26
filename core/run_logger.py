# -*- coding: utf-8 -*-
"""运行日志系统：HTTP 审计 + 流程事件，一次运行一个 JSONL 文件。

设计目标（服务于"当天实战 -> 复盘日志 -> 迭代脚本 -> 次日再战"）：
1. 覆盖所有接口调用：请求参数、响应原文、耗时、异常，全部落盘——
   抢购是链式流程（预约/资格 -> 下单 -> 订单），任何一环的新字段、
   新报错都要能从日志里直接看到；
2. 敏感凭证脱敏（access_key/SESSDATA/csrf/sign 只留首尾）；
3. 每行带 run_id / phase / 序号，可按调用链回放；
4. --replay 直接产出可读报告（tools/replay.py 消费本文件）。
"""
import json
import threading
import time
from datetime import datetime
from pathlib import Path

# 参与脱敏的键（大小写不敏感）；Cookie 头整体脱敏
SENSITIVE_KEYS = {
    "access_key", "sessdata", "bili_jct", "csrf", "csrf_token",
    "sign", "appsec", "dedeuserid",
}
KEEP_HEAD, KEEP_TAIL = 6, 4


def mask_value(value) -> str:
    """凭证值脱敏：保留首 KEEP_HEAD 尾 KEEP_TAIL。"""
    s = str(value)
    if len(s) <= KEEP_HEAD + KEEP_TAIL:
        return "***"
    return f"{s[:KEEP_HEAD]}...{s[-KEEP_TAIL:]}"


def mask_mapping(params: dict | None) -> dict:
    """对参数字典做深脱敏（仅顶层键名匹配即脱敏值）。"""
    if not params:
        return {}
    out = {}
    for k, v in params.items():
        if str(k).lower() in SENSITIVE_KEYS:
            out[k] = mask_value(v)
        else:
            out[k] = v
    return out


class NullRunLogger:
    """空实现：测试/不想落盘时使用，接口与 RunLogger 一致。"""

    run_id = ""
    path = None
    phase = ""

    def log_http(self, **kw): ...
    def log_event(self, event, **fields): ...
    def log_meta(self, **fields): ...


class RunLogger:
    """一次运行（check/reserve/rush）写一个 logs/run_<ts>_<mode>.jsonl。"""

    def __init__(self, log_dir: Path | str, mode: str = "run"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{mode}"
        self.path = self.log_dir / f"run_{self.run_id}.jsonl"
        self.phase = mode          # 当前阶段标签，client 每条日志带上
        self._seq = 0
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.log_meta(event="run_start", mode=mode)

    # ------------------------------------------------------------------ core
    def _write(self, row: dict):
        row["seq"] = self._next_seq()
        row["run_id"] = self.run_id
        row["t"] = datetime.now().isoformat(timespec="milliseconds")
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    # ------------------------------------------------------------------ api
    def log_meta(self, event: str, **fields):
        self._write({"type": "meta", "event": event,
                     "elapsed_s": round(time.monotonic() - self._t0, 3), **fields})

    def log_event(self, event: str, **fields):
        self._write({"type": "event", "phase": self.phase, "event": event, **fields})

    def log_http(self, *, phase: str | None = None, method: str = "", url: str = "",
                 params: dict | None = None, data: dict | None = None,
                 json_body: dict | None = None, status_code: int | None = None,
                 api_code: int | None = None, message: str = "",
                 response_text: str = "", elapsed_ms: float = 0.0,
                 error: str = ""):
        """记录一次 HTTP 调用。成功失败都记，response_text 保留原文。"""
        # 只取 path 部分做短名，便于报告展示
        short = url.split("//", 1)[-1].split("/", 1)[-1] if url else ""
        self._write({
            "type": "http", "phase": phase or self.phase,
            "method": method, "url": url, "api": short,
            "request": {
                "params": mask_mapping(params),
                "data": mask_mapping(data),
                "json": json_body,
            },
            "response": {
                "status_code": status_code, "api_code": api_code,
                "message": message, "text": response_text,
            },
            "elapsed_ms": round(elapsed_ms, 1),
            "error": error,
        })
