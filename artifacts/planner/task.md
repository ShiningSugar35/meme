# Solana Meme Quant Trading System - 当前开发状态

> 更新时间：2026-08-12
> 当前范围：**除真实 live BUY 外，本地单机/单用户版本全部闭环**。系统已完成 Top 3 + `rules_only` 重构、schema v11（v8 去 profile + v9 USD-only 手续费会计 + v10 staged model rollover + v11 H1/no-completed）迁移、健康感知的 16:00/17:00 日/周训练、候选等待空仓换模、模型 generation 统计和四策略模拟链；实盘 provider/journal/reconciliation/liquidation 继续 fail-closed。

## Phase 1：数据与 Schema — COMPLETE

- [x] legacy CSV 旧 H2 路径事实保留 provenance；生产标签已迁移到 H1 binary-v4
- [x] 当前真实库迁移前审计：2379 samples、2374 mature、414 positives
- [x] schema v8：`predictions` 物理删除 `profile`，UNIQUE(`sample_id`,`model_id`,`strategy_key`)
- [x] schema v8：`positions` 物理删除 `profile`，`account_kind` 仅 `simulation/live`，模拟策略由 `strategy_key` 表达
- [x] `active_model_slots` 保存 slot 1..3、model id、综合分、单一 threshold、选中时间
- [x] 旧三档预测/仓位/成交原子迁移到 neutral legacy strategy；迁移副本验证 2379 samples / 207 predictions / 90 positions / 180 trades 原数保留
- [x] 正式真实 DB 已完成 schema v11 幂等升级（v10 daily trigger / 待空仓候选持久化 + v11 H1 字段 / completed 写入阻断）
- [x] 迁移前 SQLite backup：`data/backups/meme_quant_pre_top3_20260811T155151Z.db`
- [x] H1/no-completed 真实迁移 backup：`data/backups/meme_quant_pre_h1_no_completed_20260812T092259Z.db`；删除 123 completed，416 个旧 H2 正类通过 GMGN 1m Kline 重算为 381 正 / 35 负，0 API 错误；迁移后 2292 mature H1 / 4 pending
- [x] durable `simulation_sessions` / `training_runs` / `agent_proposals` / trade journal

## Phase 2：持续采集与入场特征 — COMPLETE

- [x] README 指定 8 Launchpad / 2 生命周期（new_creation / near_completion）/ 12 Key 角色分工
- [x] GMGN discovery/enrichment adapter、共享限流、429 cooldown、本地 safety filter、top-holder 后置过滤
- [x] quote 侧仅 SOL/USDC/USDT；目标 Token 排除稳定币/包装主资产
- [x] admission-time `price` 可训练；`launchpad` 仅元数据；`ln(liquidity_usd)` 持续采集且可 opt-in
- [x] `price_change_1h/5m` 缺失时仅用 `T-1h → T` 历史 1m Kline 回补
- [x] Collector paper-exit / label / discovery stage isolation
- [x] monitor-only：关闭 discovery 仍继续模拟退出与 T+1h 标签

## Phase 3：Top 3 ML / 奥卡姆 / 自更新 — COMPLETE

### 评价公式

- [x] 当前固定离线经济单位：`U = 3×TP - FP`
- [x] 当前 p/r 恒等式测试：`U = N_positive × recall × (4 - 1/precision)`
- [x] 固定 $50 仅用于模型公平离线排名：`fixed_profit_usd = 5 × U`
- [x] 实际模拟/实盘 sizing 不变：`min(1% × entry liquidity, $50)`，并继续计滑点/平台费/网络费
- [x] 当前经济得分 `E = mean(clip((3TP-FP)/(3N+), -1, 1))`
- [x] 泛化得分 `G = 0.60×AP Skill + 0.20×Stability + 0.20×Decay`
- [x] 综合分 `S = 0.60×E + 0.40×G`
- [x] 最终 holdout certification-only，不参与模型、feature、threshold 或 Top 3 排名

### 候选与奥卡姆

