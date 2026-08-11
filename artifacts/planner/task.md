# Solana Meme Quant Trading System - 当前开发状态

> 更新时间：2026-08-10  
> 当前范围：**除真实 live BUY 外，本地单机/单用户版本全部闭环**。实盘 provider/journal/reconciliation/liquidation 接口保留并 fail-closed，等待未来真实钱包与 GMGN 现场契约。

## Phase 1：数据与 Schema — COMPLETE

- [x] 阅读并对齐 `README.md`、`开发文档.md`、`architecture_review.md`
- [x] 根目录 `meme数据.csv` 审计：40 列、2319 行
- [x] legacy CSV 幂等迁移到 `data/meme_quant.db`
  - total 2319
  - mature 2317
  - pending 2
  - tag0 1892
  - tag1 409
  - tag2 16
  - legacy `utility_eligible=false`
- [x] SQLite schema v6 幂等 migration；真实库升级后样本/标签数量不漂移
- [x] 完整标签路径字段：first TP/SL、exit reason、same-bar conflict、gross return、return source
- [x] legacy tag2 使用 +25% 已知下限并标 `legacy_floor`
- [x] legacy pending 即使后续 K-line 补成 mature，也保留原 `utility_eligible=false`，不因缺失 raw liquidity 被误升级为真实美元效用样本
- [x] `simulation_sessions` registry
- [x] durable `training_runs`
- [x] durable `agent_proposals`

## Phase 2：持续采集与入场特征 — COMPLETE

- [x] 保留 README 指定 10 个 Launchpad / 3 生命周期 / 12 Key 角色分工
- [x] 全局共享限流/429 cooldown
- [x] 本地 safety filter + top-holder 后置过滤
- [x] README 入场特征持续入库
- [x] `price` 明确为 admission-time `entry_price`，允许默认训练
- [x] `launchpad` 仅元数据，不进模型
- [x] `ln(liquidity_usd)` 从新样本开始持续采集，但当前默认训练关闭
- [x] `price_change_1h/5m` 若 snapshot 缺失，准入时立即用 `T-1h → T` 历史 1m Kline 回补；不读取未来
- [x] CollectorWorker stage isolation：paper exit / label / discovery 互不拖死
- [x] monitor-only 模式：关闭 discovery 时仍继续模拟退出与 T+2h 标签

## Phase 3：ML 训练、Champion 与自更新 — COMPLETE

- [x] 显式模型 feature allowlist
- [x] 模型中心自选 feature schema + 成熟样本 coverage
- [x] 默认 recipe 31 个输入特征；`ln(liquidity_usd)` 可后续 opt-in
- [x] 时序扩展窗口、2h embargo、<120d EARLY_STAGE / >=120d 120d+30d 规则
- [x] 五候选：Logistic Regression / HistGradientBoosting / XGBoost / ExtraTrees / RandomForest；2026-08-10 当前 `.venv` 已安装 XGBoost 3.4.0，真实训练 run `0e4a4cd7-d933-45f3-8c1b-3662a566d21e` 验证五候选均实际执行，XGBoost=`ok`
- [x] Precision 20% + min trades 硬门槛
- [x] 一个 Champion + aggressive/balanced/conservative 三阈值
- [x] Occam 近似等价优先简单模型
- [x] production refit bundle + pre-holdout evaluation bundle
- [x] incumbent recipe 在当前 pre-holdout 历史上重建，与 Challenger 同 OOS rows 公平比较
- [x] 真实 USD utility / 5% normalized lift 自动晋级门禁
- [x] candidate / rejected / champion / retired 状态语义
- [x] retired Champion 工件可加载时人工原子 rollback
- [x] durable TrainingWorker 串行消费 manual/weekly/startup/degraded queue
- [x] interrupted running training restart recovery + bounded retries
- [x] 周日北京时间 03:00；错过启动补排；`scheduled_for` 唯一
- [x] 自动训练继承当前 Champion feature schema
- [x] 7 日 OOS model health；legacy/proxy 不触发自动退化
- [x] real USD Precision/ROI 退化 → degraded training queue + cooldown

### 真实数据库训练验收

- [x] 首轮真实训练完成：Logistic Regression 被 Occam 选择为 Champion
  - model id: `20260810T095656Z-logistic_regression-f9b6fb04`
  - EARLY_STAGE
  - 31 features
  - thresholds: aggressive≈0.385538, balanced≈0.385538, conservative=0.8
- [x] 第二轮真实训练完成：成功 rebuild incumbent 并公平比较
- [x] 第二轮 Challenger 因 legacy OOS 无真实 liquidity/精确 tag2 close 被安全拒绝，原 Champion 保留

## Phase 4：固定模拟 / Shadow 完整链 — COMPLETE

- [x] simulation session 三账户：paper/balanced、shadow_aggressive、shadow_conservative
- [x] 每账户显式 `1000 USD + 0.1 SOL`
- [x] session registry：restart 恢复；reset 关闭旧 session、创建新 session，不删历史
- [x] 每账户最大 10 仓
- [x] 同 Token 多模拟批次允许
- [x] 单笔资金 `min(1% * entry liquidity, $50)`
- [x] seeded BUY/SELL quote：price impact/slippage/1% fee/network fee/latency/failure
- [x] Prediction → 三档信号 → 对应 paper/shadow 开仓幂等
- [x] 1m Kline first-touch：0.9x SL / 1.6x TP / 2h timeout / same-bar SL first
- [x] 同币多仓共享一次 Kline fetch
- [x] exit trigger 持久化；SELL failure → closing；restart 后只重试原退出
- [x] 理论标签收益与 executable simulation PnL 完全分离
- [x] cash/SOL fee ledger、trade journal、position PnL
- [x] simulation history API + Portfolio 历史会话 UI
- [x] Dashboard/Portfolio 默认只统计当前 simulation session；历史不污染当前 PnL
- [x] 一键清仓 paper/shadow 使用真实新鲜市场参考，不伪造零 PnL close

