# -*- coding: utf-8 -*-
"""抢购仪表盘：实时状态 + 运行记录 + 调用明细。

零第三方依赖（标准库实现），与抢购脚本共用 config/secrets.py 凭证。

用法：
    python tools/dashboard.py            # 127.0.0.1:8777
    python tools/dashboard.py --port 9000 --host 0.0.0.0
打开 http://127.0.0.1:8777
"""
import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.auth import (                     # noqa: E402
    credentials_view,
    qrcode_poll,
    qrcode_start,
    save_credentials,
)
from core.client import BiliClient          # noqa: E402
from core.logstats import (                 # noqa: E402
    failure_breakdown,
    list_runs,
    load_rows,
)
from core.notify import (                   # noqa: E402
    notify_config_view,
    save_notify_config,
    send_mail,
)
from core.run_logger import NullRunLogger   # noqa: E402

LOGS_DIR = ROOT / "logs"

# ---------------------------------------------------------------- 实时状态
_status_lock = threading.Lock()
_live = {"ts": 0, "ok": None, "data": {}, "error": ""}


def refresh_live(max_age_s: float = 4.0) -> dict:
    """查询 attract_card 实时状态（结果缓存几秒，多请求共享）。"""
    with _status_lock:
        if _live["ok"] is not None and time.time() - _live["ts"] < max_age_s:
            return dict(_live)
    client = BiliClient(run_logger=NullRunLogger(), timeout=8)
    try:
        data = client.get_attract_card()
        snap = {"ts": time.time(), "ok": True, "data": data, "error": ""}
    except Exception as ex:
        snap = {"ts": time.time(), "ok": False, "data": {},
                "error": f"{ex}"[:200]}
    with _status_lock:
        _live.update(snap)
    return dict(snap)


