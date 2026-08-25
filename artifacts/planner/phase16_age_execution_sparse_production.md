# Phase 16：1.8/0.9/90m + Age-aware + Execution-risk-aware + Sparse Budget 生产化方案

状态：**DESIGN FROZEN / IMPLEMENTATION IN PROGRESS**  
日期：2026-08-24  
适用分支：`main`  
适用环境：Windows 本地单机、SQLite、CPU-first、`event1m_regime_v3`

## 1. 目标与不可违反边界

本工作包把 Phase 15 的第一研究方案正式工程化：

> **1.8x / 0.9x / 90min + age-aware + execution-risk-aware + model-specific sparse budget + drift/calibration**

目标不是单纯把 TP 从 1.6 改到 1.8，而是把“标签、模型、概率可信度、交易预算、执行风险、漂移门控、模型热更新”闭成同一个可审计生产合同。

硬边界：

1. 生产采集不得因模型重训/切换停止；Collector 与模型生命周期继续解耦。
2. 禁止随机切分、禁止最终 holdout 参与模型/特征/阈值/预算选择。
3. 禁止把入场后的 K 线、止损穿透、成交结果作为 winner 模型输入。
4. execution-risk 只能使用入场时可知特征预测“未来若发生 stop 时的 severe gap 风险”。
5. 新旧退出策略必须按**仓位快照**隔离；热更新后旧仓不得被新 1.8/90 规则重解释。
6. 模型不满足 recent/certification 门时允许 abstain，不为了交易频率放宽风控。
7. 当前 `DRY_RUN=true` 边界不改变；本轮“正式上线”指生产采集/标签/训练/模型模拟链正式使用新策略，不解除实盘自动 BUY 的既有停放门。

## 2. Phase 15 事实基线

固定 1183 mature / 1183 Kline 快照：

- `1.8/0.9/60m`：176/1183=14.88%；`age<60m` 后 143/802=17.83%。
- `1.8/0.9/90m`：182/1183=15.38%；`age<60m` 后 146/802=18.20%。
- `age>=60m` 的对应正类率只有 8.66% / 9.45%，差异 p<1e-4。
- p18/90 full 正式 shadow Top3：LightGBM/CatBoost/ExtraTrees；recent holdout 只有 ExtraTrees 极稀疏头部保留稳定信号。
- p18/90 + age<60：802/146；Logistic recent Top5%=4/8，ExtraTrees Top15%=7/24，但 young stop 5% trimmed loss=-23.53%，因此必须把执行风险纳入决策。
- only 60–120m：160 条/11 正类，development economic score 全负，不适合单独建模。
- severe stop-gap 可学：722 linked clean stops，ExtraTrees chronological holdout AUC≈0.677、AP≈0.466；预测风险 Top20% severe≈47.2%，Bottom20%≈8.3%。
- stop gap 主因是触发时已经穿透（与净损失 Spearman≈0.82），不是几秒 execution delay。
- simple prior correction、简单 recent 300/500/700 rolling window 均未稳定修复 drift。

结论：需要“高赔率标签 + 稀疏选择 + age 风险底线 + execution-risk head + recent drift gate”，而不是继续堆一个普通分类器。

## 3. 权威来源检索与落地决策

### 3.1 Learning-to-Rank：适配，但本轮只做受控候选，不替代主分类器

**XGBoost 官方**提供 `rank:ndcg / rank:map / rank:pairwise`，并支持 `lambdarank_pair_method=topk`，可直接把优化重点放到 NDCG@K / 头部排序：

- https://xgboost.readthedocs.io/en/latest/parameter.html
- https://xgboost.readthedocs.io/en/latest/python/examples/learning_to_rank.html

**LightGBM 官方**提供 `lambdarank` 与更快的 `rank_xendcg`，并明确 `lambdarank_truncation_level` 应接近目标 K（通常 K+3）：

- https://lightgbm.readthedocs.io/en/latest/Parameters.html

适配性：高。当前真实交易价值集中在 Top5–10%，LTR 与目标比普通 logloss 更一致。

本轮决策：**不直接替换 winner classifier**。原因是当前 mature 仅约 1.2k，按时间切成“query/day/hour bucket”后每组正类更少，LambdaMART 易出现 query 稀疏/不稳定。先把“Top-K 预算选择”做成模型后处理生产层；LTR 保留为后续 shadow candidate，待 query 样本量与 OOS 稳定性门通过再晋级。

