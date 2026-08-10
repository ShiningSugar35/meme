# Solana Meme Quant Trading System - Architecture Review

> 本文主体保留早期架构审阅与设计门禁；实际实现状态以文末“2026-08-10 实现状态附录”、`README.md` 与 `开发文档.md` 为准。若早期审阅中的字段/接口假设与 README 后续冻结规则冲突，以 README 为业务事实源。

## 1. 审阅目标与原则

本架构审阅关注：数据事实可追溯、训练无泄漏、模拟与实盘收益分层、持久化任务可恢复、实盘 fail-closed、外部 API 契约隔离，以及单机第一版向未来扩展时不制造不可逆技术债。

核心原则：

- 用户最后明确确认的业务规则 > README 冻结规则 > 工程实现建议；
- 任何入场后才产生的事实都不能进入模型输入；
- 模拟成交、标签理论收益、真实链上收益分层展示；
- 关键任务必须先持久化再执行，进程重启不得静默丢任务；
- pending/unknown 不是失败，实盘不得基于模糊状态重复广播；
- 外部 GMGN 字段必须在 adapter/DTO 层归一，不让领域服务直接依赖未验证响应；
- 当前交付是 localhost 单用户单进程第一版，跨进程/公网化属于明确的后续扩展。

## 2. 领域分层

```text
GMGN Data
  ↓
Discovery / Enrichment / Safety Filter
  ↓
Immutable-ish Sample Snapshot
  ↓
Champion Prediction + 3 Thresholds
  ↓
Simulation Accounts / parked Live Interface
  ↓
1m Kline Position Monitor + T+2h Label Finalizer
  ↓
Training Queue / OOS Evaluation / Promotion / Rollback
```

建议保持以下边界：

- `collector/`：发现、补齐、过滤、Kline DTO、标签事实；
- `ml/`：特征 schema、时序切分、模型候选、阈值、经济评价与模型工件；
- `services/`：SQLite 编排、durable job、simulation session、Agent 审批、运行生命周期；
- `trading/simulator/`：无网络的可注入执行模型；
- `trading/live/`：唯一允许真实签名/广播的区域，当前保持 fail-closed；
- `risk/`：账户/钱包风控语义，不与预测模型耦合；
- `frontend/`：只展示后端事实和发起明确用户动作，不复制业务规则。

## 3. 样本与标签事实

### 3.1 样本唯一性

每次通过初筛的观察按 `(chain,address,observed_at)` 形成独立 sample。不能按 token address 唯一覆盖，因为同 Token 跨 2 小时窗口再次出现仍是新的训练事件；固定模拟盘也允许同币多批次。

### 3.2 入场时特征

生产训练采用显式 allowlist，而不是“数据库新增什么列模型就自动吃什么”。当前 README 冻结的默认训练字段中：

- `price` 是 admission-time `entry_price`，允许默认训练；
- `launchpad` 持续保存但不进模型；
- `ln(liquidity_usd)` 从新样本开始持续采集，当前 legacy 覆盖率为 0，因此默认关闭，但属于模型中心可选特征；
- raw `liquidity` 单独保留给资金公式/美元效用，不直接作为默认模型输入；
- `price_change_1h/5m` 只能使用 `T` 之前的事实。快照缺失时，应立即取 `T-1h → T` 历史 1m Kline 回补，不能等未来窗口数据参与当次评分。

硬排除：身份字段、`launchpad`、未来 2h max/min/final close、tag、first-touch/退出/PnL/成交结果、任何 observed_at 后生成字段。

### 3.3 标签

冻结规则：

- first SL 0.9x -> `tag=0`；
- first TP 1.6x -> `tag=1`；
- 两小时内均未触及，final close 严格 `>1.2x` -> `tag=2`；
- 其余 -> `tag=0`；
- 同一 1m candle 同时触发 TP/SL，止损优先。

新 schema 持久化：`first_take_profit_at`、`first_stop_loss_at`、`exit_reason`、`same_bar_conflict`、`gross_return_rate`、`return_source`。legacy tag2 使用 +25% 已知下限并明确 `legacy_floor`，不能伪装为真实 close。

## 4. 数据库与持久化

SQLite 单机第一版启用 WAL、外键、busy timeout 和短事务。当前 schema v6 的关键对象：

