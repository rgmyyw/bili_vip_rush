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
# 服务暂停开关（仪表盘写入，daemon 触发点与 rush 倒计时每秒检查）
PAUSE_JSON_PATH = Path(__file__).resolve().parent / "pause.json"

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
        DINGTALK_WEBHOOK = ""
        DINGTALK_SECRET = ""


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
# 钉钉推送（选配）：机器人 webhook（可带加签 secret），齐备才启用
DINGTALK_WEBHOOK = _cred("BILI_DINGTALK_WEBHOOK", "DINGTALK_WEBHOOK",
                         "dingtalk_webhook")
DINGTALK_SECRET = _cred("BILI_DINGTALK_SECRET", "DINGTALK_SECRET",
                        "dingtalk_secret")

# ----------------------------------------------------------------------------
# App 签名参数（B站安卓客户端公开 appkey/appsec）
# ----------------------------------------------------------------------------
APP_KEY = "1d8b6e7d45233436"
APP_SEC = "560c52ccd288fed045859ed18bffd973"
BUILD = "9110400"   # 09-27 实战抢成功的组合,抢购链路参数冻结不动
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
# 凭证体检探针：App 端账号接口（无/失效凭证返回 code=61000 或 -400/-101，
# 有效返回 0）。attract_card 匿名也返回 0，不能用作体检。
URL_MY_INFO = "https://app.bilibili.com/x/v2/account/myinfo"
URL_ATTRACT_CARD = f"{API_BASE}/x/vip/activity/sale/summer2026/attract_card"
# 预约:App 实测(HTTP/2 authority=api 网关)形态——api 域 + 不签名 +
# JSON body + 活动页 H5 头(app-key/native_api_from/referer 等);big 域
# 从外部直连 404,App 实际也走 api 网关
URL_RESERVE = f"{API_BASE}/x/vip/activity/sale/summer2026/reserve"
RESERVE_BUILD = "9130500"   # App 实测 build;仅预约用,抢购 BUILD 冻结
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
# 目标套餐：唯一、只抢这一个，禁止任何备选/兜底购买。
#
# 26moe_cdd178 = 178 元"买1年得5年"超级大会员（含 QQ音乐豪华年卡/知乎年卡/
# 肯德基大神卡/京东plus 各一年，云视听小电视 tv 组件，isContinuous 连续包年）。
# 资格 token 在 buyComponentEvo_info 的 buySets 中返回。
# 抢不到就是抢不到，绝不落到其他 178 套餐（见 ALLOWED_PANEL_TYPES 硬闸）。
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
    # 2026-09-28 删除 26moe_xny178(178年卡N选1)备选：备选会在首选无货时
    # 落单买错套餐（09-27 实际买到 xny178，用户损失 178 元）。
    # 只抢 cdd178；恢复备选必须同时改 ALLOWED_PANEL_TYPES。
]

# 下单硬性白名单：panel_type 不在此清单的套餐，抢购循环直接拒绝下单并
# 记录 panel_blocked 事件。这是独立于 TARGET_PLANS 的第二道闸——即使配置
# 被误改/上游同步带回备选，也不会买错套餐。要新增可买套餐须显式改这里。
ALLOWED_PANEL_TYPES = {"26moe_cdd178"}

# ----------------------------------------------------------------------------
# 抢购节奏（借鉴 glm-rush：三段自适应间隔 + 抖动 + 多重熔断）
#
#   开售瞬间拼手速（爆发段小间隔）-> 黄金窗口快速重试 ->
#   库存大概率没了转慢速省请求防风控；间隔随机抖动避免匀速请求特征。
#   注意：App 签名 + 同账号场景不适合多路并发同一下单（会重复下单），
#   用爆发间隔换速度而非并发路数。
# ----------------------------------------------------------------------------
RUSH_EARLY_SECONDS = 0.5      # 提前量:开售前 0.5s 起打(11:59:59.5,开售前仅 1-2 发探测)
RUSH_EARLY_ADAPTIVE = True    # 按预热实测往返自适应提前量(0.4~0.6s)
RUSH_CONCURRENCY = 10         # 开售瞬间并发路数(单IP有效并发~10-20,更高被网关丢弃反降有效量)
PREWARM_CONNECTIONS = 2       # 每路预热连接数(填连接池,防单连接偶发失败)
RUSH_BURST_COUNT = 8          # 前 N 次为爆发段
RUSH_BURST_INTERVAL = 0.04    # 爆发段间隔（秒）
RUSH_WAVE_DELAYS = (0.0, 0.06, 0.12)  # 阶梯相位密化:黄金窗仅1.3s,10路须全在场均匀铺开
RUSH_FAST_WINDOW = 10.0       # 开售后 N 秒内为快速段
RUSH_FAST_INTERVAL = 0.12     # 快速段间隔
RUSH_SLOW_INTERVAL = 0.30     # 慢速段间隔
RUSH_JITTER = 0.30            # 间隔抖动 ±30%
RUSH_MAX_ATTEMPTS = 1000      # 总尝试上限（熔断）
RUSH_RISK_PAUSE = 10          # 连续 N 次 412/403 风控拦截即全局停抢
RUSH_THROTTLE_MAX_S = 2.0     # -702 频控自适应退避上限(占比100%时每路退避2s)
# 时间分段火力(距开售秒数,负=提前):黄金窗豁免节流全速;尾段降密度;到点收兵
RUSH_PEAK_FROM = -0.05        # 黄金窗起点(开售后才爆发;10-01 实战证明
                               # 开售前全速全是 69422 无效响应还烧频控信用)
RUSH_PROBE_INTERVAL = 0.3     # 探测-爆发:开售前仅 worker-0 以此间隔低频
                               # 探测(~3发),其余路休眠到开售瞬间满血爆发
RUSH_PEAK_TO = 0.8            # 黄金窗终点(开售后0.8s)
RUSH_TAIL_FROM = 2.0          # 尾段起点(降密度扫尾,捡锁单释放回流)
RUSH_PEAK_DENSITY = 4.5       # 黄金窗贴线密度:10路x(40ms*4.5)=55发/秒
                               # ≈账号频控阈值(60发),整个窗口每发有效;
                               # 全速250发/秒0.24s烧完额度后哑火10s(10-01实证)
RUSH_TAIL_DENSITY = 12.0      # 尾段间隔倍率(~20发/秒低密度;频控惩罚期
                               # >10s,高密度纯浪费额度)
RUSH_TAIL_STOP_S = 10.0       # 开售后 N 秒主动收兵(66名额早尽,继续打只烧频控)
RUSH_DURATION_SECONDS = 14.0  # 总时长兜底(提前2s+黄金+尾段)
RUSH_THROTTLE_WINDOW = 30     # 节流判定窗口(最近 N 发)
RUSH_PREWARM_PARALLEL = 32    # 多路预热线程池大小(串行预热在多路下来不及)
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
