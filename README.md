# Solana Meme Trading Bot

一个面向 Solana meme token 的自动化发现、筛选、模拟/实盘交易、持仓风控和交易审计系统。项目由 FastAPI 后端、SQLite 数据库、GMGN/Jupiter/Jito/RPC Provider、React + Vite 前端组成。

当前主链路以 GMGN OpenAPI 为行情与风控数据源，使用 Jupiter Quote/Swap 进行模拟成交与实盘路由估值，使用 Jito/RPC 承接实盘广播、tip、链上确认与交易回填。

> 重要：数据库存储时间统一使用 UTC；前端展示时间统一格式化为北京时间（Asia/Shanghai）。

---

## 安全规则

- 默认优先使用 `online_readonly + SIM` 跑真实行情与模拟交易。
- 开启真实广播前必须人工复核 `.env`、钱包、Provider Mode、Jito、RPC、安全门和前端运行态。
- 禁止提交 `.env`、SQLite 数据库、日志导出、API key、私钥、raw transaction。
- `PROVIDER_MODE=live` 只代表允许使用真实 Provider；是否真实开仓仍需经过后端安全门与前端运行态开关。
- Jito 不可用时严禁自动 fallback 到普通 RPC 广播。
- 真实交易的净收益以链上 RPC `getTransaction` 回填的 wallet delta 为最终口径；回填前使用 Jupiter quote 估算口径并标记 `PENDING_RPC_BACKFILL`。
- 模拟交易使用 Jupiter quote 的保守口径，优先使用 `otherAmountThreshold`，并记录 sell tax、费用上界和 fallback 来源。

---

## 快速开始

### 1. 环境准备

```powershell
python -m venv .venv
.\.venv\Scripts\Activate
pip install -r requirements.txt
```

### 2. 配置 `.env`

示例结构如下。请不要把真实 `.env` 提交到仓库。

```env
APP_ENV=development
SQLITE_PATH=./data/trading_bot.sqlite3

# provider mode: mock | online_readonly | live
PROVIDER_MODE=online_readonly
DRY_RUN=true
SIMULATION_ENABLED=true

# GMGN
GMGN_API_BASE_URL=https://openapi.gmgn.ai
GMGN_TRENCHES_PATH=/v1/trenches
GMGN_TRENCHES_METHOD=POST
GMGN_TRENCHES_TYPES=new_creation,near_completion
GMGN_TRENCHES_PLATFORMS=Pump.fun,Moonshot,moonshot_app,letsbonk,memoo,token_mill,jup_studio,bags,believe,heaven

GMGN_TOKEN_INFO_PATH=/v1/token/info
GMGN_TOKEN_SECURITY_PATH=/v1/token/security
GMGN_TOKEN_POOL_INFO_PATH=/v1/token/pool_info
GMGN_KLINE_PATH=/v1/market/token_kline
GMGN_TOKEN_HOLDERS_PATH=/v1/market/token_top_holders

# 推荐使用编号 key；系统也兼容 CSV 样式
GMGN_API_KEY_1=
GMGN_API_KEY_2=
GMGN_API_KEY_3=
GMGN_API_KEY_4=
# client_id 不再复用，每次请求自动生成唯一 UUID；保留为空即可

# Jupiter
JUPITER_API_BASE_URL=https://api.jup.ag/swap/v1
JUPITER_API_KEY_1=

# RPC
SOLANA_RPC_URL=
SOLANA_RPC_HTTP_URLS=

# Jito
JITO_ENABLED=true
JITO_BLOCK_ENGINE_URL=https://mainnet.block-engine.jito.wtf
JITO_TIP_FLOOR_URL=https://bundles.jito.wtf/api/v1/bundles/tip_floor
JITO_TIP_STREAM_WS=wss://bundles.jito.wtf/api/v1/bundles/tip_stream

# Wallet: only needed when live trading is actually enabled
WALLET_PUBLIC_KEY=
WALLET_PRIVATE_KEY_BASE58=

# Trading / risk
POLL_INTERVAL_SECONDS=60
ACTIVE_POSITION_PRICE_POLL_SECONDS=2
DUST_FORCE_EXIT_USD=12.5
ENTRY_MAX_USD=80
ENTRY_SIZE_LIQUIDITY_PCT=0.01
```

GMGN OpenAPI 对请求频率较敏感。Trenches 路由权重较高；若返回 429 或被临时 ban，应读取响应中的 reset 时间，不要持续重试。

### 3. 启动后端

