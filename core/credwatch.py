# -*- coding: utf-8 -*-
"""凭证巡检：常驻后台线程，每小时体检一次凭证有效性。

推送策略（防轰炸）：
  - 有效 -> 失效（状态翻转）：立即邮件+钉钉告警；
  - 持续失效：每 24h 重发一次提醒；
  - 失效 -> 恢复：推送恢复通知；
  - 无法判定（网络/风控层异常）：不计失效，短间隔复核；
  - 判失效前先复核一次，防单次抖动误报。
巡检不受抢购暂停开关影响（暂停的是下单，不是监控）。
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)


def check_once() -> bool | None:
    """立即体检一次：True=有效 / False=失效 / None=无法判定。

    先重载凭证（仪表盘上传 HAR 后无需重启即用新值）再探测。
    """
    from core.auth import reload_credentials_into_settings
    from core.client import BiliClient
    from core.run_logger import NullRunLogger
    reload_credentials_into_settings()
    client = BiliClient(run_logger=NullRunLogger(), timeout=8)
    return client.check_login()


def _alert_credential_invalid() -> dict:
    from core.notify import notify_all, reload_notify_into_settings
    reload_notify_into_settings()   # 网页改 SMTP/钉钉后告警立即用新值
    return notify_all(
        "[B站抢购] 凭证已失效",
        "凭证体检失效（每小时巡检发现）。\n"
        "请重新抓包并在仪表盘点「上传HAR」更新，更新后自动恢复。\n"
        "在凭证恢复前，抢购轮次将在预检阶段停止。")


def _alert_credential_recovered() -> dict:
    from core.notify import notify_all, reload_notify_into_settings
    reload_notify_into_settings()
    return notify_all("[B站抢购] 凭证已恢复",
                      "凭证体检恢复有效（此前曾失效），抢购服务正常。")


class CredWatchState:
    """巡检状态机（纯逻辑，便于测试）：喂入每次体检结果与时间戳，
    返回需要执行的动作列表（"alert_invalid"/"alert_recovered"）。"""

    def __init__(self, realert_every_s: float = 86400.0):
        self.last_state = None        # None=未知 / True=有效 / False=失效
        self.last_alert_ts = 0.0
        self.realert_every_s = realert_every_s

    def step(self, ok: bool | None, now: float) -> list:
        if ok is None:
            return []   # 无法判定：状态不变，不告警
        actions = []
        if ok is False:
            if (self.last_state is not False
                    or now - self.last_alert_ts > self.realert_every_s):
                actions.append("alert_invalid")
                self.last_alert_ts = now
        elif self.last_state is False:
            actions.append("alert_recovered")
        self.last_state = ok
        return actions


def _watch_loop(interval_s: float, recheck_s: float):
    state = CredWatchState()
    while True:
        try:
            ok = check_once()
            if ok is None:
                time.sleep(recheck_s)
                continue
            for action in state.step(ok, time.time()):
                if action == "alert_invalid":
                    # 复核一次再告警，防单次网络抖动误报
                    time.sleep(recheck_s)
                    if check_once() is False:
                        sent = _alert_credential_invalid()
                        logger.warning("凭证失效告警已推送: %s", sent)
                elif action == "alert_recovered":
                    sent = _alert_credential_recovered()
                    logger.info("凭证恢复通知已推送: %s", sent)
        except Exception:
            logger.exception("凭证巡检异常(继续)")
        time.sleep(interval_s)


def start_cred_watch(interval_s: float = 3600.0,
                     recheck_s: float = 300.0) -> threading.Thread:
    """启动巡检线程（daemon）。interval_s=巡检间隔，recheck_s=复核间隔。"""
    t = threading.Thread(target=_watch_loop,
                         args=(interval_s, recheck_s),
                         daemon=True, name="credwatch")
    t.start()
    logger.info("凭证巡检已启动: 每 %s 分钟一次", interval_s / 60)
    return t
