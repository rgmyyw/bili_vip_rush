# -*- coding: utf-8 -*-
"""B站活动接口客户端：状态查询 / 预约 / 下单。

所有请求带 App 端签名（appkey/sign）与登录 cookie；
接口返回 code != 0 时抛 BiliApiError，原始响应文本保留在异常里，
便于 flows 层落盘现场。
"""
import json
import logging
import time
from typing import Any

import requests

from config import settings
from core.run_logger import NullRunLogger
from core.signer import signed_params

logger = logging.getLogger(__name__)

# 引流卡开售状态
DRAINAGE_ON_SALE = "ON_SALE"
DRAINAGE_SOLD_OUT_TODAY = "SOLD_OUT_TODAY"
DRAINAGE_ABOUT_TO_OPEN = "ABOUT_TO_OPEN"
DRAINAGE_NOT_ON_SALE = "NOT_ON_SALE"


class BiliApiError(RuntimeError):
    """接口返回非 0 或网络失败。response_text 保存原始现场。"""

    def __init__(self, message: str, code: int | None = None,
                 response_text: str = ""):
        super().__init__(message)
        self.code = code
        self.response_text = response_text


class BiliClient:
    def __init__(self, timeout: float = 10.0, run_logger=None):
        self.timeout = timeout
        self.run_logger = run_logger or NullRunLogger()
        self.phase = "init"   # 由 flows 层更新，写入每条 HTTP 日志
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": settings.APP_UA,
            "Referer": "https://www.bilibili.com/blackboard/era/rZPKSDqrJEOrtkVi.html",
        })

    # ------------------------------------------------------------------ util
    def _request(self, method: str, url: str, extra_params: dict | None = None,
                 data: dict | None = None, json_body: dict | None = None,
                 bare_query: dict | None = None) -> dict:
        """发请求并解析 envelope。返回 dict；code != 0 抛 BiliApiError。

        默认走 App 签名参数；传 bare_query 则按原样作为 query（部分
        dokodemoDoor 接口不签名，仅带 build/mobi_app 等）。
        每次调用（含网络异常）都写 HTTP 审计日志。
        """
        if bare_query is not None:
            params = dict(bare_query)
        else:
            params = settings.build_common_params()
            if extra_params:
                params.update(extra_params)
            params = signed_params(params, settings.APP_KEY, settings.APP_SEC)
        headers = {"Cookie": settings.build_cookie_header()}
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        t0 = time.monotonic()
        resp = None
        try:
            resp = self.session.request(
                method, url, params=params, data=data, json=json_body,
                headers=headers, timeout=self.timeout,
            )
            elapsed = (time.monotonic() - t0) * 1000
            text = resp.text
            payload = None
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            # 单条审计：HTTP 状态与接口 code/message 一起落盘
            self.run_logger.log_http(
                phase=self.phase, method=method, url=url, params=params,
                data=data, json_body=json_body, status_code=resp.status_code,
                api_code=payload.get("code") if payload else None,
                message=payload.get("message", "") if payload else "",
                response_text=text[:4000], elapsed_ms=elapsed)
            if resp.status_code != 200:
                raise BiliApiError(
                    f"HTTP {resp.status_code} {url}", code=resp.status_code,
                    response_text=text)
        except requests.RequestException as ex:
            elapsed = (time.monotonic() - t0) * 1000
            self.run_logger.log_http(
                phase=self.phase, method=method, url=url, params=params,
                data=data, json_body=json_body, status_code=None,
                error=str(ex), elapsed_ms=elapsed)
            raise BiliApiError(f"网络失败: {ex}") from ex

        if payload is None:
            raise BiliApiError(f"响应非 JSON: {url}", response_text=text)

        code = payload["code"]
        if code != 0:
            raise BiliApiError(
                f"接口报错 code={code} message={payload.get('message')} {url}",
                code=code, response_text=text)
        return payload.get("data") or {}

    # ------------------------------------------------------------------ APIs
    def get_attract_card(self) -> dict:
        """引流卡状态：drainage_status / next_open_at / current_time / isReserved / has_buy。"""
        return self._request(
            "GET", settings.URL_ATTRACT_CARD,
            extra_params={
                "scene": "activity",
                "web_location": "888.153775",
                "new_act_panel": 1,
                "ext_params": json.dumps(settings.EXT_PARAMS, separators=(",", ":")),
                "statistics": json.dumps(
                    {"appId": 1, "platform": 3, "version": "9.11.0", "abtest": ""},
                    separators=(",", ":")),
            })

    def get_server_time(self) -> int:
        """以 attract_card 的 current_time 作为服务器时钟。"""
        return int(self.get_attract_card()["current_time"])

    def reserve(self) -> dict:
        """一键预约（写操作）。data 里 csrf 必带。"""
        return self._request(
            "POST", settings.URL_RESERVE,
            data={
                "activity_code": "summer2026",
                "csrf": settings.CSRF,
            })

    def get_buy_component(self) -> dict:
        """买赠组件信息：buySets（目标套餐 token 的 hasBuy 资格）。

        与 App 抓包一致：POST JSON、query 不签名（仅 build/mobi_app 等）。
        只查询目标套餐（买1年得5年）的资格——其他套餐一律不查不买。
        """
        import time as _time
        return self._request(
            "POST", settings.URL_BUY_COMPONENT,
            bare_query={
                "build": settings.BUILD,
                "mobi_app": settings.MOBI_APP,
                "platform": settings.PLATFORM,
                "channel": "xiaomi_cn_tv.danmaku.bili_20210930",
                "csrf": settings.CSRF,
            },
            json_body={
                "activityCode": "moe2026",
                "inputMap": {
                    "buyId": "moe2026",
                    "pageCode": "sub",
                    "skus": [
                        {"actToken": settings.TARGET_PLANS[0]["act_token"],
                         "type": "tv"},
                    ],
                    "shareNo": None,
                },
                "ts": int(_time.time()),
            })

    def create_order(self, plan: dict) -> dict:
        """下单锁名额。plan 见 settings.TARGET_PLANS。

        返回订单信息（含 order_no 等）；成功即锁定名额，需在时限内支付。
        """
        data = {
            "act_token": plan["act_token"],
            "appId": plan["app_id"],
            "appSubId": plan["app_sub_id"],
            "months": plan["months"],
            "orderType": plan["order_type"],
            "panel_type": plan["panel_type"],
            "product_type": plan.get("product_type", "1"),
            "from_activity": 1,
            "pay_sdk_version": "1.5.4",
            "dtype": 3,  # 3=APP（ur 枚举：H5:2, APP:3）
            "csrf": settings.CSRF,
        }
        return self._request("POST", settings.URL_CREATE_ORDER, data=data)

    def get_order_status(self, order: dict, app_id: str = "241") -> dict:
        """下单后校验订单状态（双重校验：create 成功 -> status 确认）。

        order_no 的字段名在不同返回结构里不统一，做兜底提取；
        提不到时返回空 dict（不阻塞主流程）。
        """
        order_no = self.extract_order_no(order)
        if not order_no:
            return {}
        return self._request("GET", settings.URL_ORDER_STATUS,
                             extra_params={"order_no": order_no,
                                           "app_id": app_id})

    @staticmethod
    def extract_order_no(order) -> str:
        if not isinstance(order, dict):
            return ""
        for key in ("order_no", "orderNo", "orderId"):
            if order.get(key):
                return str(order[key])
        for sub_key in ("payParams", "payParam", "pay_params", "pay_param"):
            sub = order.get(sub_key)
            if isinstance(sub, dict):
                for key in ("order_no", "orderId"):
                    if sub.get(key):
                        return str(sub[key])
        return ""

    # ------------------------------------------------------------------ misc
    def check_login(self) -> bool | None:
        """凭证体检：App 端账号接口验证登录态（attract_card 匿名也返回 0，
        不能区分凭证有效性，必须用需要登录的 myinfo）。

        返回 True=有效 / False=凭证失效 / None=网络或风控层异常，无法判定。
        """
        self.phase = "credcheck"
        try:
            self._request("GET", settings.URL_MY_INFO)
        except BiliApiError as ex:
            # 接口业务码为负（-101/-400）或 61000 这类大码 → 凭证问题；
            # 100~599 是 HTTP 层错误（412 风控等），code=None 是网络失败，
            # 这两种不能断定凭证失效
            if ex.code is not None and (ex.code < 0 or ex.code >= 1000):
                return False
            return None
        return True

    def prewarm(self, connections: int = 1) -> float | None:
        """开抢前预热连接：提前完成 DNS + TCP + TLS，进 Session 连接池。

        connections>1 时并发预热多条连接（填池，防单连接偶发握手失败
        耽误第一发）。返回最慢一次成功预热的耗时（秒），全失败返回
        None；耗时供抢购层自适应第一发提前量。预热失败不影响正式请求。
        """
        n = max(1, int(connections))
        results: list = [None] * n

        def _one(i: int):
            t0 = time.monotonic()
            try:
                self.session.head(settings.API_BASE + "/", timeout=2)
                results[i] = time.monotonic() - t0
            except Exception:
                pass

        if n == 1:
            _one(0)
        else:
            import threading
            threads = [threading.Thread(target=_one, args=(i,))
                       for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        vals = [v for v in results if v is not None]
        return max(vals) if vals else None

    def pay_link(self, plan: dict) -> str:
        """H5 收银台链接（下单成功后手动打开支付）。"""
        return (
            f"https://big.bilibili.com/mobile/activityPay"
            f"?act_token={plan['act_token']}&app_id={plan['app_id']}"
            f"&panel_type={plan['panel_type']}&months={plan['months']}"
            f"&order_type={plan['order_type']}&app_sub_id={plan['app_sub_id']}")
