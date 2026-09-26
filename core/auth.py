# -*- coding: utf-8 -*-
"""B站 TV 端扫码登录：一次扫码拿到全套凭证。

协议（passport-tv-login，appkey/appsec 为 TV 客户端公开参数）：
    1. POST /x/passport-tv-login/qrcode/authcode -> auth_code（二维码内容）
    2. 用户用手机 B站 App 扫码并确认
    3. POST /x/passport-tv-login/qrcode/poll 轮询：
       code=0     成功 -> access_token + cookie_info(SESSDATA/bili_jct/...)
       code=86090 已扫码待确认
       code=86038 二维码已失效

凭证保存到 config/credentials.json（字段级合并，本地私有文件），
读取优先级与环境变量/secrets.py 的关系见 config/settings.py。
"""
import json
import logging
import os
import time

import requests

from config import settings
from core.signer import app_sign

logger = logging.getLogger(__name__)

TV_APP_KEY = "4409e2ce8ffd12b8"
TV_APP_SEC = "59b43e04ad6965f34319062b478f83dd"
LOCAL_ID = "3333333"

URL_AUTHCODE = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
URL_POLL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"

POLL_WAITING = 86039     # 等待扫码
POLL_SCANNED = 86090     # 已扫码，等待确认
POLL_EXPIRED = 86038     # 二维码失效

# credentials.json 键 <-> settings 属性 / 环境变量
CRED_FIELDS = {
    "bili_access_key": ("BILI_ACCESS_KEY", "ACCESS_KEY"),
    "bili_csrf": ("BILI_CSRF", "CSRF"),
    "bili_jct": ("BILI_BILI_JCT", "BILI_JCT"),
    "bili_sessdata": ("BILI_SESSDATA", "SESSDATA"),
    "bili_uid": ("BILI_DEDE_USER_ID", "DEDE_USER_ID"),
}

_session = requests.Session()
_session.headers.update({"User-Agent": settings.APP_UA})


class AuthError(RuntimeError):
    pass


def _tv_signed(params: dict) -> dict:
    params = dict(params, appkey=TV_APP_KEY, ts=int(time.time()))
    params["sign"] = app_sign(params, TV_APP_SEC)
    return params


def qrcode_start() -> dict:
    """申请扫码登录二维码。返回 {auth_code, url}。"""
    try:
        resp = _session.post(URL_AUTHCODE, timeout=10,
                             data=_tv_signed({"local_id": LOCAL_ID}))
        payload = resp.json()
    except Exception as ex:
        raise AuthError(f"申请二维码失败: {ex}") from ex
    if payload.get("code") != 0:
        raise AuthError(f"申请二维码失败 code={payload.get('code')} "
                        f"{payload.get('message')}")
    data = payload["data"]
    return {"auth_code": data["auth_code"], "url": data["url"]}


def qrcode_poll(auth_code: str) -> dict:
    """轮询扫码状态（前端定时调用）。

    返回 {status: waiting|scanned|success|expired|error, message, ...}
    success 时凭证已保存并携带脱敏视图。
    """
    try:
        resp = _session.post(URL_POLL, timeout=10, data=_tv_signed({
            "auth_code": auth_code, "local_id": LOCAL_ID}))
        payload = resp.json()
    except Exception as ex:
        return {"status": "error", "message": f"轮询失败: {ex}"}

    code = payload.get("code")
    if code == 0:
        creds = _extract_credentials(payload.get("data") or {})
        save_credentials(creds, source="扫码登录")
        view = credentials_view()
        view["status"] = "success"
        view["message"] = "登录成功，凭证已更新"
        return view
    if code == POLL_WAITING:
        return {"status": "waiting", "message": "等待扫码..."}
    if code == POLL_SCANNED:
        return {"status": "scanned", "message": "已扫码，请在手机上确认"}
    if code == POLL_EXPIRED:
        return {"status": "expired", "message": "二维码已失效，请重新生成"}
    return {"status": "error",
            "message": f"code={code} {payload.get('message', '')}"}


