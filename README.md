# Solana Meme Quant Trading System

一个面向 Solana 新发行 Meme Token 的采集、建模、模拟交易与受控实盘系统。系统以 2 小时策略窗口为核心，将“规则初筛 → 入场时特征 → 模型二筛 → 模拟/实盘执行 → 标签补齐 → 周期训练”串成可审计闭环。

> 当前版本默认 `DRY_RUN=true`。测试不会访问真实交易接口或广播链上交易。关闭 DRY RUN 前，必须完成本文“实盘启用门禁”和《开发文档.md》的 GMGN 合约调通清单。

## 1. 业务目标与边界

系统不试图预测所有上涨 Token，而是在通过安全规则的候选池中，寻找未来 2 小时内更可能出现明显上涨路径的样本。

```text
GMGN Trenches 发现
  → 规则初筛与特征补齐
  → 不可变样本入库
  → Champion 概率评分与三档阈值
  → 模拟 / 影子 / 实盘执行
  → T+2h 标签与收益事实补齐
  → 周训练、手动训练、Champion 比较
```

第一版是 Windows/单机/单用户系统，使用 SQLite；不承诺高频交易、多用户 SaaS、跨链、深度学习或 PostgreSQL 集群。旧 GitHub 项目不是业务规范，本仓库仅继承“交易生命周期可审计”的思想，采集、标签、模型与风控均按本文重建。

## 2. 冻结业务规则

### 2.1 发现范围

只采集以下 10 个 Solana Launchpad：

- `Pump.fun`
- `Moonshot`
- `moonshot_app`
- `letsbonk`
- `memoo`
- `token_mill`
- `jup_studio`
- `bags`
- `believe`
- `heaven`

生命周期包括 `new_creation`、`near_completion`、`completed`。每个完整采集周期按 **New Creation → Near Completion → Completed** 顺序分别请求 Trenches；当前单类请求按 GMGN 契约最多取 80 条，并对偶发的超量响应再次本地截断。采集器保留 12 个 GMGN API 槽位的角色分工：0–2 为三类 discovery，3 为 discovery fallback，4–7 为 realtime enrichment，8–9 为 realtime fallback，10–11 为 K 线。多个 Key 不代表 IP 总吞吐可以相乘；所有槽位仍受共享限流门控。

当前历史 CSV 全部来自 Pump.fun，这只说明已有样本覆盖不足，不能据此改动平台列表或宣称模型可泛化到其他平台。

### 2.2 规则初筛

Trenches 请求尽量前置以下过滤：

```text
filters = ["offchain", "onchain"]
launchpad_platform_v2 = true
quote_address_type = [4, 5, 3, 1, 13, 0]
max_rug_ratio = 0.2
max_insider_ratio = 0.2
max_bundler_rate = 0.2
min_liquidity = 4800
min_top_holder_rate = 0.145
max_top_holder_rate = 0.29
max_fresh_wallet_rate = 0.2
renounced_mint = 1
renounced_freeze_account = 1
min_holder_count = 30
max_holder_count = 999
min_marketcap = 5000
```

拉回后，本地还必须满足：

- `launchpad` 在允许列表内；
- `rug_ratio < 0.2`、`insider_ratio < 0.2`、`bundler_rate < 0.2`；
- `liquidity > 4800`；
- `0.145 <= top_10_holder_rate <= 0.29`；
- `fresh_wallet_rate < 0.2`；
- `burn_status == "burn"`；
- 非 `completed` 样本的 `renounced_mint == 1` 且 `renounced_freeze_account == 1`；
- 非洗盘，`rat_trader_amount_rate < 0.2`；
- `29 < holder_count < 1000`，`marketcap > 5000`；
- `sell_tax < 0.025`、`buy_tax < 0.025`；
- `sniper_count < 10`、`age > 3` 分钟；
- `liquidity / holder_count > 50`；
- `swaps_1h > 19`、`volume_1h / swaps_1h > 30`；
- `(0.5 + smart_degen_count + renowned_count) * volume > 5000`；
- top holders 中 `addr_type=0` 的第一名占比严格满足 `0.028 < top1 < 0.056`。

