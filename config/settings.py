# -*- coding: utf-8 -*-
"""B站联合会员抢购 - 全局配置。

凭证读取优先级：环境变量 > config/secrets.py（本地私有，不入 git）。
两者都缺失时凭证为空串，实际请求会收到 -101，分类器会提示更新凭证。

邮件配置额外支持 config/notify.json（仪表盘网页保存生成），
优先级：环境变量 > notify.json > secrets.py。
"""
import copy
import json as _json
import os
from pathlib import Path

# 仪表盘保存的邮件配置文件（含授权码，不入 git/镜像）
NOTIFY_JSON_PATH = Path(__file__).resolve().parent / "notify.json"
# 仪表盘保存/扫码登录刷新的登录凭证文件（不入 git/镜像）
CREDENTIALS_JSON_PATH = Path(__file__).resolve().parent / "credentials.json"

try:
    from config import secrets as _secrets
except ImportError:   # 未创建 secrets.py 时退化为空凭证
    class _secrets:   # noqa: N801 - 占位对象
        ACCESS_KEY = ""
        CSRF = ""
        BILI_JCT = ""
        SESSDATA = ""
        DEDE_USER_ID = ""
        SMTP_HOST = ""
        SMTP_PORT = 465
        SMTP_USER = ""
        SMTP_PASS = ""
        NOTIFY_TO = ""