## Phase 5：Agent / 本地控制台 — COMPLETE

- [x] Agent read-only context
- [x] durable proposal registry
- [x] 允许 proposal：train_model / rollback_model / reset_simulation / pause_new_entries / resume_new_entries
- [x] live buy/sell/liquidation、wallet transfer、secret access 在创建阶段拒绝
- [x] 人工 approve/reject 后才执行非实盘白名单动作
- [x] proposal result/error/audit 持久化
- [x] `/agent` 审批页
- [x] Dashboard / Portfolio / Signals（产品名“样本采集”）/ Runtime / Models / Agent 六页面与固定侧栏顺序
- [x] 模型中心 feature coverage、自选 schema、训练 queue、rollback
- [x] Portfolio `simulation/live × balanced/aggressive/conservative` 两层视图；顶栏模式切换；只跑模拟时默认模拟，live 启用时默认实盘
- [x] Portfolio 当前仓位缓存 GMGN 当前流动性/市值；Token 悬浮复制；交易历史 SQL 真分页、时间筛选、page size 持久记忆；simulation audit 三档独立
- [x] live Portfolio 同构前后端账本 scaffold 已标记 GMGN Trading API，自动 live BUY 仍保持停放
- [x] Runtime 显示 TrainingWorker / model health / monitor-only / reconciliation / liquidation
- [x] Runtime Collector 实时终端：三生命周期独立 returned/accepted/rejected/duplicate 统计 + 最近 250 条后端结构化采集事件

## Phase 6：API / Regression / Real DB 验收 — COMPLETE

- [x] FastAPI non-live E2E：durable model train queue
- [x] FastAPI non-live E2E：simulation reset/history
- [x] FastAPI non-live E2E：Agent unsafe reject + human approve/reject
- [x] GMGN trade adapter sanitized fixture contracts
- [x] 当前部署环境只读 GMGN data smoke：`new_creation limit=1` discovery 可达，enrichment 可执行；不写库、不交易、不输出 Key
- [x] 发现 GMGN 可能返回超过请求 limit 的候选后，已在 DiscoveryService 本地二次 `[:limit]` 截断；复验 candidates=1
- [x] live journal crash window / pending / unknown / reconciliation mock tests
- [x] persistent liquidation mock tests
- [x] 后端 full `pytest -q`：**90/90 passed**（新增持仓按档位/时间筛选、三档交易审计、当前市场快照、模拟/实盘清仓作用域隔离回归）
- [x] 前端 `npm run build`：passed
- [x] 真实 `data/meme_quant.db`：schema v6 / 2319 条 legacy 已迁移并开始持续追加新样本；2026-08-10 当前 2320 samples（2319 mature + 1 pending）/ Champion 工件存在

## Phase 7：Live — PARKED BY USER SCOPE

以下不是本轮继续开发项，接口保留且 fail-closed：

- [ ] 真实 wallet snapshot：available USD / total equity / SOL balance / SOL-USD / token balances
- [ ] real Token decimals / `output_amount_raw` GMGN 现场契约
- [ ] 无 stable order id 的 wallet/token balance reconciliation
- [ ] shadow 与真实钱包共同资金闸门
- [ ] BJT 日初真实钱包总权益（含未实现 PnL）20% 日损 E2E
- [ ] 实盘连续 5 亏真实钱包 E2E
- [ ] README 已冻结 `balanced` → RiskService → LiveTradingService 自动 BUY
- [ ] 用户当次明确授权后的极小额 buy → restart reconcile → sell 现场验收

在上述真实资金事实完成前：

- `DRY_RUN=true`
- 没有自动 live BUY pipeline
- 不使用模拟钱包数值绕过实盘门禁

## 后续自然演进（不是当前漏项）

1. 持续采集新样本；关注 `ln(liquidity_usd)` coverage。
2. 当该字段覆盖率足够后，在模型中心勾选它创建新 recipe，系统会自动把该 feature schema 延续到后续周训。
3. 样本跨度达到 120 天后，训练自动从 EARLY_STAGE 切换到标准 120d/30d 时序方案。
4. 如果未来公网化：先做正式身份/session/CSRF/角色权限。
5. 如果未来多进程/多服务器：引入共享 limiter、lease、PostgreSQL。

## 环境事实

- `D:\meme` 已重新关联 `https://github.com/ShiningSugar35/meme.git`；完整非实盘重构与后续 Portfolio/Collector 优化均按正常 Git 流程提交到 `main`。
- 本地开发使用 `scripts/start_runtime.py --reload` 的 Windows supervisor 监控 `backend/**/*.py`；已实测源码变化后自动重启 Uvicorn 子进程并恢复 Collector/TrainingWorker。实盘常驻禁止 reload。
- 根目录没有 `AGENTS.md`。
- `.env` 未被复制到文档、测试或前端；真实凭据继续留在本地秘密配置。
