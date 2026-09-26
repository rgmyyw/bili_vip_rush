# -*- coding: utf-8 -*-
"""secrets.py 模板：复制为 config/secrets.py 并填入自己的抓包凭证。

获取方式：
  - ACCESS_KEY / CSRF：安卓 App 抓包任意 api.bilibili.com 请求的 query 参数
  - SESSDATA / BILI_JCT / DEDE_USER_ID：网页 Cookie（或 App 响应头 Set-Cookie）

注意：config/secrets.py 已被 .gitignore 排除；也可不建此文件，
改用环境变量 BILI_ACCESS_KEY / BILI_CSRF / BILI_SESSDATA / BILI_BILI_JCT /
BILI_DEDE_USER_ID 注入（Docker 部署推荐）。
"""
ACCESS_KEY = ""
CSRF = ""
BILI_JCT = ""
SESSDATA = ""
DEDE_USER_ID = ""