- [x] LR / DecisionTree / HGB / GradientBoosting / AdaBoost / ExtraTrees / RandomForest / RBF-SVM / XGBoost
- [x] LightGBM / CatBoost / FLAML optional candidate；缺依赖显式 `skipped`
- [x] AutoML 若未来启用，只能嵌套在 outer train 内的时间切分，不能接触 final holdout
- [x] 每算法比较 12 / 20 / 全量特征
- [x] one-standard-error 内优先最小特征子集
- [x] 每模型仅一个冻结 decision threshold
- [x] `active_model_slots` Top 3 原子发布；Rank 1 兼容 `champion` 状态，Rank 2/3 保持 active candidate
- [x] 历史 retired Rank 1 工件可加载时人工 rollback 到 slot 1

### 正式真实训练验收

正式 run：`89c2d088-ea1b-4bc7-9aec-2ca7a92220b2`

- [x] 2374 mature / 414 positive / 31 candidate feature pool
- [x] Rank 1 Random Forest：12 features，threshold `0.1420217464`，E `0.288594`，G `0.489628`，S `0.369008`
  - model id `20260811T162814Z-random_forest-eab07444`
- [x] Rank 2 Decision Tree：12 features，threshold `0.11`，E `0.303030`，G `0.440438`，S `0.357993`
  - model id `20260811T162814Z-decision_tree-7313f40a`
- [x] Rank 3 Gradient Boosting：12 features，threshold `0.1182348833`，E `0.282281`，G `0.469720`，S `0.357257`
  - model id `20260811T162814Z-gradient_boosting-2eba2852`
- [x] 三个模型均由 one-standard-error 奥卡姆规则压缩到 12 特征
- [x] final certification：RF `$875` / DT `$890` / GB `$1000` / `rules_only` `$915`
- [x] HGB/XGBoost 等 final 局部成绩更高时也不会反向改写开发期 Top 3
- [x] 模型显示名统一使用算法全称，例如 `20260812-Random Forest / 20260812-Decision Tree / 20260812-Gradient Boosting`
- [x] H1/no-completed 重训 run `ca922c9b-a835-486b-ba61-e94c04978399` 已按 17:00 `startup_catchup` 完成并激活当前 Top 3：`Extra Trees / AdaBoost / Random Forest`；v11 继续统一经过 16:00/17:00 staged rollover

## Phase 4：四策略 Simulation — COMPLETE

- [x] 策略键：`model_1 / model_2 / model_3 / rules_only`
- [x] 每策略独立 `1000 USD` 单一 USD 账本；模拟盘不维护 SOL reserve
- [x] 每策略最大 10 仓；同 Token 可跨策略/批次共存
- [x] `model_1/2/3` 各自使用对应 active model + 单一 threshold
- [x] `rules_only` 对所有 fresh rule-admitted sample 直接开模拟仓，不创建模型 prediction
- [x] 四策略复用完全相同 BUY/SELL/滑点/费率/网络费/0.9x SL/1.6x TP/1h timeout/卖出失败重试链
- [x] 网络费保留原始 SOL 数量，并按手续费实际发生时 SOL/USD 冻结折算为 USD 扣减模拟现金/PnL；缺失新鲜 FX 时 fail-closed，不事后补价
- [x] schema v9 持久化 `asset_usd_prices` 与 `platform_fee_usd / sol_usd_price / network_fee_usd / slippage_cost_usd / fee_occurred_at`；v11 保持该费用会计不变
- [x] 卡片 `当前余额` 是可用现金，不含已投入持仓本金；v9+ 完整费用交易逐笔审计满足 `net_pnl = gross_pnl - platform/network fees`，滑点已进入 fill price
- [x] `model_1/2/3` 卡片按当前 model_id + selected_at 统计 8 项 generation 指标，sample.entry_time 也必须不早于 selected_at；`rules_only` Precision=当前 session 成熟样本正类率，Recall=100%（存在正类时）
- [x] 同 Token 四策略仓位合并为一次市场 Kline 请求
- [x] SELL failure 持久化 closing；restart 只重试原退出；no-route/重试耗尽按总损失关闭
- [x] Dashboard/Portfolio 默认只统计当前 active simulation session；历史不污染当前 PnL
- [x] simulation audit 第一列语义为“模型”，四策略独立记录 actual first entry / last exit / trades / PnL

## Phase 5：前端 / Agent — COMPLETE

