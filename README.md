# B站联合会员抢购（178 元"买1年得5年"超大会员）

针对 B 站 2026 夏促活动页（`blackboard/era/rZPKSDqrJEOrtkVi.html`）每日
12:00 限量开售的 **178 元超级大会员（原价 388，含 QQ音乐/知乎/肯德基大神卡/京东
等联名年卡赠品）** 的个人抢购脚本。

> 仅限本人账号、本人消费使用。请求节奏为三段自适应间隔（爆发 60ms ->
> 快速 120ms -> 慢速 300ms，带 ±30% 抖动），请勿改成高频轰炸——既容易
> 被风控封号，也影响他人正常购买。

## 使用

```bash
cd /d/User/Desktop/bili_vip_rush

python main.py --check          # 只读预检：开售状态/是否预约/购买资格
python main.py --reserve        # 未预约时先一键预约
python main.py --rush           # 自动等待下一次开售（明天12:00）并抢购
python main.py --rush --at 11:59:50   # 指定本地时刻附近开抢
python main.py --rush --now     # 已开售后立即抢
python main.py --daemon 11:50   # 常驻：每天 11:50 自动执行一轮（Docker 推荐）
```

抢到后终端会打印订单号（`order_no`），**名额已锁定，需尽快支付**：
- 手机上打开支付链接（脚本成功后打印），或
- 打开 B 站 App -> 我的 -> 大会员，页面会显示待支付订单。

## 抢购策略（综合 glm-rush 与 rush-glm-codingplan，按 App 签名场景裁剪）

- **三段自适应间隔**（glm-rush）：开售瞬间是拼手速的窗口——前 8 发爆发段
  （60ms）-> 黄金 10s 快速段（120ms）-> 之后慢速段（300ms）省请求防风控；
  间隔全程 ±30% 随机抖动，避免匀速请求特征。
- **五态响应分类器**（rush-glm-codingplan）：`core/classifier.py` 集中判定
  每次失败的性质——凭证失效/已购买立即停止（不烧重试预算）、售罄 linger、
  系统错误冷却、其余重试。隔天复盘遇到新错误码只改这一个文件。
- **T-3s 连接预热**（rush-glm-codingplan）：开抢前对 api.bilibili.com 发一次
  轻请求，DNS+TCP+TLS 提前进 Session 连接池。实测冷启动 344ms vs 复用
  31ms，开抢第一发省约 300ms。
- **收尾忙等 + 倒计时**：最后 0.2s 自旋等待消除系统定时器粒度（Windows
  sleep 粒度 ~15ms）；等待期间终端实时倒计时。
- **双重校验**：`create/activity` 返回订单后，再调 `order/status` 确认
  订单状态，一并写入 order_ok 事件。
- **多重熔断**：总尝试上限 400 次；连续 5 次系统错误冷却 2s；"已购/重复/
  限购"立即停；"售罄"再坚持 3s 即停。
- **不用多路并发**：两个参考项目的多 worker 并发是浏览器/匿名场景打法；
  我们是 App 签名 + 同账号，同一 token 并发下单会产生重复订单，用爆发
  间隔换速度而不是并发数。

## 流程说明

1. **校时**：调用 `attract_card` 取服务器 `current_time`，采样 5 次取中位数
   计算本地时钟偏移，按服务器时间精确等待到开售时刻（默认提前 0.6s 发第一发）。
2. **抢购**：开售后按 `config/settings.py: TARGET_PLANS` 顺序依次尝试下单
   （先抢 `26moe_cdd178` 超级大会员，备选 `26moe_xny178` 年卡），三段自适应
   间隔 + 抖动，任一成功立即停止并打印订单号与支付链接。
3. **熔断**：接口报"已购/重复/限购"立即停止（避免重复下单）；报"售罄"再
   坚持 3 秒即停。全部尝试结果（含原始响应）落盘 `logs/run_*.jsonl`。

## 日志系统（复盘迭代闭环）

抢购是链式流程（状态/资格 -> 下单 -> 订单），任何一环都可能有没见过的
报错或新字段。日志系统的目标：**当天实战留下完整现场，复盘后改脚本，
次日再战，直到整条链路打通**。