### 3.2 概率校准：立即采用 Sigmoid/Platt，拒绝 Isotonic

scikit-learn 1.9 官方说明：

- 校准数据必须与基模型拟合数据独立；
- sigmoid / isotonic / temperature 均可用于校准；
- isotonic 在校准样本远小于 1000 时容易过拟合；
- sigmoid 是严格单调映射，不改变排名。

来源：

- https://scikit-learn.org/stable/modules/calibration.html
- https://scikit-learn.org/stable/modules/generated/sklearn.calibration.CalibratedClassifierCV.html

本轮决策：**采用 chronological development OOS prediction 上的 sigmoid calibrator**。不使用 final holdout；不使用 isotonic。这样概率才能承担 age break-even floor 与 execution-risk 联合决策含义。

### 3.3 Conformal / Risk Control：思想采用，暂不引入 MAPIE 运行时依赖

Conformal Risk Control / MAPIE BinaryClassificationController 的核心价值是：用独立 calibration set 对 precision / recall / FPR 等风险提供有限样本控制。

参考：

- Angelopoulos et al., *Conformal Risk Control*, ICLR 2024：https://openreview.net/forum?id=33XGfHLtZg
- MAPIE risk-control 文档：https://mapie.readthedocs.io/

本轮决策：**采用其“校准集与训练集隔离 + 风险上界/下界门”的工程思想**，但不新增 MAPIE 依赖。当前部署先用可审计的 Wilson precision lower bound + 最小 observation gate，避免在 ~1k 样本的 Windows 单机上增加不必要依赖。后续达到 3k+ mature 可做 MAPIE shadow 对拍。

### 3.4 Concept Drift：采用轻量双窗口检测，River ADWIN 暂不引依赖

ADWIN 的自适应窗口思想适合流式漂移；River 提供成熟实现。但本项目标签有 90min 延迟、数据流低频且 SQLite 单机，直接将 ADWIN 作为强制依赖收益有限。

参考：

- River drift/ADWIN：https://riverml.xyz/

本轮决策：实现**无新依赖的 recent-vs-reference 漂移 gate**：正类先验变化 + 关键 feature PSI/KS 诊断 + recent model precision evidence。状态分 `normal/caution/severe`，只提高阈值/冻结模型开仓，不阻塞 Collector 和持仓退出。后续 River ADWIN 做 shadow 候选。

### 3.5 TabPFN / TabM / FT-Transformer / DeepHit

- TabPFN：当前系统已集成；TabPFN 官方定位为小表格 foundation model，但 CPU 推理成本仍高。本机为 CPU-first，继续 1 estimator/受控特征网格，不扩大主线权重。
- TabM（ICLR 2025）是强 tabular DL 基线，但更适合有 GPU 或更大数据；当前约 1.2k mature 不值得增加 PyTorch 训练主链复杂度。
- FT-Transformer 是成熟 tabular Transformer，但论文/复现实证并不支持“所有表格都优于 GBDT”，且 CPU 训练成本高。
- DeepHit 能原生表达 TP/SL first-touch competing risks，理论匹配度高；但当前事件数、CPU 环境和维护复杂度都不满足生产条件。

参考：

- TabPFN / PriorLabs：https://github.com/PriorLabs/TabPFN
- TabM：https://openreview.net/forum?id=sdFhnoi9El
- FT-Transformer：Gorishniy et al., NeurIPS 2021
- DeepHit：Lee et al., AAAI 2018, https://ojs.aaai.org/index.php/AAAI/article/view/11842

本轮决策：**全部不进入本次生产主线**；保留 shadow/research backlog。

### 3.6 CatBoost Ordered Boosting / GBDT 家族

CatBoost ordered boosting、XGBoost/LightGBM/ExtraTrees 继续是当前数据量与 CPU 环境最匹配的主力。前沿优化重点不再是“增加更多同类 GBDT”，而是让模型输出经过正确的时间 OOS 校准、预算化和风险门控。

## 4. 目标生产架构

