# Solana Meme Quant Trading System - Architecture Review

> 本文主体保留架构审阅与设计门禁；实际实现状态以文末“2026-08-12 实现状态附录”、`README.md` 与 `开发文档.md` 为准。schema v11 / Top 3 / `rules_only` / 模拟盘单一 USD 会计 / 16:00-17:00 staged model rollover 是当前权威业务语义；历史三档字段与旧 SOL reserve 字段只允许作为数据库迁移兼容事实存在。

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
Top-3 Predictions + rules_only Baseline
  ↓
Simulation Accounts / parked Live Interface
  ↓
3s（可配置）Current-Price Position Monitor + T+1h Kline Label Finalizer
  ↓
Training Queue / OOS E-G-S Ranking / Top-3 Publish / Rollback
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

每次通过初筛的观察按 `(chain,address,observed_at)` 形成独立 sample。不能按 token address 唯一覆盖，因为同 Token 跨 1 小时窗口再次出现仍是新的训练事件；固定模拟盘也允许同币多批次。

### 3.2 入场时特征

生产训练采用显式 allowlist，而不是“数据库新增什么列模型就自动吃什么”。当前 README 冻结的默认训练字段中：

- `price` 是 admission-time `entry_price`，允许默认训练；
- `launchpad` 持续保存，用于准入、展示、审计与导出，不进入模型训练或预测特征；
- `ln(liquidity_usd)` 从新样本开始持续采集，当前 legacy 覆盖率为 0，因此默认关闭，但属于模型中心可选特征；
- raw `liquidity` 单独保留给资金公式/美元效用，不直接作为默认模型输入；
- `price_change_1h/5m` 只能使用 `T` 之前的事实。快照缺失时，应立即取 `T-1h → T` 历史 1m Kline 回补，不能等未来窗口数据参与当次评分。

硬排除：身份字段、`launchpad`、未来 H1/H2 max/min/final close、tag、first-touch/退出/PnL/成交结果、任何 observed_at 后生成字段。

### 3.3 标签

冻结规则：

- first SL 0.9x -> `tag=0`；
- first TP 1.6x -> `tag=1`；
- 两小时内未先触及 `1.6x`，无论 final close 如何 -> `tag=0`；
- 标签只允许 `0/1`；
- 同一 1m candle 同时触发 TP/SL，止损优先。

schema v11 持久化 H1 的 `price_1h_max_ratio / price_1h_min_ratio / final_1h_close_ratio` 与 `first_take_profit_at / first_stop_loss_at / exit_reason / same_bar_conflict / gross_return_rate / return_source`；旧 H2 max/min/final-close 仅保留 provenance。

## 4. 数据库与持久化

SQLite 单机第一版启用 WAL、外键、busy timeout 和短事务。当前 schema v11 的关键对象：

- `samples`
- `models`
- `active_model_slots`
- `predictions`
- `simulation_sessions`
- `asset_usd_prices`
- `positions`
- `trades`
- `training_runs`
- `agent_proposals`
- `runtime_state`
- `audit_logs`

数据库初始化必须对旧库幂等 migration。真实 `data/meme_quant.db` 已升级到 schema v11：v8 阶段 `predictions`/`positions` 物理删除 `profile`；v9 新增 `asset_usd_prices` 与每笔交易的费时 SOL/USD 审计字段；v10 扩展 durable training trigger 支持 `daily` 并保留 `completed + promoted=0` 的待激活候选；v11 新增 H1 标签审计字段，并通过 SQLite trigger 拒绝 `token_type='completed'` 的新增/回写。旧 H2 字段仅作 provenance，旧交易缺少原始 FX 事实时保持 NULL，不使用当前价格伪回填。

## 5. 采集器架构

### 5.1 Discovery / Enrichment

只覆盖 `Pump.fun / Moonshot / moonshot_app / letsbonk / jup_studio / bags / believe / heaven` 8 个 Solana Launchpad 和三类生命周期。quote 侧只允许 `SOL/USDC/USDT`，目标 Token 排除 `SOL/USDT/USDC/PYUSD/WBTC/WETH`。多个 API Key 只是角色槽位，不能把同一 IP 的吞吐按 Key 数相乘。