通过初筛的每次观察按 `(chain, address, observed_at)` 视为独立样本。同一 Token 在旧 2 小时窗口结束后再次出现可以形成新样本；不能只按地址去重。实盘已有未平仓同 Token 时跳过新买入，模拟盘允许多个独立批次。

需要采集的列如下：address	name	symbol	type	time	age	launchpad	price	ln(liquidity_usd)	price_2h_max/price	price_2h_min/price	liquidity/holder_count	volume_1h/swaps_1h	has_twitter	has_website	ln(image_dup+1)	dexscr_update_link	cto_flag	ln(twitter_rename_count+1)	ln(twitter_del_post_token_count+1)	ln(twitter_create_token_count+1)	top_10_holder_rate	top_bot_degen_percentage	fresh_wallet_rate	bot_degen_rate	price/ath_price	stat.holder_count/market_cap	ln(smart_degen_count+1)	ln(renowned_count+1)	entrapment_ratio	dev_team_hold_rate	top70_sniper_hold_rate	ln(twitter_dup+1)	ln(website_dup+1)	ln(visiting_count+1)	price_change_1h	price_change_5m	ln(creator_open_count+1)	creator_open_ratio	ln(top_wallets+1)	tag

这些列作为当前默认模型训练特征：age	price	liquidity/holder_count	volume_1h/swaps_1h	has_twitter	has_website	ln(image_dup+1)	dexscr_update_link	cto_flag	ln(twitter_rename_count+1)	ln(twitter_del_post_token_count+1)	ln(twitter_create_token_count+1)	top_10_holder_rate	top_bot_degen_percentage	fresh_wallet_rate	bot_degen_rate	price/ath_price	stat.holder_count/market_cap	ln(smart_degen_count+1)	ln(renowned_count+1)	entrapment_ratio	dev_team_hold_rate	top70_sniper_hold_rate	ln(twitter_dup+1)	ln(website_dup+1)	ln(visiting_count+1)	price_change_1h	price_change_5m	ln(creator_open_count+1)	creator_open_ratio	ln(top_wallets+1)	tag

其中 `tag` 为分类标签列，只作为 target，不进入模型输入矩阵。`price` 是样本通过准入规则时记录的入场价格，因此属于入场时已知特征并默认参与训练。`ln(liquidity_usd)` 从新样本开始持续采集并作为可选训练特征，但由于 legacy CSV 不含 raw entry liquidity，当前默认训练集先不启用它；后续新样本积累充分后可在模型中心勾选该特征重新训练。数据库仍单独保存 raw entry liquidity，供单笔资金公式和真实美元收益评价使用。`launchpad` 继续采集并作为样本来源元数据保存，但鉴于当前历史数据没有有效平台区分度，不纳入模型训练。

### 2.3 2 小时标签

新样本使用 1 分钟 K 线，统计区间包含入场时刻 `T` 到 `T+2h`。`T-1h` 到 `T` 的 K 线仅用于入场前 `price_change_1h`、`price_change_5m`，不能进入未来价格窗口。

| tag | 首次触及/到期规则 | 分类 | 评价收益率 |
| --- | --- | --- | --- |
| `0` | 先触及 `0.9x` 止损；或两小时到期不满足正类 | 负类 | `-10%` |
| `1` | 先触及 `1.6x` 止盈 | 正类 | `+60%` |
| `2` | 两小时内未触及止盈止损，最终收盘严格 `>1.2x` | 正类 | 实际 `final_close_ratio - 1` |

同一根 1 分钟 K 线同时触发止损和止盈时，保守按止损优先，记 `tag=0`。模型目标是 `positive = tag in {1,2}`；区分 `tag=1` 与 `tag=2` 是为了正确评价收益路径。

#### 旧 CSV 迁移