```bash
python -m uvicorn backend.app.main:app --reload
```

常用检查：

```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/runtime/status
curl http://localhost:8000/api/providers/health
curl http://localhost:8000/api/runtime/filter-stats
```

### 4. 启动前端

```bash
cd frontend
npm install
npm run dev
```

默认端口：`5173`。Vite 代理 `/api` 到后端 `8000`。

### 5. 测试

```bash
python -m pytest -q
python -m py_compile backend/app/main.py
cd frontend && npm run build
```

---

## 当前核心链路

### 1. Discovery 拉池

`DiscoveryRunner` 按运行态加载启用策略组，然后拉 GMGN Trenches：

- `new_creation`
- `near_completion`（GMGN 原始返回可能位于 `data.pump`，系统归一为 `near_completion`）

当前 type shard 逻辑会按 slot 计划分别拉两个类型。若所有尝试失败，后端会写入 `system_events`，前端可在【数据源健康诊断】和【API Slot 状态】中查看。

### 2. 严格 AND 筛选链路

当前入场筛选链路是严格 AND，不再允许 `price_filter OR kline_fallback` 这种并集逻辑：

```text
risk_filter
→ top_holder_filter
→ smart_degen_filter（仅当 x <= 0.15 / min_smart_degen_count_api 不为空时启用）
→ price_filter
→ kline_fallback
→ create position
```

任意一环失败，直接跳过后续 API 调用，以节约 GMGN 权重。

当 x > 0.15 时，聪明钱条件为 not required，不调用聪明钱 holder API，也不因聪明钱为空淘汰池子。

指标口径：

- **通过风控筛选** = `risk_filter_passed AND top_holder_filter_passed AND (smart_degen 不要求 OR smart_degen_filter_passed)`
- **价格面筛选通过** = `price_filter_passed AND kline_fallback_passed`
- **就绪可创建** = 风控通过 AND 价格面通过

### 3. 建仓

通过完整筛选链后进入 `TradingPipeline`：

- SIM：使用 Jupiter quote 估算买入 token 数量，写入模拟 BUY trade event。
- LIVE：通过 Jupiter quote/swap 与 Jito/RPC 执行真实链上交易；必须通过安全门才允许广播。

每次 BUY 后写入 `position_audits` 的 `ENTRY` audit，记录买入前完整数据快照。

### 4. 持仓监控与撤仓

`ActivePositionPriceRunner` 负责活跃仓位价格监控与价格类退出：

- 硬止盈：`>1.6x` 撤 50%；`>2.1x` 全撤
- 硬止损：`<0.7x` 撤 50%；`<0.45x` 全撤
- type 变为 `completed` 时全撤
- API 异常/空字段撤仓前应至少重试 3 次

`PositionRiskRunner` 负责持仓期间风控复查、DUST、聪明钱/Top3 触发逻辑。触发撤仓时写入 `EXIT` audit，包含具体退出原因、风控失败项、DUST 明细、聪明钱触发详情等。

### 5. 交易审计

核心表：

| 表 | 作用 |
|---|---|
| `tokens` | token 主表，记录 mint、symbol、name、pool、latest snapshot |
| `token_metric_snapshots` | GMGN 快照与归一化风控字段 |
| `token_strategy_matches` | 每一阶段筛选的 pass/fail 详情 |
| `positions` | 仓位状态、剩余数量、剩余价值、入场价、退出原因 |
| `trade_events` | 每次 BUY/SELL 交易行为流水 |
| `position_audits` | ENTRY / EXIT / DECISION 审计 JSON |
| `position_smart_money_baselines` | 聪明钱入场基线与后续比较 |
| `provider_requests` | 外部 Provider 请求摘要、状态码、错误 |
| `system_events` | 运行态事件、异常、诊断日志 |
| `runtime_state` | 运行模式、安全门、session 等 |

`POST /api/runtime/emergency/export-trade-audit` 会导出交易审计 JSON。每个 position 包含：

- `entry_metrics_source`
- `entry_metrics`
- `trade_events`
- `exit_audits`
- `loss_debug_summary`
- realized PnL

ENTRY audit 应包含买入时：

- 池子 type / launchpad / platform
- rug / entrapment / insider / bundler
- liquidity / holder_count / marketcap / volume
- top holder / fresh wallet / creator balance
- wash trading / rat / suspected insider / sell tax
- socials 明细
- burn_status / sniper_count
- TOP1 普通钱包（addr_type=0）持仓
- 1h swaps / volume / price change
- 聪明钱最大/最小持仓地址、份额、价值
- Jupiter quote 与 route 信息

