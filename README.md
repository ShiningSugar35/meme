# Solana Meme Trading Bot

自动化 Solana meme 代币交易系统。包含 FastAPI 后端和 React + Vite 前端，支持模拟交易和实盘（实盘默认关闭）。

**安全规则：**

- `DRY_RUN=true`、`LIVE_TRADING_ENABLED=false` 默认不广播交易
- 禁止提交 `.env` 到仓库（在 `.gitignore` 中）
- 所有日志脱敏，不记录完整 API key、private key、raw transaction
- Jito 不可用时严禁 fallback RPC 广播

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
APP_ENV=development
SQLITE_PATH=./data/trading_bot.sqlite3
DRY_RUN=true
LIVE_TRADING_ENABLED=false
PROVIDER_MODE=mock

# GMGN (动态编号: GMGN_API_KEY_1, _2, ... _N)
GMGN_API_BASE_URL=https://api.gmgn.ai
GMGN_API_KEY_1=

# Jupiter (动态编号: JUPITER_API_KEY_1, _2, ... _N)
JUPITER_API_BASE_URL=https://quote-api.jup.ag
JUPITER_API_KEY_1=

# RPC (动态编号: ANKR_API_KEY_1, _2, ... _N)
ANKR_API_KEY_1=
SOLANA_RPC_HTTP_PRIMARY=https://api.mainnet-beta.solana.com

# Jito
JITO_ENABLED=true
JITO_BLOCK_ENGINE_URL=https://mainnet.block-engine.jito.wtf

# Wallet (仅实盘需要)
WALLET_PUBLIC_KEY=
WALLET_PRIVATE_KEY_BASE58=
```

API key 支持任意数量动态扫描，新增 key 只需在 `.env` 中按编号添加即可。

### 3. 启动后端

```bash
python -m uvicorn backend.app.main:app --reload
```

端口 8000，健康检查 `GET /health`。

### 4. 启动前端

```bash
cd frontend
npm install
npm run dev
```

端口 5173，Vite 自动代理 `/api` 到后端 8000。CORS 已配置 localhost:5173。

### 5. 运行测试

```bash
python -m pytest -q
```

159 个测试通过，6 个 smoke 测试默认跳过（需 `--run-smoke` 启用真实 API 联调）。

## 项目结构

```
meme/
├── .env                    # 密钥配置（不提交）
├── .gitignore
├── requirements.txt
├── backend/
│   └── app/
│       ├── main.py         # FastAPI 入口 + CORS
│       ├── config.py       # 动态 API key 扫描 + 风控参数
│       ├── api/            # 21 个 REST 路由
│       ├── db/
│       │   ├── schema.sql
│       │   ├── database.py
│       │   └── repositories.py
│       ├── providers/      # GMGN / Jupiter / Jito / RPC
│       ├── runners/        # 5 个异步 Runner
│       ├── services/       # PriceAggregator / EventBus / ProviderFactory
│       ├── strategy/       # Filters / Sizing / Slippage / Exit Rules
│       ├── trading/        # 交易执行管线
│       └── tests/          # 159 个测试
├── frontend/               # React + Vite + TypeScript + Tailwind
│   ├── src/
│   │   ├── pages/          # 7 个页面
│   │   └── api/client.ts   # API 封装
│   └── ...
└── docs/
    └── API_FIELD_MAPPING_NOTES.md
```

## 核心架构

### Provider Mode（三模）

| 模式 | 说明 |
|------|------|
| `mock` | 本地模拟数据，不调用外部 API |
| `online_readonly` | 调用真实 API，只读，不广播交易 |
| `live` | 真实 API + 可广播交易（需 LIVE_TRADING_ENABLED=true） |

### PriceAggregator（三层降级）

1. GMGN WebSocket 订阅 → 2. GMGN latest price → 3. Jupiter quote fallback

tick_snapshots 记录 source：`GMGN_SUBSCRIPTION` / `GMGN_LATEST` / `JUPITER_QUOTE_FALLBACK`

### Runner 清单

| Runner | 职责 |
|--------|------|
| DiscoveryRunner | 60s 轮询 GMGN trenches，去重 |
| SecondFilterRunner | 二筛 + Jupiter 验证 |
| PriceMonitorRunner | 使用 PriceAggregator 采集 tick |
| PositionRiskRunner | 动态扫描频率 + dust 强平 (0.125 SOL) |
| KillSwitchRunner | rolling 10 ROI ≤ -20% 暂停新开仓 |

### 安全门（Safety Gate）

交易执行前校验：PROVIDER_MODE=mock 放行测试；非 mock 时 DRY_RUN=true 或 LIVE_TRADING_ENABLED=false 或 JITO_ENABLED=false 均阻止广播。

## API 端点

基础路径 `http://localhost:8000`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 系统健康 |
| GET | `/api/config/strategies` | 策略列表 |
| POST | `/api/config/strategies` | 创建策略 |
| PUT | `/api/config/strategies/{id}` | 更新策略 |
| POST | `/api/config/apply` | 应用配置 |
| POST | `/api/config/pause-new-entries` | 暂停开仓 |
| POST | `/api/config/resume-new-entries` | 恢复开仓 |
| GET | `/api/tokens` | 代币列表 |
| GET | `/api/tokens/{mint}` | 代币详情 |
| GET | `/api/tokens/{mint}/snapshots` | 指标快照 |
| GET | `/api/tokens/{mint}/decisions` | 策略决策 |
| GET | `/api/positions` | 持仓列表 |
| GET | `/api/positions/{id}` | 持仓详情 |
| POST | `/api/positions/{id}/manual-close` | 手动平仓 |
| GET | `/api/trades` | 交易记录 |
| GET | `/api/trades/provider-requests` | Provider 日志 |
| GET | `/api/logs/recent` | 近期系统日志 |
| GET | `/api/logs/stream` | SSE 实时日志流 |
| GET | `/api/risk/kill-switch` | Kill switch 状态 |
| POST | `/api/risk/kill-switch/reset` | 重置 |
| GET | `/api/providers/health` | Provider 健康 |
| POST | `/api/mock/run-once` | 触发模拟生命周期 |
