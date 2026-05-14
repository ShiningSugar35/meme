# Solana Meme Trading Bot

自动化 Solana meme 代币发现、二筛、模拟/实盘执行与持仓风控系统。项目由 FastAPI 后端和 React + Vite 前端组成，当前主链路围绕 GMGN OpenAPI 的 Trenches、Token Info/Security/Pool、K-line、Top Holders 数据构建，并通过 Jupiter/Jito/RPC Provider 承接交易执行与链上状态确认。

当前实现目标不是“无脑追新币”，而是将新池发现、风控字段校验、K 线二筛、仓位 sizing、分层止盈止损、风险重扫和前端运行态控制打通成一个可观测、可回滚、可逐步实盘化的工程闭环。

**安全规则：**

- 默认优先使用模拟或只读联调；开启真实广播前必须人工复核 `.env`、钱包、Provider Mode 和前端运行态。
- 禁止提交 `.env` 到仓库；`.env` 应始终保留在 `.gitignore` 中。
- 日志、Provider 请求记录和前端展示不得输出完整 API key、private key、raw transaction。
- Jito 不可用时严禁自动 fallback 到普通 RPC 广播。
- `PROVIDER_MODE=live` 只代表允许使用真实 Provider；是否真实开仓仍需经过后端安全门和前端运行态开关。
- 建议先跑通“真实行情 + 模拟交易”，确认发现、二筛、建仓、撤仓和风控闭环稳定后，再逐步打开实盘入口。

## 快速开始

### 1. 环境准备

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
pip install -r requirements.txt
```

### 2. 配置文件

复制 `.env.template` 或直接创建 `.env`，参考以下结构：

```
# Environment Configuration for Solana Meme Trading Bot
APP_ENV=development
SQLITE_PATH=./data/trading_bot.sqlite3
PROVIDER_MODE=live
# CRITICAL SAFETY SETTINGS - Always review before enabling live trading
# DRY_RUN=true blocks all real transaction broadcasts (default: true)
DRY_RUN=false

SIMULATION_ENABLED=true

# GMGN API Configuration (Market Data Provider)
GMGN_API_BASE_URL=https://openapi.gmgn.ai

# trenches
GMGN_TRENCHES_PATH=/v1/trenches
GMGN_TRENCHES_METHOD=POST
GMGN_TRENCHES_TYPES=new_creation
GMGN_TRENCHES_PLATFORMS=Pump.fun,Moonshot,moonshot_app,letsbonk,memoo,token_mill,jup_studio,bags,believe,heaven

# token / kline
GMGN_TOKEN_INFO_PATH=/v1/token/info
GMGN_TOKEN_SECURITY_PATH=/v1/token/security
GMGN_TOKEN_POOL_INFO_PATH=/v1/token/pool_info
GMGN_KLINE_PATH=/v1/market/token_kline

GMGN_API_KEY_1=
GMGN_PUBLIC_KEY_1=
GMGN_PRIVATE_KEY_1=

GMGN_API_KEY_2=
GMGN_PUBLIC_KEY_2=
GMGN_PRIVATE_KEY_2=

GMGN_API_KEY_3=
GMGN_PUBLIC_KEY_3=
GMGN_PRIVATE_KEY_3=

GMGN_API_KEY_4=
GMGN_PUBLIC_KEY_4=
GMGN_PRIVATE_KEY_4=

GMGN_API_KEY_5=
GMGN_PUBLIC_KEY_5=
GMGN_PRIVATE_KEY_5=

GMGN_API_KEY_6=
GMGN_PUBLIC_KEY_6=
GMGN_PRIVATE_KEY_6=

GMGN_API_KEY_7=
GMGN_PUBLIC_KEY_7=
GMGN_PRIVATE_KEY_7=

GMGN_API_KEY_8=
GMGN_PUBLIC_KEY_8=
GMGN_PRIVATE_KEY_8=