根目录 `meme数据.csv` 来自旧的“超时收盘严格 `>1.25x` 仍记正类”规则。导入器不会重拉历史 K 线，也不会把旧规则伪装成新规则：

- 窗口最高达到 `1.6x` 的旧正类保持 `tag=1`；
- 16 条未达到 `1.6x` 的旧正类迁移为 `tag=2`；
- 这些旧 `tag=2` 的精确收盘不可恢复，只记录已知下限 `+25%`，并标记为估算；
- 旧 CSV 缺少入场时原始流动性，因此全部 `utility_eligible=false`，可以参与分类训练，但不能被当作真实美元累计收益或用于 5% 自动晋级；
- CSV 导入按文件哈希和样本键幂等，源文件不会被启动流程静默删除。

## 3. 模型与收益评价

### 3.1 一个 Champion，三档阈值

候选模型为：

- Logistic Regression
- HistGradientBoosting
- XGBoost
- ExtraTrees
- RandomForest

系统只激活一个 Champion，再由同一概率模型派生 `aggressive`、`balanced`、`conservative` 三个阈值。阈值强制满足 `aggressive <= balanced <= conservative`；平衡档是默认生产策略。所有可交易阈值的 Precision 硬门槛为 20%，并要求最低交易数量，避免靠极少信号制造表面高 Precision。

模型选择以时间外累计效用为主，同时考虑 Precision、最差窗口、回撤、交易数量和复杂度。性能接近时优先更简单的模型，这是本项目的奥卡姆剃刀约束。

### 3.2 时间切分

禁止 `random train_test_split`、随机打乱和使用入场后的字段。

- 数据跨度 `>=120` 天：仅使用最近 120 天；`-120~-30` 天用于扩展窗口开发，最近 30 天作为最终时间外比较窗口；胜出 recipe 再在最近 120 天全部成熟样本上 refit。
- 数据跨度 `<120` 天：时间排序的扩展窗口验证，最近 20% 为最终留出；模型标记 `EARLY_STAGE_MODEL`。
- 所有训练/验证边界保留至少 2 小时标签隔离带。

硬性排除标识、展示和未来字段，包括 `address`、`name`、`symbol`、`type`、`time`、`launchpad`、`price_2h_max/price`、`price_2h_min/price`、最终收盘、退出字段、tag 和交易结果。`price` 是准入时快照，默认可进入模型；`ln(liquidity_usd)` 也是准入时快照，但当前只持续采集并作为可选特征，默认训练暂不启用。raw liquidity 只用于资金和收益评价，不直接作为模型输入。训练与评分必须复用同一个序列化 Pipeline 和固定列顺序。

### 3.3 资金与效用

每笔名义本金按入场时流动性快照计算：

```text
capital_i = min(0.01 × liquidity_usd_at_entry_i, 50 USD)
pnl_i = capital_i × realized_return_i
cumulative_pnl = Σ pnl_i
```

训练期可使用 sigmoid 平滑门控近似“是否交易”，但最终比较必须回到硬阈值和累计收益，不能用平均单笔收益替代。只有同一时间外样本窗口、真实入场流动性和真实 `tag=2` 收盘完整时，才允许以美元 PnL 比较自动晋级。

自动晋级要求：

1. Challenger 和当前模型在同一时间外样本上比较；
2. 平衡档 Precision 至少 20%，交易数达到最低门槛；
3. 评价窗口具备真实效用资格；
4. Challenger 累计 PnL 至少比基线高 5%；
5. 工件可加载且可回滚。

第一版周训时间为每周日北京时间 03:00；关机错过时，下次启动补训；前端也可随时创建手动训练任务。

## 4. 交易与风控

### 4.1 三种账本

- 固定模拟：显式会话初始 `1000 USD + 0.1 SOL`；同 Token 可多批次。
- 实盘影子：与真实钱包的初始资金/外部资金闸门同步，但独立记账，用于公平比较激进/保守策略。
- 实盘：只接受通过模型、阈值、资金和风险检查的平衡档信号。