采集流程：

1. discovery 尽量前置服务端 filter；
2. realtime token/security/pool 补齐；
3. local safety filter；
4. top holders 后置调用；
5. creator history 等可选补齐；
6. admission feature 形成 sample；
7. 模型评分；
8. 后续 T+1h label finalization。

自 2026-08-13 起，Trenches 前置筛选与 local safety filter 对 `top_10_holder_rate` 使用同一闭区间：`0.125 <= top_10_holder_rate <= 0.28`；local safety filter 同时要求 `volume_1h / swaps_1h > 31`。旧版采集脚本同步采用相同阈值，legacy CsvImporter 对已 log 化的成交均额特征执行等价的 `feature > ln(31)`。该口径已追溯清理真实样本集：删除 182 条不合格样本和 36 条关联 predictions；4 个已关闭模拟持仓只解除 sample/prediction 引用，8 条历史成交及账户流水保留。迁移后剩余 1676 条样本且新口径违规数为 0。

### 5.2 限流与故障隔离

全局共享 2 RPS 安全门限；429 按 reset/retry-after 全局冷却。Cloudflare HTML block 不能当普通业务 429 无限探测。

CollectorWorker 的三个阶段必须互相隔离：

- paper position monitor；
- label finalizer；
- discovery。

Discovery 失败不能阻断已有模拟仓位退出或标签成熟。关闭 discovery 时，如果 paper monitor 开启，worker 进入 `monitor_only`：不发现新 Token，但仍拉 Kline 做退出与 label finalization。

## 6. ML 训练架构

### 6.1 时间切分

禁止 random split。所有 train/test 边界至少 1h embargo：

- `<120d`：扩展窗口开发 + 最近 20% final holdout，标记 `EARLY_STAGE_MODEL`；
- `>=120d`：只用最近 120d，开发区间与最近 30d final holdout 分离。

### 6.2 扩展候选池与奥卡姆

候选池覆盖 LogisticRegression、DecisionTree、HistGradientBoosting、GradientBoosting、AdaBoost、ExtraTrees、RandomForest、RBF-SVM、XGBoost，并登记 LightGBM/CatBoost/FLAML optional candidates。缺依赖必须显式 `skipped`；AutoML 只能嵌套在 outer train 内部时间切分。

模型拟合与交易评价分离：训练仍做二分类；开发期经济单位自 2026-08-14 起为 `U=5TP-FP`，Precision/Recall 恒等式为 `U=N+×r×(6-1/p)`。经济得分 E 为各 chronological OOS fold 的归一化 capture，泛化得分 G 综合 AP Skill、稳定性、衰减，综合 `S=0.60E+0.40G`。当前 E/G/S 排名公式保持冻结不变。特征数默认从 4 到可用全量逐维搜索；每个 chronological fold 的特征排序只读取本 fold train slice，最终再用 final-train 确定生产特征名，one-standard-error 内优先更小维度。

### 6.3 Top 3 单一决策线

一次训练按开发期 `S` 选出三个候选 model，每个模型只有一个冻结决策线，并分别映射未来的 `model_1/model_2/model_3`。训练完成后候选先持久化，必须等旧 `model_1/2/3` 全部空仓才原子写入 `active_model_slots`；模型上线前的历史 sample 不允许由新模型补评分。`rules_only` 不产生 prediction，直接交易全部规则准入样本。未来真实 live BUY 使用当时 Rank 1 模型及其决策线；schema v11 仍无 `profile`。

### 6.4 最终 holdout 隔离与 Top 3 发布

最终 holdout 不是排行榜：模型、特征子集、决策线和 Top 3 次序必须全部在开发期 OOS 内冻结，final 只能在选择完成后生成 certification 指标。任何“final 更赚钱所以改排名”的行为都视为测试集泄漏。

正确实现：

