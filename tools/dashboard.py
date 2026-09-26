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

from core.client import BiliClient          # noqa: E402
from core.logstats import (                 # noqa: E402
    failure_breakdown,
    list_runs,
    load_rows,
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
</style></head><body>
<h1>B站联合会员抢购仪表盘 <small id="clock"></small></h1>
<div class="grid" id="live"></div>
<h1 style="margin-top:6px">抢购记录 <span class="muted" id="runcount"></span></h1>
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

setInterval(()=>{document.getElementById("clock").textContent =
  new Date().toLocaleString("zh-CN");}, 1000);
refreshLive(); refreshRuns();
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

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"仪表盘: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