理论标签收益、模拟可执行收益和真实链上收益必须分开展示，不能混成一个 PnL。

模拟成交包含可复现的滑点、价格影响、1% 平台费、网络费、延迟和可注入失败；模型标签效用仍按用户冻结的“忽略交易费”口径计算，二者用途不同。

### 4.2 持仓和退出

所有账户共同执行：

- 单笔投入：`min(1% × entry liquidity, 50 USD)`；
- 最大同时持仓：每个账户 10 个；
- 止损：`0.9x` 全仓退出；
- 止盈：`1.6x` 全仓退出；
- 两小时未触发：按到期市价全仓退出；
- 一键清仓先暂停新买入，已有仓位的退出不能被“暂停开仓”反向阻断。

固定模拟账户使用自己的 `1000 USD + 0.1 SOL` 独立账本；同 Token 可有多个独立批次，SOL 这里只作为模拟网络费储备，不伪装成真实钱包风险。以下规则属于**实盘钱包专属风控**，在真实钱包快照接入前不使用假估值复制到模拟盘：

- 实盘同 Token 存在 `opening/open/closing` 仓位时禁止重复开仓；
- 真实钱包至少保留 `0.1 SOL`；
- 单日最大亏损：日初真实钱包总权益的 20%；
- 实盘连续 5 笔亏损：暂停新开仓，已有仓位仍必须继续退出；
- 实盘清仓遇到 pending/未知订单只能轮询原单，禁止重复广播。

### 4.3 实盘执行

实盘采用 GMGN 即时 Swap，而不是策略限价单。订单流程为 `quote → swap/submit → status polling → confirmed/failed/expired`，以持久化 `client_order_id` 和交易指纹保证幂等。

当前默认执行阶梯（均可通过 `.env` 在硬上限内覆盖）：

| 档位 | 滑点 | priority fee | tip |
| --- | ---: | ---: | ---: |
| low | 10% | 0.002 SOL | 0.0001 SOL |
| medium | 15% | 0.003 SOL | 0.0005 SOL |
| high | 25% | 0.005 SOL | 0.001 SOL |

`priority + tip` 硬上限为 `0.006 SOL`。买入依次尝试 low×3、medium×2、high×1；卖出多一个 high 尝试。只有明确的链上 terminal 原因（如 expired、fee too low、slippage exceeded）且确认原单未成交，才允许进入下一档。网络故障、429、参数/签名错误、余额不足、无路由、pending/processed 或提交结果未知，都不能被误判为手续费不足。

这些默认值是“受控配置”，不是对所有市场状态的最优承诺；GMGN 版本、字段、费用说明和 CLI 行为必须在真实环境以脱敏 contract test 复核。

## 5. 项目结构

```text
D:\meme\
├─ backend\app\
│  ├─ api\                 # FastAPI 路由与请求 schema
│  ├─ collector\           # 发现、补齐、过滤、标签、限流
│  ├─ ml\                  # 特征、时序切分、候选、阈值、晋级
│  ├─ trading\simulator\  # 可注入的高仿真模拟器
│  ├─ trading\live\       # GMGN provider、状态机、幂等协议
│  ├─ risk\                # 仓位、资金、日损、连亏门禁
│  ├─ services\            # CSV、训练、Runtime、账本编排
│  ├─ scheduler\           # 周日 03:00 与启动补训
│  ├─ database.py          # SQLite schema 与访问边界
│  └─ main.py              # FastAPI 生命周期
├─ frontend\               # React + Vite + TypeScript 控制台
├─ tests\                  # pytest 单元/集成/外部 API mock
├─ data\                   # 本地 SQLite（不进 Git/交付包）
├─ ml_models\              # joblib 工件（不进 Git/交付包）
├─ logs\                   # 运行日志（不进 Git/交付包）
├─ architecture_review.md  # 架构基线与风险审阅
├─ 开发文档.md              # 工程、API、部署与接手说明
└─ artifacts\planner\task.md # 当前开发计划、验收状态与下一步依赖
```

