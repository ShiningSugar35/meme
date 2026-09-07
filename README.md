# Solana Meme Quant Trading System

面向 Solana 新发行 Meme Token 的单机量化研究、采集、建模、模拟交易与受控实盘系统。系统以可审计的时间点事实为基础，把候选发现、规则准入、模型评分、执行风险、模拟成交、标签补齐与周期训练组成闭环。

> 默认 `DRY_RUN=true`。未经《开发文档.md》规定的实盘门禁、人工授权与真实合约验收，不广播链上自动买入。

## 1. 系统边界

- 运行形态：Windows、单机、单用户、SQLite、CPU-first。
- 研究对象：允许清单内的 Solana Launchpad 新创建及接近完成阶段 Token。
- 当前特征代：`event1m_regime_v7`。v4/v5/v6 仅保留历史审计，不进入当前训练；v7 以“前置候选 age>3m → 所有必需模型特征完成并验证为数值完整 → 最终轻量刷新市场事实 → 正式 SafetyFilter 仍要求 age>5m → 再提交 entry/决策时点”为因果快照合同。
- 当前标签：`sl090_tp180_m90_binary_v5`，90 分钟窗口，0.9x 止损、1.8x 止盈，同一分钟同时触发时止损优先，超时未触及止盈记负类。
- 当前模型决策合同：`phase19_expected_return_paper_v1`。
- 当前上线认证合同：`phase19_expected_return_certification_v1`。
- 当前经济目标：`precision_recall_payoff_v4`，+3/-1 收益比，`J=(3TP-FP)/N_positive`。
- 模拟交易和理论标签收益、可执行报价收益、真实链上收益分别核算，不混为同一 PnL。

系统不是高频撮合器，不承诺覆盖所有 Meme Token，也不以“每天必须交易”为目标。没有满足质量与风控条件的机会时，模型保持不交易是合法状态。

## 2. 核心链路

```text
GMGN Trenches 候选发现
  → 本地粗筛与权威 enrichment
  → fail-closed 安全准入
  → 不可变入场时特征快照
  → rules_only 基线模拟
  → Active Top 3 校准概率与模型专属阈值
  → 模拟交易；年龄、execution-risk 与 drift 只记录/建模
  → 模拟或受控实盘执行
  → 90 分钟 first-touch 标签补齐
  → chronological OOS 训练与上线认证
```

### Discovery 来源实验

生产 Collector 当前仍以 Trenches lifecycle 发现为正式入口；若需要评估其他发现策略，可使用独立 shadow discovery experiment 并行比较多个来源。Trending 会把 GMGN 官方能够等价表达的 age、liquidity、marketcap、holder、top10、insider、bundler 范围以及 `is_internal_market` 生命周期范围先推到 `/v1/market/rank`，从服务器端限定为尚未 migrated/completed 的 launchpad 内部市场；Trending 请求不显式设置 `limit`，使用 GMGN 默认/最大 100 条语义。缺失 required fact 必须优先使用同一采集周期已观测到的 Trenches 原始事实补齐，再按字段定向补拉对应 endpoint family；只有上游 range 已严格证明某个安全事实、而 PIT 可用的 rank/Trenches/address API 均不返回该数值时，才允许以带来源、谓词和实际请求阈值的 server-qualification provenance 作为最终 fallback，不得伪造数值，也不得替代其他 SafetyFilter。完整 SafetyFilter、top-holder 和 PIT 特征链仍必须执行。实验来源不写正式 `samples`、模型训练或 `rules_only`。状态可用 `scripts\discovery_experiment_status.py` 查询。

## 3. 生产准入合同

发现只负责传输层和生命周期范围；为给后续 enrichment 留出时间，服务器端与本地 discovery prefilter 的年龄下界仅为严格 `age>3min`，这不是交易放宽。业务阈值仍由最终本地准入统一执行，正式样本/交易继续严格要求 `5<age_minutes<300`。关键准入条件包括：

