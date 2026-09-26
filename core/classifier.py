# -*- coding: utf-8 -*-
"""下单失败响应分类器（借鉴 rush-glm-codingplan 的五态模型）。

抢购循环里每次失败只问一句"这是什么性质的失败"，由这里集中回答。
隔天复盘迭代时，新错误码/新风控提示只需改这一个文件并补单测。

分类结果决定 rush 循环行为：
    CREDENTIAL_EXPIRED -> 立即停止（重试无意义，提示更新抓包凭证）
    ALREADY_DONE       -> 立即停止（已有订单/已购买）
    SOLD_OUT           -> 售罄 linger 计时（短暂坚持后停止）
    SYSTEM_ERROR       -> 连续计数，达阈值冷却
    RETRY              -> 继续下一轮（默认）
"""
from enum import Enum, auto

# 命中这些关键词视为"本轮没抢到"
SOLD_OUT_WORDS = ("售罄", "抢完", "已抢完", "卖完", "SOLD_OUT")
# 命中这些关键词说明名额已到手或已有订单
ALREADY_WORDS = ("已购", "已有", "重复", "限购")
# 登录态/凭证失效特征
CREDENTIAL_CODES = (-101, -111)          # 未登录 / csrf 校验失败
CREDENTIAL_WORDS = ("未登录", "请先登录", "access_key", "登录失效", "账号未登录")


class Outcome(Enum):
    SOLD_OUT = auto()
    ALREADY_DONE = auto()
    CREDENTIAL_EXPIRED = auto()
    SYSTEM_ERROR = auto()
    RETRY = auto()


def classify_error(ex) -> Outcome:
    """把 BiliApiError 分类为 Outcome。

    ex.code: None=网络失败/非JSON；>=500=HTTP状态码；其余为 B站接口 code。
    """
    code = getattr(ex, "code", None)
    msg = str(ex)

    if code in CREDENTIAL_CODES or any(w in msg for w in CREDENTIAL_WORDS):
        return Outcome.CREDENTIAL_EXPIRED
    if any(w in msg for w in ALREADY_WORDS):
        return Outcome.ALREADY_DONE
    if any(w in msg for w in SOLD_OUT_WORDS):
        return Outcome.SOLD_OUT
    if code is None or code >= 500:
        return Outcome.SYSTEM_ERROR
    return Outcome.RETRY