- `samples`
- `models`
- `predictions`
- `simulation_sessions`
- `positions`
- `trades`
- `training_runs`
- `agent_proposals`
- `runtime_state`
- `audit_logs`

数据库初始化必须对旧库幂等 migration。真实 `data/meme_quant.db` 已从 legacy 状态升级到 v6，2319 条样本数量/标签分布未漂移。

## 5. 采集器架构

### 5.1 Discovery / Enrichment

只覆盖 README 指定的 10 个 Solana Launchpad 和三类生命周期。多个 API Key 只是角色槽位，不能把同一 IP 的吞吐按 Key 数相乘。

采集流程：

1. discovery 尽量前置服务端 filter；
2. realtime token/security/pool 补齐；
3. local safety filter；
4. top holders 后置调用；
5. creator history 等可选补齐；
6. admission feature 形成 sample；
7. 模型评分；
8. 后续 T+2h label finalization。

### 5.2 限流与故障隔离

全局共享 2 RPS 安全门限；429 按 reset/retry-after 全局冷却。Cloudflare HTML block 不能当普通业务 429 无限探测。

CollectorWorker 的三个阶段必须互相隔离：

- paper position monitor；
- label finalizer；
- discovery。

Discovery 失败不能阻断已有模拟仓位退出或标签成熟。关闭 discovery 时，如果 paper monitor 开启，worker 进入 `monitor_only`：不发现新 Token，但仍拉 Kline 做退出与 label finalization。

## 6. ML 训练架构

### 6.1 时间切分

禁止 random split。所有 train/test 边界至少 2h embargo：

- `<120d`：扩展窗口开发 + 最近 20% final holdout，标记 `EARLY_STAGE_MODEL`；
- `>=120d`：只用最近 120d，开发区间与最近 30d final holdout 分离。

### 6.2 五候选与奥卡姆

候选：Logistic Regression、HistGradientBoosting、XGBoost、ExtraTrees、RandomForest。固定随机种子；XGBoost 缺依赖时显式 skipped。所有可交易候选要求开发/最终留出 Precision ≥20% 且达到最低交易数。

模型选择以时间外可比效用为主，兼顾 Precision、最差窗口、回撤、交易数和复杂度；在设定的近似等价区间内优先简单模型。

### 6.3 三阈值

一个 Champion 生成 aggressive/balanced/conservative 三档，且强制 `aggressive <= balanced <= conservative`。三档不是三个模型。未来真实 live BUY 固定使用 README 的 balanced；两类 shadow 仅用于模拟策略对照。

### 6.4 公平 Champion 比较

不得拿生产全量 refit artifact 直接在最新 holdout 回测作为 incumbent，因为其训练范围可能已经见过比较数据。

正确实现：

1. Challenger 在当前数据上构建 pre-holdout evaluation bundle；
2. 读取 incumbent 的 algorithm + feature schema；
3. 在同一当前 pre-holdout 历史上重建 incumbent evaluation bundle；
4. candidate/incumbent 必须拥有完全相同 final OOS row indices；
5. 在共享 OOS 上按各自 development 阶段冻结的 balanced threshold 比较；
6. 只有真实 liquidity + 实际 tag2 close 完整时才允许美元 PnL 自动晋级；
7. Challenger 必须满足 20% Precision、最低交易数和 5% normalized PnL lift。

第一模型可 bootstrap 成 Champion，但不是“已证明真实美元超额收益”。未晋级模型终态为 `rejected`；只有曾经是 Champion 的模型才为 `retired`，用于人工回滚。

## 7. Durable Training 与模型健康

### 7.1 TrainingWorker

手动、weekly、startup catch-up、degraded 都只创建 `training_runs` durable queue item。唯一 `TrainingWorker` 串行消费。API 断线、浏览器关闭不影响任务。

进程重启：

- `running` 且 retry budget 未耗尽 -> `queued`，retry +1；
- 达上限 -> `failed`；
- 不能让残留 running 永久占锁。

### 7.2 周日 03:00

每个 BJT 周日 03:00 有唯一 `scheduled_for`。重复启动不能重复创建。同计划 run 失败只重排同一行，有限 retry。自动训练默认继承当前 Champion 的 feature schema，因此用户以后手动启用 `ln(liquidity_usd)` 后，周训不会悄悄恢复旧 recipe。

### 7.3 7 日退化