- Launchpad、quote 资产与目标 Token 必须在允许范围；
- rug、insider、bundler、fresh-wallet、wash-trading、税费、sniper 等安全指标通过；
- `liquidity > 5000`；
- 严格 `0.14 < top_10_holder_rate < 0.25`；
- 严格 `5 < age_minutes < 300`；
- `29 < holder_count < 1000`、`marketcap > 5000`；
- `liquidity / holder_count > 50`；
- `swaps_1h > 19`、`volume_1h / swaps_1h > 31`；
- 过去 1 小时（年龄不足 1 小时时从出生起）`buy_swaps / total_swaps < 0.95`；
- 创建来源钱包过去 24 小时发池/发币次数 `<20`；Solana 用创建交易 fee payer/originating signer 对应 EVM `tx.origin` 意图；
- mint/freeze 权限已放弃、burn 状态明确；
- top-holder 结构满足冻结合同。

关键字段缺失、不可解析、非有限值或状态未知时拒绝，不用默认值伪造安全事实。新增链上条件放在常规本地深筛与 top-holder 之后：GMGN 入场时事实优先，缺失/不完整才使用 Alchemy Mainnet RPC，Ankr 不参与。完整阈值以 `backend/app/collector/` 与《开发文档.md》为准。

模型的新训练候选使用 `ln(marketcap/liquidity)` 代替历史 `ln(marketcap+1)`；后者仅为旧工件兼容保留。当前可选目录 61 项、默认 29 项；当前新增候选共22项（2项链上 + 15项985monitor公共事件 + 5项浏览器登录态FOMO）。v7 生产采集先并行完成链上、公共985、账号985与PIT Kline等外部模型特征观测；配置的985monitor公共/账号源只要请求失败、窗口截断、未登录或任一训练字段仍为 `None/NaN/Inf`，该候选本轮就不进入正式样本。全部特征到手后只轻量刷新 price/liquidity/marketcap/age/holder/1h activity 等易变GMGN市场事实并重跑最终 SafetyFilter，随后才冻结 `entry_time/entry_price/feature_snapshot_at`；冻结后不再发任何模型特征请求。每条样本在 `_feature_snapshot_timing` 保存各源 cutoff 与最终 entry commit。完整观测但该 Token 无事件时，latest mention 使用15分钟右截尾 `ln(901)`、FOMO buy-ratio 用中性 `0.5`、USD imbalance 与 `ln(USD+1)` 用 `0`。两个 source coverage 特征因正式样本必然完整而恒为1，已与6个无Solana证据的私有Pump特征一起退役。985monitor网页标签和Chrome本身无需常驻；退出登录、清除站点数据或token失效后账号源会使候选 fail-closed，重新登录即可恢复。凭据不入库、不落artifact、不进Git，整个路径不依赖LLM/Agent。

## 4. 模型与决策

候选模型采用时间顺序开发折、至少 90 分钟标签隔离和最终时间留出；禁止随机打乱、未来字段泄漏以及使用最终留出集反向选择特征、阈值或模型。

每个候选模型独立保存：

- 入场时特征子集；
- chronological development OOS 概率校准器；
- 模型专属 development OOS 期望收益最优 operating point 与固定在线阈值；
- 经济评价、泛化评价和训练 provenance；
- execution-risk 模型及 drift reference。

Paper 模型复筛主链为：规则安全准入 → 模型校准概率达到 development OOS 冻结阈值 → 模拟交易。阈值在 development OOS 的全部 distinct calibrated-probability operating points 中搜索，唯一主目标为 `J = Recall × (4 - 1/Precision) = (3TP-FP)/N_positive`；当前 +3/-1 盈亏比对应 25% 盈亏平衡 Precision。年龄只保留 Collector 的 `5 < age_minutes < 300` 原始准入事实；execution-risk 与 drift 继续记录/监控但不投票。

Drift 的 normal/caution/severe 只用于模型健康监控、提前重训和特征治理，不直接改变 paper selected。AdaptivePolicy 可以在模型基础阈值之上收紧，但不能把 development OOS 冻结阈值向下放宽；execution-risk 与 drift 都不再是额外 paper 硬门。

`rules_only` 不经过模型二筛，用于持续记录全准入池的可执行性事实；它不受模型换代暂停和有限资金余额影响，不是推荐策略，也不代表模型应当模仿全买。