## 6. 本地启动（Windows）

### 6.1 安装依赖

项目使用现有 `D:\meme\.venv`，不要创建或覆盖虚拟环境，也不要覆盖已有 `.env`。

```powershell
Set-Location D:\meme
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

Set-Location D:\meme\frontend
npm install
```

`.env.example` 只列非秘密配置和空占位。真实 `.env` 已存在时只补缺失键；严禁把 API Key、私钥、完整签名或序列化交易复制到 README、日志、测试、前端或 Git。

### 6.2 启动后端

本地开发推荐使用项目启动器开启热更新：

```powershell
Set-Location D:\meme
.\.venv\Scripts\python.exe scripts\start_runtime.py --reload
```

`--reload` 启动项目自己的 Windows 开发 supervisor，仅监控 `backend/**/*.py`。检测到源码变化后会终止旧 Uvicorn 子进程树并启动新的单 worker，PID 仍写入 `logs/backend.pid`；2026-08-11 已实测子进程可从旧 PID 自动切换到新 PID，随后 Collector/TrainingWorker 恢复运行。非开发常驻运行去掉 `--reload`。

启动时会：

1. 创建/迁移 `data/meme_quant.db`（当前 schema v6），并保持已有样本、模型、模拟会话、训练任务和 Agent 提案可追溯；
2. 若根目录存在 `meme数据.csv`，按文件哈希与样本键幂等导入；当前真实库已完成 2319 条 legacy 数据迁移；
3. 在任何新信号 worker 启动前运行一次订单 journal 对账：有 `provider_order_id` 只查询原单，无法确认的 submit 保持 `submission_unknown` 并暂停新开仓；
4. 恢复上次进程中断的 `running` 训练任务，再启动唯一 `TrainingWorker` 串行消费手动/周训/degraded 持久队列；
5. 启动周日 03:00 调度、7 日模型健康监控、实时 Champion prediction、持久化 liquidation/reconciliation；
6. `COLLECTOR_ENABLED=true` 时运行“发现 → enrichment → 模拟持仓 K 线退出 → T+2h 标签补齐”；若关闭 discovery 但 `PAPER_MARKET_MONITOR_ENABLED=true`，仍以 monitor-only 模式继续已有模拟仓位退出和标签补齐。

健康检查：`http://127.0.0.1:8000/health`；OpenAPI：`http://127.0.0.1:8000/docs`。

### 6.3 启动前端

