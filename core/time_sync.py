# -*- coding: utf-8 -*-
"""服务器时间校准：用接口返回的 current_time 估算本地时钟偏移。"""
import statistics
import time
from typing import Callable

# 返回服务器 Unix 秒 的回调（由 client 提供），网络失败抛异常
ServerTimeGetter = Callable[[], int]


def measure_offset(get_server_time: ServerTimeGetter, samples: int = 5) -> float:
    """采样 N 次，返回 (服务器时间 - 本地时间) 偏移的中位数（秒）。

    每次采样以请求发出与响应收到的中点作为本地参考时刻，
    假设网络往返对半分摊，抵消传输耗时。
    """
    offsets = []
    for _ in range(samples):
        t0 = time.time()
        server_ts = get_server_time()
        t1 = time.time()
        offsets.append(server_ts - (t0 + t1) / 2)
        time.sleep(0.15)
    return statistics.median(offsets)


def server_now(offset: float) -> float:
    """换算当前服务器时间。"""
    return time.time() + offset


def seconds_until(target_server_ts: float, offset: float) -> float:
    """距离服务器目标时间还剩多少秒（可为负）。"""
    return target_server_ts - server_now(offset)


# ---------------------------------------------------------------- NTP 校准
NTP_SERVERS = ("ntp.aliyun.com", "ntp1.aliyun.com", "cn.pool.ntp.org",
               "time.windows.com")


def measure_ntp_offset(servers=NTP_SERVERS, timeout: float = 2.0) -> float | None:
    """查多个 NTP 服务器,返回 (NTP时间 - 本地时间) 偏移中位数(秒)。

    毫秒级精度,用于交叉验证 B 站接口校时(current_time 为秒级整数,
    采样可能被网络抖动污染)。全部失败返回 None(不阻塞,仅降级)。
    """
    import ntplib as _ntp

    offsets = []
    for host in servers:
        try:
            c = _ntp.NTPClient()
            resp = c.request(host, version=3, timeout=timeout)
            offsets.append(resp.offset)
        except Exception:
            continue
    if not offsets:
        return None
    return statistics.median(sorted(offsets))


def measure_offset_precise(get_server_time, probe_interval: float = 0.1,
                           max_wait: float = 2.5) -> float | None:
    """亚秒级校时：探测 server_ts 整秒跳变瞬间。

    B 站 current_time 为整数秒，中位数法存在固有量化误差(可达 ±500ms)。
    本方法以 ~100ms 间隔轻探测，捕获 server_ts N→N+1 的翻转时刻：
    该瞬间服务器钟恰为 N+1.000，本地时刻即跳变观测点，精度 ≈ 探测
    间隔的一半(±50ms)。平均 0.5s 内即可捕获(约 5~8 发轻请求)，
    失败返回 None(调用方退回中位数法)。
    """
    prev = get_server_time()
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_wait:
        time.sleep(probe_interval)
        t_a = time.time()
        cur = get_server_time()
        t_b = time.time()
        if cur > prev:
            # cur>prev 说明整秒跳变发生于两次探测之间,此刻服务器钟
            # 恰为 cur.0x;以本请求中点近似服务器时刻(消除 RTT/2 偏差)
            return float(cur) - (t_a + t_b) / 2
        prev = cur
    return None