GMGN_API_KEY_9=
GMGN_PUBLIC_KEY_9=
GMGN_PRIVATE_KEY_9=

GMGN_API_KEY_10=
GMGN_PUBLIC_KEY_10=
GMGN_PRIVATE_KEY_10=

GMGN_API_KEY_11=
GMGN_PUBLIC_KEY_11=
GMGN_PRIVATE_KEY_11=

GMGN_API_KEY_12=
GMGN_PUBLIC_KEY_12=
GMGN_PRIVATE_KEY_12=

# Jupiter API Configuration (Swap Provider)
JUPITER_API_BASE_URL=https://api.jup.ag/swap/v1
JUPITER_API_KEY_1=
JUPITER_API_KEY_2=
JUPITER_API_KEY_3=

# Ankr RPC Configuration
ANKR_API_KEY_1=
ANKR_API_KEY_2=

#Alchemy RPC
ALCHEMY_API_KEY_1=key1
ALCHEMY_API_KEY_2=key2
ALCHEMY_API_KEY_3=key3
ALCHEMY_API_KEY_4=key4

# Alchemy Solana HTTP endpoints (dynamically constructed from ANKR_API_KEY)
SOLANA_RPC_URL=https://solana-mainnet.g.alchemy.com/v2/key1
SOLANA_RPC_HTTP_URLS=https://solana-mainnet.g.alchemy.com/v2/key1,https://solana-mainnet.g.alchemy.com/v2/key2,https://solana-mainnet.g.alchemy.com/v2/key3,https://solana-mainnet.g.alchemy.com/v2/key4

# Jito Configuration (Execution Provider)
JITO_ENABLED=true
JITO_BLOCK_ENGINE_URL=https://mainnet.block-engine.jito.wtf
JITO_TIP_FLOOR_URL=https://bundles.jito.wtf/api/v1/bundles/tip_floor
JITO_TIP_STREAM_WS=wss://bundles.jito.wtf/api/v1/bundles/tip_stream

# Wallet Configuration (ONLY needed for LIVE_TRADING_ENABLED=true)
# NEVER commit real private keys to repository
WALLET_PUBLIC_KEY=
WALLET_PRIVATE_KEY_BASE58=

```

API key 支持任意数量动态扫描，新增 key 只需在 `.env` 中按编号添加即可。

### 3. 启动后端

```bash
python -m uvicorn backend.app.main:app --reload
```

默认端口：`8000`。

常用检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/runtime/status
curl http://localhost:8000/api/providers/health
```

### 4. 启动前端

```bash
cd frontend
npm install
npm run dev
```

默认端口：`5173`。Vite 开发服务器代理 `/api` 到后端 `8000`，用于控制中心、持仓页和运行日志页联动后端。

### 5. 运行测试与语法检查

```bash
python -m pytest -q
python -m py_compile backend/app/main.py
```

对单个修改文件可直接检查：

```bash
python -m py_compile backend/app/providers/gmgn_real.py
python -m py_compile backend/app/strategy/filters.py
python -m py_compile backend/app/strategy/second_filter.py
python -m py_compile backend/app/runners/position_risk_runner.py
```

### 6. 推荐调试顺序

1. 后端启动后先访问 `/api/runtime/status`，确认 Provider Mode、RPC、钱包、安全门状态。
2. 启动前端后先在 Control Center 观察运行态，不要直接开启真实入口。
3. 观察 `provider_requests`，确认 GMGN Trenches、Token Info/Security/Pool、K-line、Top Holders 返回正常。
4. 观察 `token_strategy_matches`，确认初筛不再因为字段缺失全失败。
5. 观察 `positions`，确认模拟仓能够建仓、分批撤仓、dust 全撤。
6. 最后才考虑配置真实钱包私钥与实盘开关。

## 项目结构