```text
Collector / immutable PIT features
        │
        ├── 90m barrier label: TP1.8 / SL0.9 / timeout negative
        │
        ├── Winner model family (existing diverse 13-candidate pool)
        │       └── chronological OOS probability
        │               └── Sigmoid calibrator (OOS only)
        │
        ├── Execution-risk head
        │       └── P(severe stop-gap | entry-time features)
        │
        └── Decision Policy
                ├── model-specific sparse budget threshold (5/7.5/10/15%)
                ├── age-aware probability floor
                ├── 60–120m hard abstain
                ├── execution-risk max gate
                ├── drift state adjustment
                └── AdaptivePolicy only after the above base safety floor
```

## 5. 生产策略详细合同

### 5.1 新标签合同

- SL：0.90x
- TP：1.80x
- window：90min
- same 1m bar：SL 优先
- timeout：negative
- 新版本：`sl090_tp180_m90_binary_v5`
- label gross proxy：positive +80%，negative -10%（仅标签/审计，模型经济目标仍以真实 route ledger 校准的 `fixed_3_to_1_v3` 排名）
- `price_1h_*` 保留历史，不再被新标签覆盖；新增 generic barrier audit 字段：
  - `label_max_price_ratio`
  - `label_min_price_ratio`
  - `label_final_close_ratio`
  - `label_window_seconds`

### 5.2 仓位退出策略快照

positions 新增：

- `execution_policy_version`
- `stop_loss_ratio`
- `take_profit_ratio`
- `max_holding_seconds`

既有仓回填旧合同：`h1_tp160_sl090_v1 / 0.9 / 1.6 / 3600`。新仓使用：`m90_tp180_sl090_v2 / 0.9 / 1.8 / 5400`。

PositionMonitor 只读取仓位自己的 stop/take/expires 快照，禁止用当前全局常量重解释旧仓。

### 5.3 Sigmoid calibration

每个 candidate 在 chronological development folds 产生 OOS probabilities 后：

1. 仅用 development OOS `(p_raw, y)` 拟合 `LogisticRegression(C=1e6)` 的单变量 sigmoid；输入使用 clipped logit(p)。
2. OOS threshold/budget/economics 全部在 calibrated p 上评价。
3. final holdout estimator 仍只用 pre-holdout train 拟合，预测后应用同一个 development calibrator；holdout 不拟合 calibrator。
4. refit production estimator 用 refit data 拟合，继续携带 development calibrator。
5. 若 OOS 两类不足或 calibrator 失败，fail-closed：该 candidate 不可晋级。

### 5.4 Model-specific sparse budget

候选预算：5%、7.5%、10%、15%。

每个模型只在 development OOS 上：

- 对 calibrated probability 按分位数形成固定在线 threshold；
- 至少 `max(5, ceil(2% × OOS rows))` 个 selected；
- `profit_units > 0`；
- Precision 必须高于 global 25% break-even；
- 优先最大 `profit_units`，同等时优先更高 precision、更小预算；
- 保存 `budget_fraction / budget_threshold / OOS trade count / precision / Wilson LCB / profit_units`。

在线不做跨样本实时排序；使用训练期确定的固定 quantile threshold，因此不会等待一批未来样本。

### 5.5 Age-aware gate

基于真实 route-aware stop break-even：

| entry age | production action | calibrated P(winner) floor |
|---|---|---:|
| 2–10m | allow with stronger floor | 0.29 |
| 10–30m | allow with stronger floor | 0.29 |
| 30–60m | allow | 0.22，但最终仍受全局 0.25 与 budget threshold 约束 |
| 60–120m | **ABSTAIN** | — |
| 120–300m | allow conservatively | 0.25 |

实际 base threshold = `max(model_budget_threshold, 0.25, age_floor)`。

Age gate 只影响 model_1/2/3；rules_only 继续作为无模型执行基线，以便持续收集全准入池执行事实。

### 5.6 Execution-risk head

目标仅在 clean linked stop 样本中构造：

`severe_gap = trigger_reference_price <= stop_loss_price * 0.90`

即第一次 stop trigger 时，已比 0.9x 止损线再穿透至少 10%。

