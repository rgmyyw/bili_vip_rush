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
import threading
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
from core.pause import RushPausedError, is_paused
from core.run_logger import RunLogger
from core.time_sync import measure_offset, seconds_until

logger = logging.getLogger(__name__)


class PlanValidationError(RuntimeError):
    """待抢套餐不在下单白名单(自检拦截,已单独告警,勿再按 crashed 通知)。"""

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# 兼容旧引用（关键词常量已迁至 core.classifier）
__all__ = ["RushFlow", "next_interval", "SOLD_OUT_WORDS", "ALREADY_WORDS",
           "is_system_error"]


def is_system_error(ex) -> bool:
    return classify_error(ex) is Outcome.SYSTEM_ERROR


def phase_pacing(sale_s: float) -> tuple:
    """按距开售秒数返回 (间隔倍率, 节流豁免, 是否收兵)。

    黄金窗(开售前0.5s~后0.8s)豁免节流全速夺名额;尾段(后2s起)降密度
    扫尾;开售后 10s 主动收兵——66 名额早尽,继续打只烧账号频控额度。
    """
    try:
        if sale_s >= settings.RUSH_TAIL_STOP_S:
            return (0.0, False, True)
        if settings.RUSH_PEAK_FROM <= sale_s <= settings.RUSH_PEAK_TO:
            return (1.0, True, False)     # 黄金窗:全速豁免
        if sale_s >= settings.RUSH_TAIL_FROM:
            return (settings.RUSH_TAIL_DENSITY, False, False)
        return (1.0, False, False)        # 提前窗/回落段:常规+自适应节流
    except Exception:   # 配置缺失/类型异常:退化为常规节奏,绝不因分段崩
        logging.getLogger(__name__).exception(
            "phase_pacing 分段判定异常,退化常规节奏 sale_s=%s", sale_s)
        return (1.0, False, False)


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
        self.last_result: dict = {}   # rush() 结束后的结果摘要（通知用）
        self._result_lock = threading.Lock()   # 并发 worker 的终态写入锁

    # -------------------------------------------------------------- helpers
    def validate_plans(self) -> list:
        """起跑前一致性自检：所有待抢套餐必须在下单白名单内。

        返回违规 panel_type 列表（空=通过）。防配置漂移/上游同步带回
        备选导致买错——与抢购循环内的逐发硬闸构成双保险。
        """
        bad = [p.get("panel_type") for p in self.plans
               if p.get("panel_type") not in settings.ALLOWED_PANEL_TYPES]
        return [b for b in bad if b is not None]
    def _set_phase(self, phase: str):
        self.client.phase = phase

    def _record(self, event: str, **fields):
        self.logs.log_event(event, **fields)

    # -------------------------------------------------------------- precheck
    def precheck(self) -> dict:
        """开售前自检，返回简要状态。"""
        self._set_phase("precheck")
        bad = self.validate_plans()
        if bad:
            from core.notify import notify_all
            self._record("plan_whitelist_violation", panels=bad,
                         allowed=sorted(settings.ALLOWED_PANEL_TYPES))
            notify_all("[B站抢购] 配置异常已拦截",
                       f"检测到白名单外套餐 {bad}，本轮与后续抢购已中止。\n"
                       "请检查 TARGET_PLANS/ALLOWED_PANEL_TYPES 后恢复。")
            raise PlanValidationError(
                f"套餐白名单校验失败: {bad}，拒绝起跑")

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
            # 已购则本轮跳过下单（宁可不动，绝不多买）
            plan["_skip"] = bool(st and st.get("hasBuy"))
            self._record("buy_set", token=token, state=st,
                         skip=plan["_skip"])

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
        # 校时:B 站接口为准(目标时钟),NTP 毫秒级交叉验证——
        # B 站 current_time 是秒级整数,采样可能被网络抖动污染;
        # 与 NTP 偏差过大时重采样一次,取更接近 NTP 的一组
        from core.time_sync import measure_ntp_offset
        ntp_off = measure_ntp_offset()
        offset = measure_offset(self.client.get_server_time,
                                samples=settings.TIME_SYNC_SAMPLES)
        if ntp_off is not None and abs(offset - ntp_off) > 0.5:
            logger.warning("B站校时 %+0.3fs 与 NTP %+0.3fs 偏差大,重采样",
                           offset, ntp_off)
            offset2 = measure_offset(self.client.get_server_time,
                                     samples=settings.TIME_SYNC_SAMPLES)
            offset = (offset2 if abs(offset2 - ntp_off) < abs(offset - ntp_off)
                      else offset)
        logger.info("时钟偏移 %+0.3fs(NTP %+0.4fs)，目标开售 %s",
                    offset, ntp_off if ntp_off is not None else float("nan"),
                    datetime.fromtimestamp(target_ts))
        self._record("calibrated", offset_s=round(offset, 3),
                     ntp_offset_s=(round(ntp_off, 4)
                                   if ntp_off is not None else None),
                     target_ts=target_ts)

        early = settings.RUSH_EARLY_SECONDS if early_seconds is None else early_seconds
        total = settings.RUSH_DURATION_SECONDS if duration is None else duration
        n_workers = max(1, int(settings.RUSH_CONCURRENCY))
        self._worker_clients = []

        # 显式等待：长距离每秒刷新倒计时；T-3s 预热连接（DNS+TLS 进池）；
        # 最后 0.2s 忙等消除系统定时器粒度（Windows sleep 粒度 ~15ms）
        wait_until = target_ts - early
        prewarmed = False
        while True:
            remain = seconds_until(wait_until, offset)
            if remain <= 0:
                break
            if remain > 3.5:
                # 倒计时期间每秒检查暂停：仪表盘点暂停后本轮立即中止
                if is_paused():
                    raise RushPausedError("倒计时等待期间服务被暂停")
                m, s = divmod(int(remain), 60)
                h, m = divmod(m, 60)
                print(f"\r      距开售 {h:02d}:{m:02d}:{s:02d}   ",
                      end="", flush=True)
                time.sleep(min(remain - 3.5, 1.0))
                continue
            if not prewarmed and remain <= 3.0:
                prewarmed = True
                # 每路独立 client 独立连接池，逐路预热（此时距开售>2s，
                # 预热耗时充裕）；耗时最慢的一路作为网络往返实测
                # 多路时串行预热来不及(T-3s 内),线程池并发预热
                # 多路本身即多连接,每路 1 条;单路才按配置多条
                from concurrent.futures import ThreadPoolExecutor
                self._worker_clients = [self.client]
                for i in range(n_workers - 1):
                    try:
                        self._worker_clients.append(self._spawn_client())
                    except Exception:   # 单路创建失败:跳过,少一路不废轮
                        logger.exception("预热期 worker-%s 创建失败,跳过",
                                         i + 1)
                per = (max(1, settings.PREWARM_CONNECTIONS)
                       if n_workers <= 3 else 1)
                try:
                    pool = ThreadPoolExecutor(
                        max_workers=min(n_workers,
                                        settings.RUSH_PREWARM_PARALLEL))
                    try:
                        times = list(pool.map(
                            lambda c: c.prewarm(connections=per),
                            self._worker_clients))
                    finally:
                        pool.shutdown(wait=True)
                    vals = [t for t in times if t is not None]
                    worst = max(vals) if vals else None
                except Exception:   # 线程池整体失败:降级为不预热裸打
                    logger.exception("并发预热整体失败,降级裸连(不影响开抢)")
                    worst = None
                self._record(
                    "prewarm", workers=n_workers,
                    ok=bool(worst is not None),
                    worst_ms=round(worst * 1000) if worst else None)
                logger.info("连接预热 %s（%s 路）",
                            f"实测 {worst * 1000:.0f}ms" if worst else "失败(忽略)",
                            n_workers)
                # 自适应第一发提前量：按预热实测往返收紧/放宽，仅未显式
                # 指定 early_seconds 时生效；重算等待点后重新进循环
                if (settings.RUSH_EARLY_ADAPTIVE and worst is not None
                        and early_seconds is None):
                    early = min(2.2, max(1.8, worst * 2 + 0.15))
                    wait_until = target_ts - early
                    self._record("early_adjusted", early_s=round(early, 3),
                                 measured_rtt_ms=round(worst * 1000))
                    logger.info("自适应提前量 %.2fs", early)
                continue
            if remain > 0.2:
                time.sleep(0.05)
            else:
                while seconds_until(wait_until, offset) > 0:
                    pass   # 忙等：短自旋换毫秒级触发精度
        print()   # 结束倒计时行，避免与后续日志粘行

        # 开抢前最后一道闸：T-3s 内暂停同样中止，宁可错过不误买
        if is_paused():
            raise RushPausedError("开抢前服务处于暂停状态")

        self._set_phase("rush")
        self._lead_s = early    # 实际提前量:循环 elapsed 换算距开售秒数
        self._record("rush_start", target_ts=target_ts, offset=offset,
                     prewarmed=prewarmed, workers=n_workers, lead_s=early)

        counter = {"lock": threading.Lock(), "n": 0, "timed_out": False,
                   "risk": 0, "crashed": 0, "win_total": 0, "win_702": 0, "win_rate": 0.0}
        stop_event = threading.Event()
        self._result_lock = threading.Lock()
        deadline = time.monotonic() + total
        rush_t0 = time.monotonic()

        # worker 集合:优先用等待期预热好的;等待循环未走过(如 --now
        # 立即开抢/目标已过)则现场生成,保证并发路数不因路径退化成单路
        clients = list(self._worker_clients or [])
        if not clients:
            clients = [self.client]
            for i in range(n_workers - 1):
                try:
                    clients.append(self._spawn_client())
                except Exception:   # 单路创建失败:跳过该路,不废整轮
                    logger.exception("worker-%s client 创建失败,跳过", i + 1)
        if len(clients) <= 1:
            return self._rush_attempts(deadline, total, rush_t0, stop_event,
                                       counter, self.client, 0, interval)

        orders: list = []
        order_lock = threading.Lock()

        def _worker(i: int, client):
            try:
                # 阶梯相位:三梯队错峰(0/0.15/0.35s)+梯队内均摊爆发节奏
                # 66 名额场景:开售瞬间拥挤失败后,第二三波补枪命中率高
                try:
                    waves = tuple(settings.RUSH_WAVE_DELAYS or (0.0,))
                except Exception:
                    waves = (0.0,)
                if i:
                    wave = waves[min(i % len(waves), len(waves) - 1)]
                    intra = (i // len(waves)) * settings.RUSH_BURST_INTERVAL \
                        / max(1, len(clients) // len(waves))
                    delay = wave + intra
                    if delay > 0:
                        time.sleep(delay)
                o = self._rush_attempts(deadline, total, rush_t0, stop_event,
                                         counter, client, i, interval)
                if o is not None:
                    with order_lock:
                        if not orders:
                            orders.append(o)
                    stop_event.set()
            except Exception as ex:   # 兜底：单路崩溃不拖垮其余路
                with counter["lock"]:
                    counter["crashed"] += 1
                    crashed = counter["crashed"]
                self._record("worker_crashed", worker=i, error=repr(ex),
                             crashed=crashed, total=len(clients))
                logger.exception("worker-%s 未预期异常(%s/%s 崩溃)",
                                 i, crashed, len(clients))
                if crashed >= len(clients):   # 全部路崩溃才放弃本轮
                    stop_event.set()

        threads = [threading.Thread(target=_worker, args=(i, c), daemon=True)
                   for i, c in enumerate(clients)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return orders[0] if orders else None

    def _spawn_client(self) -> BiliClient:
        """为并发 worker 克隆 client：独立连接池，共享日志审计。"""
        c = BiliClient(run_logger=self.client.run_logger,
                       timeout=getattr(self.client, "timeout", 10.0))
        c.phase = self.client.phase
        return c

    def _set_result(self, result: dict, force: bool = False):
        """并发安全的终态写入：成功(force)覆盖一切；非成功仅首次写入。"""
        lock = getattr(self, "_result_lock", None)
        if lock is None:   # 测试用 __new__ 绕过 __init__ 的场景
            self._result_lock = lock = threading.Lock()
        with lock:
            if force or not getattr(self, "last_result", None):
                self.last_result = result

    def _rush_attempts(self, deadline, total, rush_t0, stop_event, counter,
                       client, worker_id, interval=None) -> dict | None:
        """单路尝试循环（串行模式同样走这里）。多路共享尝试计数/停抢
        标志/终态：任一路成功或命中终态（已购/凭证失效/售罄超时/达上限）
        即全局停，绝不连环下单。"""
        sold_out_at = None
        syserr_streak = 0
        while not stop_event.is_set() and time.monotonic() < deadline:
            with counter["lock"]:
                counter["n"] += 1
                attempt = counter["n"]
                if attempt > settings.RUSH_MAX_ATTEMPTS:
                    self._record("stop_reason", reason="max_attempts",
                                 attempts=attempt - 1)
                    logger.warning("达到尝试上限 %s 次",
                                   settings.RUSH_MAX_ATTEMPTS)
                    self._set_result({"result": "max_attempts",
                                      "attempts": attempt - 1})
                    stop_event.set()
                    return None
            for plan in self.plans:
                if plan.get("_skip"):
                    continue
                # 下单硬闸：panel_type 不在白名单的套餐绝不购买（防止配置
                # 误改/带回备选导致买错套餐——09-27 备选落单事故的根治）
                if plan.get("panel_type") not in settings.ALLOWED_PANEL_TYPES:
                    if not plan.get("_blocked_logged"):
                        plan["_blocked_logged"] = True
                        self._record("panel_blocked", plan=plan["name"],
                                     panel_type=plan.get("panel_type"),
                                     allowed=sorted(
                                         settings.ALLOWED_PANEL_TYPES))
                        logger.error(
                            "套餐 %s(panel_type=%s)不在下单白名单，已拒买",
                            plan["name"], plan.get("panel_type"))
                    continue
                try:
                    order = client.create_order(plan)
                    try:
                        pay_link = client.pay_link(plan)
                    except Exception:  # 日志辅助失败不影响主流程
                        pay_link = ""
                    # 双重校验：create 成功后向 order/status 确认订单状态
                    try:
                        order_status = client.get_order_status(order)
                    except Exception as ex:
                        order_status = {"check_error": str(ex)}
                    self._record("order_ok", attempt=attempt,
                                 worker=worker_id,
                                 plan=plan["name"], order=order,
                                 order_status=order_status,
                                 pay_link=pay_link)
                    logger.info("下单成功(worker-%s): %s -> %s (status=%s)",
                                worker_id, plan["name"], order, order_status)
                    self._set_result({"result": "success", "order": order,
                                      "pay_link": pay_link,
                                      "attempts": attempt}, force=True)
                    stop_event.set()
                    return order
                except BiliApiError as ex:
                    text = (ex.response_text or "")[:500]
                    self._record("order_fail", attempt=attempt,
                                 worker=worker_id,
                                 plan=plan["name"], code=ex.code,
                                 message=str(ex), raw=text)
                    # 风控熔断:412/403 达阈值即全局停(多路并发下防持续
                    # 轰炸被拉黑——宁可停手保账号)
                    if ex.code in (412, 403):
                        with counter["lock"]:
                            counter["risk"] += 1
                            risk = counter["risk"]
                        if risk >= settings.RUSH_RISK_PAUSE:
                            self._record("stop_reason", reason="risk_control",
                                         code=ex.code, hits=risk)
                            logger.error("疑似触发风控(412/403 x%s),全局停抢", risk)
                            self._set_result({"result": "risk_control",
                                              "attempts": attempt})
                            stop_event.set()
                            return None
                    # -702 频控窗口统计(自适应节流依据)
                    with counter["lock"]:
                        counter["win_total"] += 1
                        if ex.code == -702:
                            counter["win_702"] += 1
                    outcome = classify_error(ex)
                    if outcome is Outcome.CREDENTIAL_EXPIRED:
                        self._record("stop_reason",
                                     reason="credential_expired",
                                     message=str(ex))
                        logger.error(
                            "凭证失效(%s)，重试无意义。请重新抓包更新 "
                            "config/secrets.py 或环境变量 BILI_* 后再战。",
                            ex.code)
                        self._set_result({"result": "credential_expired",
                                          "attempts": attempt})
                        stop_event.set()
                        return None
                    if outcome is Outcome.ALREADY_DONE:
                        self._record("stop_reason", reason="already_owned",
                                     message=str(ex))
                        logger.info("已有订单/已购买，停止: %s", ex)
                        self._set_result({"result": "already_owned",
                                          "attempts": attempt})
                        stop_event.set()
                        return None
                    if outcome is Outcome.SOLD_OUT:
                        if sold_out_at is None:
                            sold_out_at = time.monotonic()
                        elif (time.monotonic() - sold_out_at
                              > settings.SOLD_OUT_LINGER_SECONDS):
                            self._record("stop_reason", reason="sold_out",
                                         message=str(ex))
                            logger.info("已售罄，停止重试")
                            self._set_result({"result": "sold_out",
                                              "attempts": attempt})
                            stop_event.set()
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
                                 worker=worker_id,
                                 plan=plan["name"],
                                 error=repr(ex))
                    logger.exception("下单未预期异常")
            elapsed = time.monotonic() - rush_t0
            # 时间分段火力 + -702 自适应节流(任何计算异常退化为保守
            # 1s 间隔继续打,本路绝不因节奏计算退出)
            try:
                sale_s = elapsed - getattr(self, "_lead_s", 0.0)  # 距开售
                dens, exempt, stop_now = phase_pacing(sale_s)
            except Exception:
                logger.exception("节奏计算异常,保守节奏继续")
                self._record("pacing_fallback", worker=worker_id)
                dens, exempt, stop_now = 1.0, False, False
            if stop_now:
                with counter["lock"]:
                    first = not counter["timed_out"]
                    counter["timed_out"] = True
                if first:
                    self._record("tail_stop", sale_s=round(sale_s, 2),
                                 attempts=counter["n"])
                    logger.info("尾段收兵(开售后 %ss)", sale_s)
                    self._set_result({"result": "timeout",
                                      "attempts": counter["n"]})
                stop_event.set()
                return None
            with counter["lock"]:
                if counter["win_total"] >= settings.RUSH_THROTTLE_WINDOW:
                    counter["win_rate"] = (counter["win_702"]
                                           / counter["win_total"])
                    counter["win_total"] = counter["win_702"] = 0
                rate = counter.get("win_rate", 0.0)
            extra = 0.0 if exempt else settings.RUSH_THROTTLE_MAX_S * rate
            time.sleep(next_interval(attempt, elapsed, interval) * dens
                       + extra)
        with counter["lock"]:
            first_timeout = not counter["timed_out"]
            counter["timed_out"] = True
        if first_timeout and not stop_event.is_set():
            self._record("rush_timeout", attempts=counter["n"])
            logger.warning("坚持 %ss 后仍未抢到", total)
            self._set_result({"result": "timeout", "attempts": counter["n"]})
        return None