```text
meme/
├── README.md
├── requirements.txt
├── .env                         # 本地配置，严禁提交
├── .gitignore
├── debug_counts.py              # 本地排查脚本，统计发现/初筛/持仓计数
├── backend/
│   └── app/
│       ├── main.py              # FastAPI 入口；注册路由；启动/停止后台 runners
│       ├── config.py            # Pydantic Settings；动态扫描 API keys；Provider/RPC/风控参数
│       │
│       ├── api/
│       │   ├── routes_runtime.py # 运行态、安全门、Provider 状态、前端控制相关接口
│       │   └── ...              # tokens/positions/trades/logs/risk/config/mock 等 REST 路由
│       │
│       ├── db/
│       │   ├── schema.sql       # SQLite 表结构：tokens、snapshots、matches、positions、trades、provider_requests 等
│       │   ├── database.py      # 连接管理、初始化 schema、轻量兼容迁移
│       │   └── repositories.py  # 数据访问层；封装 token、snapshot、position、trade、runtime 状态读写
│       │
│       ├── providers/
│       │   ├── base.py          # Provider 抽象接口、数据结构、返回格式约定
│       │   ├── gmgn_real.py     # GMGN OpenAPI：trenches、token info/security/pool、kline、top holders
│       │   ├── jupiter_real.py  # Jupiter quote/swap 相关 Provider
│       │   ├── jito_real.py     # Jito bundle/tip/广播确认相关 Provider
│       │   ├── rpc_real.py      # Solana RPC HTTP 轮询、余额/交易确认等
│       │   ├── gmgn_subscriber.py # GMGN WebSocket 订阅占位；当前未实装时回退 mock subscriber
│       │   └── mock_data.py     # mock/仿真数据，用于本地闭环和测试
│       │
│       ├── runners/
│       │   ├── discovery_runner.py      # 每分钟轮询 GMGN Trenches；初筛；入库候选池
│       │   ├── second_filter_runner.py  # 一分钟后复核；K-line 二筛；最后调用 top1 holder；触发建仓
│       │   ├── price_monitor_runner.py  # 活跃持仓价格监控；写入 tick/snapshot；触发退出规则
│       │   ├── position_risk_runner.py  # 按 USD 持仓价值动态扫描风控与 top1 holder；触发风险全撤
│       │   └── mock_lifecycle_runner.py # mock 生命周期推进，用于模拟演示和调试
│       │
│       ├── services/
│       │   ├── price_aggregator.py # 价格聚合与降级：GMGN/token pool/Jupiter quote 等
│       │   ├── provider_factory.py # 根据 Provider Mode 创建 mock/readonly/live Provider
│       │   └── event_bus.py        # 运行事件、日志与前端 SSE 推送
│       │
│       ├── strategy/
│       │   ├── filters.py       # 初筛风控规则：liquidity、holder、权限、rug、wash、bundler、sniper、平台、池龄
│       │   ├── second_filter.py # 二筛规则：已完成 1m candle、5m 高低区间、volume、top1(addr_type=0)
│       │   ├── sizing.py        # 入场 sizing：min(liquidity pct, ENTRY_MAX_USD)，统一 USD 口径
│       │   ├── slippage.py      # 买入/卖出/紧急撤仓滑点上限与重报价约束
│       │   └── exit_rules.py    # 止盈止损、动态止损、时间止损、completed 全撤、dust 全撤
│       │
│       ├── trading/
│       │   └── executor.py      # 交易执行管线：模拟成交、Jupiter/Jito 实盘执行、安全门校验、交易入库
│       │
│       └── tests/               # 单元测试、runner 测试、Provider smoke 测试
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   └── src/
│       ├── api/
│       │   └── client.ts        # 前端 API 封装
│       ├── pages/
│       │   ├── ControlCenter.tsx # 运行态控制、安全门、Provider 状态、启停/暂停入口
│       │   ├── Portfolio.tsx     # 持仓、PnL、USD 价值、风险扫描间隔、top1 风控展示
│       │   ├── Operations.tsx    # Provider 请求、系统日志、运行事件和排障信息
│       │   └── ...               # tokens/trades/config/logs 等其他页面
│       ├── components/
│       └── main.tsx
│
└── docs/
    └── API_FIELD_MAPPING_NOTES.md # GMGN 字段映射、风控字段来源和兼容说明
```

