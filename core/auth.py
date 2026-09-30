# -*- coding: utf-8 -*-
"""凭证存取与 HAR 导入。

凭证保存到 config/credentials.json（字段级合并，本地私有文件），
读取优先级与环境变量/secrets.py 的关系见 config/settings.py。
唯一更新入口=仪表盘「上传HAR」（TV 扫码凭证不被活动接口认可，已删除）。
"""
import json
import logging
import os
import time

import requests

from config import settings
from core.signer import app_sign

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------- HAR 导入
def parse_har_credentials(har_text: str) -> dict:
    """从 HAR JSON 文本提取全套登录凭证（App 抓包/浏览器导出皆可）。

    提取规则（按用户要求的"明确来源接口"语义）：
      - access_key：取含该 query 参数的请求，并记录其接口为来源；
        同一请求 query 里的 csrf 一并采用（App 签名请求惯例）。
      - Cookie 三件套（SESSDATA/bili_jct/DeduUserID）：取全 HAR 中
        出现的值（同一抓包会话内唯一）。
      - csrf 若无独立来源，与 bili_jct 同值配套（B 站惯例）。
    返回 {creds: {bili_* 键, 仅含提取到的字段}, source_api: str,
    missing: [缺失字段名]}。不完整时仍返回已提取部分，由调用方决定。
    """
    from urllib.parse import urlparse, parse_qs

    try:
        har = json.loads(har_text)
        entries = har["log"]["entries"]
    except Exception as ex:
        return {"creds": {}, "source_api": "", "missing": ["all"],
                "error": f"HAR 解析失败: {ex}"[:200]}

    best_ak, best_csrf, source_api = "", "", ""
    cookie: dict = {}
    for e in entries:
        req = e.get("request") or {}
        try:
            q = parse_qs(urlparse(req.get("url", "")).query)
        except Exception:
            q = {}
        ak = (q.get("access_key") or [None])[0]
        if ak:
            best_ak = ak
            source_api = (req.get("method", "GET") + " "
                          + req.get("url", "").split("?")[0])
            if q.get("csrf"):
                best_csrf = q["csrf"][0]
        for h in req.get("headers", []):
            if str(h.get("name", "")).lower() == "cookie":
                for part in str(h.get("value", "")).split(";"):
                    k, _, v = part.strip().partition("=")
                    if k in ("SESSDATA", "bili_jct", "DedeUserID") and v:
                        cookie[k] = v

    creds: dict = {}
    if best_ak:
        creds["bili_access_key"] = best_ak
    csrf = best_csrf or cookie.get("bili_jct", "")
    if csrf:
        creds["bili_csrf"] = csrf
    if cookie.get("SESSDATA"):
        creds["bili_sessdata"] = cookie["SESSDATA"]
    if cookie.get("bili_jct"):
        creds["bili_jct"] = cookie["bili_jct"]
    if cookie.get("DedeUserID"):
        creds["bili_uid"] = cookie["DedeUserID"]

    want = ["bili_access_key", "bili_csrf", "bili_sessdata",
            "bili_jct", "bili_uid"]
    missing = [k for k in want if not creds.get(k)]
    return {"creds": creds, "source_api": source_api, "missing": missing}


def import_har_credentials(har_text: str) -> dict:
    """解析 HAR 并落盘保存。返回 save 视图 + 来源接口。"""
    parsed = parse_har_credentials(har_text)
    if parsed.get("error"):
        return parsed
    if not parsed["creds"]:
        return {"error": "HAR 中未找到任何凭证(access_key/Cookie 均缺)",
                "missing": parsed["missing"]}
    view = save_credentials(parsed["creds"], source="HAR上传")
    view["source_api"] = parsed["source_api"]
    view["missing"] = parsed["missing"]
    view["updated_fields"] = sorted(parsed["creds"].keys())
    return view
