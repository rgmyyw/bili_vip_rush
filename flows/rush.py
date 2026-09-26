# -*- coding: utf-8 -*-
"""抢购主流程：预检 -> 校时 -> 等待开售 -> 轮询下单锁名额。

设计原则：
- 显式等待（基于服务器时钟偏移计算等待时间，不盲 sleep 固定值）；
- 每一次尝试的结果（成功/失败/原始响应）都追加到 logs/ 现场文件；
- 频率保守（默认 0.25s/次），成功立即停止。
"""
import json
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from config import settings
from core.classifier import (
    ALREADY_WORDS,
    SOLD_OUT_WORDS,
    Outcome,
    classify_error,
)
from core.client import (
    BiliApiError,
    BiliClient,
    DRAINAGE_ABOUT_TO_OPEN,
    DRAINAGE_ON_SALE,
    DRAINAGE_SOLD_OUT_TODAY,
)
from core.run_logger import RunLogger
from core.time_sync import measure_offset, seconds_until

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# 兼容旧引用（关键词常量已迁至 core.classifier）
__all__ = ["RushFlow", "next_interval", "SOLD_OUT_WORDS", "ALREADY_WORDS",
           "is_system_error"]


def is_system_error(ex) -> bool:
    return classify_error(ex) is Outcome.SYSTEM_ERROR


def next_interval(attempt: int, elapsed: float,
                  override: float | None = None) -> float:
    """三段自适应间隔：爆发 -> 快速 -> 慢速，叠加 ±JITTER 抖动。

    override 显式指定基准间隔（测试用），此时不再抖动。
    """
    if override is not None:
        return max(0.0, override)
    if attempt <= settings.RUSH_BURST_COUNT:
        base = settings.RUSH_BURST_INTERVAL
    elif elapsed <= settings.RUSH_FAST_WINDOW:
        base = settings.RUSH_FAST_INTERVAL
    else:
        base = settings.RUSH_SLOW_INTERVAL
    jitter = 1 + random.uniform(-settings.RUSH_JITTER, settings.RUSH_JITTER)
    return max(0.0, base * jitter)