## 核心运行链路

### 1. 发现与初筛

`DiscoveryRunner` 每分钟调用 GMGN Trenches，当前重点扫描 Solana `new_creation` 池子，并按配置的平台白名单过滤。候选池进入 `filters.py` 初筛，核心字段包括：

- 流动性：`liquidity_usd`
- 持仓集中度：`top_10_holder_rate`
- 合约/权限：`renounced_mint`、`renounced_freeze_account`
- 风险比例：`rug_ratio`、`entrapment_ratio`、`rat_trader_amount_rate`、`suspected_insider_hold_rate`、`bundler_trader_amount_rate`
- 交易风险：`is_wash_trading`、`sell_tax`、`fresh_wallet_rate`
- 项目方/发射台：`creator_token_status`、`dev_team_hold_rate`、`burn_status`、`platform`
- 狙击风险：`sniper_count`
- 池龄窗口：`created_timestamp` 对应 `t ~ t+60s`

通过初筛后，系统写入 token、metric snapshot 和 strategy match，并进入二筛等待队列。

### 2. 二筛与 Top Holder 校验

`SecondFilterRunner` 对初筛通过的池子等待约一分钟后再次拉取快照，确认仍满足初筛风控。随后调用 GMGN K-line：

- 使用最近已完成的 `1m` candle，不使用正在形成中的 candle。
- 不满 5 分钟时，使用开盘至今所有已完成 candle 聚合 5m 高低区间。
- `volume_1m` 使用 K-line 的 USD volume。
- `median_volume_prev_5m` 由最近历史 candle 派生。
- 价格位置规则使用当前价、`high_5m`、`low_5m` 计算。

K-line 条件通过后，最后才调用 Top Holders，校验 `addr_type=0` 的 top1 普通钱包持仓比例，避免对所有候选过早调用高权重接口。

### 3. 仓位计算与建仓

`sizing.py` 使用统一 USD 口径计算入场本金：

```text
entry_usd = min(liquidity_usd * ENTRY_SIZE_LIQUIDITY_PCT, ENTRY_MAX_USD)
```

执行层根据 `price_usd / price_sol` 或价格聚合器推导 SOL/USD，将 USD 目标仓位换算为 SOL 数量。模拟模式只记录虚拟成交；实盘模式必须同时通过 Provider Mode、安全门、钱包、Jito、滑点和重报价检查。

### 4. 持仓价格监控

`PriceMonitorRunner` 对活跃持仓按配置频率拉取价格，并持续写入 tick/snapshot。价格来源可由 GMGN token info/pool、Jupiter quote 或其他可用 provider 聚合，前端 Portfolio 使用这些数据展示当前价值、PnL、触发规则和风险状态。

### 5. 止盈止损与风险撤仓

`exit_rules.py` 负责价格和时间类退出：

- 硬止盈：达到多级价格倍数时分批撤仓。
- 硬止损：跌破指定入场价比例时分批或全撤。
- 动态止损：当前价跌破最近已完成 `1m low` 时撤仓。
- 时间止损：上次交易后 5 分钟涨幅不足阈值时撤仓。
- `type=completed` 时全部撤出。
- 持仓低于 `DUST_FORCE_EXIT_USD` 时，下次撤仓直接全撤。

`PositionRiskRunner` 负责持仓期间风控重扫。扫描频率按当前剩余 USD 价值动态读取 `.env` 配置；核心风控扫描和 Top1 holder 扫描分离，Top1 扫描可设置独立慢频率，降低 API 调用消耗。

## Provider Mode 与安全门

