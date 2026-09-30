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
from core.notify import notify_enabled, notify_rush_result
from core.pause import RushPausedError, is_paused, pause_state
from core.run_logger import RunLogger
from flows.rush import PlanValidationError, RushFlow

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
    ap.add_argument("--test-notify", action="store_true",
                    help="发送测试邮件，验证 SMTP 配置")
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
    # 仪表盘/扫码更新的凭证与邮件配置（credentials.json/notify.json）
    # 常驻进程每轮开始前重载，改配置无需重启
    from core.auth import reload_credentials_into_settings
    from core.notify import reload_notify_into_settings
    reload_credentials_into_settings()
    reload_notify_into_settings()

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
            # 每天自动尽力预约(用户 09-30 要求):场次重置后补约;预约
            # 形态已对齐 App(code=0);业务态失败(-400 已约/未开)忽略,
            # 不阻塞抢购、不发失败邮件
            try:
                client.phase = "reserve"
                result = client.reserve()
                logging.info("自动预约成功: %s", result)
                logs.log_event("reserve_auto_ok", result=str(result)[:200])
            except Exception as ex:
                logging.info("自动预约未成功(忽略,不影响抢购): %s",
                             str(ex)[:150])
                logs.log_event("reserve_auto_skip", error=str(ex)[:150])
            flow.precheck()   # 抢前自检：状态/资格一眼可见
            order = flow.rush(target_ts=run_target_ts(args))
            if order:
                print("\n" + "=" * 60)
                print("抢购成功！订单信息：")
                print(order)
                print("=" * 60)
                # 支付链接用目标套餐生成（白名单保证 plans[0] 即唯一
                # 可购套餐，不按名字匹配，防改名错配）
                print("支付链接(手机打开):")
                print(client.pay_link(flow.plans[0]))
            else:
                print("本轮未抢到。复盘: python tools/replay.py")
                exit_code = 1
            # 结果推送(邮件+钉钉双通道,任一配置即发;成功含支付链接)
            from config import settings as _st
            has_channel = (notify_enabled()
                           or (_st.DINGTALK_WEBHOOK or "").strip())
            if has_channel and flow.last_result:
                flow.last_result["log_file"] = logs.path.name
                sent = notify_rush_result(flow.last_result)
                logs.log_event("notify", sent=sent,
                               result=flow.last_result.get("result"))
    except KeyboardInterrupt:
        logs.log_meta(event="run_interrupted", reason="KeyboardInterrupt")
        print("\n手动中断，日志已保存")
        exit_code = 130
    except PlanValidationError as ex:   # 白名单自检拦截:已双通道告警,不再发 crashed
        logs.log_meta(event="plan_validation_blocked", reason=str(ex))
        print(f"\n本轮已拦截: {ex}")
        logging.error("套餐白名单自检未通过: %s", ex)
        return 2
    except RushPausedError as ex:   # 暂停中止：非失败，不发邮件
        logs.log_meta(event="paused_abort", reason=str(ex))
        print(f"\n本轮已中止：服务处于暂停状态（{ex}）")
        logging.warning("本轮因服务暂停中止: %s", ex)
        return 0
    except Exception as ex:  # 兜底：任何未预期异常都要留现场
        import traceback
        logs.log_meta(event="run_crashed", error=repr(ex),
                      traceback=traceback.format_exc())
        logging.exception("运行异常，现场已写入日志")
        if notify_enabled():
            notify_rush_result({"result": "crashed",
                                "log_file": logs.path.name})
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
        from core.credwatch import start_cred_watch
        start_cred_watch()   # 凭证巡检:每小时体检,失效邮件+钉钉告警
        while True:
            target = next_run_ts(args.daemon)
            logging.info("下一轮触发: %s%s",
                         datetime.fromtimestamp(target).strftime(
                             "%Y-%m-%d %H:%M:%S"),
                         "（服务已暂停，到点将跳过）" if is_paused() else "")
            while True:
                remain = target - time.time()
                if remain <= 0:
                    break
                time.sleep(min(remain, 3600.0))   # 分段睡，抗时钟跳变
            if is_paused():
                st = pause_state()
                logging.warning("服务已暂停(%s)，跳过本轮起跑",
                                st.get("reason") or "未注明原因")
                time.sleep(300)   # 暂停态下 5 分钟后再看（恢复后等下个周期）
                continue
            try:
                run_one_cycle("daemon", args)
            except KeyboardInterrupt:
                raise
            # 未预期异常已在 run_one_cycle 内兜底，这里继续下一周期
        return 0

    if args.test_notify:
        from core.notify import notify_enabled, send_mail
        if not notify_enabled():
            print("邮件通知未配置：请在 config/secrets.py 或环境变量 "
                  "BILI_SMTP_HOST/USER/PASS 填写 SMTP 信息")
            return 1
        ok = send_mail("[B站抢购] 测试邮件",
                       "收到这封邮件说明抢购结果推送配置成功。\n"
                       "抢购成功后会推送订单号与支付链接。")
        print("测试邮件发送", "成功，请查收" if ok else "失败，检查授权码/端口")
        return 0 if ok else 1

    if not (args.check or args.reserve or args.rush):
        print(__doc__)
        return 2
    return run_one_cycle(
        "check" if args.check else "reserve" if args.reserve else "rush", args)


if __name__ == "__main__":
    sys.exit(main() or 0)