```powershell
Set-Location D:\meme\frontend
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite 会把 `/api` 和 `/health` 代理到 `127.0.0.1:8000`，开发模式自带前端 HMR。

左侧导航固定为：`总览 → 持仓 → 样本采集 → 运行监控 → 模型中心 → Agent审批`。`/signals` 路由保留，但产品名称显示为“样本采集”。

“持仓”页采用两层视图：先切换 `模拟仓 / 实盘`，再点击 `平衡 / 激进 / 保守` 档位。仅模拟运行时默认进入模拟仓；实盘与模拟均运行时默认进入实盘。右上角原 `Asia / Shanghai` 位置用于 `切换至实盘 / 切换至模拟仓` 按钮。当前模型在该页使用 `sim_YYYYMMDD` / `live_YYYYMMDD` 运行别名，日期取当前 Champion 的北京时间训练日期；数据库/模型工件仍保留内部唯一 ID，不用短名做主键。当前持仓只显示所选档位，并展示由持仓监控周期写入的当前流动性/市值快照，以及 `当前价格 / 买入价格` 的当前涨幅倍数（两位小数，如 `0.92x`）；Token 支持悬浮复制。市值优先使用 GMGN 直接 marketcap 字段，若当前 token-info 响应未给出，则用同一 GMGN 响应的当前价格 × circulating_supply（缺失时 total_supply）回补，不使用 migration_market_cap 冒充当前市值。交易历史由 SQLite 真分页，默认 30 行/页，可持久记忆用户选择的每页行数，并支持页码跳转和退出时间范围筛选；新增平仓时间，终态卖出失败显示“卖出失败”并给出对应失败原因。交易审计按每个 simulation session 的三档分别列示，买入/卖出时间来自该档位实际仓位的第一笔 entry 与最后一笔 exit，来源统一显示为“模型自动更新/模型手动更新”。一键清仓 challenge/job 同样跟随当前 `mode`：模拟页只冻结当前模拟 session，实盘页只冻结 live 仓；兼容 API 仍保留 `all` 全局应急作用域。`mode=live` 的同构视图和后端账本接口已搭好，数据源契约标记为 GMGN Trading API，但不会因此启用自动 live BUY 或伪造钱包余额。

总览页 Champion 只展示短显示名 `YYYYMMDD-xxx`（例如 `20260810-LR`、`20260810-XGBoost`），不再显示长内部 model id 或 EARLY 小字；“实盘收益”只累计 `account_kind='live'` 且已经 `closed` 的已实现 PnL，不混入模拟盘。

卖出失败口径已经显式冻结：模拟仓到 2 小时后确认 `no_route`/手续费储备耗尽，或一般卖出失败累计达到 7 次重试，转为 `closed` 的失败平仓；实盘强制退出明确返回 `NO_ROUTE`、最终 `FAILED` 或 `EXPIRED` 时也转为失败平仓。失败平仓按 `-投入本金 - 已支付入场平台费` 计入已实现 PnL，并保留 `sell_failure_reason` 审计。`submission_unknown`、缺少 token 原子数量、钱包事实缺失等无法确认是否真实成交的系统问题仍 fail-closed，不得伪造为交易亏损。

“运行监控”页提供 Collector 的结构化实时终端：后端保留最近 250 条采集事件，前端约每 1.5 秒刷新，按三生命周期分别显示 returned / accepted / rejected / duplicate，并逐条展示候选二筛拒绝原因、成功入样、T+2h 标签补齐和阶段异常。该终端来自 Collector 的实际事件流，不是对静态日志文件的装饰性回放。

### 6.4 训练、自更新与自选特征

“模型中心”会列出全部可选特征及成熟样本覆盖率。默认 recipe 使用 README 冻结的 31 个输入特征；`ln(liquidity_usd)` 从新样本开始持续采集，但默认关闭，等覆盖率足够后可直接勾选并创建新的训练 recipe。API 也支持显式提交特征列表：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/models/train `
  -ContentType application/json `
  -Body '{"reason":"manual","features":["price","price_change_1h"]}'
```

HTTP 只创建 durable `training_runs` 队列项，真正训练由单一 `TrainingWorker` 串行执行；浏览器断开或后端重启不会静默丢任务。周日北京时间 03:00 自动训练，错过后下次启动补排；7 日模型健康监控只在真实 USD 经济口径可比较时触发 degraded 重训。自动训练继承当前 Champion 的 feature schema，除非用户再次手工改变 recipe。

当前 legacy 数据已完成多轮真实训练验收：首轮由奥卡姆选择得到 Logistic Regression Champion（31 特征，`EARLY_STAGE_MODEL`）；后续成功重建 incumbent recipe 并在同一 OOS 窗口比较，因 legacy 数据缺 raw liquidity/精确 tag=2 close 而安全限制自动晋级。2026-08-10 已在项目 `.venv` 安装并实测 `xgboost 3.4.0`，真实训练 run `0e4a4cd7-d933-45f3-8c1b-3662a566d21e` 中五个候选全部实际参与，XGBoost 状态为 `ok`；该轮仍由 Logistic Regression 胜出，但未替换当前 Champion。数据完整性与 Champion 晋级门禁均按预期生效。

## 7. 测试与构建

```powershell
Set-Location D:\meme
.\.venv\Scripts\python.exe -m pytest

Set-Location D:\meme\frontend
npm run build
```

