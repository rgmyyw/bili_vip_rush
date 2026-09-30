# -*- coding: utf-8 -*-
"""邮件通知测试：mock smtplib，验证内容/降级/主流程不受影响。"""
from unittest import mock

import core.notify as notify_mod
from core.notify import notify_enabled, notify_rush_result, send_mail


def config_smtp(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "SMTP_HOST", "smtp.test.com")
    monkeypatch.setattr(notify_mod.settings, "SMTP_PORT", 465)
    monkeypatch.setattr(notify_mod.settings, "SMTP_USER", "bot@test.com")
    monkeypatch.setattr(notify_mod.settings, "SMTP_PASS", "authcode")
    monkeypatch.setattr(notify_mod.settings, "NOTIFY_TO", "me@test.com")


def test_disabled_by_default(monkeypatch):
    monkeypatch.setattr(notify_mod.settings, "SMTP_HOST", "")
    assert not notify_enabled()
    # 未配置时静默跳过且不抛异常
    assert send_mail("s", "b") is False
    assert notify_rush_result({"result": "success"}) is False


def test_send_mail_ssl_success(monkeypatch):
    config_smtp(monkeypatch)
    with mock.patch.object(notify_mod.smtplib, "SMTP_SSL") as ssl_cls:
        send_mail("[B站抢购] 测试", "正文")
    ssl_cls.assert_called_once_with("smtp.test.com", 465, timeout=15)
    server = ssl_cls.return_value
    server.login.assert_called_once_with("bot@test.com", "authcode")
    server.sendmail.assert_called_once()
    args = server.sendmail.call_args.args
    assert args[0] == "bot@test.com" and args[1] == ["me@test.com"]
    # 中文会 base64 编码，用 policy 解析回来验证（自动解码 header）
    from email import message_from_string
    from email.policy import default as email_policy
    msg = message_from_string(args[2], policy=email_policy)
    assert msg["Subject"] == "[B站抢购] 测试"
    assert msg.get_content().strip() == "正文"


def test_send_mail_failure_never_raises(monkeypatch):
    config_smtp(monkeypatch)
    with mock.patch.object(notify_mod.smtplib, "SMTP_SSL",
                           side_effect=OSError("dns fail")):
        assert send_mail("s", "b") is False   # 失败返回 False 不抛异常


def test_notify_success_contains_pay_link(monkeypatch):
    config_smtp(monkeypatch)
    with mock.patch.object(notify_mod, "send_mail",
                           wraps=notify_mod.send_mail) as m:
        notify_rush_result({
            "result": "success",
            "order": {"order_no": "ORD1"},
            "pay_link": "https://pay.example/ORD1",
            "attempts": 3,
            "log_file": "run_x.jsonl",
        })
    subject, body = m.call_args.args
    assert "成功" in subject and "尽快支付" in subject
    assert "ORD1" in body and "https://pay.example/ORD1" in body


def test_notify_failure_reasons(monkeypatch):
    config_smtp(monkeypatch)
    with mock.patch.object(notify_mod, "send_mail") as m:
        notify_rush_result({"result": "sold_out", "attempts": 120,
                            "log_file": "run_y.jsonl"})
        notify_rush_result({"result": "credential_expired",
                            "log_file": "run_z.jsonl"})
    subjects = [c.args[0] for c in m.call_args_list]
    assert "未成功：已售罄" in subjects[0]
    assert "凭证失效" in subjects[1]


def test_rush_sets_last_result(tmp_path):
    """rush 各终止路径填充 last_result（通知数据源）。"""
    import json
    import time as _t
    from core.client import BiliApiError
    from flows.rush import RushFlow

    class FakeClient:
        def __init__(self, outcomes):
            self.outcomes = outcomes

        def get_attract_card(self):
            return {"next_open_at": _t.time() - 1, "current_time": int(_t.time()),
                    "drainage_status": "ON_SALE", "isReserved": True,
                    "has_buy": False}

        def get_server_time(self):
            return int(_t.time())

        def get_buy_component(self):
            return {"buySets": []}

        def create_order(self, plan):
            r = self.outcomes.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        def pay_link(self, plan):
            return "https://pay/x"

        def get_order_status(self, order, app_id="241"):
            return {"status": 1}

    # 成功路径
    flow = RushFlow(FakeClient([{"order_no": "O1"}]), plans=[{
        "name": "p", "act_token": "T", "app_id": "241", "app_sub_id": "s",
        "panel_type": "26moe_cdd178", "months": 12, "order_type": 1,
        "product_type": "1"}], log_dir=tmp_path)
    flow.rush(target_ts=_t.time() - 1, early_seconds=0, interval=0, duration=5)
    assert flow.last_result["result"] == "success"
    assert flow.last_result["order"] == {"order_no": "O1"}

    # 凭证失效路径
    flow2 = RushFlow(FakeClient(
        [BiliApiError("code=-101 message=账号未登录", code=-101)]), plans=[{
        "name": "p", "act_token": "T", "app_id": "241", "app_sub_id": "s",
        "panel_type": "26moe_cdd178", "months": 12, "order_type": 1,
        "product_type": "1"}], log_dir=tmp_path)
    flow2.rush(target_ts=_t.time() - 1, early_seconds=0, interval=0,
               duration=5)
    assert flow2.last_result["result"] == "credential_expired"


def test_notify_rush_result_dual_channel(monkeypatch):
    """抢购结果通知邮件+钉钉双通道(钉钉即达,支付时效关键)。"""
    from core import notify
    calls = {"mail": 0, "ding": 0}
    monkeypatch.setattr(notify, "notify_enabled", lambda: True)
    monkeypatch.setattr(notify, "send_mail",
                        lambda s, b: calls.__setitem__("mail", calls["mail"]+1) or True)
    monkeypatch.setattr(notify, "send_dingtalk",
                        lambda t: calls.__setitem__("ding", calls["ding"]+1) or True)
    ok = notify.notify_rush_result(
        {"result": "risk_control", "attempts": 10, "log_file": "x.jsonl"})
    assert ok is True
    assert calls == {"mail": 1, "ding": 1}   # 双通道各一次


def test_notify_rush_result_risk_control_text(monkeypatch):
    from core import notify
    subjects = []
    monkeypatch.setattr(notify, "notify_enabled", lambda: True)
    monkeypatch.setattr(notify, "send_mail",
                        lambda s, b: subjects.append(s) or True)
    monkeypatch.setattr(notify, "send_dingtalk", lambda t: True)
    notify.notify_rush_result({"result": "risk_control", "attempts": 5})
    assert "风控" in subjects[0]   # 中文文案而非裸英文码
