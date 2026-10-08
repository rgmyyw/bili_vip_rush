# -*- coding: utf-8 -*-
"""邮件推送：抢购结果通知（成功含支付链接 / 凭证失效提醒 / 失败原因）。

原则：通知永远不能影响抢购主流程——未配置时静默跳过，发送失败只记
日志事件并返回 False。
"""
import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

import requests
import os
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
    "budget_cap": "弹药预算用尽(30发)未抢到",
    "freq_wall": "触发频控墙(-702)已收兵",
    "timeout": "超时未抢到",
    "crashed": "运行异常",
    "risk_control": "触发风控已停抢",
}


def notify_enabled() -> bool:
    return bool(settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASS)


def _notify_json() -> dict:
    try:
        return json.loads(settings.NOTIFY_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def reload_notify_into_settings():
    """常驻进程每轮前调用：按 env > notify.json > secrets.py 重载邮件配置。"""
    from config import settings as settings_mod
    settings_mod.SMTP_HOST = settings_mod._cred(
        "BILI_SMTP_HOST", "SMTP_HOST", "host") or ""
    settings_mod.SMTP_PORT = int(
        settings_mod._cred("BILI_SMTP_PORT", "SMTP_PORT", "port") or 465)
    settings_mod.SMTP_USER = settings_mod._cred(
        "BILI_SMTP_USER", "SMTP_USER", "user") or ""
    settings_mod.SMTP_PASS = settings_mod._cred(
        "BILI_SMTP_PASS", "SMTP_PASS", "pass") or ""
    settings_mod.DINGTALK_WEBHOOK = settings_mod._cred(
        "BILI_DINGTALK_WEBHOOK", "DINGTALK_WEBHOOK", "dingtalk_webhook") or ""
    settings_mod.DINGTALK_SECRET = settings_mod._cred(
        "BILI_DINGTALK_SECRET", "DINGTALK_SECRET", "dingtalk_secret") or ""
    settings_mod.NOTIFY_TO = (
        settings_mod._cred("BILI_NOTIFY_TO", "NOTIFY_TO", "to")
        or settings_mod.SMTP_USER)


def _field_source(env_name: str, json_key: str) -> str:
    """字段值来源：环境变量 / 仪表盘 / 本地文件 / 未配置。"""
    if os.environ.get(env_name):
        return "环境变量"
    if _notify_json().get(json_key):
        return "仪表盘"
    return "本地文件"


def notify_config_view() -> dict:
    """仪表盘用的配置视图：授权码不回传，只返回是否已设置与各字段来源。"""
    return {
        "enabled": notify_enabled(),
        "host": settings.SMTP_HOST,
        "port": settings.SMTP_PORT,
        "user": settings.SMTP_USER,
        "to": settings.NOTIFY_TO,
        "pass_set": bool(settings.SMTP_PASS),
        "dingtalk_webhook": (settings.DINGTALK_WEBHOOK or "")[:60],
        "dingtalk_set": bool(settings.DINGTALK_WEBHOOK),
        "sources": {
            "host": _field_source("BILI_SMTP_HOST", "host"),
            "user": _field_source("BILI_SMTP_USER", "user"),
            "pass": _field_source("BILI_SMTP_PASS", "pass"),
            "to": _field_source("BILI_NOTIFY_TO", "to"),
            "dingtalk": _field_source("BILI_DINGTALK_WEBHOOK",
                                      "dingtalk_webhook"),
        },
    }


# 仪表盘表单字段 -> (环境变量, notify.json 键) 映射，save/reload 共用
_FORM_FIELDS = {
    "host": "BILI_SMTP_HOST",
    "user": "BILI_SMTP_USER",
    "pass": "BILI_SMTP_PASS",
    "to": "BILI_NOTIFY_TO",
    "dingtalk_webhook": "BILI_DINGTALK_WEBHOOK",
    "dingtalk_secret": "BILI_DINGTALK_SECRET",
}


def save_notify_config(cfg: dict) -> dict:
    """仪表盘保存邮件配置（字段级合并，与本地配置共存生效）。

    合并规则——只持久化表单里明确填写的字段：
      - 字段有值 -> 写入 notify.json 的对应键；
      - 字段留空 -> 删除该键，回落到 secrets.py / 环境变量的值；
      - 未提交的字段（None）-> 保持 notify.json 现状。
    读取优先级（字段级）：环境变量 > notify.json > secrets.py。
    """
    from config import settings as settings_mod

    try:
        merged = _notify_json()
    except Exception:
        merged = {}
    if not isinstance(merged, dict):
        merged = {}

    for key in ("host", "port", "user", "pass", "to",
                "dingtalk_webhook", "dingtalk_secret"):
        val = cfg.get(key)
        if val is None:
            continue                      # 未提交：保持现状
        val = str(val).strip()
        if not val:
            merged.pop(key, None)         # 显式清空：删键回落本地配置
            continue
        merged[key] = int(val) if key == "port" else val

    settings.NOTIFY_JSON_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    reload_notify_into_settings()
    return notify_config_view()


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
            # 587 等端口必须先 STARTTLS 再登录（QQ 邮箱强制，明文直接被断开）
            try:
                server.starttls()
            except smtplib.SMTPNotSupportedError:
                pass   # 服务器不支持 TLS 时退回明文（部分内网中继）
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
    # 抢购结果双通道:邮件 + 钉钉(成功通知有支付时效,IM 即达;
    # 未配置钉钉时静默跳过,不影响邮件)
    send_dingtalk(f"{subject}\n{body}")
    return send_mail(subject, body)


def send_dingtalk(text: str) -> bool:
    """钉钉机器人推送（加签模式）。未配置 webhook 返回 False；
    任何失败不抛异常。"""
    webhook = (settings.DINGTALK_WEBHOOK or "").strip()
    if not webhook:
        return False
    url = webhook
    secret = (settings.DINGTALK_SECRET or "").strip()
    if secret:
        ts = str(round(time.time() * 1000))
        digest = hmac.new(secret.encode("utf-8"),
                          f"{ts}\n{secret}".encode("utf-8"),
                          hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        url = f"{webhook}&timestamp={ts}&sign={sign}"
    try:
        resp = requests.post(url, timeout=8,
                             json={"msgtype": "text", "text": {"content": text}})
        return bool(resp.json().get("errcode") == 0)
    except Exception as ex:
        logger.warning("钉钉推送失败(不影响主流程): %s", ex)
        return False


def notify_all(subject: str, body: str) -> dict:
    """邮件+钉钉双通道推送。返回各通道结果。"""
    mail_ok = send_mail(subject, body) if notify_enabled() else False
    ding_ok = send_dingtalk(f"{subject}\n{body}")
    return {"mail": mail_ok, "dingtalk": ding_ok}