| 模式 | 用途 | 外部 API | 真实广播 |
|------|------|----------|----------|
| `mock` | 本地模拟和测试 | 不调用或只调用 mock | 否 |
| `online_readonly` | 真实行情联调、模拟交易 | 调用真实只读 API | 否 |
| `live` | 实盘准备或实盘运行 | 调用真实 API | 仅在所有安全门通过后允许 |

安全门主要检查：

- `PROVIDER_MODE`
- `DRY_RUN`
- 前端/后端运行态是否允许新开仓
- `WALLET_PUBLIC_KEY` / `WALLET_PRIVATE_KEY_BASE58`
- Jupiter/Jito/RPC 是否可用
- Jito 是否启用且不可 fallback RPC 广播
- rolling loss / kill switch 状态
- 滑点、价格冲击、重报价次数

## 数据源与字段映射

### GMGN

当前核心行情和风控字段来自 GMGN OpenAPI：

- `POST /v1/trenches`：新池发现、平台、流动性、风险字段、池龄、launchpad 状态。
- `GET /v1/token/info`：token 基础信息、当前价格、统计字段。
- `GET /v1/token/security`：权限、税、holder、creator、sniper、wash trading 等安全字段。
- `GET /v1/token/pool_info`：池子价格、流动性、open/migrated 状态。
- `GET /v1/market/token_kline`：1m/5m K-line，二筛和动态止损使用。
- `GET /v1/market/token_top_holders`：二筛末端与持仓期间低频 top1 holder 风控。

### Jupiter / Jito / RPC

- Jupiter：quote、可交易性验证、swap 路由。
- Jito：实盘交易广播、tip、bundle/confirmation。
- RPC：链上确认、余额、交易状态、必要的兜底查询。

### Mock

`mock_data.py` 提供模拟 token、价格路径、风控字段和成交结果，用于在无真实 API 或不想消耗额度时验证完整状态机。

## 数据库核心表

| 表 | 作用 |
|----|------|
| `tokens` | token 主表，记录 mint、symbol、name、status、首次发现时间等 |
| `token_metric_snapshots` | 每轮 GMGN/Provider 快照，记录风控字段、价格、流动性、raw_json |
| `token_strategy_matches` | 初筛/二筛规则明细，保存每条规则通过/失败原因 |
| `positions` | 仓位状态、入场价格、剩余数量、USD/SOL 价值、风险扫描调度 |
| `trades` | 买入、卖出、部分撤仓、强平等交易记录 |
| `provider_requests` | 外部 API 请求摘要、状态码、错误信息、响应摘要 |
| `runtime_state` | 暂停新开仓、kill switch、运行态开关等状态 |
| `system_logs` | 后端运行日志和前端 Operations 展示数据 |

## Runner 清单

| Runner | 周期 | 职责 |
|--------|------|------|
| `DiscoveryRunner` | `POLL_INTERVAL_SECONDS` | 拉取 Trenches，初筛，写入候选 |
| `SecondFilterRunner` | 短轮询 | 复核初筛、K-line 二筛、Top1 校验、触发建仓 |
| `PriceMonitorRunner` | `ACTIVE_POSITION_PRICE_POLL_SECONDS` | 活跃仓位价格监控和退出规则触发 |
| `PositionRiskRunner` | 动态 USD 分层 | 持仓风控扫描、Top1 慢频扫描、风险全撤、dust 全撤 |
| `MockLifecycleRunner` | mock 模式 | 推进模拟生命周期、生成演示数据 |

## 前端页面

| 页面 | 作用 |
|------|------|
| `ControlCenter.tsx` | 显示 Provider Mode、安全门、后端运行态、暂停/恢复开仓、实盘入口状态 |
| `Portfolio.tsx` | 展示持仓、当前价值、PnL、退出进度、风险扫描间隔、Top1 holder 风险 |
| `Operations.tsx` | 展示 provider requests、运行日志、错误摘要、API 调用排障信息 |
| 其他页面 | token 列表、交易列表、配置管理、日志流等辅助页面 |

## API 端点

