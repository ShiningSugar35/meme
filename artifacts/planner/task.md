# Solana Meme Quant Trading System - 当前开发状态

> 更新时间：2026-08-12
> 当前范围：**除真实 live BUY 外，本地单机/单用户版本全部闭环**。系统已完成 Top 3 + `rules_only` 重构、schema v8 去 profile 迁移、真实 Top 3 训练和四策略模拟链；实盘 provider/journal/reconciliation/liquidation 继续 fail-closed。

## Phase 1：数据与 Schema — COMPLETE

- [x] legacy CSV 2319 行幂等迁入 SQLite，binary-v3 标签与 2h 路径事实可追溯
- [x] 当前真实库迁移前审计：2379 samples、2374 mature、414 positives
- [x] schema v8：`predictions` 物理删除 `profile`，UNIQUE(`sample_id`,`model_id`,`strategy_key`)
- [x] schema v8：`positions` 物理删除 `profile`，`account_kind` 仅 `simulation/live`，模拟策略由 `strategy_key` 表达
- [x] `active_model_slots` 保存 slot 1..3、model id、综合分、单一 threshold、选中时间
- [x] 旧三档预测/仓位/成交原子迁移到 neutral legacy strategy；迁移副本验证 2379 samples / 207 predictions / 90 positions / 180 trades 原数保留
- [x] 正式真实 DB 已升级 schema v8
- [x] 迁移前 SQLite backup：`data/backups/meme_quant_pre_top3_20260811T155151Z.db`
- [x] durable `simulation_sessions` / `training_runs` / `agent_proposals` / trade journal

## Phase 2：持续采集与入场特征 — COMPLETE

- [x] README 指定 8 Launchpad / 3 生命周期 / 12 Key 角色分工
- [x] GMGN discovery/enrichment adapter、共享限流、429 cooldown、本地 safety filter、top-holder 后置过滤
- [x] quote 侧仅 SOL/USDC/USDT；目标 Token 排除稳定币/包装主资产
- [x] admission-time `price` 可训练；`launchpad` 仅元数据；`ln(liquidity_usd)` 持续采集且可 opt-in
- [x] `price_change_1h/5m` 缺失时仅用 `T-1h → T` 历史 1m Kline 回补
- [x] Collector paper-exit / label / discovery stage isolation
- [x] monitor-only：关闭 discovery 仍继续模拟退出与 T+2h 标签

## Phase 3：Top 3 ML / 奥卡姆 / 自更新 — COMPLETE

### 评价公式

- [x] 固定离线经济单位：`U = 6×TP - FP`
- [x] p/r 恒等式测试：`U = N_positive × recall × (7 - 1/precision)`
- [x] 固定 $50 仅用于模型公平离线排名：`fixed_profit_usd = 5 × U`
- [x] 实际模拟/实盘 sizing 不变：`min(1% × entry liquidity, $50)`，并继续计滑点/平台费/网络费
- [x] 经济得分 `E = mean(clip((6TP-FP)/(6N+), -1, 1))`
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
- [x] 模型短名按北京时间训练日显示，本次应为 `20260812-RF / 20260812-DT / 20260812-GB`

## Phase 4：四策略 Simulation — COMPLETE

- [x] 策略键：`model_1 / model_2 / model_3 / rules_only`
- [x] 每策略独立 `1000 USD + 0.1 SOL`
- [x] 每策略最大 10 仓；同 Token 可跨策略/批次共存
- [x] `model_1/2/3` 各自使用对应 active model + 单一 threshold
- [x] `rules_only` 对所有 fresh rule-admitted sample 直接开模拟仓，不创建模型 prediction
- [x] 四策略复用完全相同 BUY/SELL/滑点/费率/网络费/0.9x SL/1.6x TP/2h timeout/卖出失败重试链
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
- [x] four-strategy session reset / ledger
- [x] rules-only 无 prediction 开仓
- [x] Top3 prediction cycle + stale-signal 边界
- [x] 同币四策略一次 Kline 的 1m first-touch E2E
- [x] 三 active model health/degraded queue
- [x] no-route / retry exhausted / live terminal failed sell 计入已实现亏损
- [x] live journal/reconciliation/liquidation mock/fixture fail-closed
- [x] 后端 full `pytest -q`：**98/98 passed**
- [x] 前端 `npm run build`：passed

## Phase 7：Runtime / 发布收尾 — COMPLETE

- [x] 正式真实 DB schema v8 migration
- [x] 正式 Top 3 training + active slots
- [x] 创建新的四策略 active simulation session：`sim_5d975f6978f847de8aec8e5da57ccffa`
- [x] 启动 backend Windows reload supervisor：PID 28404
- [x] Collector / PredictionWorker / TrainingWorker / PaperMonitor / Scheduler / ModelHealth / Reconciliation / Liquidation 全部 `running`
- [x] `/health`、`/api/dashboard`、`/api/models`、`/api/simulation`、`/api/portfolio/view` 真实 API smoke 通过；Top3 labels=`20260812-RF/DT/GB`
- [x] 前端 `http://127.0.0.1:5173` 返回 200，`/@vite/client` 存在，Vite HMR 正常
- [x] 再次确认 `DRY_RUN=true`、`live_trading_enabled=false`
- [x] 临时 `data/top3_migration_test.db` 已删除；保留 pre-top3 SQLite 安全备份
- [x] `git diff --check` clean；final backend `98/98` passed；final frontend production build passed
- [x] commit + push `main`
- [x] Git worktree clean

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

## 环境事实

- repo：`D:\meme` / `https://github.com/ShiningSugar35/meme.git` / `main`
- 本地开发：`scripts/start_runtime.py --reload` + Windows supervisor；frontend Vite HMR
- `.env` 是本地秘密事实源，严禁复制到源码/文档/日志/测试
- 当前系统仍是 localhost 单用户第一版；公网认证、多实例 lease/PostgreSQL 属于未来扩展