只评估当前 Champion balanced 档成熟 OOS predictions。数据不足只报告；legacy/estimated economics 只要混入窗口就禁止自动退化判断。

只有 recent 与 baseline 都是真实 USD 可比口径时：

- Precision <20%，或
- recent ROI < positive baseline ROI × configured degradation ratio（默认 70%）

才排 `degraded` training run，并有 cooldown。新模型仍必须经过标准共享 OOS promotion gate；退化不是绕过晋级规则的后门。

## 8. 模拟交易架构

### 8.1 Simulation Session

每个显式 simulation session 创建三账户：paper/balanced、shadow_aggressive、shadow_conservative，各自 1000 USD + 0.1 SOL。应用重启恢复，不自动重置。

`simulation_sessions` registry 持久保存历史。reset 只有在无模拟开放仓时才允许：关闭旧 session，创建唯一 active session，重置三账户，不删除历史 position/trade/PnL。

### 8.2 成交模型

BUY/SELL 经 seeded local quote model，包含：

- 流动性占比 price impact；
- 随机滑点；
- 1% platform fee；
- network fee；
- latency；
- injectable failures。

失败不得伪造持仓或零 PnL 平仓。

### 8.3 市场驱动退出

Prediction 只负责评分/开仓，不能在样本成熟后用 tag 事后“代替模拟成交”。`PaperPositionMonitor` 使用 1m Kline：

- 0.9x SL；
- 1.6x TP；
- 2h timeout close；
- same bar SL first。

触发后冻结 reason/reference/trigger，position -> closing。SELL quote 失败时持久化 `paper_exit_pending`，后续周期/重启只重试原退出，不因行情变化改写原因。同 token 多仓共享 Kline fetch。

## 9. Live 接口与 fail-closed

当前 live 自动 BUY **刻意未接通**。保留：

- GMGN CLI/HTTP provider adapters；
- quote/swap/status 类型；
- `intent_created -> quoting -> submission_started -> submitted` durable journal；
- startup/periodic reconciliation；
- DRY_RUN + two-click challenge；
- persistent liquidation job。

任何 `submission_started` 或 submit-stage 异常且无稳定 order id，都视为 `submission_unknown`，不能重发。有 order id 只轮询原单。

真实 live 继续缺：wallet snapshot、SOL/USD、token balance/decimals、`output_amount_raw` 现场契约、无 order id 的余额对账、真实 day-start equity 风控、自动 balanced BUY 和小额 E2E。因此默认 `DRY_RUN=true`，不能用模拟数值绕门禁。

## 10. Agent 权限

Agent context 为只读。允许的 proposal：

- train_model
- rollback_model
- reset_simulation
- pause_new_entries
- resume_new_entries

proposal 持久化到 SQLite，必须人工 approve/reject。批准后才执行白名单动作并审计。

永久禁止 Agent proposal/execution：live buy/sell/liquidation、wallet transfer、secret access。没有 `/api/agent/execute` 或 `/api/agent/liquidate`。

## 11. 前端

当前六个本地页面：

- Dashboard
- Models
- Signals
- Portfolio
- Runtime
- Agent Approval

Models 提供 feature coverage、自选 schema、durable training runs、Champion/rejected/retired 与 rollback；Portfolio 区分当前 simulation session 和历史；Runtime 显示 TrainingWorker/model health/monitor-only/reconciliation/liquidation；Agent 页面展示 allow/block 列表与人工审批。

## 12. 单机第一版边界

本项目当前明确面向 localhost 单用户单进程。以下是未来扩展，不作为本地版“未完成”：

- 公网身份认证、session/CSRF、多租户角色；
- 多进程共享 limiter、distributed lease、PostgreSQL；
- 动态链上 fee percentile source；
- 跨链、高频、深度学习。

如果未来公网化/多实例化，应先升级这些基础设施，不能直接复制当前单进程 runtime state 语义。

## 13. 测试与验收

自动测试禁止真实广播交易。当前必测面覆盖：

- legacy CSV/schema migration；
- 标签边界/first-touch；
- entry-time 特征与未来泄漏；
- 时序 split/5 models/thresholds/Occam；
- promotion/degraded/rollback；
- durable training queue/restart/scheduler retry；
- simulation session、ledger、first-touch、closing recovery；
- monitor-only collector lifecycle；
- Agent approval；
- FastAPI non-live workflows；
- live journal/reconciliation/liquidation mock/contract fixtures；
- frontend production build。