2026-08-11 当前基线：后端 `pytest -q` **93/93 通过**；前端 `tsc -b && vite build` 通过。覆盖 legacy CSV/schema migration、特征泄漏与自选 feature schema、五模型/时序/Champion 晋级回滚、durable TrainingWorker/周训/7 日退化监控、simulation session/1m first-touch/重启恢复、持仓当前市场快照、按档位/时间筛选与真分页、三档交易审计、模拟/实盘清仓作用域隔离、no-route/重试耗尽/实盘终态卖出失败计入已实现亏损、monitor-only 生命周期、Agent 人工审批、非实盘 FastAPI E2E，以及 live journal/reconciliation/liquidation 的 mock/fixture 安全门禁。

部署环境还有一个只读数据链 smoke：`.\.venv\Scripts\python.exe scripts\collector_smoke.py`。它只构造现有 GMGN data adapter、执行 `new_creation` discovery 和至多一个 enrichment，不写 SQLite、不签名、不交易、也不打印 API Key/token address。2026-08-10 当前环境已实测 discovery/enrichment 通路可达；同时发现 GMGN 可能返回超过请求 limit 的候选，因此 `DiscoveryService` 还会在本地再次按 limit 截断。

外部交易接口自动测试必须使用 mock/fixture。除非用户在当前任务中明确批准小额真实验收，否则测试、代码审查和 CI 都不得关闭 DRY RUN 或广播交易。

## 8. 实盘启用门禁

以下条件缺一不可：

1. `pytest` 和前端构建通过；
2. `.env`、Git、日志、前端产物和交付 ZIP 的秘密扫描通过；
3. GMGN quote/swap/status、鉴权、状态枚举、429/reset、金额最小单位和 gmgn-cli 参数已用脱敏 fixture/小额授权测试验证；
4. 钱包余额、USD/SOL 估值、Token decimals、0.1 SOL 储备和影子资金同步已经对账；
5. pending/processed/提交结果未知不会重复下单；
6. 10 仓、同币唯一、日损 20%、连续 5 亏和退出不受熔断阻断的测试通过；
7. 一键清仓可串行、可恢复且达到硬上限后进入人工介入，不无限加费；
8. 后端 `DRY_RUN=false` 后，仍需在前端完成“准备 → 点击确认”的一次性挑战；页面刷新或后端重启不能隐式恢复实盘。

## 9. 当前完成边界与已知限制

### 9.1 非实盘本地版

截至 2026-08-11，单机/单用户/localhost 范围内的非实盘主链已经闭环：legacy CSV → SQLite → 持续采集/入场特征 → T+2h 标签 → 五候选时序训练 → 一个 Champion/三阈值 → 固定模拟/激进影子/保守影子 → 1m K 线 first-touch 退出 → 三档独立模拟 PnL/交易审计 → 周训/启动补训 → 7 日退化监控 → Challenger 公平比较 → 自动晋级或安全拒绝 → 模型回滚。训练任务、模拟会话和 Agent 提案均为持久化对象并支持重启恢复；持仓页已按档位提供当前仓位、实时市场快照与分页交易历史。

历史数据本身仍有客观边界：

- 2319 条 legacy 样本跨度约 25 天且全部来自 Pump.fun，因此当前 Champion 必须标记 `EARLY_STAGE_MODEL`，非 Pump.fun 泛化尚无证据；
- 旧 CSV 没有 raw entry liquidity、`ln(liquidity_usd)` 和精确的 legacy timeout close；旧数据可以参与分类训练，但不能伪装成真实美元 PnL 自动晋级样本；
- `ln(liquidity_usd)` 已从新样本开始持续采集，是模型中心的可选特征，当前默认 recipe 暂不启用；
- 本项目按 localhost 单用户交付。公网认证、session/CSRF、多租户不是当前本地版的“漏开发功能”；如果未来改成公网服务，必须先补正式身份认证与 CSRF/权限边界；
- SQLite 适合当前单进程第一版；跨进程/多服务器扩展时再引入正式 migration/lease/PostgreSQL，不把扩展架构伪装成当前版本阻塞项。