- **HTTP 审计**：每次接口调用（成功/接口报错/网络异常/非 JSON 响应）都
  落一行 JSONL——请求参数、响应**原文**（截断 4KB）、耗时、HTTP 状态、
  接口 code/message。改 `core/client.py` 缺什么参数，直接看响应原文。
- **调用链**：每行带自增 `seq` / `phase`（precheck/calibrate/rush/reserve）/
  `run_id`，一次运行一个文件 `logs/run_<时间>_<模式>.jsonl`。
- **脱敏**：access_key/csrf/sign/SESSDATA 只保留首 6 尾 4，日志可以放心
  发给别人看/喂给 AI 分析。
- **兜底捕获**：抢购循环里未预期异常（含代码 bug）记录 `unexpected_error`
  后继续；进程崩溃/Ctrl+C 也会写 `run_crashed`/`run_interrupted` +
  traceback。
- **复盘报告**：

```bash
python tools/replay.py                    # 复盘最近一次运行
python tools/replay.py logs/run_xxx.jsonl # 指定文件
```

报告含：调用序列表（时间/阶段/接口/耗时/状态码/摘要）、失败分类统计
（按 接口+错误码+信息 聚合计数——"未开售 ×30、缺参数 ×2"一目了然）、
关键接口（下单/预约/状态）响应原文。迭代脚本就靠它定位断点。

## 关键接口（逆向自 App 抓包 + 活动页搭建配置）

| 用途 | 接口 | 方式 |
|---|---|---|
| 开售/预约状态 | `/x/vip/activity/sale/summer2026/attract_card` | GET, App签名 |
| 一键预约 | `/x/vip/activity/sale/summer2026/reserve` | POST, App签名 |
| 购买资格 | `/pgc/activity/dokodemoDoor/getEasy/moe2026_buyComponentEvo_info` | POST JSON, 不签名 |
| 下单锁名额 | `/x/vip/order/create/activity` | POST, App签名 |
| 订单状态校验 | `/x/vip/order/status` | GET, App签名 |

App 签名规则：参数（含 `appkey/ts`）按 key 排序 urlencode 后拼 `appsec`
取 MD5。实现见 `core/signer.py`，已用 HAR 抓包中的真实 sign 做黄金用例
验证（`tests/test_signer.py`），与安卓客户端算法一致。

178 套餐参数（`TARGET_PLANS[0]`）：
`act_token=987488888720260824193333, appId=241, appSubId=26moe_fhc,
panel_type=26moe_cdd178, months=12, orderType=1(连续包年)`。

## Docker 部署

镜像内置时区 `Asia/Shanghai`（开售时刻解析依赖本地时区，勿改动该环境变量）。
默认入口为 `python main.py`，`docker run`/`compose` 直接跟 CLI 参数即可。

```bash
# 构建与预检
docker compose build
docker compose run --rm bili-rush --check

# 常驻模式（推荐）：每天 11:50 自动起跑一轮
# 脚本会校时后自己等到服务器 next_open_at（12:00）开抢，11:50 只是起跑线
docker compose up -d
docker compose logs -f bili-rush
```

- **日志卷**：`./logs` 已挂载到容器 `/app/logs`，宿主机直接
  `python tools/replay.py` 复盘，或 `docker compose exec bili-rush
  python tools/replay.py`。
- **凭证注入**（可选，优先级高于 `config/settings.py`）：通过环境变量
  `BILI_ACCESS_KEY / BILI_CSRF / BILI_SESSDATA / BILI_BILI_JCT /
  BILI_DEDE_USER_ID` 注入，镜像与仓库可不含明文凭证：
  ```yaml
  environment:
    - BILI_ACCESS_KEY=xxx
    - BILI_CSRF=xxx
  ```
- **一次性命令**（不常驻）：
  ```bash
  docker compose run --rm bili-rush --rush
  docker compose run --rm bili-rush --reserve
  ```
- 容器重启策略 `unless-stopped`：宿主机重启后自动恢复常驻。

本地开发用 `pip install -r requirements-dev.txt`（含 pytest）；运行镜像只装
`requirements.txt`（requests）。

## 仪表盘

```bash
python tools/dashboard.py            # http://127.0.0.1:8777
```

- **实时状态卡**：开售状态（开售中/今日售罄/即将开售）、距下次开售倒计时、
  预约/已购、凭证有效性（5s 自动刷新，服务端代理调用 attract_card）。
