# Solana Meme 标签策略只读研究报告（2026-08-24）

## 1. 研究边界

本轮为纯研究分析，不修改生产 `samples` 标签、生产 TP/SL、模型工件或 active slots。

- 固定快照：1177 条 `event1m_regime_v3` mature binary 样本。
- 当前生产标签：TP=1.6x、SL=0.9x、window=60min、same-bar stop-first。
- 重新拉取 GMGN 1m Kline：1177/1177 成功，0 错误；缓存于 `kline_cache_v3_2h.json.gz`。
- 网格：TP={1.6,1.8,2.0}；SL={0.9,0.8,0.75,0.7}；window={60,90,120}；trailing={off,20%,25%,30%}；lookback={5,10,15min}，共 360 套。
- trailing 的 rolling high 只使用当前 1m bar 之前已经完成的历史 bar，避免用同一根 OHLC 的 high→low 未知顺序制造虚假 trailing stop。
- 理想化收益：TP 按 barrier、hard SL 按 barrier、trailing 按 trailing line、timeout 按末根 close；另用真实 rules-only 止损成交成本做 execution-stress。

## 2. 基线复现

生产基线被分钟 K 线回放精确复现：

- positive = 217 / 1177 = 18.4367%。
- 最近 25%（295 条）positive = 41 / 295 = 13.8983%。
- 理想化平均收益 = +3.92%/笔；最近约 +0.67%/笔。
- winner 到 1.6x 的时间中位数约 2.63min；75% 约 7.77min；90% 约 22.3min。
- loser 触发 0.9x SL 的时间中位数约 1.15min。

因此延长时间窗口不是最主要矛盾；winner/loser 大多在很早阶段已分化。

## 3. 最关键结论：不要放宽 hard SL

将 SL 放宽为 0.8/0.75/0.7 会提高标签正类率，但经济表现恶化。

例：TP=1.6x、window=120min：

- SL=0.8：positive 23.96%，理想化均值 +0.68%，最近 -1.95%。
- SL=0.75：positive 25.57%，理想化均值 -1.13%，最近 -4.02%。
- SL=0.7：positive 27.10%，理想化均值 -2.77%，最近 -5.42%。

“正类更多”不等于更可交易。放宽 SL 救回部分先跌后涨样本，但新增亏损与更差的盈亏平衡条件抵消了收益。

## 4. 当前最值得研究的 no-trailing 候选

### A. 保守控制组：1.6x / 0.9x / 90min

- positive = 225 / 1177 = 19.12%。
- recent positive ≈14.92%。
- 理想化平均收益 +4.08%；recent +1.22%。
- 与当前 60min 基线的逐样本配对收益差：+0.165pp；95% CI [-0.103,+0.432]pp，p≈0.227。

结论：略好，但提升不足以证明值得换标签。

### B. 首选 challenger：1.8x / 0.9x / 90min

- positive = 180 / 1177 = 15.29%。
- recent positive = 34 / 295 = 11.53%。
- 保留旧正类约 81.57%，仅 3 个旧负类转正。
- 理想化平均收益 +4.93%；recent +1.59%。
- 与当前基线逐样本配对收益差 +1.011pp；95% CI [+0.140,+1.882]pp，p≈0.023。
- 固定同一批 entry-time 特征、chronological 前75%训练/后25%测试，Logistic：AP=0.1947、ROC-AUC=0.5828、top10% precision=26.67%，相对 recent base rate lift≈2.31x。

结论：在“收益提升、标签密度、可学性、recent 稳定性”之间最均衡，适合作为下一轮只读/影子 challenger。

### C. 激进 challenger：2.0x / 0.9x / 90–120min

90min：
- positive = 147 / 1177 = 12.49%。
- recent positive = 26 / 295 = 8.81%。
- 理想化平均收益 +5.28%；recent +1.31%。
- 配对收益差 +1.355pp；95% CI [+0.114,+2.597]pp，p≈0.033。
- Logistic top10% precision=23.33%，lift≈2.65x。

120min：
- positive = 153 / 1177 = 13.00%。
- recent positive ≈9.15%。
- 理想化平均收益 +5.49%；recent约 +0.96%。
- 配对收益差 +1.570pp；95% CI [+0.302,+2.838]pp，p≈0.015。

结论：全样本理想收益最高，但正类更稀、recent 安全垫更薄、可学性下降；适合作为第二 challenger，不宜直接替代生产标签。

## 5. trailing stop：可作为执行研究，不适合作为当前 label

上一轮 20% trailing 将正类压到约 1%。本轮修正为“仅引用已完成历史 bar 的 rolling high”后，结果仍显示明显误杀。

表现较好的 trailing 方案集中在 SL=0.9、TP=1.8/2.0、drawdown=25–30%，但：

- 大量旧 winner 被改判为负类。
- trailing stop 后仍会在窗口内到达目标 TP 的比例常约 20%–33%。
- 例如 2.0x/0.9x/90m、25% trailing、5min lookback：recent 理想化收益约 +2.30%，但 old-positive retention 仅约 50.2%，trailing false-kill 约 30.2%。