1. 每个算法只在 development folds 上拟合、预测并搜索单一决策线；
2. 每个算法在 fold-train-only 特征排序下自适应扫描 4..N 的特征数，并在 one-standard-error 内选最小维度；
3. 对每个保留候选计算 E、G、`S=0.60E+0.40G`；
4. 仅按 development S 排序并冻结 Top 3 次序、特征和 threshold；
5. Top 3 候选冻结后才在最终 holdout 上计算 certification Precision/Recall/固定 $50 理论收益；
6. 三个 artifact 成功持久化后，training run 先 `completed/promoted=0`；训练与激活解耦，16:00 起冻结 `model_1/2/3/rules_only` 四策略新买入，17:00 可先训练，候选等待四策略全部空仓；
7. 空仓后 `active_model_slots` 原子写入 slot 1..3，Rank 1 标为 champion，同时新建 simulation session 并重置四策略账本/统计；可选候选缺依赖显式 skipped；final 只审计，不回写排名。

2026-08-13 age<240 数据迁移后，run `3aa752e0-7557-4602-aa79-22d9b5381c7f` 在 1856 条 mature H1 v4 样本上完成自适应 4..N / fold-train-only 重训，激活 RandomForest / AdaBoost / HistGradientBoosting（8 / 8 / 6 features），并创建新的 `model_generation_upgrade` simulation session。最终 holdout 始终不参与 Top 3 排名；历史 `retired` Rank 1 保留人工回滚能力。

## 7. Durable Training 与模型健康

### 7.1 TrainingWorker

手动、daily、weekly、startup catch-up 都只创建 `training_runs` durable queue item。唯一 `TrainingWorker` 串行消费，并在没有待训练任务时继续检查 `completed/promoted=0` 候选是否已经满足空仓激活条件。API 断线、浏览器关闭不影响训练；进程重启也不会丢失已训练但尚未激活的 generation。

进程重启：

- `running` 且 retry budget 未耗尽 -> `queued`，retry +1；
- 达上限 -> `failed`；
- 不能让残留 running 永久占锁。

### 7.2 16:00 冻结 / 17:00 训练

正常模型健康状态按每周约定日（当前周日）BJT 16:00 冻结三个模型策略新买入、17:00 创建唯一 `scheduled_for` 训练；`insufficient_data` 时同样时刻改为每日执行。重复启动不能重复创建。同计划 run 失败只重排同一行，有限 retry。自动训练继承当前 Rank 1 的 requested feature pool，再由各算法执行 fold-train-only 的自适应特征维度选择。

### 7.3 7 日退化

分别评估当前三个 active model 的成熟 OOS predictions。数据不足只报告；近期 fixed-payoff economic capture 与各模型训练基线比较，不再依赖 profile 或逐笔 raw liquidity。

当某个 active model 最近窗口达到最低样本量后：

- recent economic capture < training baseline capture × configured degradation ratio（默认 70%），
- 只需一个 active model 触发即排一条 durable run；其他同时退化不会重复排队。

触发后排 `degraded` training run，并有 cooldown；该 run 仍从完整候选池重新执行 chronological OOS + E/G/S + Top 3 原子发布，退化不是绕过模型选择纪律的后门。

## 8. 模拟交易架构

### 8.1 Simulation Session

每个显式 simulation session 创建四策略账本：`model_1 / model_2 / model_3 / rules_only`，各自 1000 USD 单一 USD 账本。schema v9 的 `account_kind` 只分 `simulation/live`；策略完全由 `strategy_key` 表达。模拟盘不维护 SOL reserve，应用重启恢复，不自动重置。

`simulation_sessions` registry 持久保存代际。人工 reset、重大 model generation activation 或 rollback 只有在四策略全部无 `opening/open/closing/manual_intervention` 仓位时才允许；换代先冻结四策略新开仓，全部平仓后关闭旧 session、创建带 `model_generation_upgrade`/`model_generation_rollback` 原因的新 active session，四账本统一回到 1000 USD。

### 8.2 成交模型

BUY 仍经 seeded local quote model，包含流动性 price impact、随机滑点、1% platform fee、按手续费发生时 SOL/USD 冻结的 network fee、latency 与 injectable failures。失败不得伪造持仓或零 PnL 平仓。

