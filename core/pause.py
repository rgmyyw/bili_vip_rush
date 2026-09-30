# -*- coding: utf-8 -*-
"""服务暂停开关：config/pause.json 共享文件，仪表盘与抢购进程共用。

语义：paused=true 时 daemon 到点不起跑新一轮；正在倒计时的一轮
（rush 等待开售阶段）每秒检查，暂停即中止，不下单、不发失败邮件。
"""
import json
import time

from config import settings


class RushPausedError(RuntimeError):
    """一轮抢购因服务暂停而中止（非失败，不触发失败邮件）。"""


def _read() -> dict:
    try:
        d = json.loads(settings.PAUSE_JSON_PATH.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:   # 文件缺失/损坏视为未暂停，不阻塞抢购主链路
        return {}


def is_paused() -> bool:
    return bool(_read().get("paused"))


def pause_state() -> dict:
    d = _read()
    return {
        "paused": bool(d.get("paused")),
        "reason": str(d.get("reason") or ""),
        "ts": str(d.get("ts") or ""),
    }


def set_paused(paused: bool, reason: str = "") -> dict:
    data = {
        "paused": bool(paused),
        "reason": reason if paused else "",
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    settings.PAUSE_JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data