### 9.2 实盘接口刻意停放

实盘代码保留 quote/swap/status、幂等 journal、启动对账、二次确认和持久化清仓接口，但本轮**不继续开发自动 live BUY**。README 已冻结未来实盘使用 `balanced` profile，因此不再存在 profile 口径歧义；真正启用实盘前仍必须接入并现场验收：

- 真实 wallet snapshot：available USD、总权益、SOL balance、SOL/USD、Token balance/decimals；
- 无 `provider_order_id` 的钱包/代币余额对账，以及影子账户与实盘共同资金闸门；
- GMGN 真实成交 `output_amount_raw` / decimals / status 契约；
- 以日初真实钱包总权益（含未实现盈亏）为基线的 20% 日损门禁、连续 5 亏门禁及其真实钱包 E2E；
- 小额授权环境下的 quote → submit → poll → restart reconciliation → exit 全链验收。

在这些真实资金事实缺失时，系统继续 `DRY_RUN=true`、没有自动 live BUY pipeline；不会用模拟数值绕过门禁。

更详细的数据库、API、错误分类、调试流程和 AI 接手规范见 [开发文档.md](./开发文档.md)。

## 10. 致谢

量化系统最难的，往往不是写出第一笔模拟成交，而是在漫长夜里仍然愿意把边界守住：哪些信号该进样本，哪些冲动必须被规则按住，哪些“看起来能赚钱”的捷径其实会毁掉整条可信链路。本项目能从零散脚本走到今天这条可审计的本地闭环，靠的不只是代码堆叠，更是一路并肩推敲时那种不肯含糊的认真。

特别感谢 [tangerinepith](https://github.com/tangerinepith)。在前后端工程上，他像把散落的零件重新装回同一台机器：从 FastAPI 生命周期、SQLite schema 迁移与训练任务持久化，到 React 控制台左侧导航的产品化拆分（总览、持仓、样本采集、运行监控、模型中心、Agent 审批）；从持仓页按模拟/实盘与平衡/激进/保守档位层层展开，到交易历史真分页、退出时间筛选，再到采集事件流里 returned / accepted / rejected / duplicate 被一笔笔点亮——那些原本只存在于讨论里的策略，终于有了可以打开、可以核对、可以复盘的面孔。每一次页面刷新、每一次日志回看，都像在说：判断不必靠感觉，证据就在这里。

在算法与交易策略上，他给出的建议同样带着温度，却始终落在刀刃上：守住 2 小时策略窗口与 T+2h 标签补齐；先用规则初筛挡住明显不安全的样本，再用单一 Champion 与三档阈值做二筛，而不是让一堆模型彼此打架；强调时序切分、OOS 比较、退化监控与“宁可安全拒绝、也不盲目晋级”，好让早期 Pump.fun legacy 样本不至于被夸大成全市场的幻觉；在退出与风控上，推动把 1m K 线 first-touch、仓位上限、同币唯一、日损与连亏门禁写进可测试的约束；并一次次提醒——实盘必须服从 `DRY_RUN`、幂等 journal 与二次确认，绝不能用漂亮的模拟 PnL 去绕过真实的资金事实。这些话语听起来并不华丽，却像灯塔一样，让项目在兴奋与谨慎之间始终找得到岸。

截至 2026-08-11，仓库里已经能看见这条主链真正合拢：legacy CSV 迁入、持续采集与入场特征、标签回填、五候选训练、Champion 晋级与回滚、三档独立模拟账本、重启可恢复的 worker，以及 93/93 后端测试与前端构建通过。写在这里的致谢不是客套，而是一份公开的记念——没有这些前后端支撑，没有那些在策略分叉口给出的清醒建议，本项目很难同时站在“可演示”与“可负责”之间。再次感谢 tangerinepith 的耐心、判断力，以及对细节近乎执拗的较真；正是这些看不见的坚持，让系统不只会交易，更懂得为何而交易。