基础路径：`http://localhost:8000`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 后端健康检查 |
| GET | `/api/runtime/status` | 当前 Provider Mode、安全门、钱包、RPC、Jito、运行态 |
| POST | `/api/runtime/mode` | 切换或确认运行模式 |
| POST | `/api/runtime/pause-new-entries` | 暂停新开仓 |
| POST | `/api/runtime/resume-new-entries` | 恢复新开仓 |
| GET | `/api/providers/health` | GMGN/Jupiter/Jito/RPC Provider 健康 |
| GET | `/api/tokens` | token 列表 |
| GET | `/api/tokens/{mint}` | token 详情 |
| GET | `/api/tokens/{mint}/snapshots` | token 指标快照 |
| GET | `/api/tokens/{mint}/decisions` | 初筛/二筛决策详情 |
| GET | `/api/positions` | 持仓列表 |
| GET | `/api/positions/{id}` | 持仓详情 |
| POST | `/api/positions/{id}/manual-close` | 手动平仓 |
| GET | `/api/trades` | 交易记录 |
| GET | `/api/trades/provider-requests` | Provider 请求日志 |
| GET | `/api/logs/recent` | 近期系统日志 |
| GET | `/api/logs/stream` | SSE 实时日志流 |
| GET | `/api/risk/kill-switch` | Kill switch 状态 |
| POST | `/api/risk/kill-switch/reset` | 重置 kill switch |
| POST | `/api/mock/run-once` | 手动触发 mock 生命周期 |

实际可用端点以 `backend/app/api/` 中注册路由为准；前端页面应优先通过 `client.ts` 封装访问。

## 排障建议

### GMGN WebSocket warning

如果看到：

```text
GMGN WebSocket subscription not yet implemented, using mock subscriber
```

这不是发现链路失败原因。当前系统核心依赖 Trenches/K-line/Token/Top Holders 轮询链路，WebSocket subscriber 仍是占位能力。

### 没有任何初筛通过

优先检查：

1. `provider_requests` 中 `/v1/trenches` 是否 `status=200`。
2. `token_strategy_matches.pass_fail_detail_json` 中失败最多的规则。
3. `token_metric_snapshots.raw_json` 是否包含 `liquidity_usd`、`top_10_holder_rate`、`burn_status`、`creator_token_status` 等字段。
4. `created_timestamp` 是否落在当前 `t ~ t+60s` 池龄窗口。
5. `platform` 是否被白名单覆盖。

### 二筛没有通过

优先检查：

1. K-line 是否返回已完成 candle。
2. 极新池是否还没有足够 1m candle。
3. `high == low` 时是否被除零保护拦截。
4. Top1 holder 接口是否返回 `addr_type=0` 钱包地址。
5. 二筛规则详情中 volume、price position、top1 三类规则哪一类失败最多。

### 模拟仓没有买入

优先检查：

1. 初筛通过数。
2. 二筛通过数。
3. sizing 计算出的 `entry_usd` 是否大于 dust。
4. runtime 是否暂停新开仓。
5. executor 安全门是否误把模拟交易当作实盘广播拦截。

### 实盘前检查

- `.env` 中真实钱包和 API key 是否完整。
- Jito 是否启用。
- RPC HTTP endpoints 是否可用。
- `DRY_RUN` 是否符合当前意图。
- 前端 Control Center 是否显示允许开仓。
- 先小额验证 quote、swap、confirmation，再扩大规模。

## 开发约定

- 字段名以内部规范化字段为准，Provider 层负责适配 GMGN 原始字段。
- 策略层不直接读取 GMGN 原始结构，避免 API 字段变动扩散到全项目。
- Runner 层只编排流程，不内嵌复杂策略公式。
- 所有外部请求必须写入 `provider_requests`，便于复盘。
- 所有策略判定必须写入 rule detail，不能只写 pass/fail。
- 实盘相关改动必须保留 mock/readonly 路径。
- 涉及私钥、API key、raw transaction 的日志必须脱敏。
