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
        if sale_s >= getattr(settings, "RUSH_CROWD_STOP_S",
                              settings.RUSH_TAIL_STOP_S):
            # 冲刺后直接收兵:拥挤窗结束=名额分尽,继续打只吃 -702
            return (0.0, False, True)
        if 0 <= sale_s < getattr(settings, "RUSH_CROWD_WINDOW", 2.0):
            # 拥挤冲刺(锚点起):每路 CROWD_INTERVAL,豁免节流——2.4s
            # 拥挤窗是持续竞争期,密度即穿越期望(10-06 仅 21 发全 43055)
            dens = (getattr(settings, "RUSH_CROWD_INTERVAL", 0.8)
                    / settings.RUSH_BURST_INTERVAL)
            return (dens, True, False)
        if sale_s >= settings.RUSH_TAIL_FROM:
            # 尾段目标 ~20 发/秒,dens 按路数换算(固定 12 在 60 路下=125/秒超频)
            dens = (max(1, int(settings.RUSH_CONCURRENCY))
                    / (20.0 * settings.RUSH_BURST_INTERVAL))
            return (dens, False, False)
        if sale_s < 0:
            # 探测段(开售前):低频探测密度(300ms 级),配合 worker 隔离
            # ——仅 worker-0 发请求,频控信用留给开闸瞬间
            dens = (getattr(settings, "RUSH_PROBE_INTERVAL", 0.3)
                    / settings.RUSH_BURST_INTERVAL)
            return (dens, False, False)
        # 回落段:保持总速贴线(dens 与黄金窗同换算)——60 路 40ms 的
        # 全密度会在该段理论打出 600+ 发/秒,首轮即冲爆频控额度
        dens = (max(1, int(settings.RUSH_CONCURRENCY))
                / (getattr(settings, "RUSH_TARGET_RPS", 55.0)
                   * settings.RUSH_BURST_INTERVAL))
        return (dens, False, False)        # 回落段:匀速贴线+自适应节流
    except Exception:   # 配置缺失/类型异常:退化为常规节奏,绝不因分段崩
        logging.getLogger(__name__).exception(
            "phase_pacing 分段判定异常,退化常规节奏 sale_s=%s", sale_s)
        return (1.0, False, False)


