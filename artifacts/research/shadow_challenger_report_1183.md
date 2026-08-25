# Shadow Challenger 与 Age / Execution Risk 只读研究报告（1183 mature 快照）

## 1. 研究边界

本轮全部为 read-only / shadow 研究。生产 `samples.tag`、生产 TP/SL/timeout、active model slots、training_runs、simulation/live 策略均未因本研究改动。

- 固定快照：1183 条 `event1m_regime_v3` mature 样本。
- 快照生产标签正类：219 / 1183。
- 2h / 1m Kline 缓存：1183 / 1183 覆盖。
- 当前经济目标保持 `fixed_3_to_1_v3`。
- 生产基线仍为 1.6x / 0.9x / 60min。
- 主 challenger：1.8x / 0.9x / 90min。
- 激进 challenger：2.0x / 0.9x / 90min。
- 完整 shadow 主赛马复用生产 `FeatureBuilder + ModelTrainer + TrainerConfig`、当前 44 feature pool、chronological folds、13-model candidate pool；不注册模型、不写训练运行、不激活。

## 2. 用户新增专项：1.8x / 0.9x 在 age<1h 后的样本数与正类率

### 2.1 1.8x / 0.9x / 60min

全部 mature：

- n = 1183
- positive = 176
- positive rate = **14.88%**

仅 entry age < 60min：

- n = **802**
- positive = **143**
- positive rate = **17.83%**
- hard-SL = 644
- timeout = 15

age >= 60min：

- n = 381
- positive = 33
- positive rate = **8.66%**

age<60 相对 age>=60：

- +9.17 个百分点
- relative lift ≈ 2.06x
- 两比例检验 p≈3.5e-5

### 2.2 1.8x / 0.9x / 90min

全部 mature：

- n = 1183
- positive = 182
- positive rate = **15.38%**

仅 entry age < 60min：

- n = **802**
- positive = **146**
- positive rate = **18.20%**
- hard-SL = 646
- timeout = 10

age >= 60min：

- n = 381
- positive = 36
- positive rate = **9.45%**

age<60 相对 age>=60：

- +8.76 个百分点
- relative lift ≈ 1.93x
- 两比例检验 p≈9.6e-5

### 2.3 这一专项的含义

- `age<60m` 对 1.8x 标签是显著富化，而不是轻微变化。
- 60→90min 在 age<60 子集只新增 3 个正类（143→146），正类率仅 +0.37pp；时间延长的边际作用远小于 age 筛选本身。
- `1.8/0.9/90 + age<60` 保留 802 条 / 146 正类，正类率 18.20%，与当前生产标签全体约 18% 的 class density 接近，因此不会因为 TP 提高到 1.8x 就把训练集压成严重稀疏标签。
- 但 age<60 仍存在时间漂移，不能把它理解为稳定规则。

## 3. 完整生产栈 shadow：1.8x / 0.9x / 90min

全体 1183：

- positive = 182 / 1183 = **15.38%**
- final chronological holdout = 237 条
- holdout positive = 27 / 237 = **11.39%**

开发期 Top3：

1. LightGBM：dev Precision 51.7%，29 trades；final 6 trades / 0 win。
2. CatBoost：dev Precision 58.8%，17 trades；final 1 trade / 0 win。
3. ExtraTrees：dev Precision 30.4%，56 trades；final 2 trades / 1 win。

这说明把标签改成 1.8x 并没有消除 recent regime shift。开发期最强的 LightGBM / CatBoost 在 final recent holdout 中仍会失效。

### recent 固定交易预算

ExtraTrees 是唯一在 final holdout 头部保留明显信号的正式候选：

- Top5%：12 trades / 4 wins = **33.3% Precision**；使用 +75% TP（给目标价留 5pp 折损）和 -21.93% clean trimmed stop，压力收益约 **+10.38% / trade**。
- Top7.5%：18 / 4 = 22.2%，基本归零。
- Top10%：24 / 5 = 20.8%，转负。

LightGBM / CatBoost 的 recent 排序整体无效。

结论：1.8x/90 的有效信号如果存在，也高度集中在模型特定的极少数头部，而不是“所有 Top3 模型统一放宽交易”。

## 4. 完整生产栈 shadow：2.0x / 0.9x / 90min

全体 1183：

- positive = 149 / 1183 = **12.60%**
- final chronological holdout = 237
- holdout positive = 21 / 237 = **8.86%**

开发期 Top3：

1. CatBoost：dev Precision 39.3%，final threshold 0 trades。
2. RBF-SVM：dev Precision 42.9%，final 7 trades / 0 win。
3. Logistic Regression：dev Precision 44.4%，final 2 trades / 1 win。

### recent 固定交易预算