## 14. 规范冲突与最终决议

| 事项 | 最终决议 |
| --- | --- |
| legacy `>1.25x` 正类 vs 新 `>1.2x tag2` | 新数据 v2；16 条旧非 TP 正类迁 tag2、+25% legacy floor。 |
| `price` 是否泄漏 | 当前源码/README确认其为准入 `entry_price`，允许默认训练。 |
| `launchpad` | 采集/审计但不训练；历史无区分度。 |
| `ln(liquidity_usd)` | 新样本持续采集；legacy 无法反推，当前默认关闭；模型中心可后续 opt-in。 |
| raw liquidity | 经济 sizing/PnL 专用，不作为默认 model input。 |
| 三档 vs 三模型 | 一个 Champion + 三 threshold。 |
| future live profile | README 已冻结 balanced。 |
| 模拟每次 $1000 | 显式 new session 才重置；应用 restart 恢复。 |
| 当前本地版 vs 公网/多进程 | localhost 单用户完成；公网/分布式属于后续扩展。 |

## 15. 开发门禁

在当前非实盘范围，发布候选至少要求：

1. legacy migration 数量/标签不漂移；
2. full pytest 通过；
3. frontend build 通过；
4. Champion artifact 可加载；
5. simulation session/ledger/exit recovery 通过；
6. training queue/scheduler/health/rollback 通过；
7. Agent unsafe proposal 拒绝、安全 proposal 需人工审批；
8. `.env`、私钥、API Key 不进入源码/日志/前端/测试。

进入真实 live 还需额外完成 README“实盘启用门禁”，不能因为非实盘版已闭环而跳过。

## 16. 开发环境事实

- `D:\meme` 当前没有 `.git` 元数据，不能声称已 commit/push；
- 根目录没有 `AGENTS.md`；
- 真实 `.env` 是本地秘密事实源，不应复制进文档或测试；
- 当前部署保持单进程 FastAPI。

## 17. 早期架构基线说明

本文件最初包含大量目标态 API/SSE/分布式 job/共享 limiter 设计。实际第一版没有为了“追齐设计稿”而强行引入未需要的 SSE、PostgreSQL 或公网认证；这些目标态思想仍可作为未来扩展参考，但不覆盖 README 当前本地单机产品边界。

## 18. 2026-08-10 实现状态附录

### 非实盘本地版

已完成：

- 2319/2319 legacy CSV 迁移，2317 mature、2 pending、tag0=1892、tag1=409、tag2=16；
- SQLite schema v6，完整 label facts、durable training runs、simulation sessions、Agent proposals；
- README 默认 31 特征 recipe；`price` 纳入，`launchpad` 排除，`ln(liquidity_usd)` 新采集/可选/默认关闭；
- 入场 `price_change_1h/5m` 缺失时使用 `T-1h → T` 历史 Kline 回补，不读取未来；
- 五候选 + OOS 时间切分 + 一个 Champion/三阈值 + Occam；
- 真实数据库首轮训练得到 Logistic Regression EARLY_STAGE Champion；第二轮完成 incumbent rebuild 并因 legacy 真实效用不完整安全拒绝 Challenger；
- TrainingWorker、周日 03:00/startup catch-up、有限 retry、restart recovery、7 日 model health、degraded queue、rollback；
- simulation 三账户 session、市场驱动 1m first-touch、SELL failure/restart recovery、session history、monitor-only worker；
- Agent durable proposal + 人工 approve/reject + 非实盘白名单执行；live/wallet/secret proposal fail-closed；
- FastAPI non-live route smoke tests；
- GMGN trade adapter 脱敏 fixture contract tests；
- 后端 `pytest -q` **86/86 通过**；前端 `npm run build` 通过。

### 实盘接口停放

保留 journal/reconciliation/two-click/liquidation/provider 接口；自动 live BUY 不接通。未来只需围绕真实资金事实与 GMGN 现场契约继续：wallet/equity/SOL/USD/balance/decimals、`output_amount_raw`、ambiguous balance reconciliation、真实 day-start equity/5-loss、balanced live BUY 和小额 E2E。

### 环境

`D:\meme` 不是 Git 工作树，因此本轮没有、也不能宣称 commit/push。