EXIT audit 应包含每次卖出：

- sell_time
- exit reason code/label
- sell price / effective price / sell multiple
- sell value
- risk_failed_rules
- dust_detail
- smart_money_trigger_detail
- top3_smart_degen_trigger_detail

---

## 前端页面

### Control Center

用于运行态切换、策略组管理、交易参数编辑、安全门检查。

### Portfolio / 交易看板

包含两个模块：

1. 当前持仓
2. 历史持仓

当前持仓应按 LIVE/SIM 按钮独立请求并展示：

- LIVE 实盘策略：仅展示实盘持仓和实盘 PnL
- SIM 模拟盘策略：仅展示模拟盘持仓和模拟盘 PnL

推荐当前持仓表列：

```text
meme地址 | 交易时间 | 状态 | 当前持仓 | 收益率 | 价格变化
```

历史持仓推荐按交易行为流水展示，而不是按 position 一行展示：

```text
时间 | 方向 | 池子名 | 地址 | 交易价值 | 交易价格 | 卖出倍数 | 撤仓原因
```

### Operations / Ops & Emergency

用于导出交易审计、导出日志、备份 DB、紧急停止等。

---

## API 端点

基础路径：`http://localhost:8000`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 后端健康检查 |
| GET | `/api/runtime/status` | 运行态、安全门、worker 状态 |
| POST | `/api/runtime/mode` | 切换 IDLE / SIM_TEST / FORMAL_SIM_LIVE |
| GET | `/api/runtime/portfolio/table?account_type=SIM|LIVE` | 当前持仓表 |
| GET | `/api/runtime/trade-events-ledger?account_type=SIM|LIVE|ALL` | 交易行为流水 |
| GET | `/api/runtime/pnl-summary` | SIM/LIVE PnL 汇总 |
| GET | `/api/runtime/filter-stats` | discovery、AND 筛选、数据源健康诊断 |
| GET | `/api/runtime/trading-params` | 交易参数 |
| PUT | `/api/runtime/trading-params` | 更新交易参数 |
| POST | `/api/runtime/emergency/export-trade-audit` | 导出完整交易审计 |
| POST | `/api/runtime/emergency/export-logs` | 导出错误日志与筛选摘要 |
| POST | `/api/runtime/emergency/backup-db` | 备份 SQLite |
| POST | `/api/runtime/emergency/stop-live` | 停止实盘入口 |
| POST | `/api/runtime/emergency/resume-live` | 恢复 FORMAL_SIM_LIVE |
| POST | `/api/runtime/emergency/sell-all-live` | 当前未完整接入批量实盘清仓，返回 501 |

---


## 项目结构

```text
backend/app/
├── main.py
├── config.py
├── api/
│   └── routes_runtime.py
├── db/
│   ├── schema.sql
│   ├── database.py
│   └── repositories.py
├── providers/
│   ├── base.py
│   ├── gmgn_real.py
│   ├── jupiter_real.py
│   ├── jito_real.py
│   ├── rpc_real.py
│   └── mock_data.py
├── runners/
│   ├── discovery_runner.py
│   ├── active_position_price_runner.py
│   ├── position_risk_runner.py
│   ├── kill_switch_runner.py
│   └── mock_lifecycle_runner.py
├── strategy/
│   ├── filters.py
│   ├── thresholds.py
│   ├── sizing.py
│   └── slippage.py
└── trading/
    ├── executor.py
    ├── audit_builder.py
    ├── accounting.py
    └── fee_tip.py

frontend/src/
├── api/client.ts
├── pages/ControlCenter.tsx
├── pages/Portfolio.tsx
├── pages/Operations.tsx
└── main.tsx
```

---

## 开发约定

- Provider 层负责适配外部 API 原始字段，策略层尽量只读内部归一化字段。
- Runner 层负责编排流程，不应内嵌过多策略公式。
- 所有外部请求必须写入 `provider_requests`。
- 所有筛选判断必须写入 `token_strategy_matches.pass_fail_detail_json`。
- 所有 BUY/SELL 必须写入 `trade_events`。
- 所有仓位创建和撤仓必须写入 `position_audits`。
- 实盘相关改动必须保留 mock / online_readonly / live 三条路径。
- 涉及私钥、API key、raw transaction 的日志必须脱敏。
- 每次开发完成必须同步更新 README。