class RushFlow:
    def __init__(self, client: BiliClient, plans: list | None = None,
                 run_logger: RunLogger | None = None,
                 log_dir: Path = LOGS_DIR):
        self.client = client
        self.plans = plans if plans is not None else settings.plan_payloads()
        self.logs = run_logger or RunLogger(log_dir, mode="rush")
        self.client.run_logger = self.logs

    # -------------------------------------------------------------- helpers
    def _set_phase(self, phase: str):
        self.client.phase = phase

    def _record(self, event: str, **fields):
        self.logs.log_event(event, **fields)

    # -------------------------------------------------------------- precheck
    def precheck(self) -> dict:
        """开售前自检，返回简要状态。"""
        self._set_phase("precheck")
        card = self.client.get_attract_card()
        self._record("attract_card", **card)
        status = card.get("drainage_status")
        reserved = bool(card.get("isReserved"))
        has_buy = bool(card.get("has_buy"))
        next_open = card.get("next_open_at")

        comp = self.client.get_buy_component()
        buy_sets = {b.get("token"): b for b in comp.get("buySets", [])}
        for plan in self.plans:
            token = plan["act_token"]
            st = buy_sets.get(token)
            plan["_eligible"] = None if st is None else not st.get("hasBuy")
            self._record("buy_set", token=token, state=st)

        logger.info(
            "状态=%s 已预约=%s 已购=%s 下次开售=%s",
            status, reserved, has_buy,
            datetime.fromtimestamp(next_open) if next_open else None)
        if not reserved:
            logger.warning("尚未预约！可先执行 --reserve")
        return card

    # ------------------------------------------------------------------ rush
    def rush(self, target_ts: float | None = None,
             early_seconds: float | None = None,
             interval: float | None = None,
             duration: float | None = None) -> dict | None:
        """等待开售并抢购。返回成功订单 dict，失败返回 None。

        target_ts: 开售 Unix 秒；None 则取 attract_card 的 next_open_at。
        """
        self._set_phase("calibrate")
        card = self.client.get_attract_card()
        if target_ts is None:
            target_ts = float(card["next_open_at"])
        offset = measure_offset(self.client.get_server_time,
                                samples=settings.TIME_SYNC_SAMPLES)
        logger.info("时钟偏移 %+0.3fs，目标开售 %s",
                    offset, datetime.fromtimestamp(target_ts))
        self._record("calibrated", offset_s=round(offset, 3),
                     target_ts=target_ts)

        early = settings.RUSH_EARLY_SECONDS if early_seconds is None else early_seconds
        total = settings.RUSH_DURATION_SECONDS if duration is None else duration

        # 显式等待：长距离每秒刷新倒计时；T-3s 预热连接（DNS+TLS 进池）；
        # 最后 0.2s 忙等消除系统定时器粒度（Windows sleep 粒度 ~15ms）
        wait_until = target_ts - early
        prewarmed = False
        while True:
            remain = seconds_until(wait_until, offset)
            if remain <= 0:
                break
            if remain > 3.5:
                m, s = divmod(int(remain), 60)
                h, m = divmod(m, 60)
                print(f"\r      距开售 {h:02d}:{m:02d}:{s:02d}   ",
                      end="", flush=True)
                time.sleep(min(remain - 3.5, 1.0))
                continue
            if not prewarmed and remain <= 3.0:
                prewarmed = True
                ok = self.client.prewarm()
                self._record("prewarm", ok=ok)
                logger.info("连接预热 %s", "成功" if ok else "失败(忽略)")
            if remain > 0.2:
                time.sleep(0.05)
            else:
                while seconds_until(wait_until, offset) > 0:
                    pass   # 忙等：短自旋换毫秒级触发精度
        print()   # 结束倒计时行，避免与后续日志粘行

        self._set_phase("rush")
        self._record("rush_start", target_ts=target_ts, offset=offset,
                     prewarmed=prewarmed)

        deadline = time.monotonic() + total
        rush_t0 = time.monotonic()
        sold_out_at = None
        attempt = 0
        syserr_streak = 0
        while time.monotonic() < deadline:
            attempt += 1
            if attempt > settings.RUSH_MAX_ATTEMPTS:
                self._record("stop_reason", reason="max_attempts",
                             attempts=attempt - 1)
                logger.warning("达到尝试上限 %s 次", settings.RUSH_MAX_ATTEMPTS)
                return None
            for plan in self.plans:
                if plan.get("_skip"):
                    continue
                try:
                    order = self.client.create_order(plan)
                    try:
                        pay_link = self.client.pay_link(plan)
                    except Exception:  # 日志辅助失败不影响主流程
                        pay_link = ""
                    # 双重校验：create 成功后向 order/status 确认订单状态
                    try:
                        order_status = self.client.get_order_status(order)
                    except Exception as ex:
                        order_status = {"check_error": str(ex)}
                    self._record("order_ok", attempt=attempt,
                                 plan=plan["name"], order=order,
                                 order_status=order_status,
                                 pay_link=pay_link)
                    logger.info("下单成功: %s -> %s (status=%s)",
                                plan["name"], order, order_status)
                    return order
                except BiliApiError as ex:
                    text = (ex.response_text or "")[:500]
                    self._record("order_fail", attempt=attempt,
                                 plan=plan["name"], code=ex.code,
                                 message=str(ex), raw=text)
                    outcome = classify_error(ex)
                    if outcome is Outcome.CREDENTIAL_EXPIRED:
                        self._record("stop_reason",
                                     reason="credential_expired",
                                     message=str(ex))
                        logger.error(
                            "凭证失效(%s)，重试无意义。请重新抓包更新 "
                            "config/settings.py 或环境变量 BILI_* 后再战。",
                            ex.code)
                        return None
                    if outcome is Outcome.ALREADY_DONE:
                        self._record("stop_reason", reason="already_owned",
                                     message=str(ex))
                        logger.info("已有订单/已购买，停止: %s", ex)
                        return None
                    if outcome is Outcome.SOLD_OUT:
                        if sold_out_at is None:
                            sold_out_at = time.monotonic()
                        elif (time.monotonic() - sold_out_at
                              > settings.SOLD_OUT_LINGER_SECONDS):
                            self._record("stop_reason", reason="sold_out",
                                         message=str(ex))
                            logger.info("已售罄，停止重试")
                            return None
                    elif outcome is Outcome.SYSTEM_ERROR:
                        syserr_streak += 1
                        if syserr_streak >= settings.RUSH_SYSERR_PAUSE:
                            self._record("syserr_cooldown",
                                         streak=syserr_streak)
                            logger.warning("连续 %s 次系统错误，冷却 %ss",
                                           syserr_streak,
                                           settings.RUSH_SYSERR_COOLDOWN)
                            time.sleep(settings.RUSH_SYSERR_COOLDOWN)
                            syserr_streak = 0
                    else:   # Outcome.RETRY：普通业务错误，继续
                        syserr_streak = 0
                except Exception as ex:  # 未预期异常：留现场后继续下一轮
                    self._record("unexpected_error", attempt=attempt,
                                 plan=plan["name"],
                                 error=repr(ex))
                    logger.exception("下单未预期异常")
            elapsed = time.monotonic() - rush_t0
            time.sleep(next_interval(attempt, elapsed, interval))
        self._record("rush_timeout", attempts=attempt)
        logger.warning("坚持 %ss 后仍未抢到", total)
        return None