只有 Logistic 的极稀疏头部值得继续观察：

- Top5%：12 / 4 = **33.3% Precision**；按 +95% TP / -21.93% stop 压力约 **+17.05% / trade**。
- Top7.5%：18 / 4 = 22.2%，仍正但明显下降。
- Top10%：24 / 4 = 16.7%，转负。
- Top15–20% 在该小样本中略正，但安全垫很薄，不能据此扩仓。

结论：2.0x 是更高赔率、更低 base rate、更依赖极稀疏筛选的 challenger。它不适合作为当前全局生产标签替代，但可以作为 aggressive shadow head。

## 5. age<60 的正式 1.8x/90 ablation

样本：

- rows = **802**
- positive = **146**
- positive rate = **18.20%**
- final holdout = 161
- final holdout positive = 23 / 161 = **14.29%**

Top3：ExtraTrees / Logistic Regression / CatBoost。

正式 candidate threshold：

- Logistic：dev Precision 50%，8 trades；final 2 trades / 1 win = 50%。
- ExtraTrees：dev Precision 37.5%，16 trades；final threshold 0 trades。
- CatBoost：dev Precision 50%，6 trades；final threshold 0 trades。

### age<60 专属 stop stress

clean linked stop（age<60）真实：

- n = 505
- mean = -24.99%
- median = -20.24%
- 5% trimmed mean = **-23.53%**

因此不能用全局 -21.93% stop 成本乐观评价年轻币。

按 `TP stress=+75%`、`age<60 stop=-23.53%` 重算：

Logistic：

- threshold：2 trades / 1 win，约 **+25.74% / trade**。
- Top5%：8 / 4 = **50% Precision**，约 **+25.74% / trade**。
- Top7.5%：12 / 4 = 33.3%，约 **+9.32% / trade**。
- Top10%：16 / 4 = 25%，约 **+1.11% / trade**。
- Top15%：24 / 7 = 29.2%，约 **+5.21% / trade**。
- Top20%：32 / 7 = 21.9%，约 **-1.97% / trade**。

ExtraTrees：

- Top5/7.5/10% 都约 25% Precision，压力约 +1.11%。
- Top15%：24 / 7 = 29.2%，压力约 **+5.21% / trade**。
- Top20%：25%，约 +1.11%。

CatBoost recent 头部不理想。

这些样本仍很少，不能把 8 trades / 4 wins 直接当作生产阈值证据，但 `1.8/90 + age<60 + model-specific sparse budget` 是当前最值得继续积累 shadow OOS 的组合。

## 6. age 60–120m ablation

### 排除 60–120m

- rows = 1023
- positive = 171 = **16.72%**
- final holdout = 205，positive 22 = **10.73%**
- Top3 = ExtraTrees / LightGBM / CatBoost

ExtraTrees：

- final threshold：2 trades / 1 win
- Top5%：10 trades / 4 wins = **40% Precision**，全局压力约 +16.84% / trade
- Top7.5%：15 / 4 = 26.7%，仍正
- Top10%：20 / 4 = 20%，转负

LightGBM / CatBoost recent 排序仍弱。

排除这个 age 段有一定富化作用，但仍不能修复整个模型族的 recent drift。

### 只训练 60–120m

- rows = **160**
- positive = **11** = **6.88%**
- final holdout = 32
- final positive = 5

虽然生产同款 Trainer 能形式上完成，但开发评估窗口里的 positive 已低到约 3 个；Top3 的 development economic score 全部为负：

- RBF-SVM：development profit_units -2
- CatBoost：-3
- ExtraTrees：-5

结果对 1 个 winner 的位置极端敏感，无法视为可独立训练 regime。

结论：60–120m 更适合降权/排除研究，不适合单独建模。

## 7. Age 不是单向利好：年轻币的 stop gap 更严重

clean stop 的 severe-gap 定义：第一次观测到跌破 0.9x 时，已经比止损线再低至少 10%。

- <10m：severe gap **38.5%**；平均 stop 净损失约 -26.48%。
- 10–30m：**36.0%**；平均约 -24.26%。
- 30–60m：25.0%；平均约 -21.78%。
- 60–120m：20.4%；平均约 -20.15%。
- 120–300m：12.6%；平均约 -19.82%。

真实 TP/SL break-even Precision 也随 age 变化：

- <10m：约 **27.37%**
- 10–30m：约 **27.63%**
- 30–60m：约 **20.00%**
- 60–120m：约 23.72%
- 120–300m：约 21.91%

因此 `age<60` 不能做成无条件放行规则。它是“更高 winner prior + 更高尾部执行风险”。正确方向是 age-aware threshold / execution-risk penalty。

## 8. Stop execution gap：真正的损失来自哪里

clean broad rules-only stop：