# ---------------------------------------------------------------- HTTP 服务
PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>B站联合会员抢购仪表盘</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--line:#262b36;--fg:#dfe4ec;--dim:#8b93a3;
       --ok:#3fb96f;--warn:#e0a63f;--bad:#e0564f;--accent:#4f8ef7}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:14px/1.5 "Segoe UI",system-ui,sans-serif;padding:20px}
 h1{font-size:18px;margin:0 0 16px} h1 small{color:var(--dim);font-weight:400}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
   gap:12px;margin-bottom:18px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
   padding:12px 14px}
 .k{color:var(--dim);font-size:12px} .v{font-size:20px;font-weight:600;margin-top:4px}
 .v.ok{color:var(--ok)} .v.bad{color:var(--bad)} .v.warn{color:var(--warn)}
 table{width:100%;border-collapse:collapse;background:var(--card);
   border:1px solid var(--line);border-radius:10px;overflow:hidden}
 th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
 th{color:var(--dim);font-weight:500;font-size:12px}
 tr:last-child td{border-bottom:none} tr.runrow{cursor:pointer}
 tr.runrow:hover{background:#1d2230}
 .tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
 .tag.success{background:#173a27;color:var(--ok)}
 .tag.sold_out{background:#3a2a17;color:var(--warn)}
 .tag.credential_expired{background:#3a1717;color:var(--bad)}
 .tag.timeout,.tag.max_attempts,.tag.already_owned{background:#26303f;color:var(--accent)}
 .tag.done,.tag.crashed{background:#262b36;color:var(--dim)}
 a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
 #detail{margin-top:18px;display:none}
 .fails{margin:10px 0;padding:10px 12px;background:var(--card);
   border:1px solid var(--line);border-radius:10px;font-size:13px}
 .muted{color:var(--dim)} code{background:#222834;padding:1px 5px;border-radius:4px}
 button{background:var(--card);color:var(--fg);border:1px solid var(--line);
   border-radius:8px;padding:6px 14px;cursor:pointer}
 button:hover{border-color:var(--accent)}
 input{width:100%;background:#10131a;color:var(--fg);border:1px solid var(--line);
   border-radius:6px;padding:7px 9px;margin-top:4px;font:inherit}
 input:focus{outline:none;border-color:var(--accent)}
</style></head><body>
<h1>B站联合会员抢购仪表盘 <small id="clock"></small></h1>
<div class="grid" id="live"></div>
<h1 style="margin-top:6px">抢购记录 <span class="muted" id="runcount"></span></h1>
<div class="card" id="notifycard" style="margin-bottom:18px">
 <div style="display:flex;justify-content:space-between;align-items:center">
  <div><b>邮件通知</b> <span id="nstate" class="tag done">加载中</span>
    <span class="muted" id="nsummary" style="margin-left:8px"></span></div>
  <button onclick="toggleForm()">配置</button>
 </div>
 <div id="nform" style="display:none;margin-top:12px">
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px">
   <div><div class="k">SMTP 服务器</div><input id="f_host" placeholder="smtp.qq.com"></div>
   <div><div class="k">端口 (465=SSL, 587=STARTTLS)</div><input id="f_port" type="number" value="465"></div>
   <div><div class="k">发件邮箱</div><input id="f_user" placeholder="you@qq.com"></div>
   <div><div class="k">SMTP 授权码</div><input id="f_pass" type="password" placeholder=""></div>
   <div><div class="k">收件邮箱 (留空=发给自己)</div><input id="f_to" placeholder="me@qq.com"></div>
  </div>
  <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
   <button onclick="saveCfg()">保存</button>
   <button onclick="testMail()">发送测试邮件</button>
   <span id="nmsg" class="muted"></span>
  </div>
 </div>
</div>
<div class="card" id="credcard" style="margin-bottom:18px">
 <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
  <div><b>登录凭证</b> <span id="cstate" class="tag done">加载中</span>
    <span class="muted" id="csummary" style="margin-left:8px"></span></div>
  <div style="display:flex;gap:8px">
   <button onclick="verifyCred()">验证</button>
   <button onclick="qrLogin()">扫码登录</button>
   <button onclick="toggleCredForm()">手动更新</button>
  </div>
 </div>
 <div id="qrbox" style="display:none;margin-top:14px;text-align:center">
  <div id="qrgrid" style="display:inline-block"></div>
  <div id="qrmsg" class="muted" style="margin-top:8px">用手机 B站 App 扫码并确认登录</div>
 </div>
 <div id="cform" style="display:none;margin-top:12px">
  <div class="muted" style="margin-bottom:8px">粘贴抓包凭证（留空=不修改该字段；与本地 secrets.py 字段级共存）</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px">
   <div><div class="k">access_key (App)</div><input id="c_access_key"></div>
   <div><div class="k">csrf (App)</div><input id="c_csrf"></div>
   <div><div class="k">SESSDATA (Web)</div><input id="c_sessdata"></div>
   <div><div class="k">bili_jct (Web)</div><input id="c_bili_jct"></div>
   <div><div class="k">DedeUserID</div><input id="c_uid"></div>
  </div>
  <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
   <button onclick="saveCred()">保存凭证</button>
   <span id="cmsg" class="muted"></span>
  </div>
 </div>
</div>
<table id="runs"><thead><tr>
 <th>开始时间</th><th>模式</th><th>结果</th><th>尝试</th><th>HTTP</th><th>耗时</th><th>订单</th>
</tr></thead><tbody></tbody></table>
<div id="detail">
 <h1 style="margin-top:18px">运行明细 <button onclick="hideDetail()">收起</button></h1>
 <div class="fails" id="failbox"></div>
 <table id="rows"><thead><tr>
  <th>#</th><th>时间</th><th>阶段</th><th>接口/事件</th><th>状态</th><th>耗时</th><th>摘要</th>
 </tr></thead><tbody></tbody></table>
</div>
<script>
const fmtDur = s => { s=Math.max(0,Math.round(s));
  const h=~~(s/3600),m=~~(s%3600/60),x=s%60;
  return (h?h+"h":"")+(m?m+"m":"")+x+"s"; };

async function j(u){const r=await fetch(u);return r.json();}

function liveCards(d){
  const now = Date.now()/1000;
  let next = "", cnt = "";
  if(d.ok && d.data.next_open_at){
    next = new Date(d.data.next_open_at*1000).toLocaleString("zh-CN");
    const gap = d.data.next_open_at - now;
    cnt = gap>0 ? fmtDur(gap) : "已到开售时间";
  }
  const stMap = {ON_SALE:["开售中!","ok"], SOLD_OUT_TODAY:["今日售罄","warn"],
                 ABOUT_TO_OPEN:["即将开售","warn"], NOT_ON_SALE:["未开售",""]};
  const st = d.ok ? (stMap[d.data.drainage_status]||[d.data.drainage_status,""]) : ["查询失败","bad"];
  return `
  <div class="card"><div class="k">实时状态</div>
    <div class="v ${st[1]}">${st[0]}</div>
    <div class="k">${d.ok?"":"<code>"+d.error+"</code>"}</div></div>
  <div class="card"><div class="k">距下次开售</div>
    <div class="v ${d.ok?"warn":""}">${cnt||"—"}</div>
    <div class="k">${next}</div></div>
  <div class="card"><div class="k">预约</div>
    <div class="v ${d.ok&&d.data.isReserved?"ok":""}">${d.ok?(d.data.isReserved?"已预约":"未预约"):"—"}</div></div>
  <div class="card"><div class="k">已购买</div>
    <div class="v ${d.ok&&d.data.has_buy?"ok":""}">${d.ok?(d.data.has_buy?"是":"否"):"—"}</div></div>
  <div class="card"><div class="k">凭证</div>
    <div class="v ${d.ok?"ok":"bad"}">${d.ok?"有效":"失效/网络异常"}</div></div>`;
}

async function refreshLive(){
  try{ document.getElementById("live").innerHTML = liveCards(await j("/api/summary")); }
  catch(e){ document.getElementById("live").innerHTML =
    '<div class="card"><div class="k">实时状态</div><div class="v bad">仪表盘后端不可达</div></div>'; }
}

async function refreshRuns(){
  const runs = await j("/api/runs");
  document.getElementById("runcount").textContent = "("+runs.length+" 次运行)";
  const tb = document.querySelector("#runs tbody");
  tb.innerHTML = runs.map(r=>{
    const order = r.order ? `<a href="${r.pay_link}" target="_blank">${(r.order.order_no||"订单")} 去支付</a>` : "—";
    const dur = r.started&&r.ended ? fmtDur((new Date(r.ended)-new Date(r.started))/1000) : "—";
    return `<tr class="runrow" onclick="showDetail('${r.file}')">
      <td>${(r.started||"").replace("T"," ").slice(5,23)}</td>
      <td>${r.mode}</td>
      <td><span class="tag ${r.result}">${r.result}</span>
          ${r.stop_message?"<div class='muted'>"+r.stop_message+"</div>":""}</td>
      <td>${r.attempts}</td><td>${r.http_calls}</td><td>${dur}</td><td>${order}</td></tr>`;
  }).join("");
}

async function showDetail(file){
  const d = await j("/api/runs/"+file);
  document.getElementById("detail").style.display = "block";
  const fb = document.getElementById("failbox");
  fb.innerHTML = d.failures.length
    ? "<b>失败分类</b><br>"+d.failures.map(f=>`<code>${f[1]}</code> ×${f[3]} ${f[2]} <span class="muted">${f[0]}</span>`).join("<br>")
    : "<b>失败分类</b><br>无失败记录";
  document.querySelector("#rows tbody").innerHTML = d.rows.map(r=>{
    const t=(r.t||"").slice(11,23);
    if(r.type==="http"){
      const resp=r.response||{};
      const code=resp.api_code!=null?resp.api_code:(r.error?"NET":"");
      return `<tr><td>${r.seq}</td><td>${t}</td><td>${r.phase||""}</td>
        <td>${(r.method||"")+" "+(r.api||"")}</td><td>${code}</td>
        <td>${r.elapsed_ms||""}ms</td><td class="muted">${(resp.message||r.error||"").slice(0,60)}</td></tr>`;
    }
    return `<tr><td>${r.seq}</td><td>${t}</td><td>${r.phase||""}</td>
      <td colspan="3">◉ ${r.event}</td>
      <td class="muted">${JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k])=>!["type","t","phase","event","seq","run_id"].includes(k)))).slice(1,90)}</td></tr>`;
  }).join("");
  document.getElementById("detail").scrollIntoView({behavior:"smooth"});
}
function hideDetail(){document.getElementById("detail").style.display="none";}

async function refreshNotify(){
  const c = await j("/api/notify-config");
  const st = document.getElementById("nstate");
  st.textContent = c.enabled ? "已启用" : "未配置";
  st.className = "tag " + (c.enabled ? "success" : "done");
  const src = c.sources || {};
  document.getElementById("nsummary").textContent = c.enabled
    ? `${c.user} → ${c.to} (${c.host}:${c.port}) · 来源: ${src.host}服务器/${src.pass}授权码`
    : "抢购结束不会发邮件，点击右侧配置（本地 secrets.py 或表单均可）";
  if(document.getElementById("nform").style.display === "block" &&
     !document.getElementById("f_host").value){
    // 只预填来源为"仪表盘"的值；本地/环境变量来源留空，保存时回落，
    // 避免把本地配置固化进 notify.json
    document.getElementById("f_host").value =
      src.host === "仪表盘" ? (c.host || "") : "";
    document.getElementById("f_host").placeholder =
      src.host !== "仪表盘" && c.host ? `本地已配 ${c.host}，填写则覆盖` : "smtp.qq.com";
    document.getElementById("f_port").value = (src.host === "仪表盘") ? c.port : 465;
    document.getElementById("f_user").value =
      src.user === "仪表盘" ? (c.user || "") : "";
    document.getElementById("f_user").placeholder =
      src.user !== "仪表盘" && c.user ? `本地已配 ${c.user}，填写则覆盖` : "you@qq.com";
    document.getElementById("f_to").value =
      src.to === "仪表盘" ? ((c.to === c.user) ? "" : (c.to || "")) : "";
    document.getElementById("f_pass").placeholder =
      c.pass_set ? `已设置(来源:${src.pass})，留空则用本地配置` : "邮箱设置里生成的授权码";
  }
}
function toggleForm(){
  const f = document.getElementById("nform");
  f.style.display = f.style.display === "block" ? "none" : "block";
  if(f.style.display === "block") refreshNotify();
}
async function saveCfg(){
  const msg = document.getElementById("nmsg");
  msg.textContent = "保存中...";
  const r = await fetch("/api/notify-config", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({
      host: f_host.value.trim(), port: +f_port.value || 465,
      user: f_user.value.trim(), pass: f_pass.value, to: f_to.value.trim()})});
  const d = await r.json();
  msg.textContent = d.error ? d.error : "已保存" + (d.enabled ? "，通知已启用" : "（信息不全，未启用）");
  f_pass.value = ""; refreshNotify();
}
async function testMail(){
  const msg = document.getElementById("nmsg");
  msg.textContent = "发送中...";
  const r = await fetch("/api/notify-test", {method:"POST"});
  const d = await r.json();
  msg.textContent = d.ok ? "测试邮件已发送，请查收" : d.error;
}

/* ---------------- 登录凭证卡 ---------------- */
async function refreshCred(){
  const c = await j("/api/credentials");
  const st = document.getElementById("cstate");
  const complete = c.set && c.set.access_key && c.set.sessdata;
  st.textContent = complete ? "已配置" : "不完整";
  st.className = "tag " + (complete ? "success" : "credential_expired");
  const src = c.sources || {};
  document.getElementById("csummary").textContent =
    `UID ${c.uid} · access_key ${c.set.access_key ? c.access_key + "(" + src.access_key + ")" : "未设"} · SESSDATA ${c.set.sessdata ? "(" + src.sessdata + ")" : "未设"}` +
    (c.updated_at ? ` · 更新于 ${c.updated_at}(${c.updated_by})` : "");
}
function toggleCredForm(){
  const f = document.getElementById("cform");
  f.style.display = f.style.display === "block" ? "none" : "block";
}
async function saveCred(){
  const msg = document.getElementById("cmsg");
  msg.textContent = "保存中...";
  const r = await fetch("/api/credentials", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({
      access_key: c_access_key.value.trim(), csrf: c_csrf.value.trim(),
      sessdata: c_sessdata.value.trim(), bili_jct: c_bili_jct.value.trim(),
      uid: c_uid.value.trim()})});
  const d = await r.json();
  msg.textContent = d.error ? d.error : "已保存，立即生效（可用上方\"验证\"实测）";
  ["c_access_key","c_csrf","c_sessdata","c_bili_jct","c_uid"].forEach(id=>{
    document.getElementById(id).value = "";});
  refreshCred();
}
async function verifyCred(){
  const st = document.getElementById("cstate");
  st.textContent = "验证中...";
  const d = await j("/api/summary");
  st.textContent = d.ok ? "有效 ✓" : "失效/异常";
  st.className = "tag " + (d.ok ? "success" : "credential_expired");
}
let qrTimer = null;
async function qrLogin(){
  document.getElementById("qrbox").style.display = "block";
  document.getElementById("qrmsg").textContent = "生成二维码...";
  document.getElementById("qrgrid").innerHTML = "";
  const d = await j2("/api/qrcode/start", "POST");
  if(d.error){
    document.getElementById("qrmsg").textContent = d.error;
    return;
  }
  renderQR(d.matrix);
  document.getElementById("qrmsg").textContent =
    "用手机 B站 App 扫码并确认登录" + (d.qr_error ? "（"+d.qr_error+"）" : "");
  if(qrTimer) clearInterval(qrTimer);
  qrTimer = setInterval(async ()=>{
    const p = await j2("/api/qrcode/poll", "POST", {auth_code: d.auth_code});
    const m = document.getElementById("qrmsg");
    if(p.status === "success"){
      clearInterval(qrTimer); m.textContent = "✓ " + p.message;
      setTimeout(()=>{document.getElementById("qrbox").style.display="none";}, 2000);
      refreshCred();
    } else if(p.status === "expired" || p.status === "error"){
      clearInterval(qrTimer); m.textContent = p.message;
    } else if(p.status === "scanned"){
      m.textContent = p.message;
    }
  }, 2000);
}
function renderQR(matrix){
  if(!matrix){ return; }
  const n = matrix.length, cell = Math.max(3, Math.min(8, Math.floor(240/n)));
  const grid = document.getElementById("qrgrid");
  grid.innerHTML = "";
  grid.style.display = "grid";
  grid.style.gridTemplateColumns = `repeat(${n}, ${cell}px)`;
  grid.style.gap = "0";
  grid.style.background = "#fff"; grid.style.padding = "8px"; grid.style.borderRadius = "8px";
  matrix.forEach(row => row.forEach(v=>{
    const d = document.createElement("div");
    d.style.width = cell+"px"; d.style.height = cell+"px";
    d.style.background = v ? "#000" : "#fff";
    grid.appendChild(d);
  }));
}
async function j2(u, method, body){
  const r = await fetch(u, {method, headers:{"Content-Type":"application/json"},
    body: body ? JSON.stringify(body) : null});
  return r.json();
}

setInterval(()=>{document.getElementById("clock").textContent =
  new Date().toLocaleString("zh-CN");}, 1000);
refreshLive(); refreshRuns(); refreshNotify(); refreshCred();
setInterval(refreshLive, 5000);
setInterval(refreshRuns, 10000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):   # 静默访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "bad json"}, 400)

        if path == "/api/notify-config":
            if not isinstance(body, dict):
                return self._json({"error": "bad body"}, 400)
            try:
                return self._json(save_notify_config(body))
            except Exception as ex:
                return self._json({"error": f"保存失败: {ex}"[:200]}, 500)
        if path == "/api/notify-test":
            ok = send_mail("[B站抢购] 仪表盘测试邮件",
                           "收到这封邮件说明抢购结果推送配置成功。\n"
                           "抢购成功后会推送订单号与支付链接。")
            return self._json({"ok": ok,
                               "error": "" if ok else "发送失败: 检查授权码/端口/发件人"})
        if path == "/api/credentials":
            # 表单短字段名 -> credentials.json 键
            mapping = {"access_key": "bili_access_key", "csrf": "bili_csrf",
                       "sessdata": "bili_sessdata", "bili_jct": "bili_jct",
                       "uid": "bili_uid"}
            creds = {mapping[k]: v for k, v in body.items() if k in mapping}
            try:
                return self._json(save_credentials(creds, source="仪表盘"))
            except Exception as ex:
                return self._json({"error": f"保存失败: {ex}"[:200]}, 500)
        if path == "/api/qrcode/start":
            try:
                start = qrcode_start()
            except Exception as ex:
                return self._json({"error": f"{ex}"[:200]}, 500)
            try:
                import qrcode as _qr
                qr = _qr.QRCode(border=1)
                qr.add_data(start["url"])
                start["matrix"] = qr.get_matrix()
            except Exception as ex:       # qrcode 库缺失时退化为仅链接
                start["matrix"] = None
                start["qr_error"] = f"二维码渲染失败({ex})，请安装: pip install qrcode"
            return self._json(start)
        if path == "/api/qrcode/poll":
            auth_code = str(body.get("auth_code") or "")
            if not auth_code:
                return self._json({"status": "error",
                                   "message": "缺少 auth_code"}, 400)
            return self._json(qrcode_poll(auth_code))
        return self._json({"error": "not found"}, 404)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/summary":
            self._json(refresh_live())
        elif path == "/api/runs":
            self._json(list_runs(LOGS_DIR))
        elif path == "/api/notify-config":
            self._json(notify_config_view())
        elif path == "/api/credentials":
            self._json(credentials_view())
        elif path.startswith("/api/runs/"):
            name = path[len("/api/runs/"):]
            f = (LOGS_DIR / name)
            if (not f.name.startswith("run_") or not f.exists()
                    or ".." in name or "/" in name.strip("/")):
                return self._json({"error": "not found"}, 404)
            rows = load_rows(f)
            self._json({"rows": rows, "failures": failure_breakdown(rows)})
        else:
            self._json({"error": "not found"}, 404)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="B站抢购仪表盘")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8777)
    args = ap.parse_args()

    # Windows 下 SO_REUSEADDR 允许多进程双绑定同一端口，旧进程会抢答请求。
    # 启动前主动探测，被占用则直接报错退出，避免出现"幽灵旧进程"。
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((args.host, args.port))
    except OSError:
        print(f"端口 {args.host}:{args.port} 已被占用（可能有旧仪表盘进程）。"
              f"请先结束旧进程再启动。")
        return 1
    finally:
        probe.close()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"仪表盘: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