def _extract_credentials(data: dict) -> dict:
    """从 poll 成功响应提取全套凭证（缺什么存什么）。"""
    creds = {}
    if data.get("access_token"):
        creds["bili_access_key"] = data["access_token"]
    cookies = ((data.get("cookie_info") or {}).get("cookies")
               or data.get("cookie_info") or [])
    if isinstance(cookies, list):
        jar = {c.get("name"): c.get("value") for c in cookies
               if isinstance(c, dict)}
    else:
        jar = {}
    if jar.get("SESSDATA"):
        creds["bili_sessdata"] = jar["SESSDATA"]
    if jar.get("bili_jct"):
        # TV poll 的 bili_jct 同时充当 Web csrf 与 App csrf 的最佳近似
        creds["bili_jct"] = jar["bili_jct"]
        creds.setdefault("bili_csrf", jar["bili_jct"])
    if jar.get("DedeUserID"):
        creds["bili_uid"] = jar["DedeUserID"]
    return creds


def reload_credentials_into_settings():
    """常驻进程每轮抢购前调用：按 env > credentials.json > secrets.py 重载凭证。

    仪表盘/扫码更新的凭证写进 credentials.json 后，无需重启抢购进程。
    """
    from config import settings as settings_mod
    for key, (env_name, attr) in CRED_FIELDS.items():
        setattr(settings_mod, attr, settings_mod._cred(env_name, attr, key))


# ---------------------------------------------------------------- 存取
def _read_creds_json() -> dict:
    try:
        return json.loads(settings.CREDENTIALS_JSON_PATH.read_text(
            encoding="utf-8"))
    except Exception:
        return {}


def save_credentials(creds: dict, source: str = "仪表盘") -> dict:
    """字段级合并保存凭证到 credentials.json，并让本进程立即生效。

    只有非空字段会被写入；传空串表示删除该键（回落本地 secrets.py）。
    """
    from config import settings as settings_mod

    merged = _read_creds_json()
    if not isinstance(merged, dict):
        merged = {}
    for key in CRED_FIELDS:
        val = creds.get(key)
        if val is None:
            continue
        val = str(val).strip()
        if val:
            merged[key] = val
        else:
            merged.pop(key, None)
    merged["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    merged["updated_by"] = source
    settings.CREDENTIALS_JSON_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    # 立即按 env > credentials.json > secrets.py 重算生效值
    for key, (env_name, attr) in CRED_FIELDS.items():
        setattr(settings_mod, attr, settings_mod._cred(env_name, attr, key))
    return credentials_view()


def credentials_view() -> dict:
    """凭证视图：全部脱敏（首6尾4），附来源与更新时间。"""
    def _mask(v):
        v = str(v)
        return v if len(v) <= 10 else f"{v[:6]}...{v[-4:]}"

    def _source(env_name, key):
        if os.environ.get(env_name):
            return "环境变量"
        if _read_creds_json().get(key):
            return "仪表盘/扫码"
        return "本地文件"

    js = _read_creds_json()
    return {
        "access_key": _mask(settings.ACCESS_KEY) if settings.ACCESS_KEY else "",
        "csrf": _mask(settings.CSRF) if settings.CSRF else "",
        "sessdata": _mask(settings.SESSDATA) if settings.SESSDATA else "",
        "bili_jct": _mask(settings.BILI_JCT) if settings.BILI_JCT else "",
        "uid": settings.DEDE_USER_ID,
        "set": {
            "access_key": bool(settings.ACCESS_KEY),
            "csrf": bool(settings.CSRF),
            "sessdata": bool(settings.SESSDATA),
            "bili_jct": bool(settings.BILI_JCT),
        },
        "sources": {
            "access_key": _source("BILI_ACCESS_KEY", "bili_access_key"),
            "sessdata": _source("BILI_SESSDATA", "bili_sessdata"),
        },
        "updated_at": js.get("updated_at", ""),
        "updated_by": js.get("updated_by", ""),
    }
