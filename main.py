# -*- coding: utf-8 -*-
"""B站联合会员抢购 CLI。

用法（在项目根目录）：
    python main.py --check            # 只读预检：状态/预约/资格
    python main.py --reserve          # 未预约时先预约（写操作）
    python main.py --rush             # 等待下一次开售自动抢（默认明天12:00）
    python main.py --rush --at 12:00  # 指定今天某个时刻（本地时间 HH:MM[:SS]）
    python main.py --rush --now       # 立即开抢（已开售时用）
    python main.py --daemon 11:50     # 常驻模式：每天 11:50 自动执行一轮抢购
    python tools/replay.py            # 复盘最近一次运行日志

Docker：
    docker compose run --rm bili-rush --check
    docker compose up -d              # 常驻，每天 11:50 自动抢
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from core.client import BiliClient
from core.run_logger import RunLogger
from flows.rush import RushFlow

LOGS_DIR = Path(__file__).resolve().parent / "logs"


def setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def parse_args():
    ap = argparse.ArgumentParser(description="B站联合会员抢购")
    ap.add_argument("--check", action="store_true", help="只读预检")
    ap.add_argument("--reserve", action="store_true", help="执行预约（写操作）")
    ap.add_argument("--rush", action="store_true", help="等待开售并抢购")
    ap.add_argument("--now", action="store_true", help="配合 --rush：立即开抢")
    ap.add_argument("--at", metavar="HH:MM[:SS]", help="配合 --rush：指定本地时刻")
    ap.add_argument("--daemon", metavar="HH:MM[:SS]",
                    help="常驻模式：每天该时刻自动执行一轮抢购（开售时间以"
                         "服务器 next_open_at 为准）")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args()


def parse_hhmm(text: str) -> tuple:
    hh, mm, *rest = (int(x) for x in text.split(":"))
    ss = rest[0] if rest else 0
    return hh, mm, ss


def next_run_ts(hhmm: str, now: datetime | None = None) -> float:
    """计算下一次触发时刻（本地时间）：今天该时刻已过则取明天。"""
    hh, mm, ss = parse_hhmm(hhmm)
    now = now or datetime.now()
    when = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    return when.timestamp()


def run_target_ts(args) -> float | None:
    if args.now:
        return datetime.now().timestamp() - 5  # 已过点，立即开抢
    if args.at:
        return next_run_ts(args.at)
    return None


def run_one_cycle(mode: str, args) -> int:
    """执行一轮完整流程（独立的日志文件）。返回退出码。"""
    logs = RunLogger(LOGS_DIR, mode=mode)
    logs.log_meta(event="argv", argv=sys.argv[1:])   # run_start 已在构造时写入
    print(f"日志文件: {logs.path}")

    client = BiliClient(run_logger=logs)
    exit_code = 0
    try:
        if args.check:
            flow = RushFlow(client, run_logger=logs)
            flow.precheck()
        elif args.reserve:
            client.phase = "reserve"
            result = client.reserve()
            logging.info("预约结果: %s", result)
            logs.log_event("reserve_ok", result=result)
        else:
            flow = RushFlow(client, run_logger=logs)
            flow.precheck()   # 抢前自检：状态/资格一眼可见
            order = flow.rush(target_ts=run_target_ts(args))
            if order:
                print("\n" + "=" * 60)
                print("抢购成功！订单信息：")
                print(order)
                print("=" * 60)
                for plan in flow.plans:
                    if plan["name"] == "178超级大会员(买1年得5年, 连续包年)":
                        print("支付链接(手机打开):")
                        print(client.pay_link(plan))
                        break
            else:
                print("本轮未抢到。复盘: python tools/replay.py")
                exit_code = 1
    except KeyboardInterrupt:
        logs.log_meta(event="run_interrupted", reason="KeyboardInterrupt")
        print("\n手动中断，日志已保存")
        exit_code = 130
    except Exception as ex:  # 兜底：任何未预期异常都要留现场
        import traceback
        logs.log_meta(event="run_crashed", error=repr(ex),
                      traceback=traceback.format_exc())
        logging.exception("运行异常，现场已写入日志")
        exit_code = 2
    finally:
        logs.log_meta(event="run_end", exit_code=exit_code)
    return exit_code


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    setup_logging(args.verbose)

    if args.daemon:
        # 常驻调度：每天 daemon 时刻启动一轮（抢购目标以服务器
        # next_open_at 为准，daemon 时刻只是"起跑线"，提前量放余量）
        logging.info("常驻模式启动，每天 %s 自动执行", args.daemon)
        while True:
            target = next_run_ts(args.daemon)
            logging.info("下一轮触发: %s",
                         datetime.fromtimestamp(target).strftime(
                             "%Y-%m-%d %H:%M:%S"))
            while True:
                remain = target - time.time()
                if remain <= 0:
                    break
                time.sleep(min(remain, 3600.0))   # 分段睡，抗时钟跳变
            try:
                run_one_cycle("daemon", args)
            except KeyboardInterrupt:
                raise
            # 未预期异常已在 run_one_cycle 内兜底，这里继续下一周期
        return 0

    if not (args.check or args.reserve or args.rush):
        print(__doc__)
        return 2
    return run_one_cycle(
        "check" if args.check else "reserve" if args.reserve else "rush", args)


if __name__ == "__main__":
    sys.exit(main() or 0)