- **抢购记录**：每次运行的模式/结果（success/sold_out/credential_expired/
  timeout…）、尝试次数、订单号与支付直达链接；点击行展开**调用明细**
  （HTTP 序列 + 失败分类 + 事件时间线）。
- 零第三方依赖（标准库实现），解析层与 `tools/replay.py` 共用
  （`core/logstats.py`）。

Docker 下仪表盘是独立服务：`docker compose up -d` 后访问
`http://<宿主机>:8777`。

## 邮件推送

抢购结束自动发邮件：**成功→订单号+支付直达链接（尽快支付）**、
凭证失效→提醒重新抓包、其余失败→原因+复盘命令。未配置时静默跳过，
发送失败不影响抢购主流程。

```bash
# 1) 配置 config/secrets.py（或环境变量 BILI_SMTP_*）：
SMTP_HOST = "smtp.qq.com"     # 465=SSL；587 端口自动用 STARTTLS
SMTP_PORT = 465
SMTP_USER = "you@qq.com"
SMTP_PASS = "SMTP授权码"       # 邮箱设置里生成，不是登录密码
NOTIFY_TO = "me@qq.com"        # 留空=发给自己

# 2) 验证配置
python main.py --test-notify
```

Docker 下在 compose 的 environment 里加 `BILI_SMTP_HOST/PORT/USER/PASS/
BILI_NOTIFY_TO` 即可。

## 目录结构

```
config/settings.py    # 凭证 + 套餐 + 接口 + 节奏配置（改这里；凭证可被环境变量覆盖）
core/signer.py        # App 签名（HAR 黄金用例验证）
core/classifier.py    # 五态响应分类器（凭证失效/已购/售罄/系统错误/重试）
core/client.py        # 接口客户端（状态/预约/资格/下单/预热）+ HTTP 审计
core/run_logger.py    # 运行日志：审计/事件/脱敏/调用链
core/logstats.py      # 运行汇总/失败分类（replay 与 dashboard 共用）
core/time_sync.py     # 服务器时钟校准
flows/rush.py         # 抢购流程（预检→校时→等待→开抢→熔断）
tools/replay.py       # 复盘报告（失败分类/调用序列/原文现场）
tools/dashboard.py    # Web 仪表盘（实时状态/抢购记录/调用明细）
tests/                # pytest，全部 mock 网络层，无真机/无真实请求
main.py               # CLI 入口（一次性命令 + --daemon 常驻调度）
Dockerfile            # python:3.12-slim + Asia/Shanghai 时区
docker-compose.yml    # 常驻调度编排（日志卷 + 凭证环境变量）
logs/                 # 运行日志（JSONL，一次运行一个文件）
```

## 凭证维护（重要）

凭证**不进 git、不进镜像**。两种提供方式（优先级：环境变量 > 本地文件）：

1. **本地文件**：`cp config/secrets.example.py config/secrets.py`，填入抓包
   凭证（`secrets.py` 已被 `.gitignore` / `.dockerignore` 排除）。
2. **环境变量**（Docker 推荐）：
   ```yaml
   environment:
     - BILI_ACCESS_KEY=xxx
     - BILI_CSRF=xxx
     - BILI_SESSDATA=xxx
     - BILI_BILI_JCT=xxx
     - BILI_DEDE_USER_ID=xxx
   ```

若接口返回 `-101`（未登录）或 `-111`（csrf 失效），抢购会立即停止并提示
更新凭证——重新抓包后更新 `secrets.py` 或环境变量即可。这些都是**账号登录
凭证，不要外传**。

## 测试

```bash
python -m pytest tests/ -v   # 10 个用例：签名黄金用例/客户端参数组装/流程状态机
```

参考过 [BiliBiliToolPro](https://github.com/RayWangQvQ/BiliBiliToolPro) 的
工程实践（写操作不做盲目 5xx 重试、只读接口可重试、超时与退避策略）、
[glm-rush](https://github.com/qtaxm/glm-rush)（三段自适应间隔、抖动、多重
熔断）与 [rush-glm-codingplan](https://github.com/shinianhz/rush-glm-codingplan)
（五态响应分类、连接预热、忙等触发）。多 worker 并发不适用于同账号 App
签名场景，未采纳。