- [x] Dashboard：Top 3 排名、特征数、threshold、Precision/Recall、理论收益、E/G/S、四策略实际模拟 PnL、实盘已实现 PnL
- [x] Portfolio：`simulation/live` + 四策略卡；不再出现平衡/激进/保守 profile
- [x] Portfolio 当前仓位：市场快照、当前涨幅、Token copy；历史真分页/时间筛选/失败卖出原因
- [x] Simulation audit 第一列“模型”，不再显示档位/source profile
- [x] Models：Top 3、E/G/S、final certification、rules-only baseline、完整候选池与 skipped 原因、feature coverage、durable runs、Rank 1 rollback
- [x] Signals：按 model_1/2/3 过滤，显示 model label + probability / threshold
- [x] Runtime：Collector、TrainingWorker、三 active model health、scheduler、reconciliation、liquidation
- [x] Agent durable proposal + 人工 approve/reject；live/wallet/secret 类动作创建阶段拒绝

## Phase 6：Regression / DB / Build — COMPLETE

- [x] p/r 收益恒等式回归
- [x] Top 3 单阈值与 final holdout 隔离回归
- [x] schema v8 去 profile + 历史迁移回归
- [x] schema v9 USD-only 手续费会计回归：费时 SOL/USD、原始 SOL 事实保留、未来价格不泄漏、无模拟 SOL reserve
- [x] four-strategy session reset / ledger
- [x] rules-only 无 prediction 开仓
- [x] Top3 prediction cycle + stale-signal 边界
- [x] 同币四策略一次 Kline 的 1m first-touch E2E
- [x] 三 active model health 分类 + `insufficient_data` 每日17:00 / 其他状态周17:00 调度；16:00 entry freeze、17:00 先训练、候选跨重启等待空仓、空仓后原子换模
- [x] current-generation Precision/Recall 去除上线前 backlog；`rules_only` Precision/Recall 回归
- [x] no-route / retry exhausted / live terminal failed sell 计入已实现亏损
- [x] live journal/reconciliation/liquidation mock/fixture fail-closed
- [x] 后端 full `pytest -q`：**105/105 passed**
- [x] 前端 `npm run build`：passed

## Phase 7：Runtime / 发布收尾 — COMPLETE

- [x] 正式真实 DB schema v10 migration
- [x] 正式 Top 3 training + active slots；健康感知 staged rollover 已接管后续自动更新
- [x] 创建新的四策略 active simulation session：`sim_5d975f6978f847de8aec8e5da57ccffa`
- [x] 启动 backend Windows reload supervisor：PID 28404
- [x] Collector / PredictionWorker / TrainingWorker / PaperMonitor / Scheduler / ModelHealth / Reconciliation / Liquidation 全部 `running`
- [x] `/health`、`/api/dashboard`、`/api/models`、`/api/simulation`、`/api/portfolio/view` 真实 API smoke 通过；当前 Top3=`20260812-Decision Tree / 20260812-Random Forest / 20260812-XGBoost`
- [x] 前端 `http://127.0.0.1:5173` 返回 200，`/@vite/client` 存在，Vite HMR 正常
- [x] 再次确认 `DRY_RUN=true`、`live_trading_enabled=false`
- [x] 临时 `data/top3_migration_test.db` 已删除；保留 pre-top3 SQLite 安全备份
- [x] final backend `105/105` passed；final frontend production build passed
- [x] 本轮 schema v10 / USD-only / staged rollover / 8 指标改造完成并在运行态验收
- [x] 本轮按用户要求 commit + push `main`（以本轮最终 Git 验收记录为准），同时保持 backend/frontend 常驻运行

## Phase 8：Live — PARKED / FAIL-CLOSED

以下不是当前继续开发项：

- [ ] 真实 wallet snapshot：available USD / total equity / SOL / SOL-USD / token balances
- [ ] real token decimals / `output_amount_raw` 现场契约
- [ ] 无 stable order id 的 wallet/token balance reconciliation
- [ ] Rank 1 与真实钱包共同资金闸门
- [ ] BJT day-start real equity 20% daily-loss E2E
- [ ] 实盘连续 5 亏真实钱包 E2E
- [ ] `active_model_slots[1] → RiskService → LiveTradingService` 自动 BUY
- [ ] 用户当次明确授权后的极小额 buy → restart reconcile → sell 现场验收

在这些真实资金事实完成前始终保持：

- `DRY_RUN=true`
- `live_trading_enabled=false`
- 不存在自动 live BUY pipeline
- 不使用模拟钱包数字绕过实盘门禁