SELL runtime 由 `PositionMonitorWorker` 驱动：默认目标 cadence 3 秒且可在配置页调整；同 Token 的 simulation/live 仓位合并一次 current-market 请求。GMGN Key 进入动态轮换/fallback 池，Collector 与 PositionMonitor 共享默认 10 req/s 的总 limiter；多个 Key 不再被当作 IP 吞吐倍增器。0.9x SL、1.6x TP、1h timeout 都以本轮 current price 判定，而不是回看历史 1m Kline first-touch。

### 8.3 市场驱动退出

Prediction 只负责评分/开仓，不能在样本成熟后用 tag 事后“代替模拟成交”。模拟仓命中 current-price 条件后立即冻结 reason/reference/trigger，position -> closing，并在同轮调用 Jupiter Token→USDC `/swap/v2/order` quote-only route probe；真实 `outAmount` 决定模拟 SELL fill。SELL quote 失败时持久化 `paper_exit_pending`，后续周期/重启只重试原退出，不因行情变化改写原因。旧 `PaperPositionMonitor` 1m Kline 路径仅为兼容/离线测试保留，不在生产 runtime 调用。

2026-08-13 已删除所有非 `h1_route_aware_v1` 的已平仓 simulation position/trade；卡片、history 与 audit 只基于保留的 H1 执行事实。样本、预测、模型工件与 live 数据不随该清理删除。

## 9. Live 接口与 fail-closed

当前 live 自动 BUY **刻意未接通**。已存在 live 持仓由同一个 `PositionMonitorWorker` 以默认 3s（可配置）current-price cadence 监控；只有 `DRY_RUN=false` 且 runtime live gate 已武装时，触发退出才进入既有 `LiveTradingService` 幂等执行链，否则 fail-closed。保留：

- GMGN CLI/HTTP provider adapters；
- quote/swap/status 类型；
- `intent_created -> quoting -> submission_started -> submitted` durable journal；
- startup/periodic reconciliation；
- DRY_RUN + two-click challenge；
- persistent liquidation job。

任何 `submission_started` 或 submit-stage 异常且无稳定 order id，都视为 `submission_unknown`，不能重发。有 order id 只轮询原单。

真实 live 继续缺：wallet snapshot、SOL/USD、token balance/decimals、`output_amount_raw` 现场契约、无 order id 的余额对账、真实 day-start equity 风控、Rank 1 自动 BUY 和小额 E2E。因此默认 `DRY_RUN=true`，不能用模拟数值绕门禁。

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

当前六个本地页面按产品导航顺序为：

- Dashboard / 总览
- Portfolio / 持仓
- Signals / 样本采集
- Runtime / 运行监控
- Models / 模型中心
- Agent Approval / Agent审批

Portfolio 使用 `mode × strategy` 两层视图：simulation 下四张策略卡以两列布局展示 8 项指标；`current balance` 是不含持仓本金的可用 USD 现金，`realized PnL` 使用 closed position 的净 PnL。卡片不再展示 Precision/Recall，改为同一交易统计范围内的 `profit_count`（`net_pnl_usd >= 0.20 * invested_usd`）与 `loss_count`（`net_pnl_usd < 0.05 * invested_usd`）；训练、模型中心和模型健康中的 Precision/Recall 定义不受影响。当前仓位市场快照来自持仓监控，交易历史由 SQL 真分页/时间筛选。live 视图复用同构 UI/账本 contract，在钱包事实和 live BUY E2E 未完成前保持 fail-closed。Models 使用算法全称并展示 Top 3、E/G/S、final certification、rules-only 基线、候选池、feature coverage、durable training/待空仓 activation 和 Rank 1 rollback；Runtime 显示 TrainingWorker/model health/health-aware scheduler/4s position-monitor/reconciliation/liquidation。

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
- 时序 split/扩展候选池/单一决策线/`5TP-FP`/E-G-S/one-standard-error Occam/final 隔离；
- Top 3 原子发布/三 active model degraded/Rank 1 rollback；
- durable training queue/restart/scheduler retry；
- 四策略 simulation session、rules-only 无预测开仓、同 Token current-market 请求合并、3s（可配置）current-price trigger、Jupiter executable quote、ledger/closing recovery；
- collector 与独立 3s（可配置）position-monitor lifecycle；
- Agent approval；
- FastAPI non-live workflows；
- live journal/reconciliation/liquidation mock/contract fixtures；
- frontend production build。

