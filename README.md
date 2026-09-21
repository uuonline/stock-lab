## English Summary

**StockLab** is an open-source, self-hosted stock research system for
**A-share, Hong Kong, ETF and index** markets. It is designed to run on a
personal NAS via Docker and is accessed through a browser (desktop and mobile).

### Features

- **Watchlist dashboard** — real-time quotes, intraday chart, candlestick
  charts, technical indicators
- **Trading range (box) analysis** — identifies sideways price ranges and draws
  the range top (resistance) and bottom (support) on the candlestick chart,
  together with the current position within the range, boundary touch counts,
  and a confidence score. It first distinguishes range-bound conditions from
  trends (via linear-regression slope and mid-line crossing frequency) so that
  a box is never drawn on a trending stock
- **Fundamentals** — financial statements, valuation metrics, and a composite
  scoring panel with an explainable breakdown
- **Screener** — 9 built-in presets plus custom conditions (valuation, market
  cap, turnover, and 21 technical conditions including moving-average
  crossovers, MACD/KDJ signals, volume surges, 60-day highs/lows, and 6
  trading-range conditions such as "near range bottom", "volume-confirmed
  range breakout", and "stable sideways range")
- **Backtesting engine** — 8 strategies, modelling real A-share trading rules:
  T+1 settlement, price limits, commissions (min CNY 5), stamp duty, transfer
  fees, and slippage
- **AI research reports** — optional LLM integration (any OpenAI-compatible
  endpoint); falls back to a built-in rule engine when no API key is configured
- **Scheduled tasks and alerts** — 9 alert rule types with 5 notification
  channels (WeCom, Telegram, ServerChan, Bark, generic webhook)

### Technical notes

- **Multi-source failover** for market data: Tencent → Eastmoney → Sina →
  Alpha Vantage. Includes a host pool spanning multiple Eastmoney domain
  families (their blocks apply per domain family, not per host), circuit
  breakers, and exponential backoff.
- **Alpha Vantage integration** — used for end-of-day equity data, with a daily
  request quota guard, per-second throttling, and aggressive caching to stay
  within the free tier. It is placed last in the source order and only used as
  a fallback.
- **Technical indicators** implemented in pure NumPy — no TA-Lib, so no C
  toolchain is required (important for NAS deployment).
- **Frontend assets are vendored** (ECharts is bundled locally), so the UI works
  on an isolated LAN with no CDN access.
- **Docker build depends only on pip, not apt** — Debian mirrors are often
  unreachable from mainland China, and an apt failure aborts the whole build.
  Timezone data is provided via the PyPI `tzdata` package instead.

### Testing

Four layers of tests are included:

| Script | Scope |
|---|---|
| `scripts/selftest.sh` | 21 checks: environment, data source connectivity, indicator math, backtest engine, scheduler |
| `scripts/robustness.sh` | 38 checks: invalid input, injection attempts, parameter bounds |
| `scripts/frontend-test.sh` | 31 checks: executes the real frontend JS in jsdom and drives the UI |
| `scripts/calibrate.py` | Recalibrates scoring thresholds against a live market sample |

### Disclaimer

This project is intended for learning and research purposes only. It does
**not** constitute investment advice. All market data is sourced from
third-party public APIs and remains the property of the respective providers.

Licensed under the MIT License.

---

# StockLab · 私有化股票研究系统

部署在你自己的群晖 NAS 上，数据全部留在本地。覆盖 **A股 / 港股 / ETF / 指数**，
提供实时行情看板、基本面分析、技术指标选股、策略回测、AI 研究简报、定时提醒六大能力，
通过浏览器访问，手机同样可用。

```
┌─────────────────────────────────────────────────┐
│  浏览器 (PC / 手机)                              │
└──────────────────┬──────────────────────────────┘
                   │ HTTP :8787
┌──────────────────▼──────────────────────────────┐
│  群晖 NAS · Docker 容器 StockLab                 │
│  FastAPI + SQLite + APScheduler                 │
│  ┌────────────────────────────────────────┐     │
│  │ 多源容错数据层                          │     │
│  │  东财(主机池轮换) → 腾讯 → 新浪          │     │
│  └────────────────────────────────────────┘     │
└──────────────────┬──────────────────────────────┘
                   │ 挂载 ./data
┌──────────────────▼──────────────────────────────┐
│  /volume1/docker/stock-lab/data                 │
│  SQLite 数据库 / K线缓存 / AI报告                │
└─────────────────────────────────────────────────┘
```

---

## 一、部署到群晖 NAS

### 前置条件

- DSM 7.x，已安装 **Container Manager**（套件中心搜索安装）
- 开启 SSH：控制面板 → 终端机和 SNMP → 启用 SSH

### 步骤

**1. 上传代码到 NAS**

把整个 `stock-lab` 目录放到 `/volume1/docker/` 下，最终路径为
`/volume1/docker/stock-lab`。用 File Station 拖进去即可。

**2. 创建配置文件**

SSH 登录 NAS 后：

```bash
cd /volume1/docker/stock-lab
cp .env.example .env
vi .env          # 至少改一下 SL_ACCESS_TOKEN（见下方说明）
```

**3. 启动**

```bash
sudo docker compose up -d --build
```

首次构建需下载基础镜像和依赖，约 3~8 分钟（国内走阿里云 pip 镜像）。

**4. 验证**

```bash
sudo docker compose ps          # 状态应为 Up (healthy)
sudo docker compose logs -f     # 查看启动日志
```

看到这样的输出就成功了：

```
2026-09-18 14:20:01 INFO    stocklab: ==============================================================
2026-09-18 14:20:01 INFO    stocklab:   StockLab v1.0.0 启动
2026-09-18 14:20:01 INFO    stocklab:   数据目录 : /data
2026-09-18 14:20:01 INFO    stocklab:   调度器   : 启用
2026-09-18 14:20:01 INFO    stocklab: ==============================================================
```

**5. 访问**

浏览器打开 `http://你的NAS地址:8787`

> 若在 `.env` 里设置了 `SL_ACCESS_TOKEN=abc123`，首次访问需用
> `http://你的NAS地址:8787/?token=abc123`，之后会记住 30 天。

**6. 初始化数据（必做一次）**

进入「设置」页 → 点击 **刷新全市场快照**。约 1~3 分钟，完成后选股和搜索才能用。
看到 "快照条数" 接近 9900 即为完整（A股约 5560 + 港股约 2900 + ETF 约 1600）。

---

### 用 Container Manager 图形界面部署（不用 SSH）

1. Container Manager → **项目** → **新增**
2. 项目名称：`stocklab`
3. 路径：选择 `/volume1/docker/stock-lab`
4. 来源：**选择现有的 docker-compose.yml**
5. 点击下一步 → 完成

> 注意：图形界面方式无法自动读取 `.env`，需要在项目的「环境」标签页里
> 手动添加需要的变量，或者干脆不用 `.env`（全部使用默认值也能跑）。

---

### 权限问题处理

容器内以 root 运行，数据文件会属于 root。如果希望用你的 DSM 账号管理这些文件：

```bash
# 查出你的 uid（通常是 1026 或 1000）
id 你的用户名

# 修正数据目录属主
sudo chown -R 1026:100 /volume1/docker/stock-lab/data
sudo chmod -R 755 /volume1/docker/stock-lab/data
```

---

## 二、配置说明

所有配置都在 `.env` 里，改完执行 `sudo docker compose up -d` 生效。

### 访问令牌（重要）

```ini
SL_ACCESS_TOKEN=
```

- **只在局域网用**：留空即可，省事。
- **做了端口映射 / 内网穿透 / 公网访问**：**务必设置**一串随机字符。
  否则任何能访问到该端口的人都能看到你的自选股和操作数据。

生成随机令牌：

```bash
openssl rand -hex 16
```

### 数据源

```ini
SL_SOURCE_ORDER=tencent,eastmoney,sina
SL_MIN_REQUEST_INTERVAL=0.18
```

**默认把腾讯放在首位，这是实测结论而不是偏好**（5 个标的 × 15 次采样，清缓存）：

| 顺序 | 成功率 | 中位延迟 |
|---|---|---|
| 东财优先 | 100% | 289ms |
| **腾讯优先** | 100% | **39ms** |

腾讯字段几乎全覆盖且快 7 倍；东财独有的只有「主力净流入」等少数字段，
这些字段在个股详情页通过 `SL_ENRICH_SOURCES` **按需补充**（有 `SL_ENRICH_BUDGET`
时间预算，超时自动放弃，不影响页面加载）。

三个源逐级容错：腾讯 → 东财 → 新浪。**K线也是同一个顺序**，这一点值得说明：
一开始 K线是东财优先（因为它字段最全），但当东财 `push2his` 整族被限流时，
一次冷启动会**白等 13 秒**才落到腾讯。实测三家返回的 OHLCV 完全一致
（已逐根比对 600519），所以把快的放前面没有代价：

| K线源 | 冷启动延迟（清缓存） |
|---|---|
| 东财（被限流时） | 13,037ms 后失败 |
| 新浪 | 92ms |
| **腾讯** | **286ms** |

改动后同一批标的的 K线冷启动从 14.36s 降到 **0.26 ~ 0.49s（本地）/ ~1.0s（NAS）**。

**两道闸门防止单个源拖慢整条链路**：

1. **单源时间预算** `SL_KLINE_SOURCE_BUDGET`（默认 2.5s）—— 物理封顶，
   任何源超时立刻降级到下一个源。
2. **端点级熔断** —— 熔断键是「源 + 端点」（`eastmoney:kline` 与
   `eastmoney:quote` 分开）。这修掉一个隐蔽 bug：以前熔断器**按源**记录，
   东财 `quote` 一旦成功就把计数器清零，于是连续失败的 `kline` 永远等不到熔断，
   每次都要重跑一遍 13 秒。

顺带修掉了 `fetch_rotating` 里 `deadline` 的一个漏洞：原实现只在**两台主机之间**
检查死线，单台主机内部还能跑满 `timeout × retries`，实测「2.5 秒预算」实际跑了
2.9 秒。现在单次请求的超时会被剩余预算截断，剩余不足 0.4 秒就直接放弃 ——
自检里的东财检查因此从 13.2s 降到 3.8s，整个自检从 57s 降到 29s。

### 顺带挖出来的 CPU 浪费

修完网络之后，`/api/kline` 命中缓存仍然要 0.73 ~ 0.91s。量了一下才看清
**瓶颈根本不是网络**：

| 环节 | 耗时 |
|---|---|
| 取 K线（命中缓存） | 8ms |
| `indicators.compute_all(600 根)` | 347ms |
| `indicators.latest_snapshot(同样 600 根)` | 345ms |
| `box.analyze_multi`（4 个窗口） | 1.1ms |

原因：`latest_snapshot()` 内部**又调了一次 `compute_all()`**，而 `/api/kline`
本来就把两个都调了 —— 等于每次详情页白烧一倍 CPU（这台 NAS 是 4 核 2.0GHz 的
Celeron J4125，347ms 不是小数）。

改法是让 `latest_snapshot(bars, ind=None)` 接受算好的指标复用，两处调用点改成
算一次传进去。**16 组（4 标的 × 4 长度）逐一比对，输出 JSON 完全一致**，
纯粹是省掉重复计算：

| 指标 | 改前 | 改后 |
|---|---|---|
| `/api/kline` 命中缓存（NAS） | 0.73 ~ 0.91s | **0.373s** |
| 详情页首屏 4 请求并发（NAS） | 1.16s | **0.99s** |

另有**熔断器**：某个源连续失败 5 次后自动跳过它 45 秒，避免每次请求都白等重试。
实测东财的限流是**按 IP** 的（被限流时 12 个分片主机同时失效），这时熔断比
主机轮换更有效。

**全市场列表也有兜底**：这份数据原本只有东财提供。现在有两层保障：
① 主机池横跨 `push2` / `push2delay` 两个域名族（应对单族被封）；
② 两个族都不可用时，自动切到**新浪列表接口**，返回 5564 只 A股（含北交所）
并带 PE/PB/市值/换手率。快照实际来源会显示在 `/api/snapshot/status` 的
`source` 字段（`eastmoney` 或 `sina`）。

**港股**走的是东财（新浪列表不覆盖港股），但因为主机池横跨 `push2` 与
`push2delay` 两个域名族，实测在 `push2` 全族被封的情况下港股列表
（2922 只）依然能正常抓到。

---

## 三、部署自检（换到 NAS 后建议先跑）

Mac 上跑通不代表 NAS 上跑通 —— NAS 的 DNS、出口网络、防火墙策略都可能不同。
系统内置了自检，覆盖数据源、数据库、指标数学、回测、调度、时区。

**方式一：Web 界面**
打开「设置」页 → 点 **运行自检**，结果按分组列出，逐项显示通过/失败。
完整自检约 30-60 秒（含逐源可靠性采样）。只想快速确认核心功能可加
`?include_optional=0`，几秒即可出结果。

**方式二：命令行（推荐，出问题好排查）**

```bash
cd /volume1/docker/stock-lab
./scripts/selftest.sh
```

等价于：

```bash
docker exec -it stocklab python -m app.services.selftest
```

输出示例：

```
  StockLab 部署自检  ✅ PASS
==================================================================
  Python 3.11.x  |  x86_64  |  时区 CST
==================================================================

  ── 数据源 ──
   ✓ 行情获取(多源容错)     3 个标的全部拿到行情（实际使用源: tencent）
   ✓ 数据源可靠性采样       腾讯·行情 4/4 | 新浪·行情 4/4 | 东财·行情 4/4
   ✓ 东财·日线K线          30 根，末根 2026-09-18 收 1259.34
   ...
==================================================================
  全部检查通过，系统可正常使用
  通过 21/21   总耗时 43023ms
==================================================================
```

检查分三级：
- **核心（core）** —— 失败表示系统不可用，必须解决
- **可选（optional）** —— 失败只是降级（比如东财列表被限流、没配推送渠道），不影响主流程
- **跳过（–）** —— 前置条件未满足，不算失败。例如全新安装时全市场快照
  还没抓，依赖它的「选股筛选器」「标的搜索」会跳过而不是报错

所以**全新安装后第一次跑自检，结果是 WARN（通过 18/21，跳过 2）属于正常**，
不是系统有问题。等快照抓完再跑就是 PASS。

退出码 0 表示没有核心失败，可直接用于脚本判断。

### 接口健壮性测试

```bash
./scripts/robustness.sh
# 或指定地址：
BASE=http://你的NAS地址:8787 ./scripts/robustness.sh
```

38 项检查，覆盖非法标的、注入尝试、参数边界，以及一条重要的原则：
**非法输入必须被明确回报，不能假装成功**。判定标准是「不得出现 5xx」，
同时用 `grep` 校验响应体里确实带上了告警文案。

### 前端冒烟测试

```bash
./scripts/frontend-test.sh
# 或指定地址：
BASE=http://你的NAS地址:8787 ./scripts/frontend-test.sh
```

31 项检查。在 Node + jsdom 里**真实执行前端 JS**，从运行中的服务拉取真实的
HTML/JS/CSS，只 stub 掉 ECharts（jsdom 没有 canvas），然后把界面走一遍：
7 个标签页、K线绘制与周期/副图切换、选股、回测、提醒、设置，并捕获所有
JS 异常。

存在的意义：**接口全通不代表页面能用**。一个 JS 运行时错误就能让整个界面
白屏，而纯接口测试完全发现不了 —— 这个面在加入此测试前从未被验证过。
脚本会自动把 jsdom 装到临时目录，不污染项目。

### 本地服务管理

```bash
./scripts/serve.sh start      # 后台启动
./scripts/serve.sh status     # 查看状态与访问地址
./scripts/serve.sh stop       # 停止
./scripts/serve.sh restart    # 重启
./scripts/serve.sh log        # 跟踪日志
./scripts/serve.sh fg         # 前台运行（方便看报错）
```

> 不要用 `nohup python -m uvicorn ... &` 起服务：这种进程在终端会话或
> 自动化任务结束后会被回收，表现就是"服务莫名其妙没了，页面打不开"。
> 实测 `setsid` 也挡不住（进程树清理会连坐），所以长期运行请用下面的
> launchd 方式。

### 常驻运行（macOS 开机自启）

`serve.sh` 适合临时用；要长期稳定（关终端不退出、崩溃自动拉起、开机自启），
用 macOS 原生的 launchd：

```bash
cp scripts/com.stocklab.server.plist ~/Library/LaunchAgents/
launchctl load  ~/Library/LaunchAgents/com.stocklab.server.plist
launchctl start com.stocklab.server

# 查看状态
launchctl print gui/$(id -u)/com.stocklab.server | head -8

# 停止 / 卸载
launchctl stop   com.stocklab.server
launchctl unload ~/Library/LaunchAgents/com.stocklab.server.plist
```

配置里已启用 `KeepAlive`，进程被杀或崩溃会自动重启（实测 `kill -9` 后
数秒内自动恢复，服务不中断）；`RunAtLoad` 让登录即启动。

> plist 里的路径是按当前机器写死的，换机器需改 `ProgramArguments`
> 与 `WorkingDirectory` 的绝对路径。
> 注意：`launchctl load` 只接受 `~/Library/LaunchAgents/` 下的 plist，
> 直接 load 项目里的文件会报 `Input/output error`。

### 四层测试一览

```bash
./scripts/selftest.sh        # 21 项：环境与数据源连通性
./scripts/robustness.sh      # 38 项：输入边界与健壮性（不得 5xx）
./scripts/frontend-test.sh   # 31 项：前端界面真实执行
# 另有一组接口级回归（覆盖 30+ 个接口），见下方「接口清单」
```

---

### AI 研究简报

```ini
SL_AI_ENABLED=1
SL_AI_BASE_URL=https://api.deepseek.com/v1
SL_AI_API_KEY=sk-xxxxxxxx
SL_AI_MODEL=deepseek-chat
```

任何 **OpenAI 兼容** 接口都可以：

| 服务 | SL_AI_BASE_URL | SL_AI_MODEL |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://192.168.1.x:11434/v1` | `qwen2.5:14b` |

> **不配置也能用**：系统会自动降级为「本地规则引擎」，基于真实指标和财务数据
> 生成结构化简报（含估值推算、支撑压力位、风险点、关注要点），不依赖任何外部服务。

### 消息推送

```ini
SL_NOTIFY_WECOM=          # 企业微信群机器人 Webhook 完整地址
SL_NOTIFY_TG_TOKEN=       # Telegram Bot Token
SL_NOTIFY_TG_CHAT=        # Telegram Chat ID
SL_NOTIFY_SERVERCHAN=     # Server酱 SendKey
SL_NOTIFY_BARK=           # Bark 地址，如 https://api.day.app/你的Key
SL_NOTIFY_WEBHOOK=        # 通用 Webhook，POST JSON
```

填哪个就启用哪个，可同时填多个。配好后到「提醒」页点 **测试推送** 验证。

微信推送最快的方式：群里 → 右上角 → 群机器人 → 添加 → 复制 Webhook 地址。

---

## 四、功能使用

### 总览

主要指数、涨跌家数、涨跌停统计、赚钱效应，以及涨幅/跌幅/成交额/换手率四个排行榜。
点任意股票名可直接跳到个股详情。

### 自选股

支持分组管理，A股 / 港股 / ETF 可混在一个列表里。勾选「技术评分」会额外显示
每只票的技术面多空评级。开启自动刷新后每 10 秒更新一次行情。

添加方式：输入代码（`600519.SH`、`600519`、`00700`）或名称（`茅台`、`平安`）均可，带实时联想。

### 个股详情

- **分时图**（默认视图）：当日价格线 + 均价线 + 分钟量柱，昨收基准线，
  涨跌幅轴以昨收为中心对称（避免视觉误判涨跌幅度）。A股 241 点（09:30–15:00），
  港股 331 点（09:30–16:00）
- **K线图**：蜡烛图 + MA5/10/20/60，支持 5分/30分/60分/日/周/月，可缩放拖动
- **副图**：MACD / KDJ / RSI 三选一
- **箱体分析**：识别股价震荡区间，在 K线图上画出箱顶（压力）/箱底（支撑）
  与半透明箱体区域，并给出当前位置百分比、触碰次数、置信度。
  关键是**先判断是不是箱体**——趋势行情里硬画箱体会误导，
  所以用线性回归斜率 + 价格穿越中轴次数区分「震荡箱体 / 上升通道 / 下降通道」，
  非震荡形态会明确标注并降低置信度
- **技术指标**：20+ 指标实时值 + 多空综合评分 + 触发信号标签
- **基本面**：EPS、ROE、毛利率、资产负债率等，附最近 8 期营收/净利趋势
- **AI 简报**：一键生成研究报告

### 选股

9 个内置预设方案，也可自定义：

| 预设 | 说明 |
|---|---|
| 低估值蓝筹 | PE 5~20、PB<3、市值>500亿 |
| 均线金叉 | MA5 上穿 MA20 且站上 60 日线 |
| 放量突破 | 量比>2 且创 60 日新高 |
| 超跌反弹 | RSI6<30 且触及布林下轨 |
| 强势多头 | 均线多头排列 + MACD 红柱 |
| **箱体底部** | 确认震荡箱体且位置≤20% |
| **放量突破箱顶** | 突破箱体上沿且量能配合（缩量突破多为假突破） |
| **箱体震荡（高置信）** | 震荡箱体且置信度≥70%，适合区间高抛低吸 |
| 低估值+高换手 | PE<15、换手率 3%~15% |

- **快照条件**（毫秒级）：最新价、涨跌幅、成交额、换手率、PE、PB、市值、振幅
- **技术条件**（需拉K线，共 21 种）：均线金叉/多头排列、MACD金叉、KDJ金叉、
  RSI超买超卖、放量、创60日新高/新低、布林上下轨、连续上涨
- **箱体条件**（6 种）：处于震荡箱体、接近箱底、接近箱顶、向上突破箱顶、
  向下跌破箱底、放量突破箱顶。全部**先确认形态再判断位置**——
  趋势行情里位置百分比没有意义（单边下跌的票位置永远是 0%），
  所以除突破类外一律要求形态为「震荡箱体」

> 技术面条件需要逐只拉取K线，默认扫描前 150 只高成交额标的，约需 1~2 分钟。
> K线有 15 分钟缓存，同一方案重复执行会快很多。想扫更多可在请求里调 `tech_scan_limit`。

### 回测

8 种内置策略：双均线、MACD、KDJ、RSI超卖反转、布林突破、动量、放量突破、买入持有。

**严格按 A股真实规则建模**，不做"理想化回测"：

| 项目 | 处理方式 |
|---|---|
| T+1 | 当日买入次日才可卖 |
| 涨跌停 | 涨停买不进、跌停卖不出（主板10%、创业板/科创板20%、北交所30%） |
| 佣金 | 双边万2.5，单笔最低 5 元 |
| 印花税 | 卖出单边千0.5 |
| 过户费 | 沪市双边 0.001% |
| 滑点 | 双边各 0.05% |
| 交易单位 | A股按 100 股整手 |

信号在 T 日收盘产生、T+1 开盘成交，**不使用未来数据**。

输出指标：总收益、年化、基准对比、超额收益、最大回撤、夏普、索提诺、卡玛、
胜率、盈亏比、平均持仓天数、总手续费、持仓占比，以及完整资金曲线和逐笔交易明细。

> **高价股提示**：若初始资金买不起 1 手（比如茅台一手约 15 万），系统会明确提示
> 并全程空仓，不会假装成交。此时可提高初始资金，或勾选「允许零股」模式做研究性回测。

### 提醒

9 种规则：价格上穿/下穿、涨幅/跌幅超过、量比超过、均线金叉/死叉、接近N日新高/新低。

每条规则有独立冷却时间（默认 1800 秒），避免盘中反复轰炸。
盘中每 60 秒自动巡检一次（可通过 `SL_INTRADAY_INTERVAL` 调整）。

---

## 五、定时任务

容器启动后自动运行，无需配置 cron：

| 任务 | 时间 | 说明 |
|---|---|---|
| 全市场快照 | 交易日 9:05 / 12:35 / 15:05 | 更新选股与搜索数据 |
| 自选股K线同步 | 交易日 15:30 / 18:00 | 把日线沉淀到本地库 |
| 盘中提醒巡检 | 交易时段每 60 秒 | 触发规则并推送 |
| 清理 | 每日 03:00 | 清缓存、删 5 年前数据 |

时区已固定为 `Asia/Shanghai`，不依赖 NAS 系统时区设置。

---

## 六、运维

```bash
cd /volume1/docker/stock-lab

sudo docker compose logs -f          # 看日志
sudo docker compose restart          # 重启
sudo docker compose down             # 停止
sudo docker compose up -d --build    # 改代码后重新构建
```

### 备份

只需备份 `data/` 目录，里面有全部数据：

```bash
sudo tar czf stocklab-backup-$(date +%F).tar.gz data/
```

### 数据说明

| 路径 | 内容 |
|---|---|
| `data/db/stocklab.sqlite3` | 自选股、K线、财务、提醒、回测记录 |
| `data/cache/` | 缓存 |
| `data/reports/` | 预留报告输出目录 |

---

## 七、常见问题

**Q：首页打开是空白 / 一直转圈**
先看 `docker compose logs`。多半是端口冲突，改 `.env` 里的 `SL_HOST_PORT` 即可。

**Q：提示"未授权"**
你在 `.env` 里设了 `SL_ACCESS_TOKEN`。用 `http://IP:8787/?token=你的令牌` 访问一次。

**Q：行情不更新 / 部分股票没有价格**
数据源限流了。等几十秒会自动恢复；系统已内置分片主机轮换和退避重试。
若长期不稳定，把 `SL_MIN_REQUEST_INTERVAL` 调到 `0.3`。

**Q：选股结果为空**
先确认「设置」页的"快照条数"接近 9900。如果是 0，点「刷新全市场快照」。
如果快照正常但仍无结果，是条件太严，放宽 PE / 成交额等阈值试试。

**Q：ETF、指数、港股的财务数据是空的**
正常。这些标的没有财报披露，系统已做兜底提示。

**Q：回测收益是 0、交易次数是 0**
初始资金买不起 1 手。看回测结果里的黄色警告，按提示提高资金或勾选「允许零股」。

**Q：想让手机在外网也能访问**
不要直接把 8787 端口映射到公网而不设令牌。推荐方案：
① 设置 `SL_ACCESS_TOKEN`；② 用群晖自带的 **反向代理 + Let's Encrypt 证书** 套 HTTPS；
或 ③ 用 Tailscale / WireGuard 组内网，最安全。

**Q：改了代码怎么生效**
`sudo docker compose up -d --build`

**Q：某个数据源挂了怎么办**
不用管。三个源会自动降级，连续失败的源会被熔断 45 秒自动跳过。
想知道当前状态：`http://NAS:8787/api/status` 里的 `breakers` 字段会显示各源是否在熔断。
自检的「数据源可靠性采样」也能看出哪个源不稳。

**Q：详情页有时候「主力净流入」是空的**
这是设计行为。该字段只有东财提供，且东财有频控。系统给它设了
`SL_ENRICH_BUDGET` 时间预算（默认 1.5 秒），超时就放弃补充、先把页面给你，
而不是让你干等。刷新一次通常就有了。

**Q：怎么确认部署没问题**
跑 `./scripts/selftest.sh`。21 项检查全绿就可以放心用了。

---

## 八、开发者：本地运行

```bash
python3.11 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
SL_SCHEDULER_ENABLED=0 .venv/bin/python -m uvicorn app.main:app --reload --port 8787
```

接口文档：`http://127.0.0.1:8787/api/docs`

### 项目结构

```
stock-lab/
├── app/
│   ├── main.py               FastAPI 入口、鉴权中间件
│   ├── config.py             配置（全部可用环境变量覆盖）
│   ├── db.py                 SQLite 数据层（WAL 模式）
│   ├── symbols.py            代码规范化（含 000001 双身份处理）
│   ├── sources/
│   │   ├── base.py           限速 / 退避重试 / 分片主机池
│   │   ├── eastmoney.py      东财（主源）
│   │   ├── tencent.py        腾讯（备源 + 港股K线）
│   │   ├── sina.py           新浪（第三备源）
│   │   └── market.py         统一门面：多源容错 + 落库 + 搜索
│   ├── services/
│   │   ├── indicators.py     25+ 技术指标（纯 numpy，无需 TA-Lib）
│   │   ├── backtest.py       回测引擎（A股规则完整建模）
│   │   ├── screener.py       两段式选股
│   │   ├── quote.py          自选股与市场状态
│   │   ├── ai.py             AI 简报（含本地规则引擎兜底）
│   │   ├── alerts.py         提醒规则引擎
│   │   └── notify.py         5 种推送渠道
│   ├── api/routes.py         36 个 HTTP 接口
│   ├── tasks/scheduler.py    APScheduler 定时任务
│   └── web/                  前端（原生 JS + 本地化 ECharts）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

### 设计取舍

- **Dockerfile 里不用 apt**：群晖在国内网络下访问 Debian 源经常超时，而 apt 失败会
  让整个构建中断。时区数据改用 PyPI 的 `tzdata` 包提供，构建过程只依赖 pip。
  实测清空系统时区搜索路径后 `ZoneInfo("Asia/Shanghai")` 与 APScheduler 均正常，
  调度任务仍能算出正确的下次执行时间。
- **不用 pandas / TA-Lib**：群晖没有编译工具链，pip 装 TA-Lib 必然失败。指标全部
  用 numpy 纯实现，单只股票 5000 根日线毫秒级完成。
- **不用 ORM**：查询都是简单 KV 和时间序列，直接 SQLite + WAL，少一层依赖少一处故障。
- **ECharts 本地化**：前端不引用任何 CDN，内网断网也能正常显示图表。
- **主机池横跨两个域名族**：东财的封禁按**域名族**生效，不是整站。实测
  `push2.eastmoney.com` 及全部编号分片被长时间断连时，`push2delay.eastmoney.com`
  及全部编号分片**同时完全正常**。所以主机池同时包含两族（delay 族优先），
  否则被封时东财通道会整体失效、连港股列表都拿不到。
  另外东财限流是 **IP 级**的，所以还必须有熔断 —— 否则每个请求都白等 3 秒重试。
- **腾讯做行情主源**：不是因为更全，而是实测快 7 倍（39ms vs 289ms）且同样稳定。
  字段全但慢 7 倍的源不该做主源。

### 开发中踩过的坑（都已在代码里防住）

记录下来是因为它们都属于「不报错但结果是错的」类型，最难发现：

1. **东财分页每页只返回 100 条**，`pz=200` 无效。按请求的 page_size 推算页数
   会少抓一半数据（5560 只 A股只入库 2800 只）且**完全不报错**。
   现在按首屏实际返回条数推算页大小。
2. **给函数加了参数却忘了改被调函数签名**，导致某数据源每次调用都抛
   `TypeError`，又**被兜底逻辑静默吞掉**改用备用源 —— 表面上一切正常，
   实际上那条通道 100% 是死的。现在 `TypeError/AttributeError/NameError`
   会以 ERROR 级别记日志，不再混在普通网络失败里。
3. **主机池全部冷却时清空冷却重试全部主机**，一次失败引发 12 台请求风暴，
   反而把限流推向更糟。现在只挑最早恢复的一台。
4. **自检本身把接口打成限流**，于是「自检报告数据源不可用」—— 检查工具
   自己制造了假故障。现在自检的联网检查之间会节流。
5. **自检用 `1e-6` 容差比较经过 `round(4)` 的值**，把正确的计算判成错误。
   容差必须大于舍入精度。
6. **按涨跌幅排序分页会丢股票**。东财列表默认按涨跌幅排，而盘中涨跌幅
   时刻在变，股票会在页与页之间移动 —— 有的被翻两次、有的永远翻不到。
   实测 6 页重复 3 条，放大到 56 页约丢 30 只且每次不同，**选股结果直接偏掉**。
   改用按代码(`f12`)升序这个稳定键后，页与页严格连续，实测 5560/5560 零缺失。
7. **首次启动首页会卡死**。`ensure_snapshot` 原本在请求里同步抓全市场，
   空库时首页要等 1-2 分钟（浏览器直接超时）。改为后台线程异步抓取、
   接口立即返回，首页从 >60 秒降到 ~0.4 秒，并显示初始化进度。
8. **非法输入被静默忽略，比报错更危险**。选股条件把字段名写错
   （`pe_ratio` 而不是 `pe`）时，SQL 里那个条件被跳过，接口仍然返回 200
   和一堆股票 —— 用户以为"条件生效了，只是选得宽松"，实际条件根本没起作用。
   同样地，提醒规则缺 `value` 也能创建成功，但那条规则**永远不会触发**。
   现在非法条件/参数一律明确回报；前端用黄色警示框列出未生效的设置。
9. **`LIKE` 通配符没转义**。搜索框输入 `%` 或 `_` 会被当成通配符匹配所有记录，
   返回一堆随机股票。现在转义后 `%` 返回空结果，正常搜索不受影响。
10. **只在一个域名族里做主机轮换是不够的**。原本主机池只有
   `N.push2.eastmoney.com`，结果整族被封（11 个分片全部断连）时全军覆没，
   连港股列表都拿不到。后来发现 `push2delay.eastmoney.com` 整族**同时完全正常**
   —— 东财封禁是按**域名族**生效的。现在主机池横跨两族（delay 族优先），
   `push2` 全族被封期间 A股 5560 / 港股 2922 / ETF 1614 仍能完整抓到，
   而且因为第一个主机就能成功，抓取耗时从 62s 降到 26s。
   K线所在的 `push2his` 没有 delay 变体（`push2hisdelay` 返回 302 不存在），
   所以东财 K线被封期间由腾讯兜底；腾讯 K线再被限流时还有新浪兜底，
   三层下来 K线基本不会整体失效。

11. **分时接口没有备用源**。分时（trends2）原本只走东财 `push2his`，
   而该域名族被封时接口返回 0 个点、**页面显示空白但接口是 200**，
   从报错里看不出问题。实测东财 `trends2` 在 `push2delay` 域名下依然可用
   （虽然接口路径挂在 push2his 上），于是给分时单独建了横跨两族的池；
   并加入腾讯 `minute/query` 作为第二源。注意腾讯返回的成交量与成交额是
   **累计值**，必须差分还原成每分钟，否则分时量柱会单调递增、看不出放量缩量。
12. **K线也需要三个源**。原本 K线只有东财 + 腾讯两个源。实测东财
   `push2his` 整族被封、同时腾讯 `web.ifzq.gtimg.cn` 的 K线接口对所有参数
   返回 **501 反爬页面**（连 `day` 都挂），于是 K线功能整体不可用。
   加入新浪 K线接口作为第三源后，在上述劣化状态下各周期/各市场
   （A股/指数/ETF/港股）全部正常。新浪 K线不覆盖港股，港股此时靠腾讯的
   另一个端点（`hkfqkline`）兜底。
13. **`PE < 10` 会选出亏损公司**。PE 为负表示公司亏损，而 `PE < 10`
   在数学上会把 -131、-2697 这些值一并选中。实测用户想找"低估值"，
   拿到的却是芯原股份(PE=-131.58)、中科飞测(PE=-2697.14) 这类亏损股。
   现在结果里出现负 PE 且用户没写 `PE > 0` 时会明确提示。

这些坑的共同教训：**任何"看起来正常"都必须用数据验证**。所以本项目把
可验证性做进了产品本身 —— 部署自检、数据源成功率采样、熔断状态、
快照完整性都可通过接口查看。

### 关于首次初始化的失败重试

后台抓取失败时会**整体重试 3 次**（间隔 12s / 24s）。若仍失败，进入
**退避阶梯**：60s → 180s → 600s → 900s，避免对一个已经封禁的接口做请求放大
（越试越封）。前端的初始化横幅会明确显示「初始化失败，X 秒后自动重试」和
失败原因，而不是无限转圈。期间自选、个股行情、K线、回测等功能照常可用。

### 关于快照完整性

`/api/snapshot/status` 会同时返回「期望条数」和「实际入库数」：

```json
{"ready": true, "count": 5560, "expected": 5560,
 "complete": true, "failed_pages": 0}
```

抓取时若有个别页因频控失败，系统会自动做两轮补偿重试（等待 2s / 6s）。
仍有失败时 `complete` 会变 `false`，并在「设置」页用红色标出 —— 不会让你
在不知情的情况下用一个少了几十只股票的快照去选股。再点一次
「刷新全市场快照」即可补齐（入库用 UPSERT，不会重复）。

---

## 免责声明

本系统所有数据来自公开行情接口，可能存在延迟、缺失或错误。
回测结果基于历史数据，**不代表未来收益**。系统输出的一切内容仅供研究参考，
**不构成任何投资建议**。据此操作，风险自负。

---

## 许可证

MIT License —— 详见 [LICENSE](LICENSE)。

本项目为个人自用的股票研究工具，仅供学习与研究使用。
所有行情数据来自第三方公开接口，版权归各数据提供方所有。
**本项目不构成任何投资建议。**