def effective_sale_s(sale_s: float, open_obs_s) -> float:
    """开闸锚定:把钟点坐标系的 sale_s 平移到开闸观测锚点坐标系。

    钟点锚定假设 12:00:00.000 准点开闸,实战连续两天实测开闸在
    +0.2s/+0.35s 漂移——黄金窗押在整点上,真开闸时窗口已过大半。
    首个放行码(43055/非常规码)的观测时刻即锚点:开闸前的发退回
    探测段密度,开闸后的发才进黄金窗。锚点缺失/-702 不锚(频控不证
    明放行)/晚于 RUSH_OPEN_ANCHOR_MAX_S(不可信)时退化钟点锚定。
    """
    try:
        cap = getattr(settings, "RUSH_OPEN_ANCHOR_MAX_S", 1.5)
        if open_obs_s is not None and 0 < open_obs_s <= cap:
            return sale_s - open_obs_s
    except Exception:
        pass
    return sale_s


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
        # 校时(三级):整秒跳变检测(±50ms) > 中位数法(±500ms 量化误差),
        # NTP 毫秒级交叉验证防污染
        from core.time_sync import measure_ntp_offset, measure_offset_precise
        ntp_off = measure_ntp_offset()
        offset = None
        try:
            offset = measure_offset_precise(self.client.get_server_time)
            if offset is not None:
                self._record("calibrate_precise", offset_s=round(offset, 4))
                logger.info("精确校时(跳变检测) %+0.4fs", offset)
        except Exception:
            logger.exception("精确校时失败,退回中位数法")
        if offset is None:
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
                    vals = sorted(t for t in times if t is not None)
                    worst = vals[-1] if vals else None
                except Exception:   # 线程池整体失败:降级为不预热裸打
                    logger.exception("并发预热整体失败,降级裸连(不影响开抢)")
                    worst = None
                    vals = []
                self._record(
                    "prewarm", workers=n_workers,
                    ok=bool(worst is not None),
                    worst_ms=round(worst * 1000) if worst else None,
                    # 分路预热耗时分布:复盘可定位慢连接/坏路
                    rtt_min_ms=round(vals[0] * 1000) if vals else None,
                    rtt_p50_ms=round(vals[len(vals) // 2] * 1000)
                    if vals else None,
                    rtt_all_ms=[round(v * 1000) for v in vals])
                logger.info("连接预热 %s（%s 路）",
                            f"实测 {worst * 1000:.0f}ms" if worst else "失败(忽略)",
                            n_workers)
                # 自适应第一发提前量：按预热实测往返收紧/放宽，仅未显式
                # 指定 early_seconds 时生效；重算等待点后重新进循环
                if (settings.RUSH_EARLY_ADAPTIVE and worst is not None
                        and early_seconds is None):
                    early = min(0.15, max(0.08, worst * 2 + 0.15))
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
                   "risk": 0, "crashed": 0, "win_total": 0, "win_702": 0,
                   "win_rate": 0.0, "open_obs_s": None,
                   "burst": threading.Event()}
        stop_event = threading.Event()
        self._result_lock = threading.Lock()
        deadline = time.monotonic() + total
        rush_t0 = time.monotonic()

        # worker 集合:优先用等待期预热好的;等待循环未走过(如 --now
        # 立即开抢/目标已过)则现场生成,保证并发路数不因路径退化成单路
        clients = list(self._worker_clients or [])
        if not clients:
            clients = [self.client]
            if isinstance(self.client, BiliClient):
                for i in range(n_workers - 1):
                    try:
                        clients.append(self._spawn_client())
                    except Exception:   # 单路创建失败:跳过该路,不废整轮
                        logger.exception("worker-%s client 创建失败,跳过",
                                         i + 1)
            else:
                # 测试 mock 客户端:共享同一实例(避免 spawn 真实
                # BiliClient 发实际网络请求污染测试与外部接口)
                clients += [self.client] * (n_workers - 1)
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
        self._record_run_summary(counter)   # 终局汇总(任何终态)
        return orders[0] if orders else None

    def _spawn_client(self) -> BiliClient:
        """为并发 worker 克隆 client：独立连接池，共享日志审计。"""
        c = BiliClient(run_logger=self.client.run_logger,
                       timeout=getattr(self.client, "timeout", 10.0))
        c.phase = self.client.phase
        return c

    def _record_run_summary(self, counter):
        """终局一行汇总:总发数/各响应码计数/各波进场数/哨兵发数。

        复盘入口:不看逐发日志也能一眼读出全局形态,配合 volley_enter/
        sentinel 时间线可完整重建开售瞬间的时间轴。
        """
        try:
            codes = {}
            # 从日志侧统计不可靠,改用 counter 累计:此处退化为记框架,
            # 逐码统计由复盘工具按 order_fail 聚合(replay.py 已支持)
            entered = sum(1 for k in counter
                          if str(k).startswith("entered_"))
            self._record("run_summary", attempts=counter["n"],
                         workers_entered=entered,
                         burst_fired=counter["burst"].is_set(),
                         open_obs_s=counter.get("open_obs_s"),
                         crashed=counter["crashed"],
                         risk_hits=counter["risk"])
        except Exception:
            logger.exception("run_summary 记录失败(不影响主流程)")

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
        last_was_crowd = False   # 上一发 43055(挤门失败):下一发快速再挤
        while not stop_event.is_set() and time.monotonic() < deadline:
            # 探测-爆发:开售前仅 worker-0 低频探测;其余路休眠到开售
            # 瞬间(最后 50ms 忙等保毫秒级唤醒),频控信用留给爆发
            volley1 = max(1, int(getattr(settings, "RUSH_VOLLEY_1", 35)))
            volley2 = max(0, int(getattr(settings, "RUSH_VOLLEY_2", 15)))
            if worker_id >= 1:
                # 三段门:worker 1..volley1-1 第一波押 12:00:00.00;
                # volley1..volley1+volley2-1 第二波(开闸信号先到先发,
                # 兜底 +0.30s);其余为余量路(+1.0s 匀速捡漏)
                if worker_id < volley1:
                    gate = 0.0
                elif worker_id < volley1 + volley2:
                    gate = getattr(settings, "RUSH_VOLLEY2_FALLBACK", 0.30)
                else:
                    gate = 1.0
                sale_now = ((time.monotonic() - rush_t0)
                            - getattr(self, "_lead_s", 0.0))
                if sale_now < gate and not counter["burst"].is_set():
                    if gate - sale_now > 0.05:
                        # 短睡片:开闸信号到达后最多 50ms 即可进场
                        time.sleep(min(gate - sale_now - 0.05, 0.05))
                        continue
                    # 末段忙等:越过波门或收到开闸信号即进场
                    while (((time.monotonic() - rush_t0)
                            - getattr(self, "_lead_s", 0.0)) < gate
                           and not counter["burst"].is_set()):
                        pass
            # 复盘留痕:本路首次进场,记录实际进场时刻(距开售)与
            # 触发方式(定时过门/开闸信号唤醒)——回溯各波唤醒精度
            if worker_id > 0 and not counter.get("entered_" + str(worker_id)):
                with counter["lock"]:
                    counter["entered_" + str(worker_id)] = True
                self._record(
                    "volley_enter", worker=worker_id,
                    sale_s=round((time.monotonic() - rush_t0)
                                 - getattr(self, "_lead_s", 0.0), 3),
                    via_burst=counter["burst"].is_set())
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
                    _sale_now = ((time.monotonic() - rush_t0)
                                 - getattr(self, "_lead_s", 0.0))
                    # 哨兵标记:worker-0 在盯梢窗内的发次(复盘时用于
                    # 重建开闸时刻的精确观测序列)
                    sentinel = (worker_id == 0 and _sale_now < 0.5)
                    self._record("order_fail", attempt=attempt,
                                 worker=worker_id,
                                 plan=plan["name"], code=ex.code,
                                 message=str(ex), raw=text,
                                 sentinel=sentinel)
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
                    last_was_crowd = (ex.code == 43055)
                    # -702 频控窗口统计(自适应节流依据)+连续计数
                    # (连续被拒达阈值后黄金窗豁免失效,防全速被频控全吞)
                    with counter["lock"]:
                        counter["win_total"] += 1
                        if ex.code == -702:
                            counter["win_702"] += 1
                            counter["streak702"] = counter.get("streak702", 0) + 1
                        else:
                            counter["streak702"] = 0
                    # 开闸锚点:首个放行码(43055/非常规码)的观测时刻,
                    # 黄金窗/尾段计时整体平移到它(消除开闸漂移,两天
                    # 实测 +0.2s/+0.35s);-702 不作锚(只证明频控)。
                    # 首写即最早观测,时间顺序天然取 min
                    if ex.code not in (69422, -702):
                        with counter["lock"]:
                            if counter.get("open_obs_s") is None:
                                counter["open_obs_s"] = round(_sale_now, 3)
                    # 开闸信号:69422=未开售、-702=只证明频控不证明放行,
                    # 其余码(43055 拥挤在内)=网关已放行=开闸铁证即广播
                    # 全员爆发。10-02 实测:43055 最早出现在 +0.356s 与
                    # 开闸时刻一致,且比哨兵的下一发盯梢更快发现放行;
                    # 监听窗覆盖到余量路 gate 前——burst 唤醒的就是它们
                    if (ex.code not in (69422, -702)
                            and not counter["burst"].is_set()
                            and _sale_now
                            < getattr(settings, "RUSH_BURST_WINDOW_S", 1.0)):
                        counter["burst"].set()
                        self._record("burst_signal", code=ex.code,
                                     worker=worker_id,
                                     sale_s=round(_sale_now, 3))
                        logger.info("开闸信号(code=%s),全员提前爆发", ex.code)
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
            sale_s = elapsed - getattr(self, "_lead_s", 0.0)  # 距开售(钟点)
            # 开闸锚定:首个放行码观测锚点可信时,分段计时整体平移——
            # 锚点前的发退回探测密度,锚点后才进黄金窗
            with counter["lock"]:
                open_obs = counter.get("open_obs_s")
            eff_sale = effective_sale_s(sale_s, open_obs)
            try:
                dens, exempt, stop_now = phase_pacing(eff_sale)
                # 频控硬上限:连续 50 发 -702 后黄金窗豁免失效
                # (开售若不放行频控,全速=全拒=零命中,恢复节流贴线)
                if exempt:
                    with counter["lock"]:
                        if counter.get("streak702", 0) >= 50:
                            exempt = False
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
                                 open_obs_s=open_obs,
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
                    # 节流留痕:每个统计窗口一条,复盘时还原节奏变化原因
                    self._record("throttle_state",
                                 win_rate=round(counter["win_rate"], 2),
                                 streak702=counter.get("streak702", 0),
                                 attempts=counter["n"])
                rate = counter.get("win_rate", 0.0)
            extra = 0.0 if exempt else settings.RUSH_THROTTLE_MAX_S * rate
            # 冲刺窗/探测段用绝对间隔:dens 乘 next_interval 三段基准会
            # 随全局 attempt 增长漂移(0.04→0.12 后 0.8s 变 2.4s,
            # 10-07 实证二波第二轮 1.8s 间隔,冲刺密度仅打出一半)
            if interval is not None:   # 显式覆盖(测试)优先
                base_sleep = interval
            elif 0 <= eff_sale < getattr(settings, "RUSH_CROWD_WINDOW", 2.0):
                crowd_iv = (getattr(settings, "RUSH_CROWD_FAST_INTERVAL", 0.6)
                            if last_was_crowd else
                            getattr(settings, "RUSH_CROWD_INTERVAL", 0.8))
                base_sleep = crowd_iv * (
                    1 + random.uniform(-settings.RUSH_JITTER,
                                       settings.RUSH_JITTER))
            elif eff_sale < 0:
                base_sleep = (getattr(settings, "RUSH_PROBE_INTERVAL", 0.3)
                              * (1 + random.uniform(-settings.RUSH_JITTER,
                                                    settings.RUSH_JITTER)))
            else:
                base_sleep = next_interval(attempt, elapsed, interval) * dens
            # 哨兵:worker-0 在开售前后 0.5s 窗口内以 100ms 盯梢
            # (高频捕捉开闸码突变,拉响第二波;消耗 ~5 发额度)
            if (worker_id == 0 and not counter["burst"].is_set()
                    and -0.15 < sale_s < 0.5):
                base_sleep = 0.1
            time.sleep(base_sleep + extra)
        with counter["lock"]:
            first_timeout = not counter["timed_out"]
            counter["timed_out"] = True
        if first_timeout and not stop_event.is_set():
            self._record("rush_timeout", attempts=counter["n"])
            logger.warning("坚持 %ss 后仍未抢到", total)
            self._set_result({"result": "timeout", "attempts": counter["n"]})
        return None