- 特征：只允许 current approved entry-time catalog。
- 训练：chronological 80/20，ExtraTrees + Logistic 两个小候选，按 AP skill/AUC 选一个。
- 最低数据门：observations>=300、positive>=40、negative>=80；不满足则 head=`unavailable`，模型开仓使用更保守阈值但不得凭空填风险。
- certification：holdout AUC>=0.60 且 AP >= base prevalence + 0.05；否则 head 不激活。
- active risk threshold：默认 `P(severe_gap) <= 0.40`；若 drift=caution 收紧到 0.35；severe 时冻结 model entries。
- risk head 版本、训练窗口、AUC/AP/base prevalence、特征列表、artifact hash 全部写入模型/运行审计。

### 5.7 Drift / recent certification gate

不把 final holdout用于调参，但允许它决定“是否 abstain/冻结上线”，这是 deployment certification，不是 model selection。

模型 generation 只有在以下条件满足才可激活：

1. 至少一个 Top3 在 final recent window 有 >=5 个 model-policy selected observations；
2. 对已有 >=8 个 selected observations 的模型，其 final `profit_units >= 0`；
3. 若 final selected <8，标 `limited_evidence`，允许 simulation，但 threshold 增加安全 margin +0.03；
4. recent label prevalence 相对 development reference 下降 >35% 或关键 feature drift 达 severe，则 model entries freeze，Collector/rules_only/退出不停。

### 5.8 AdaptivePolicy 的位置

现有 AdaptivePolicy 继续存在，但顺序改变为：

`calibrated probability → sparse budget/age/execution/drift hard gate → adaptive threshold only upward/downward inside hard safety floor`

任何 EXPANSIVE action 都不得把 threshold 降到 base safety floor 以下，也不得绕过 age 60–120 abstain / execution-risk / severe-drift gate。

## 6. 数据迁移与热更新

1. 使用 SQLite backup API 创建 pre-v5 物理备份。
2. 扩展 Kline cache 至迁移时全部 current-generation mature 样本。
3. 所有 v3 mature 按 1.8/0.9/90m first-touch 原地重新标注为 v5；pending 保持 pending，达到 90m 后由 Collector 正常 finalization。
4. 迁移前后校验：row count、不合规 label version、tag domain、Kline coverage、FK check。
5. models/predictions 历史不删除；旧 active models 因 label_version/decision-policy version 不匹配 fail-closed，直到新模型激活。
6. position policy snapshot 使旧仓自然按旧合同退出；无需停 Collector，也无需强平。
7. 新模型训练完成后继续使用现有 `waiting_for_flat`/generation rollover 机制；当旧 generation 仓位全平时原子切换。

## 7. 软硬件可落地性评估

已确认运行环境：Windows，本地 SQLite，11th Gen Intel Core i5-1135G7，系统当前按 CPU-only 模型路径运行。

| 方案 | CPU成本 | 当前数据量适配 | 工程风险 | 本轮结论 |
|---|---:|---:|---:|---|
| GBDT/ExtraTrees/Logistic + sigmoid | 低 | 高 | 低 | **生产** |
| model-specific sparse budget | 极低 | 高 | 低 | **生产** |
| execution-risk ExtraTrees/Logistic | 低 | 高 | 中 | **生产，有数据门** |
| LightGBM/XGB LambdaMART | 低-中 | 中 | 中 | shadow/backlog |
| River ADWIN | 低 | 中 | 依赖新增 | 暂不引依赖 |
| MAPIE CRC | 低-中 | 中 | 依赖新增/样本小 | 思想采用，shadow backlog |
| TabPFN | 中-高 CPU | 中 | 已存在 | 保持现状、非主力 |
| TabM / FT-Transformer | 高 | 低 | 高 | 不生产 |
| DeepHit | 高 | 低 | 高 | 不生产 |

## 8. 验收标准（开发/审查/测试统一合同）

### A. 标签与数据

- [ ] `LabelPolicy == 0.9 / 1.8 / 5400s / sl090_tp180_m90_binary_v5`。
- [ ] same-bar SL precedence 回归测试通过。
- [ ] 新标签不覆盖 legacy `price_1h_*`，generic barrier facts 完整。
- [ ] 当前 generation mature 迁移 Kline coverage=100%，migration API/network error=0。
- [ ] migrate 后 mature `label_version=v5` violation=0、tag violation=0、FK check=0。
- [ ] pending 在 <90m 不成熟，>=90m 才可 finalization。

### B. PIT / 时序 / 校准

