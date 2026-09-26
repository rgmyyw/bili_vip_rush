# -*- coding: utf-8 -*-
"""邮件推送：抢购结果通知（成功含支付链接 / 凭证失效提醒 / 失败原因）。

原则：通知永远不能影响抢购主流程——未配置时静默跳过，发送失败只记
日志事件并返回 False。
"""
import logging
import smtplib
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import formataddr

from config import settings

logger = logging.getLogger(__name__)

REASON_TEXT = {
    "success": "抢购成功",
    "sold_out": "已售罄",
    "credential_expired": "凭证失效（需重新抓包）",
    "already_owned": "已有订单/已购买",
    "max_attempts": "达到重试上限",
    "timeout": "超时未抢到",
    "crashed": "运行异常",
}


def notify_enabled() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS)


def send_mail(subject: str, body: str) -> bool:
    """发送纯文本邮件。任何失败都不抛异常，返回 False。"""
    if not notify_enabled():
        logger.debug("邮件通知未配置，跳过")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr(("B站抢购助手", settings.SMTP_USER))
        msg["To"] = settings.NOTIFY_TO

        if settings.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT,
                                      timeout=15)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT,
                                  timeout=15)
        try:
            server.login(settings.SMTP_USER, settings.SMTP_PASS)
            server.sendmail(settings.SMTP_USER, [settings.NOTIFY_TO],
                            msg.as_string())
        finally:
            server.quit()
        logger.info("邮件已发送至 %s: %s", settings.NOTIFY_TO, subject)
        return True
    except Exception as ex:
        logger.warning("邮件发送失败(不影响抢购): %s", ex)
        return False


def notify_rush_result(result: dict) -> bool:
    """把 rush 结果推成邮件。

    result 字段（main.py 组装）：
        result: success/sold_out/credential_expired/timeout/...
        order:  订单 dict（成功时）
        pay_link: 支付链接（成功时）
        attempts: 尝试次数
        log_file: 本次运行日志文件名
    """
    reason = result.get("result", "timeout")
    title = REASON_TEXT.get(reason, reason)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if reason == "success":
        order = result.get("order") or {}
        order_no = order.get("order_no") or order.get("orderNo") or "(见日志)"
        subject = f"[B站抢购] 成功！{title}，请尽快支付"
        body = (
            f"抢购成功，名额已锁定！订单有时效，请尽快支付。\n\n"
            f"订单号: {order_no}\n"
            f"支付链接(手机打开):\n{result.get('pay_link', '')}\n\n"
            f"时间: {now}\n"
            f"日志: {result.get('log_file', '')}\n"
        )
    elif reason == "credential_expired":
        subject = f"[B站抢购] {title}"
        body = (
            f"抢购已停止：access_key/SESSDATA 失效，重试无意义。\n\n"
            f"处理：重新抓包 -> 更新 config/secrets.py 或环境变量 BILI_*。\n"
            f"时间: {now}\n日志: {result.get('log_file', '')}\n"
        )
    else:
        subject = f"[B站抢购] 未成功：{title}"
        body = (
            f"本轮抢购结束，结果: {title}\n\n"
            f"尝试次数: {result.get('attempts', '?')}\n"
            f"时间: {now}\n"
            f"复盘: python tools/replay.py {result.get('log_file', '')}\n"
        )
    return send_mail(subject, body)