def _notify_json() -> dict:
    try:
        return _json.loads(NOTIFY_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _credentials_json() -> dict:
    try:
        return _json.loads(CREDENTIALS_JSON_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _cred(env_name: str, attr: str, json_key: str | None = None) -> str:
    val = os.environ.get(env_name)
    if val:
        return val
    if json_key:
        source = (_credentials_json() if json_key.startswith("bili_")
                  else _notify_json())
        val = source.get(json_key)
        if val:
            return str(val)
    return getattr(_secrets, attr)


# ----------------------------------------------------------------------------
# 登录凭证（环境变量或 config/secrets.py，本文件不再存放明文）
# ----------------------------------------------------------------------------
ACCESS_KEY = _cred("BILI_ACCESS_KEY", "ACCESS_KEY", "bili_access_key")
CSRF = _cred("BILI_CSRF", "CSRF", "bili_csrf")
BILI_JCT = _cred("BILI_BILI_JCT", "BILI_JCT", "bili_jct")
SESSDATA = _cred("BILI_SESSDATA", "SESSDATA", "bili_sessdata")
DEDE_USER_ID = _cred("BILI_DEDE_USER_ID", "DEDE_USER_ID", "bili_uid")

# ----------------------------------------------------------------------------
# 邮件推送（环境变量 > notify.json（仪表盘）> secrets.py；齐备才启用）
# ----------------------------------------------------------------------------
SMTP_HOST = _cred("BILI_SMTP_HOST", "SMTP_HOST", "host")
SMTP_PORT = int(_cred("BILI_SMTP_PORT", "SMTP_PORT", "port") or 465)
SMTP_USER = _cred("BILI_SMTP_USER", "SMTP_USER", "user")
SMTP_PASS = _cred("BILI_SMTP_PASS", "SMTP_PASS", "pass")
NOTIFY_TO = _cred("BILI_NOTIFY_TO", "NOTIFY_TO", "to") or SMTP_USER

# ----------------------------------------------------------------------------
# App 签名参数（B站安卓客户端公开 appkey/appsec）
# ----------------------------------------------------------------------------
APP_KEY = "1d8b6e7d45233436"
APP_SEC = "560c52ccd288fed045859ed18bffd973"
BUILD = "9110400"
MOBI_APP = "android"
PLATFORM = "android"
APP_UA = (
    "Mozilla/5.0 (Linux; Android 16; 24122RKC7C Build/BP2A.250605.031.A3; wv) "
    "AppleWeb Kit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/129.0.0.0 Mobile "
    "Safari/537.36 BiliDroid/9110400 (bbcallen@gmail.com) os/android model/24122RKC7C "
    "mobi_app/android build/9110400 channel/xiaomi_cn_tv.danmaku.bili"
)

# ----------------------------------------------------------------------------
# 接口地址
# ----------------------------------------------------------------------------
API_BASE = "https://api.bilibili.com"
URL_ATTRACT_CARD = f"{API_BASE}/x/vip/activity/sale/summer2026/attract_card"
URL_RESERVE = f"{API_BASE}/x/vip/activity/sale/summer2026/reserve"
URL_BUY_COMPONENT = f"{API_BASE}/pgc/activity/dokodemoDoor/getEasy/moe2026_buyComponentEvo_info"
URL_CREATE_ORDER = f"{API_BASE}/x/vip/order/create/activity"
URL_ORDER_STATUS = f"{API_BASE}/x/vip/order/status"

# 引流卡组件要求的 ext_params（缺它会报 -400 ext_params缺少必要字段）
EXT_PARAMS = {
    "activity_id": "3ERAcwloghvy1e00",
    "component_id": "iiVsSuDEJK",
    "is_preview": False,
    "creative_id": 0,
    "new_act_panel": 1,
}

# ----------------------------------------------------------------------------
# 目标套餐（开售后依次尝试，第一个锁单成功即停）
#
# 178 元"买1年得5年"限量套餐有两个候选入口：
#   1) BuyGiveRewards 买赠组件的 tv 超级大会员（云视听小电视，截图"超大会员372天"），
#      资格 token 在 buyComponentEvo_info 的 buySets 中返回；
#   2) OgvVipBuyBase 的 26moe_xny178 年卡按钮。
# orderType: 1=连续包年(cdd178 是 isContinuous) 0=单次(xny178 renew=false)
# ----------------------------------------------------------------------------
TARGET_PLANS = [
    {
        "name": "178超级大会员(买1年得5年, 连续包年)",
        "act_token": "987488888720260824193333",
        "app_id": "241",
        "app_sub_id": "26moe_fhc",
        "panel_type": "26moe_cdd178",
        "months": 12,
        "order_type": 1,
        "product_type": "1",
    },
    {
        "name": "178年卡(26moe_xny178, 单次)",
        "act_token": "748184168320260824193741",
        "app_id": "241",
        "app_sub_id": "26moe_fhc",
        "panel_type": "26moe_xny178",
        "months": 12,
        "order_type": 0,
        "product_type": "1",
    },
]

# ----------------------------------------------------------------------------
# 抢购节奏（借鉴 glm-rush：三段自适应间隔 + 抖动 + 多重熔断）
#
#   开售瞬间拼手速（爆发段小间隔）-> 黄金窗口快速重试 ->
#   库存大概率没了转慢速省请求防风控；间隔随机抖动避免匀速请求特征。
#   注意：App 签名 + 同账号场景不适合多路并发同一下单（会重复下单），
#   用爆发间隔换速度而非并发路数。
# ----------------------------------------------------------------------------
RUSH_EARLY_SECONDS = 0.6      # 提前量：开售前多少秒发出第一发
RUSH_BURST_COUNT = 8          # 前 N 次为爆发段
RUSH_BURST_INTERVAL = 0.06    # 爆发段间隔（秒）
RUSH_FAST_WINDOW = 10.0       # 开售后 N 秒内为快速段
RUSH_FAST_INTERVAL = 0.12     # 快速段间隔
RUSH_SLOW_INTERVAL = 0.30     # 慢速段间隔
RUSH_JITTER = 0.30            # 间隔抖动 ±30%
RUSH_DURATION_SECONDS = 90.0  # 开售后最长坚持时长
RUSH_MAX_ATTEMPTS = 400       # 总尝试上限（熔断）
RUSH_SYSERR_PAUSE = 5         # 连续 N 次系统错误（网络/5xx）触发冷却
RUSH_SYSERR_COOLDOWN = 2.0    # 冷却时长（秒）
SOLD_OUT_LINGER_SECONDS = 3.0 # 收到"售罄"后再坚持几秒

# 时间校准
TIME_SYNC_SAMPLES = 5         # 采样次数，取中位数作为时钟偏移


def build_common_params() -> dict:
    """App 端公共参数（不含签名，sign 由 core.signer 补充）。"""
    return {
        "access_key": ACCESS_KEY,
        "appkey": APP_KEY,
        "build": BUILD,
        "mobi_app": MOBI_APP,
        "platform": PLATFORM,
        "csrf": CSRF,
        "disable_rcmd": 0,
    }


def build_cookie_header() -> str:
    return (
        f"SESSDATA={SESSDATA}; bili_jct={BILI_JCT}; "
        f"DedeUserID={DEDE_USER_ID}"
    )


def plan_payloads() -> list:
    """返回 TARGET_PLANS 的深拷贝列表，供流程层修改。"""
    return copy.deepcopy(TARGET_PLANS)