- [ ] feature ranking 仅看 fold train。
- [ ] calibrator 只看 development OOS；final holdout 不参与 calibrator fit。
- [ ] sigmoid monotonic，校准前后排序 Spearman≈1（允许 ties/浮点误差）。
- [ ] no future/label/trade field 可进入 winner 或 risk feature matrix。
- [ ] RBF-SVM 不再依赖 deprecated `SVC(probability=True)` 的内部随机校准。

### C. Sparse budget / age / execution risk

- [ ] 每个 active model 持久化独立 `budget_fraction` 与 `budget_threshold`。
- [ ] 在线 threshold 不低于 0.25 global floor 与对应 age floor。
- [ ] age 60–120 对 model strategy 必须 abstain；rules_only 不受影响。
- [ ] execution-risk head 低于数据/指标门时 fail-closed 为 unavailable，不伪造风险值。
- [ ] active risk head artifact 可加载、hash 可审计。
- [ ] P(severe)>risk ceiling 时 model position 不得打开。
- [ ] severe drift 时 model entries 冻结，但 Collector、rules_only 与已持仓退出继续运行。

### D. 仓位策略快照 / 热更新安全

- [ ] schema migration 给旧 position 回填 0.9/1.6/3600 old policy。
- [ ] 新 position 写入 0.9/1.8/5400 new policy。
- [ ] PositionMonitor exit reason/price/timeout 使用 position snapshot，不读当前全局 TP/timeout 重解释旧仓。
- [ ] 在同时存在 old/new policy position 的测试中，两者各自按自己的 TP/timeout 退出。
- [ ] source reload / model rollover 不要求停止 Collector。

### E. 模型生命周期

- [ ] 旧 label/decision-policy generation 在新代码下 fail-closed，不产生 model entry。
- [ ] 新训练 summary 写明 label version、decision policy version、calibration、budget、risk head、drift certification。
- [ ] final holdout 只做 certification，任何阈值/预算选择不得读取 final label 做反向优化。
- [ ] Top3 artifact 全部可加载，Prediction 能用 calibrated probability + policy gate 产生可审计 decision reason。
- [ ] staged rollover 与 rollback 保留。

### F. 专业级测试

- [ ] `python -m py_compile` 覆盖所有新增/修改后端模块。
- [ ] targeted ML/collector/prediction/paper/monitor/database tests 全过。
- [ ] full `pytest -q` 100% pass。
- [ ] frontend `npm run build` pass。
- [ ] DB migration 在临时 DB/生产备份上通过；`PRAGMA foreign_key_check`=0。
- [ ] runtime `/health=ok`；Collector 连续至少 3 cycle state=running/errors=[]；PositionMonitor cadence 正常；SOL/USD ready。
- [ ] 重训完成且 retry=0；新 Top3 objective/label/policy version 全部一致。
- [ ] 新 generation 正式激活后 Prediction model_ids 为新 Top3，旧 generation 不再开仓。
- [ ] 样本数在整个 rollout 期间持续增长，证明 Collector 未停。

## 9. 审查打回条件

任一命中即不得 push/上线：

- final holdout 参与 feature/threshold/budget/calibrator fit；
- old position 被新 global policy 改写；
- risk head 使用 stop 后事实作为输入；
- model gate 可被 Adaptive EXPANSIVE 绕过；
- 迁移不是 100% Kline coverage；
- 训练/迁移期间 Collector 停止且未自动恢复；
- 测试失败、FK 异常、artifact 无法加载；
- 新模型没有 label/policy/calibration provenance。

## 10. 开发进度

- [x] 权威来源检索与方案收敛。
- [x] Phase 15 数据事实复核。
- [x] 架构与验收合同冻结。
- [ ] Schema / position policy snapshot。
- [ ] 90m label contract / relabel migration。
- [ ] chronological sigmoid calibration。
- [ ] sparse budget optimizer。
- [ ] age-aware safety gate。
- [ ] execution-risk head。
- [ ] drift/certification gate。
- [ ] Prediction/Training/ModelRegistry 集成。
- [ ] 单元/集成测试。
- [ ] 独立代码审查与返工。
- [ ] 专业级全量测试。
- [ ] production migration + retrain + staged hot activation。
- [ ] commit + push。