## Phase 9：Event1m / Market Regime / Adaptive Shadow — COMPLETE / DATA-GATED

- [x] 正式准入统一为严格 `2 < age_minutes < 300`；Trenches `min_created=2m / max_created=300m`；`feature_snapshot_at` 单独记录真实入场快照时刻。
- [x] 正式 feature generation 为 `event1m_regime_v3`。后续用户明确要求新增特征前的样本全部作废，因此 `legacy_pre_event_v1 / event2m_regime_v1 / event2m_regime_v2` 已从运行库删除，不再保留旧 pending、旧模型或旧成交作为 v3 运行输入。
- [x] 原生产 31 特征保持默认训练池；v3 可选 catalog 为 44 项。`price_change_2m` 已替换为 `price_change_1m`，并删除 `ln(swaps_1m+1)`、`ln(volume_2m+1)`、`volume_acceleration_2m`、`creator_token_status`；其余新增 1m / marketing / social / holder-age / marketcap 与 `ln(liquidity_usd)` 仅 Shadow，禁止默认自动启用。
- [x] GMGN 真实脱敏 contract probe 用于确认保留字段的 presence/type/nullability：1m volume/swaps/buy/sell、Dex promotion、X follower、TG call、holder_count、marketcap；已删除的 creator status/2m volume 不再作为候选特征契约。
- [x] DEX Screener Public 只做低频非关键 fallback；Coinbase Public 提供 BTC Crypto family，并只在本地/GMGN SOL 事实 stale 时回补 SOL；optional provider 失败保留 missing/source-health，禁止伪造 0。
- [x] Solana Network：4 个 Alchemy 账号轮转 → Solana Public emergency。Alchemy 同 IP bounded probe 300 请求观察到 299×HTTP 200 + 1×ConnectTimeout + 0×429，只能表述为“未观察到小型严格共享硬桶”，不能证明任意负载完全独立。
- [x] Market Regime 持久化 Crypto / SOL / Network / Meme breadth / Attention / Execution / Strategy Performance、score/label/confidence/source health/reasons；Token Local 与全局 Regime lookback 严格 PIT 分层。
- [x] `model_1/2/3` 具备 15m DEFENSIVE/NEUTRAL/EXPANSIVE logit-threshold Shadow policy；`rules_only` 固定；每个模型记录 neutral counterfactual/propensity/policy version。
- [x] Safe activation fail-closed：至少 120 独立 sample cluster + 40 separating cluster，chronological 70/30 development/certification，development mean>0、certification 95% LCB>0、三模型 certification mean 均>=0；探索另需独立 readiness gate且硬上限 5%。当前 v3 数据不足，保持 Shadow-only。
- [x] `scripts/feature_family_audit.py` 提供 chronological family audit；新代未达到 200 mature / 25 positive / 50 negative 时明确 `INSUFFICIENT_DATA`，禁止 legacy/v1 backfill；达到门槛后执行 fold-train-only 排序、one-SE + 8% Occam、final holdout certification-only，并对不稳定家族给出整族删除建议。
- [x] 最终验收：后端 `pytest` 163/163、前端 production build 通过；Coinbase BTC/SOL 与 DEX Screener Public 现场只读探针健康，GMGN event probe 健康；秘密值扫描 33 项 × 172 文件无命中；重启后 backend/frontend 均 200，Collector / Prediction / Regime / Reconciliation / ModelHealth / Training 均 running，`DRY_RUN=true`，Adaptive 仍 `policy_ready=false / exploration_ready=false`。

## Phase 10：Current-generation reset / 1000-sample modeling gate — COMPLETE