## 14. 规范冲突与最终决议

| 事项 | 最终决议 |
| --- | --- |
| 旧 H2 标签 vs H1 binary-v4 | H2 负类按单调性转 H1 负类；H2 正类必须重新获取 GMGN 1m Kline 计算 H1 first-touch；旧 H2 价格事实仅审计。 |
| `price` 是否泄漏 | 当前源码/README确认其为准入 `entry_price`，允许默认训练。 |
| `launchpad` | 采集、准入、展示、审计与导出；不进入训练特征。 |
| `ln(liquidity_usd)` | 新样本持续采集；legacy 无法反推，当前默认关闭；模型中心可后续 opt-in。 |
| raw liquidity | 经济 sizing/PnL 专用，不作为默认 model input。 |
| 三档 vs 三模型 | 三档设计已删除；当前固定 Top 3 三个模型，各一条 decision threshold，另有 `rules_only` 基线。 |
| future live strategy | 使用当时 active Top 3 的 Rank 1 模型及其单一 decision threshold；schema v11 无 `profile`。 |
| 模拟每次 $1000 | 显式 new session 才重置；应用 restart 恢复。 |
| 当前本地版 vs 公网/多进程 | localhost 单用户完成；公网/分布式属于后续扩展。 |

## 15. 开发门禁

在当前非实盘范围，发布候选至少要求：

1. legacy migration 数量/标签不漂移；
2. full pytest 通过；
3. frontend build 通过；
4. Top 3 三个 artifact 均可加载，`active_model_slots` 与 model status 一致；
5. simulation session/ledger/exit recovery 通过；
6. training queue/scheduler/health/rollback 通过；
7. Agent unsafe proposal 拒绝、安全 proposal 需人工审批；
8. `.env`、私钥、API Key 不进入源码/日志/前端/测试。

进入真实 live 还需额外完成 README“实盘启用门禁”，不能因为非实盘版已闭环而跳过。

## 16. 开发环境事实

- `D:\meme` 已重新关联 GitHub 仓库 `ShiningSugar35/meme`，当前使用 `main` 跟踪 `origin/main`；
- 根目录没有 `AGENTS.md`；
- 真实 `.env` 是本地秘密事实源，不应复制进文档或测试；
- 当前部署保持单进程 FastAPI；本地开发可由 `scripts/start_runtime.py --reload` 启动 Windows supervisor，仅监控 `backend/**/*.py` 并在变更后重启 Uvicorn 单 worker；实盘常驻禁止 reload。该 supervisor 已实测自动更换子进程 PID 后恢复 `/health`、Collector 与 TrainingWorker。

## 17. 早期架构基线说明

本文件最初包含大量目标态 API/SSE/分布式 job/共享 limiter 设计。实际第一版没有为了“追齐设计稿”而强行引入未需要的 SSE、PostgreSQL 或公网认证；这些目标态思想仍可作为未来扩展参考，但不覆盖 README 当前本地单机产品边界。

## 18. 2026-08-12 实现状态附录

### 非实盘本地版

已完成：