认证失败或单个 slot 未取得逐模型证书时，系统可使用 `shadow_model_1/2/3` 记录真正 post-training 的概率、execution-risk、drift 与后续 outcome；shadow 永不创建仓位，也不进入 AdaptivePolicy 反馈。

## 5. 上线认证

Top 3、特征、概率校准和 J 最优 operating point 均先由 development OOS 冻结；最终时间留出只执行部署否决，不参与任何重选或调参。承担 final 认证的 fitted instance 就是唯一可部署实例，final 标签不得再用于部署前 refit。

1. development 必须存在满足最小交易样本数且 `J>0` 的 operating point；完整阈值空间中选择 J 最大者，J 并列时优先更高 Recall，再看 Precision/Wilson 下界；
2. Top 3 只按 development OOS J 排名，AP/AUC、稳定性、衰减、execution-score 与模型家族多样性仅作审计；
3. final 窗口样本与正负类数量达到最低证据门；
4. 同一模型按冻结阈值至少选中 1 条且 final `J>0`，才取得逐模型 paper 交易资格；
5. final AP/AP lift/ROC-AUC 继续记录为诊断，不参与重排、调阈值或部署硬门；execution-risk 与 drift 同样只作监控/研究；
6. generation 至少存在 1 个具备上述正向 J 证据的模型才可 staged activation；激活后仍只有自身 `qualified_deployment_evidence=true` 的 slot 可以开仓，其他 slot 只做 shadow。

旧标签、旧决策合同或旧认证合同的 Active 模型在新代码下 fail-closed；Collector、rules_only 与已有持仓退出继续运行。

## 6. 模拟名义交易与退出

- simulation 不设置初始本金、可用现金或余额不足门；累计亏损不会停止 `model_1/2/3/rules_only` 继续产生模拟交易；
- 单笔名义投入仍固定为 `min(1% × entry_liquidity_usd, 50 USD)`，仅用于可比的手续费、滑点、PnL 和 ROI 计算；
- 每个策略最大同时持仓 10 个；模型代切换只等待 `model_1/2/3` 平仓，`rules_only` 持续运行；
- 新仓退出快照：0.9x 止损、1.8x 止盈、90 分钟到期；
- 旧仓继续按自身快照退出，不被热更新重解释；
- simulation 与 live 复用幂等订单和退出语义，但真实广播受独立实盘门禁控制。

模拟执行记录平台费、网络费、价格影响、Jupiter 可执行 route、执行偏差与失败原因。公共价格 fallback 只用于允许的监控场景，不得补造 Collector 准入或训练事实。

## 7. 项目结构

```text
D:\meme
├─ backend\app\                 后端、采集、模型、交易与风控
├─ frontend\                    React + Vite 控制台
├─ tests\                       单元、集成和回归测试
├─ scripts\                     运维、迁移、诊断与研究脚本
├─ artifacts\research\          可复现实验与研究证据
├─ data\                        本地数据库和备份，不提交
├─ ml_models\                   模型工件，不提交
├─ logs\                        运行日志，不提交
├─ 开发文档.md                  永久工作流、架构与验收合同
└─ 进度验收.md                  历史变更、审查、测试和上线证据
```

## 8. 启动与检查

项目复用现有 `D:\meme\.venv`，不要覆盖 `.env`、数据库或虚拟环境。

```powershell
cd /d D:\meme
一键启动系统.bat
```

常用检查：

```powershell
D:\meme\.venv\Scripts\python.exe scripts\system_status.py
D:\meme\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm run build
```

运行状态以健康接口、`runtime_state`、最近完整采集周期、Prediction/PositionMonitor 状态和数据库事实为准，不以进程存在或界面动画代替验收。

## 9. 文档职责

- `README.md`：稳定的系统说明、边界与使用入口，不记录开发日记。
- `开发文档.md`：强制研发工作流、长期架构、生产合同与验收标准。
- `进度验收.md`：按时间记录需求、根因、研究、实现、审查、测试、部署和回滚点。
- `artifacts/research/`：实验代码、输入口径、结果与复现说明。

任何 LLM 或开发者接手前，必须先阅读《开发文档.md》第一章和《进度验收.md》最新未关闭条目，再检查 Git 与运行状态。