- [x] 模型资格只统计当前 `event1m_regime_v3` 的 `mature + tag` 样本；默认硬门槛 1000。
- [x] `<1000` 时 Scheduler 不创建 daily / weekly / startup-catchup run；TrainingWorker 对遗留的非 manual run 二次 fail-closed；ModelHealth 不允许 degraded retraining 旁路。
- [x] `<1000` 时 Prediction 完全跳过 Top3 打分、prediction 写入和 `model_1/2/3` 模拟仓位，但继续 `rules_only` 无模型模拟与正常结算；旧 rollover freeze 不能阻塞该基线。
- [x] 旧 generation 样本、模型 registry/active slots、模型工件、training runs、predictions、simulation sessions、positions、trades、adaptive policy 历史、collector cycle、模型/交易 runtime state、SQLite backups 与未使用 legacy DB 一次性清理；Market Regime PIT 市场事实保留。
- [x] 发布验收：全量 pytest **165/165**、frontend production build、`git diff --check` 均通过；清理后主库只保留 v3 的 `5 mature + 4 pending`，旧模型/Top3/predictions/training/positions/trades 全为 0，72 个模型工件、10 个历史备份库及 legacy DB 已删除；重启后 backend/frontend=200，Collector 一轮 120 candidates / errors=[]，scheduler=`data_collection_only`，Prediction `model_ids=[] / scored=0 / predictions=0 / model_positions=0`，`DRY_RUN=true / live_trading_enabled=false`。Git push 见本次提交。

## Phase 11：+4/-1 economics / execution audit / 2s monitor — COMPLETE

- [x] 模型经济代理由 `+5/-1` 收紧为 `+4/-1`：`U=4TP-FP`，固定 $50 为 +$20/-$5，盈亏平衡 Precision=20%；E 归一化同步为 `clip((4TP-FP)/(4N+),-1,1)`，ModelHealth 与 Adaptive Policy 共用 `WIN_UNITS=4`。
- [x] 新模型写入 `economic_objective_version=fixed_4_to_1_v2`；Prediction fail-closed 检查 active Top3 objective version，旧 +5/-1 模型不得在 1000-sample gate 刚解除时短暂恢复评分。
- [x] rules-only 会计逐笔审计确认不存在滑点/平台费/网络费双扣；旧“滑点”统计把跨源/跨时点 execution deviation 混入 slippage，170 笔 Jupiter route SELL 从 `$393.28` 重分类为真实 route price impact `$96.72`，PnL 不变并保留 legacy deviation 审计。
- [x] PositionMonitor 修复跨 Token head-of-line blocking：GMGN market response 逐个完成即处理，对应 paper SELL 立即并发启动；触发时刻改用实际行情响应时刻。回归覆盖 fast Token 在 slow Token 返回前已完成退出。
- [x] 默认/运行态 poll 由 3s 收紧为 2s；生产已观察 `target_poll_seconds=2.0`、`last_start_interval_seconds=2.0`。
- [x] 新增 `scripts/audit_rules_only_execution.py` 与 `scripts/reclassify_route_slippage.py`，前者持续核验会计恒等式并拆分 stop market gap / route price impact，后者默认 dry-run、仅显式 `--apply` 才修历史审计字段。
- [x] 最终验收：后端 pytest **168/168**，frontend production build 通过；运行态 PositionMonitor `target_poll_seconds=2.0 / last_start_interval_seconds=2.0`，当前 modeling gate 为 `193/1000`、模型评分继续关闭，live trading 保持未武装。

## Phase 12：Top10 严格区间热迁移 — COMPLETE

- [x] 当前生产 `top_10_holder_rate` 准入收紧为严格开区间 `0.14 < rate < 0.25`；`0.14` / `0.25` 边界均拒绝。
- [x] discovery prefilter、authoritative `SafetyFilter`、CsvImporter 与 legacy `量化训练采集.py` 已统一为相同严格边界；缺失 Top10 的 CSV 导入也 fail-closed。
- [x] 准入与模型特征统一使用 `normalize_token()` canonical Top10 值，禁止 `bundle.stat` 覆盖；对应冲突回归已加入。
- [x] 迁移前 SQLite backup：`data/backups/meme_quant_pre_top10_014_025_20260822T145311Z.db`。迁移快照 1009 → 799，删除 210 条不合规样本（200 mature：28 正 / 172 负；10 pending）；158 个 rules-only positions 仅解除 sample 引用保留真实账本，其中 2 个当时 open 的仓继续自然退出；0 predictions。
- [x] 迁移后 raw/features 新规则违规数=0、Top10 authority mismatch=0、`PRAGMA foreign_key_check`=0。热更新后 Collector 已继续采入新样本；2026-08-22 23:02 BJT 复核为 800 条（791 mature + 9 pending），新增样本仍满足新规；迁移时 2 个 open 解绑仓位中已有 1 个自然结算，剩余 1 个继续原退出链。
- [x] 相关定向测试通过，Python compile 通过，全量 `pytest -q` 通过；运行态 `/health=ok`，modeling=`data_collection_rules_only`、scheduler=`data_collection_only`、当前 mature=791/1000，PositionMonitor 已恢复 `state=running`、start-to-start=2.0s、market/network failure=0、blocked=0。