- 首次 stop reference 相对 0.9x 止损线，中位已额外穿透约 **-4.54%**。
- 25% 分位已穿透约 -11.86%。
- 最差 10% 已穿透约 -25.3%。
- route/fill 相对触发 reference 再损失的中位约 -2.77%。
- execution delay 中位约 3.1s。

Spearman：

- trigger overshoot vs net return：ρ≈0.82，强相关。
- execution delay vs net return：ρ≈-0.03，p≈0.38，几乎无相关。

结论：主要问题不是把 2s monitor 改成 1s，也不是 Jupiter quote latency；是价格跳跃 / 流动性塌陷让第一次可观察的 below-stop price 已经远离 0.9x。

## 9. Stop-gap 风险在入场时部分可预测

722 个有当前样本关联的 clean stop，severe-gap base rate≈29.8%；chronological 75/25：

ExtraTrees execution-risk classifier：

- AUC ≈ **0.677**
- AP ≈ **0.466**（holdout severe base≈27.1%）
- predicted-risk Top20% severe-gap≈**47.2%**
- Bottom20%≈**8.3%**
- Top20 实际 stop 净损失均值≈-30.0%
- Bottom20≈-18.8%

重要风险特征包括：

- `ln(age+1)`
- `fresh_wallet_rate`
- `liquidity/holder_count`
- `holder_count/age`
- `ln(volume_1m+1)`
- `price/ath_price`
- website / Dexscreener / smart-wallet 等状态

这比简单提高 entry liquidity 门槛更有价值。建议未来在主 winner probability 之外增加 execution-risk head，并作为阈值惩罚/拒绝条件，而非直接修改标签。

## 10. Drift-aware 诊断

已确认：

- 当前生产标签 Q1→Q4 base rate 从约 22.45% 降至 13.90%，p≈0.007。
- p18/90 + age<60 同样存在明显时间衰减，age filter 不能消除 regime drift。
- 最近 vs 早期存在 `ln(liquidity_usd)`、`price_change_1h`、`entrapment_ratio` 等显著 covariate shift。

两种简单 drift 修复均不够：

1. **prior odds correction**：在正式 challenger 中经常只减少交易数，并没有稳定找回 winner；个别模型甚至恶化。
2. **简单缩短训练窗口**：固定 final holdout，用最近 300/500/700 vs 全历史训练的轻量诊断，没有稳定优于全历史；过度截断还会显著降低可学性。

所以 drift-aware 不能只做一个概率平移或固定 rolling window。更合理的是：

- recent calibration gate
- 模型级 recent minimum observations
- model-specific trade budget
- covariate / prior drift alarm
- 必要时触发 shadow retrain，而不是自动把历史阈值继续沿用

## 11. 当前结论与优先级

### 不建议立即改生产

当前证据不支持直接把生产标签或 age 准入改掉。原因不是 challenger 没有信号，而是 recent 有效样本仍然只有个位数到十几笔，置信区间太宽。

### 最值得继续积累 OOS 的主 challenger

**1.8x / 0.9x / 90min + age-aware + execution-risk-aware + model-specific sparse budget**。

原因：

- age<60 后仍有 802 条 / 146 正类，标签密度健康。
- 1.8x 比 2.0x 更容易维持正类数量。
- 正式 age<60 Logistic / ExtraTrees 在 recent 头部均有信号。
- 但年轻币 stop gap 更严重，因此必须同时约束 execution risk。

### 激进 challenger

**2.0x / 0.9x / 90min + Logistic extremely-sparse head**。

只适合 Top5–7.5% 级别 shadow 观察，不适合宽交易。

### 当前最重要的系统方向

1. 不放宽 hard SL。
2. 不把 trailing 写进 label。
3. 不把 `age<60` 简化成无条件准入。
4. 建立 execution-gap risk head，并与 winner probability 联合决策。
5. 每个模型分别维护 recent trade-budget / calibration gate，不能给 Top3 一套统一宽阈值。
6. 60–120m 暂作为降权/排除 challenger；不建议单独训练。
7. 累积更多 recent OOS 后，再决定是否将 1.8/90 + age-aware 方案晋升为新生产 label / entry policy。

## 12. 研究资产

- `artifacts/research/kline_cache_v3_2h.json.gz`
- `artifacts/research/label_policy_grid.json`
- `artifacts/research/label_policy_grid.csv`
- `artifacts/research/label_policy_grid_report.md`
- `artifacts/research/shadow_challenger_training_1183.json`
- `artifacts/research/shadow_p18_90_only_60_120.json`
- 本报告 `artifacts/research/shadow_challenger_report_1183.md`
- 脚本：`scripts/research_extend_kline_cache.py`、`scripts/research_shadow_challengers.py`