- 当前真实库在 H1/no-completed 基础上进一步完成 age<240 迁移：严格 `1<age_minutes<240`，备份后删除 497 个不合规样本及关联 predictions/positions/trades；现有 1856 samples 全部为 mature H1 v4，344 positives / 1512 negatives，age 违规数 0；completed 仍由 DB trigger 禁止回写；
- SQLite schema v11：v8 已从 predictions/positions 物理删除 `profile` 并新增 `active_model_slots`；v9 增加 USD-only 模拟会计、`asset_usd_prices` 与手续费费时 FX 审计字段；v10 增加 daily durable trigger 与待空仓候选持久状态，旧交易缺少历史 FX 时不伪回填；
- 默认候选 feature pool 31；各算法从 4 个特征起逐维扫描到可用全量，fold-train-only 排序并用 one-standard-error 选择近优最小维度；`launchpad` 仅元数据，`ln(liquidity_usd)` 可选；
- 入场 `price_change_1h/5m` 缺失时使用 `T-1h → T` 历史 Kline 回补，不读取未来；
- 扩展候选池 + chronological OOS + 单一决策线 + `5TP-FP`/E-G-S + one-standard-error Occam + final 隔离；
- age<240 迁移后正式 run `3aa752e0-7557-4602-aa79-22d9b5381c7f` 当前发布 Random Forest / AdaBoost / HistGradientBoosting；分别选择 8 / 8 / 6 个特征，并已切入新的 `model_generation_upgrade` simulation session；
- TrainingWorker、`insufficient_data` 每日 17:00 / 其他状态周 17:00、16:00 model-entry freeze、startup catch-up、有限 retry、candidate waiting-for-flat/restart recovery、7 日 model health、rollback；
- 四策略 USD-only simulation session、Top 3 prediction + rules-only、手续费发生时 SOL/USD 冻结折算并保留原始 SOL 事实、默认 3s（可配置）current-price 持仓触发、Jupiter executable quote、SELL failure/restart recovery、8 项卡片指标、重大模型换代新 session、H1-only history、独立 position-monitor worker；
- Agent durable proposal + 人工 approve/reject + 非实盘白名单执行；live/wallet/secret proposal fail-closed；
- FastAPI non-live route smoke tests；
- GMGN trade adapter 脱敏 fixture contract tests；
- Portfolio `mode × strategy` 同构视图、当前市场快照、SQL 分页/时间筛选与四策略“模型”交易审计；
- 后端 `pytest -q` **131/131 通过**；前端 `npm run build` 通过；覆盖 adaptive feature count、fold-train-only 选择、E_exec shadow-only、Jupiter quoted/no-route/unavailable 三态、3s（可配置）current-price position monitor、live DRY_RUN gate、四策略 rollover 新 session 以及 H1-only 历史隔离。

### 实盘接口停放

保留 journal/reconciliation/two-click/liquidation/provider 接口；`GMGNAtomicProvider` 已定义 quote → swap → query_order 的受控 adapter，Portfolio live view 已接到 `account_kind='live'` 持久账本，但自动 live BUY 不接通。未来仍需围绕真实资金事实与 GMGN 现场契约继续：wallet/equity/SOL/USD/balance/decimals、`output_amount_raw`、ambiguous balance reconciliation、真实 day-start equity/5-loss、Rank 1 live BUY 和小额 E2E。

### 环境

`D:\meme` 当前是 Git 工作树并跟踪 `origin/main`；发布变更必须在测试/构建通过后 commit + push。

## 2026-08-12 增量架构决议：adaptive features + route-aware paper execution

1. **评分边界不变**：现有 E/G/S 继续作为唯一模型排名口径；route-validated `E_exec` 仅做 shadow 指标，权重为 0，避免执行数据刚起步时改变已冻结模型选择语义。
2. **特征选择改为自适应且时序安全**：不再使用固定 12/20/full 档位。每个 development fold 仅在自己的 train slice 上做特征排序，候选维度逐维比较，one-standard-error 选择最小近优维度；final holdout 仍完全隔离。
3. **模拟退出引入真实可执行性证据**：先刷新市场 liquidity，再按可审计 decimals 请求 Jupiter Token→USDC read-only quote。明确 no-route 与 API/rate-limit/network unavailable 分流；前者属于市场可执行性失败，后者不得伪装为 rug。
4. **执行数据抗选择偏差**：只有持续覆盖所有规则准入样本的 `rules_only` route-validated closed positions 进入 `E_exec` 数据集；模型账户结果不作为该指标的唯一来源。
5. **generation 隔离**：模型卡片的持仓/历史/审计必须同时绑定 current model id 与 selected_at；strategy slot 不是 model generation identity。