## Phase 13：+3/-1 economics / 1:3 重训热更新 — COMPLETE / ACTIVATED

- [x] 当前模型经济代理收紧为 `+3/-1`：`U=3TP-FP`，固定 $50 为 +$15/-$5，盈亏平衡 Precision=25%；E 归一化为 `clip((3TP-FP)/(3N+),-1,1)`，p/r 恒等式为 `N+×recall×(4-1/precision)`。
- [x] `ECONOMIC_OBJECTIVE_VERSION=fixed_3_to_1_v3`、`WIN_UNITS=3`；Trainer / ModelHealth / Adaptive Policy 共用同一经济常量，Prediction 对旧 `fixed_4_to_1_v2` active model fail-closed。H1 `1.6x/0.9x` 标签与 simulation/live 真实退出规则不变。
- [x] 定向经济学/训练/Prediction/Adaptive 回归 37/37 通过；全量 `pytest -q` 通过；frontend `npm run build` 通过。
- [x] durable manual run `b040425e-3043-4dd5-9752-f6685d83f4b9` 使用 1080 mature + 持久化 44 特征池完成，retry=0。新 Top 3：Extra Trees（35 features，threshold `0.2454529`，E/G/S=`0.139676/0.459697/0.267685`）/ Decision Tree（4 features，threshold `0.42`，E/G/S=`0.124905/0.437260/0.249847`）/ Random Forest（20 features，threshold `0.2763549`，E/G/S=`0.118388/0.447422/0.250002`）。三者 objective 均为 `fixed_3_to_1_v3`。
- [x] run 完成后先进入 staged `waiting_for_flat`；旧仓自然结算后 TrainingWorker 已于 2026-08-24 前完成原子激活，新 active Top 3 为 Extra Trees / Decision Tree / Random Forest，objective 均为 `fixed_3_to_1_v3`，并创建新的 `model_generation_upgrade` simulation session。

## Phase 14：标签策略只读网格 / Collector 事故修复 — COMPLETE

- [x] 固定 1177 条 `event1m_regime_v3` mature 样本重新拉取 2h/1m Kline，1177/1177 成功、0 错误；缓存 `artifacts/research/kline_cache_v3_2h.json.gz`。
- [x] 360 套 policy grid 完成：TP 1.6/1.8/2.0 × SL 0.9/0.8/0.75/0.7 × 60/90/120min × trailing off/20/25/30% × 5/10/15min。生产 1.6/0.9/60 基线精确复现 217/1177=18.44%。
- [x] 结论：不支持放宽 hard SL；20% trailing 仍高 false-kill；首选 challenger 为 1.8x/0.9x/90min/no-trailing，次选 2.0x/0.9x/90min。生产标签未修改。
- [x] 真实 rules-only clean ledger：stop 平均 -23.30% / trimmed -21.93%，TP1.6 平均 +68.93% / trimmed +65.39%，真实 payoff≈2.98:1、break-even Precision≈25.11%，支持现行 `fixed_3_to_1_v3`。
- [x] recent 25% 存在显著 drift：当前标签正类率 Q1→Q4 为 22.45%→13.90%，p≈0.007；`ln(liquidity_usd)` / `price_change_1h` / `entrapment_ratio` 发生显著 KS 漂移。60–120m age 分层持续最差。
- [x] read-only separability：recent holdout Logistic top10 Precision 当前/1.8x90/2.0x90 分别 30.0%/26.7%/23.3%；压力测试显示正期望主要集中在 top5–10% 信号，支持后续研究更稀疏交易而非放宽 SL。
- [x] 事故根因确认：Kline 研究隔离残留 `COLLECTOR_ENABLED=false` 与 `simulation_entries_paused=true`；09:36 后 Collector 未重启且 stale runtime 误报 running，SOL/USD fee fact 同步中断导致 2 个 paper 仓位延迟退出。已恢复配置并审计。
- [x] 防复发：Collector disabled 启动时显式写 `collector_status=disabled`；PositionMonitor 独立刷新 SOL/USD fee fact，不再依赖 Collector discovery。恢复后连续 Collector cycles 正常，首轮 accepted=3；PositionMonitor 2s cadence、`sol_usd_refresh_error=null`；全量 pytest 172/172 通过。
- [x] 结果：`artifacts/research/label_policy_grid.json`、`label_policy_grid.csv`、`label_policy_grid_report.md`、`collector_incident_20260824.md`。