而且该收益假设 trailing line 可以精确成交，与当前 Meme 执行层真实 gap 冲突。因此 trailing 更适合未来作为独立 execution overlay 做 route-aware shadow replay，而不是直接写进分类标签。

## 6. 真实执行成本才是第一矛盾

剔除本次 09:36–14:04 事故污染的 2 笔仓位后，rules-only 历史：

- stop_loss_0_9x：849 笔，平均净收益 -23.30%，5% trimmed mean -21.93%，median -19.17%。
- take_profit_1_6x：184 笔，平均净收益 +68.93%，5% trimmed mean +65.39%，median +60.19%。
- timeout_1h：77 笔，平均 +4.66%，5% trimmed mean +3.44%。

基于 trimmed TP/SL，真实 payoff ratio≈2.98:1，break-even Precision≈25.11%。这与当前 `fixed_3_to_1_v3` 的 25% break-even 极为接近，说明 1:3 经济目标有真实账本支持。

将真实平均 stop 成本 -23.30% 压回所有 no-trailing policy 后：

- 当前基线：全样本约 -6.07%/笔；recent约 -10.05%。
- 1.8x/90m：约 -5.63%；recent约 -9.59%。
- 2.0x/120m：约 -5.46%；recent约 -10.67%。

所以修改标签只能改善几个百分点，无法让“全买”变成正期望。系统必须依赖高 precision selection，同时解决止损执行 gap。

## 7. 交易频率：只交易头部 5–10% 的信号更合理

固定同一 43 个可用 entry-time 特征、Logistic、chronological 75/25，只在 recent 295 条上观察排序：

### 当前标签
- top5%：5/15=33.3%，按 trimmed TP/SL 压力收益约 +7.18%/笔；TP 再减 5pp 后约 +5.51%。
- top10%：9/30=30.0%，约 +4.27%；压力后 +2.77%。
- top15%：10/44=22.7%，转负。

### 1.8x/90m
- top7.5%：6/22=27.3%，约 +5.87%；TP 减5pp后 +4.51%。
- top10%：8/30=26.7%，约 +5.25%；压力后 +3.92%。
- top15%：11/44=25.0%，约 +3.56%；压力后 +2.31%。
- top20%：13/59=22.0%，压力后已略负。

### 2.0x/90m
- top5%：4/15=26.7%，约 +10.59%；TP 减5pp后 +9.25%。
- top10%：7/30=23.3%，约 +6.52%；压力后 +5.36%。
- top15%：8/44=18.2%，压力后转负。

这些只是 read-only holdout 结果，不是生产阈值。样本量仍小：例如 1.8 top10 的 Wilson 95% Precision CI 约 14.2%–44.4%；2.0 top10 约 11.8%–40.9%。目前证据支持“更稀疏”，但不足以直接自动改阈值。

## 8. regime drift 已经显著存在

按时间四等分，当前标签正类率：

- Q1 22.45%
- Q2 20.41%
- Q3 17.01%
- Q4 13.90%

Q1→Q4 下降约 8.55pp；两比例检验 p≈0.0071。

最近25% vs 前75%的特征 KS 漂移：

- `ln(liquidity_usd)`：KS≈0.197，p≈5.1e-8。
- `price_change_1h`：KS≈0.128，p≈0.00123。
- `entrapment_ratio`：KS≈0.119，p≈0.00351。

其中 `price_change_1h` 是现役多个模型的重要输入。当前同时存在 label-prior drift 与 covariate drift，长期固定阈值存在明显风险。

## 9. age 分层

当前标签全样本正类率：

- <10m：24.30%
- 10–30m：22.49%
- 30–60m：16.38%
- 60–120m：5.70%
- 120–300m：12.79%

最近25%：

- <10m：18.37%
- 10–30m：16.22%
- 30–60m：15.79%
- 60–120m：8.89%
- 120–300m：6.78%

用真实 stop 成本压力测试后，60–120m 在所有主候选下仍是最差分层，平均约 -14%~-15%/笔。此前该档 rules-only 账本曾被一笔事故污染的 +3432% 交易抬成正收益，剔除事故后不再成立。

因此 age 更适合被当成独立风险分层/准入研究对象，而不仅是普通连续特征。

## 10. 当前研究结论

1. **不建议放宽 hard SL 到 0.8/0.75/0.7。** 正类率会升，但经济质量下降，recent 更差。
2. **不建议将 20% trailing 写入生产 label。** 修正 OHLC 顺序后仍存在高 false-kill；若研究 trailing，应独立做执行层 shadow。
3. **下一轮首选 challenger：TP=1.8x、SL=0.9x、window=90min、无 trailing。**
4. **次选激进 challenger：TP=2.0x、SL=0.9x、window=90min。** 只适合非常稀疏的 top-score 信号研究。
5. **生产标签暂不修改。** 当前最大的可交易改进空间是：减少交易频率、提升 recent precision、修复/降低 stop execution gap、引入 drift-aware 校准/阈值。
6. **建议后续只读实验：** 对 1.8x/90m 与 2.0x/90m 用现有完整训练栈做 chronological OOS shadow training，并强制最近窗口单独验收；同时对 age 60–120m 做 exclusion/segment ablation，不直接改生产准入。