## Phase 15：Shadow Challenger / Age / Execution Risk 只读赛马 — COMPLETE

- [x] 将 Kline 冻结快照从 1177 补齐到 **1183 mature / 1183 Kline**，本轮所有 challenger 使用同一固定快照，生产后续新增样本不污染评测。
- [x] 用户新增专项完成：`1.8/0.9/60m` 全体 176/1183=14.88%，`age<60m` 后 **143/802=17.83%**；`1.8/0.9/90m` 全体 182/1183=15.38%，`age<60m` 后 **146/802=18.20%**。age<60 对应 age>=60 的正类率分别 17.83% vs 8.66%、18.20% vs 9.45%，差异显著（p<1e-4）。
- [x] 完整生产训练栈 shadow race（当前 44 feature pool、chronological folds、13-model candidate pool、`fixed_3_to_1_v3`，不注册模型/不写 training_runs/不激活）完成：p18/90 Top3=LightGBM/CatBoost/ExtraTrees；p20/90 Top3=CatBoost/RBF-SVM/Logistic。recent final holdout 显示开发最强模型仍会失效，确认标签切换不能单独解决 regime drift。
- [x] 稀疏交易预算完成：p18/90 full 仅 ExtraTrees Top5%（12/4，P=33.3%）有明显正 stress；p20/90 full 仅 Logistic 极稀疏头部有信号（Top5% 12/4，P=33.3%），扩大预算后优势迅速衰减。禁止将“一套统一 Top3 阈值”视为可行方案。
- [x] `p18/90 + age<60` 正式 ablation：802 rows / 146 positives，final holdout 161 / 23；Top3=ExtraTrees/Logistic/CatBoost。age<60 clean stop 5% trimmed mean=-23.53%；按该专属 stop stress，Logistic Top5% 8/4 约 +25.7%/trade、Top7.5% 12/4 约 +9.3%，但样本仍过小，只作为 shadow 信号。
- [x] age 60–120m 对照完成：排除该段后 1023 rows / 171 positives，ExtraTrees recent Top5% 10/4=P40%；仅训练该段仅 160 rows / 11 positives，development economic score 全负，判定不适合作为独立 model regime，优先研究降权/排除。
- [x] stop execution gap 分解：首次 below-stop reference 对 0.9x 线中位已额外穿透约 -4.54%，25% 分位约 -11.86%，最差10%约 -25.3%；fill 相对 trigger 再损失中位约 -2.77%；execution delay 与净损失几乎无相关，trigger overshoot 为主要损失源。
- [x] execution-risk head 可学性验证：722 clean linked stops，severe-gap base≈29.8%；ExtraTrees chronological holdout AUC≈0.677、AP≈0.466，风险 Top20% severe≈47.2%、Bottom20%≈8.3%，支持未来增加 entry-time execution-risk penalty/head，而不是单纯提高 entry liquidity 门槛。
- [x] drift-aware 诊断：simple prior odds correction 与简单缩短训练窗口均不能稳定修复 recent collapse；后续方向应为 recent calibration gate + model-specific trade budget + covariate/prior drift alarm + shadow retrain trigger。
- [x] 本轮未修改生产标签、active models、TP/SL/timeout 或生产准入；结果落盘：`artifacts/research/age_lt60_p18_stats_1183.json`、`shadow_challenger_training_1183.json`、`shadow_p18_90_only_60_120.json`、`shadow_challenger_report_1183.md`。

## 环境事实

- repo：`D:\meme` / `https://github.com/ShiningSugar35/meme.git` / `main`
- 本地开发：`scripts/start_runtime.py --reload` + Windows supervisor；frontend Vite HMR
- `.env` 是本地秘密事实源，严禁复制到源码/文档/日志/测试
- 当前系统仍是 localhost 单用户第一版；公网认证、多实例 lease/PostgreSQL 属于未来扩展
