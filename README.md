# Solana Meme Quant Trading System

一个面向 Solana 新发行 Meme Token 的采集、建模、模拟交易与受控实盘系统。系统以 1 小时策略窗口为核心，将“规则初筛 → 入场时特征 → 模型二筛 → 模拟/实盘执行 → 标签补齐 → 周期训练”串成可审计闭环。

> 当前版本默认 `DRY_RUN=true`。测试不会访问真实交易接口或广播链上交易。关闭 DRY RUN 前，必须完成本文“实盘启用门禁”和《开发文档.md》的 GMGN 合约调通清单。

## 1. 业务目标与边界

系统不试图预测所有上涨 Token，而是在通过安全规则的候选池中，寻找未来 1 小时内更可能出现明显上涨路径的样本。

```text
GMGN Trenches 发现
  → 规则初筛与特征补齐
  → 不可变样本入库
  → Top 3 模型独立概率评分 + 不用模型基线
  → 四策略独立模拟 / 受控实盘执行
  → T+1h 标签与收益事实补齐
  → 周训练、手动训练、Top 3 重新排名
```

第一版是 Windows/单机/单用户系统，使用 SQLite；不承诺高频交易、多用户 SaaS、跨链、深度学习或 PostgreSQL 集群。旧 GitHub 项目不是业务规范，本仓库仅继承“交易生命周期可审计”的思想，采集、标签、模型与风控均按本文重建。

## 2. 冻结业务规则

### 2.1 发现范围

只采集以下 8 个 Solana Launchpad：

- `Pump.fun`
- `Moonshot`
- `moonshot_app`
- `letsbonk`
- `jup_studio`




- `bags`
- `believe`
- `heaven`

生命周期仅包括 `new_creation`、`near_completion`。`completed` 自 2026-08-12 起永久退出采样、训练与交易，并由 SQLite trigger 阻止重新写入。每个完整采集周期按 **New Creation → Near Completion** 顺序分别请求 Trenches；当前单类请求按 GMGN 契约最多取 80 条，并对偶发的超量响应再次本地截断。采集器继续使用 12 个 GMGN API 槽位做 discovery / realtime enrichment / fallback / K 线角色分工；多个 Key 不代表 IP 总吞吐可以相乘，所有槽位仍受共享限流门控。

当前历史 CSV 全部来自 Pump.fun，这只说明已有样本覆盖不足，不能据此改动平台列表或宣称模型可泛化到其他平台。

### 2.2 规则初筛

Trenches 自 2026-08-14 起只负责候选发现与生命周期/范围约束，不再把业务阈值作为 GMGN 服务端联合预筛：

```text
filters = ["offchain", "onchain"]
launchpad_platform_v2 = true
launchpad_platform = [Pump.fun, Moonshot, moonshot_app, letsbonk, jup_studio, bags, believe, heaven]
quote_address_type = [4, 5, 3, 1, 13, 0]
min_created = "2m"
max_created = "300m"
limit <= 80
```

`rug/insider/bundler/liquidity/Top10/fresh/holder/marketcap` 等业务阈值统一由本地执行。raw Trenches 行先经过 fail-open 粗筛：只有字段存在、可解析且已经明确违反冻结阈值时才提前拒绝；字段缺失或异常不会在粗筛阶段被误杀，而是进入 `token_info/security/pool` enrichment 后由 fail-closed `SafetyFilter` 做最终权威判定。`renounced_mint / renounced_freeze_account` 也只在 enrichment 后校验，不再作为 Trenches 服务端参数发送。

GMGN 返回 pool 后按资产语义校验交易对：quote 侧只允许 `SOL/USDC/USDT`；目标 Token 排除 `SOL/USDT/USDC/PYUSD/WBTC/WETH`。

拉回后，本地还必须满足：

- `launchpad` 在允许列表内；
- quote 资产属于 `SOL/USDC/USDT`，且目标 Token 不属于 `SOL/USDT/USDC/PYUSD/WBTC/WETH`；
- `rug_ratio < 0.2`、`insider_ratio < 0.2`、`bundler_rate < 0.2`；
- `liquidity > 4800`；
- `0.14 < top_10_holder_rate < 0.25`；
- `fresh_wallet_rate < 0.2`；
- `burn_status == "burn"`；
- `renounced_mint == 1` 且 `renounced_freeze_account == 1`；
- 非洗盘，`rat_trader_amount_rate < 0.2`；
- `29 < holder_count < 1000`，`marketcap > 5000`；
- `sell_tax < 0.025`、`buy_tax < 0.025`；
- `sniper_count < 10`、`2 < age < 300` 分钟；
- `liquidity / holder_count > 50`；
- `swaps_1h > 19`、`volume_1h / swaps_1h > 31`；
- `(0.5 + smart_degen_count + renowned_count) * volume > 5000`；
- top holders 中 `addr_type=0` 的第一名占比严格满足 `0.028 < top1 < 0.056`。

新池安全数据按 fail-closed 处理。`EnrichmentService` 在正式阈值判断前最多执行 3 轮语义完整性检查；若 token/security/pool 返回成功但准入所需字段仍缺失、不可解析、NaN/Inf、负异常或布尔状态未知，则等待后重新拉取。三轮后仍不完整时以 `missing_or_invalid:<field>` 拒绝，不创建样本，因此不会进入模型评分或买入链。`is_wash_trading="null"/"none"` 不再等价于 false；top holders 缺失或 `addr_type` 异常也不会默认按 `addr_type=0` 放行。HTTP/429/网络错误仍同时服从 provider 自身的有限重试与 fallback。

通过初筛的每次观察按 `(chain, address, observed_at)` 视为独立样本。同一 Token 在 1 小时窗口结束后再次出现可以形成新样本；不能只按地址去重。实盘已有未平仓同 Token 时跳过新买入，模拟盘允许多个独立批次。

需要采集/导出的核心列如下：address	name	symbol	type	time	ln(age+1)	launchpad	price	ln(price+1)	ln(liquidity_usd)	price_1h_max/price	price_1h_min/price	final_1h_close_ratio	liquidity/holder_count	volume_1h/swaps_1h	has_twitter	has_website	ln(image_dup+1)	dexscr_update_link	cto_flag	ln(twitter_rename_count+1)	ln(twitter_del_post_token_count+1)	ln(twitter_create_token_count+1)	top_10_holder_rate	top_bot_degen_percentage	fresh_wallet_rate	bot_degen_rate	price/ath_price	stat.holder_count/market_cap	ln(smart_degen_count+1)	ln(renowned_count+1)	entrapment_ratio	dev_team_hold_rate	top70_sniper_hold_rate	ln(twitter_dup+1)	ln(website_dup+1)	ln(visiting_count+1)	price_change_1h	price_change_5m	ln(creator_open_count+1)	creator_open_ratio	ln(top_wallets+1)	tag。数据库仍保留旧 `price_2h_*` 审计列，仅用于历史 provenance，不再由新标签链路写入或参与训练/交易。

自 `event1m_regime_v3` 起，可选 feature catalog 为 **44 个**入场时数值/布尔特征，生产默认训练候选继续冻结为原 **31 个**。新增的 1m / marketing / social 字段先作为 Shadow catalog 积累同代样本，只有完成 fold-train-only chronological OOS、one-SE + 8% 奥卡姆选择和 final holdout certification 后，才允许显式晋级生产训练池；`ln(liquidity_usd)` 仍为可选 Shadow 特征。训练候选池按 feature generation 单独持久化，手动训练、每日/周训、启动补跑和退化重训都从该代已批准候选池重新开始，不能继承上一代冠军已经压缩后的子集造成代际单向收缩。每个算法在批准池内从最小可行维度逐步扫描；特征排序严格只读取 chronological development fold 的训练段，最终 holdout 不参与特征、阈值或模型选择。`launchpad` 仅作为样本来源元数据保存，不进入模型输入矩阵；`tag` 仅作为二分类 target。

`event1m_regime_v3` 的新增 Shadow 候选只补当前 31 特征未覆盖的信息：`price_change_1m`、`ln(volume_1m+1)`、1m 买卖笔数 imbalance、1m 买卖金额 imbalance、`ln(volume_1m/swaps_1m+1)`、`holder_count/age`、`ln(marketcap+1)`，以及 GMGN 已集成的 DexScreener promotion、X follower、TG call。`ln(swaps_1m+1)`、`ln(volume_2m+1)`、`volume_acceleration_2m`、`creator_token_status` 已从候选特征池删除；`price_change_2m` 被 `price_change_1m` 替代。1m price-change 只允许使用 `feature_snapshot_at` 之前已完成的历史 1m bar 或入场时已知的 1m 价格事实，T+1h 标签阶段禁止回填入场时缺失特征。历史 `legacy_pre_event_v1`、`event2m_regime_v1` 与 `event2m_regime_v2` 均保留审计但不进入 v3 训练、健康统计或 Adaptive 证据；切换 v3 时 v2 仅有 4 条 pending、无关联仓位，因此保留其后续补标而不做破坏性删除，v3 从 0 重新积累。

`tag` 为分类标签列，只作为 target，不进入模型输入矩阵。`age_minutes` 与 `samples.entry_price` 继续保存/使用 raw 入场事实：前者用于严格准入边界，后者用于标签、收益、仓位和交易核算；模型输入不再直接使用旧 `age=ln(age_minutes)` 或 raw `price`，而统一使用 `ln(age+1)` 与 `ln(price+1)`。`ln(liquidity_usd)` 从新样本开始持续采集并作为可选训练特征，但由于 legacy CSV 不含 raw entry liquidity，当前默认训练集先不启用它；后续新样本积累充分后可在模型中心勾选该特征重新训练。数据库仍单独保存 raw entry liquidity，供单笔资金公式和真实美元收益评价使用。`launchpad` 仅用于准入、展示、审计与导出。

### 2.3 1 小时标签

新样本使用 1 分钟 K 线，标签窗口为入场时刻 `T` 到 `T+1h`，版本固定为 `sl090_tp160_h1_binary_v4`。`T-1h` 到 `T` 的 K 线仅用于入场前 `price_change_1h`、`price_change_5m`，不能进入未来价格窗口。

| tag | 首次触及/到期规则 | 分类 | 评价收益率 |
| --- | --- | --- | --- |
| `0` | 先触及 `0.9x` 止损；或 1 小时内始终未触及 `1.6x` 止盈 | 负类 | `-10%` |
| `1` | 先触及 `1.6x` 止盈 | 正类 | `+60%` |

同一根 1 分钟 K 线同时触发止损和止盈时，保守按止损优先，记 `tag=0`。1 小时到期时无论最终收盘高于还是低于入场价，只要此前没有先触及 `1.6x`，均为 `tag=0`。模型目标固定为 `positive = tag == 1`；训练 chronological embargo、模型晋级 label gap 与模拟盘最大持仓时间也统一为 1 小时。

#### 2026-08-12 H1 / no-completed 迁移

迁移前先创建 SQLite 物理备份 `data/backups/meme_quant_pre_h1_no_completed_20260812T092259Z.db`。真实迁移结果：

- 删除 `completed` 样本 `123` 条，迁移后 `completed=0`；为保留历史交易审计，仅解绑 3 条相关历史 prediction/position 引用，不删除历史成交事实；
- 非 completed 的旧 H2 负类 `1876` 条可由窗口单调性安全推断为 H1 负类；
- 非 completed 的旧 H2 正类 `416` 条全部通过 GMGN 1m K 线重新抓取并按 H1 first-touch 重算，`0` API 错误；其中 `381` 条仍为正类、`35` 条转为负类；
- 迁移后共有 `2296` 条样本，其中 `2292` 条成熟 H1 v4、`4` 条正常 pending，成熟正类率约 `16.62%`；
- schema 升级到 v11，新标签写入 `price_1h_max_ratio`、`price_1h_min_ratio`、`final_1h_close_ratio`；旧 `price_2h_*` 列仅保留历史审计；
- SQLite `reject_completed_sample_insert/update` trigger 阻止未来任何脚本重新写入 completed 生命周期；
- legacy CSV importer 会直接跳过 completed。H2 负类可按单调性归为 H1 负类；H2 正类必须重新获取 H1 K 线，在完成前保持 pending，禁止用旧 H2 正类冒充 H1 标签。

#### 2026-08-13 age<240 样本池迁移

该次历史迁移当时把业务准入池收窄为严格 `1 < age_minutes < 240`；**自 2026-08-16 最新口径起当前生产准入已进一步升级为严格 `2 < age_minutes < 300`，Trenches 同步使用 `min_created=2m / max_created=300m`**。历史 legacy CSV/export 的旧 `age` 列为 `ln(age_minutes)`，`CsvImporter` 继续兼容该旧格式并先还原 raw age 做当前等价边界判断，导入后统一规范为模型特征 `ln(age+1)`；当前新采集/导出也使用 `ln(age+1)`。迁移前备份 `data/backups/meme_quant_pre_age240_20260813T0318Z.db`。本次从 2353 条样本中删除 497 条不合规样本（449 mature negative、46 mature positive、2 pending），并同步删除其 201 条 prediction、45 个 simulation position 和 88 条 trade；迁移后剩余 **1856 条 mature H1 v4 样本，344 正 / 1512 负，正类率 18.53%**，age 违规数为 0。

#### 2026-08-13 Top10 / 成交均额样本池迁移

筛选口径更新为 `0.125 <= top_10_holder_rate <= 0.28` 与 `volume_1h / swaps_1h > 31`。本地 discovery prefilter、authoritative `SafetyFilter`、legacy `量化训练采集.py` 已统一；Trenches 自 2026-08-14 起不再发送这些业务阈值，避免服务端候选搜索深度影响本地样本定义。数据库/CSV 的 `volume_1h/swaps_1h` 特征是原始成交均额的自然对数，因此 `CsvImporter` 对有该列的旧导出执行等价的 `feature > ln(31)`，防止已淘汰样本重新回流。

迁移前备份 `data/backups/meme_quant_pre_filter_top028_vps31_20260813T050755Z.db`。从 1858 条样本中删除 182 条不合格样本，其中 165 条 mature negative、17 条 mature positive；Top10 不合格 42 条（4 条 `<0.125`、38 条 `>0.28`），成交均额 `<=31` 的 144 条，二者有 4 条重叠。同步删除 36 条关联 prediction；4 个关联 simulation position 均已关闭，因此只解绑其 sample/prediction 引用，保留 8 条历史 trade 与账户流水。迁移后剩余 **1676 条样本：1674 mature（327 正 / 1347 负）+ 2 pending**，新筛选口径违规数为 0。

#### 2026-08-22 Top10 严格区间迁移

当前生产准入进一步收紧为严格开区间 `0.14 < top_10_holder_rate < 0.25`，因此 `0.14` 与 `0.25` 两个边界值本身也拒绝。`top_10_holder_rate` 的准入判断与模型特征统一读取同一个 `normalize_token()` 权威值；local discovery prefilter、authoritative `SafetyFilter`、`CsvImporter` 与 legacy `量化训练采集.py` 使用完全一致的严格边界，Trenches 仍只负责 transport/lifecycle 范围，不下发该业务阈值。

迁移前使用 SQLite backup API 创建 `data/backups/meme_quant_pre_top10_014_025_20260822T145311Z.db`。迁移快照从 **1009** 条样本中删除 **210** 条不合规样本（200 mature：28 正 / 172 负；10 pending），并将其关联的 **158** 个 `rules_only` simulation position 解除 `sample_id` 引用以保留真实账本与成交事实，其中当时 **2** 个仍未平仓的模拟仓继续按原 PositionMonitor 退出链自然结算，不做规则变更驱动的强平；关联 prediction 为 0。迁移后剩余 **799 条样本：789 mature（157 正 / 632 负）+ 10 pending**，raw 权威值与 `features_json` 新规则违规数均为 0，`PRAGMA foreign_key_check` 为 0。

## 3. 模型与收益评价

### 3.1 Top 3 模型 + 不用模型基线

候选模型池当前包括：Logistic Regression、Decision Tree、HistGradientBoosting、Gradient Boosting、AdaBoost、ExtraTrees、RandomForest、RBF-SVM、XGBoost、LightGBM、CatBoost、TabPFN 与 FLAML AutoML。CatBoost 从 2026-08-14 起作为正式依赖固定为 `catboost>=1.2.10,<2`；FLAML 固定为 `flaml>=2.6,<3`，并通过项目自己的 `FLAMLTimeSafeClassifier` 进入 `automl` family：每次只接收外层 chronological fold 的 train slice，在内部使用 `split_type=time + eval_method=holdout + 20% 最新时间段验证 + allow_label_overlap=False + auto_augment=False` 搜索 `lgbm/rf/extra_tree/catboost`，随后只在该外层 train slice 上 `retrain_full`。FLAML 使用 4/8/16/24/全量稀疏特征网格和每次 fit 5 秒预算，避免 AutoML 把日常训练时长无限放大；外层 validation 与最终 holdout 永远不会交给 AutoML。项目不使用 `flaml[automl]` extra，因为 FLAML 2.6.0 的该 extra 会要求 `xgboost<3`，与本项目正式 `xgboost>=3,<4` 候选冲突；所需 learner 依赖由项目自行维护，FLAML 内部也显式排除 xgboost。TabPFN 使用 `tabpfn>=8.3,<9` 本地推理并作为独立 `foundation` model family 参与 Top 3 多样性选择：优先使用显式锁定的 TabPFN V3；若 V3 checkpoint 尚未完成官方一次性授权/缓存且未配置 `TABPFN_TOKEN`，后台训练不会弹浏览器等待登录，而会自动回退到可用的 V2 checkpoint，并把实际版本、CPU/GPU 和 estimator 数写入模型工件指标。当前机器为 CPU-only，因此 TabPFN 使用 1 estimator，并在 4/8/16/24/全量五个维度上做稀疏奥卡姆搜索；所有候选最终统一遵守 one-SE + 相对最佳 S 损失不超过 8% 的门槛。







一次训练按开发期 chronological OOS 结果选出 **Top 3**。三个模型分别保存自己的特征子集、单一决策线、经济得分、泛化得分和综合分，并映射到 `model_1 / model_2 / model_3`。此外 `rules_only` 作为“不用模型”基线：所有通过规则初筛的新样本都进入同样的模拟执行链，只跳过模型二筛。

模型拟合仍是标准二分类问题，交易目标不直接写成训练 loss。自 **2026-08-24** 起，当前离线经济评价使用摩擦调整固定收益单位 `U = 3×TP - FP`；若以 Precision `p`、Recall `r` 和同一评估池真实正类数 `N+` 表示，则等价于 `U = N+ × r × (4 - 1/p)`。固定 $50 只用于模型间公平离线评价，对应理论美元收益 `$5 × U`，即正类代理 +$15、负类 -$5；实际交易本金仍执行 `min(1% × entry liquidity, $50)`。开发期经济得分 `E = mean(clip((3×TP-FP)/(3×N+), -1, 1))`；泛化得分 `G` 综合 AP Skill、跨时间窗口稳定性和近期衰减；综合分固定为 `S = 0.60×E + 0.40×G`。Top 3 只按开发期 OOS 的 `S` 排名，最终时间留出集只做 certification。每个算法内部不再固定 12 / 20 / 全量档位，而是在可行特征数上自适应搜索；每个 chronological development fold 只能用该 fold 的训练段进行特征排序，测试段不得参与特征选择。最终仍采用 one-standard-error 奥卡姆规则：性能没有显著低于最佳方案时选择更小维度；最终 holdout 始终只做 certification。

### 3.2 时间切分

禁止 `random train_test_split`、随机打乱和使用入场后的字段。

- 数据跨度 `>=120` 天：仅使用最近 120 天；`-120~-30` 天用于扩展窗口开发，最近 30 天作为最终时间外比较窗口；胜出 recipe 再在最近 120 天全部成熟样本上 refit。
- 数据跨度 `<120` 天：时间排序的扩展窗口验证，最近 20% 为最终留出；模型标记 `EARLY_STAGE_MODEL`。
- 所有训练/验证边界保留至少 1 小时标签隔离带。

硬性排除标识、展示和未来字段，包括 `address`、`name`、`symbol`、`type`、`time`、`launchpad`、`price_2h_max/price`、`price_2h_min/price`、最终收盘、退出字段、tag 和交易结果。raw `age_minutes` / `price` 是准入时业务事实，模型默认使用 `ln(age+1)` / `ln(price+1)`；`ln(liquidity_usd)` 当前持续采集并作为可选特征，默认训练暂不启用。raw liquidity 只用于资金和收益评价，不直接作为模型输入。训练与评分必须复用同一个序列化 Pipeline 和固定列顺序。

### 3.3 资金与效用

每笔名义本金按入场时流动性快照计算：

```text
capital_i = min(0.01 × liquidity_usd_at_entry_i, 50 USD)
pnl_i = capital_i × realized_return_i
cumulative_pnl = Σ pnl_i
```

实际模拟/实盘执行继续使用上述逐笔本金和真实交易摩擦；离线 Top 3 排名仍使用统一固定 $50。自 **2026-08-24** 起，当前模型经济代理进一步收紧为 `+3/-1`：正类按 +$15、负类按 -$5 计入排序，盈亏平衡 Precision 为 25%，以更贴近当前累计交易样本下的可执行收益结构；该调整只影响模型选择、决策线搜索、ModelHealth 与 Adaptive/OPE 的经济效用，不改变 H1 `1.6x/0.9x` 标签与真实交易退出规则。系统另从 `rules_only` 的 route-validated 平仓中采集 `E_exec` shadow 指标，用于观察手续费、滑点和 no-route 对真实可执行收益的影响；`E_exec` 当前权重固定为 0，不参与模型排名。模型概率与决策线都来自开发期 OOS；最终 holdout 只生成审计成绩单，不再参与阈值或排名。

Top 3 更新规则：

1. 所有候选在相同 chronological OOS 开发折上比较；
2. 每个算法只保留 one-standard-error 内更小的特征子集；
3. 按 `S = 0.60E + 0.40G` 排名前三并各自固定一个决策线；
4. 最近最终 holdout 只做 certification，禁止反向调模型或阈值；
5. 三个 Top 模型工件必须可加载，Rank 1 历史版本保留可回滚链。

自动训练与模型切换从 schema v10 起拆成两个阶段，当前数据库 schema 为 v12；**模型生命周期现在先受当前特征代的成熟样本门槛约束**。`event1m_regime_v3` 中 `label_status='mature' AND tag IN (0,1)` 的样本少于 **1000** 条时，系统进入 `data_collection_only`：每日/每周/启动补训/健康降级补训均不排队，16:00 rollover freeze 也不生效；即使数据库意外残留 active model，Prediction 也不做模型打分和 `model_1/2/3` 模拟，只继续 `rules_only` 无模型模拟，以便积累可执行性事实。成熟样本达到 1000 条后，自动模型生命周期才重新取得资格：正常状态按每周约定日（当前为周日）北京时间 **17:00** 训练，模型健康为 `insufficient_data` 时临时加速为每天 17:00；后续继续遵守既有 16:00 freeze、17:00 durable training、全平后原子切换规则。手动训练仍属于显式运维动作，不由该自动门槛主动触发。

## 4. 交易与风控

### 4.1 四策略模拟账本与实盘边界

- 模型生命周期就绪时，`model_1 / model_2 / model_3 / rules_only` 四个模拟策略各自拥有 `1000 USD` 单一 USD 独立账本；模拟盘不维护 SOL 余额，同 Token 可在不同策略、不同批次同时存在。当前代成熟样本 `<1000` 时只有 `rules_only` 账本允许产生新模拟仓位，`model_1/2/3` 不打分、不交易。达到门槛后，重大模型换代/rollback 仍在四策略全平后创建新 simulation session，四账本统一重置。
- 四策略统一复用同一 BUY/SELL、滑点、费用、0.9x 止损、1.6x 止盈、1h 到期和卖出失败重试链；差别只在是否经过某个模型决策线。每次实际发生的网络费仍以 SOL 原始数量记录，并使用手续费发生时的 SOL/USD 折算成 USD 直接进入现金与 PnL。
- 已存在的 simulation/live 持仓统一由独立 `PositionMonitorWorker` 以默认 2s（可配置）current-price cadence 监控。同 Token 多仓仍合并为一次 GMGN current-market 请求；GMGN Key 数量不再固定为 12 个，所有已配置 Key 进入轮换/fallback 池，但 Collector 与 PositionMonitor 共享同一个默认 10 req/s IP 总预算，避免多 Key 误放大公网请求。simulation 命中退出条件后立即冻结该 Token 实际行情响应时的触发价/触发时刻，并在该响应到达时立刻启动受控并发 executable quote；不同 Token 不再等待整批 GMGN 行情中最慢请求返回，避免 head-of-line blocking。实盘自动 BUY 仍刻意停放；live 退出只有在 `DRY_RUN=false` 且 runtime gate 已武装时才进入既有幂等执行链，否则 fail-closed。

理论标签收益、模拟可执行收益和真实链上收益必须分开展示，不能混成一个 PnL。

模拟 BUY 包含可复现的滑点、价格影响、1% 平台费、网络费、延迟和可注入失败；SELL 由本轮 current market price 触发，并在 token decimals 可审计时调用 Jupiter Token→USDC `GET /swap/v2/order` 做 quote-only executable 路由验证（不传 `taker`，响应不包含待签交易），绝不签名或调用 `/execute`。Jupiter quoted route 的 `priceImpact` 单独作为滑点/价格冲击审计；GMGN 当前价到 Jupiter fill 的差异另记 `exit_execution_deviation_bps`，不得再次归类为 slippage。明确无路由进入 `no_route` 重试/终态全损；429、网络或 API 故障只记为 quote unavailable，并降级到“当前流动性 + 本地冲击模型”，不得误判为 rug。网络费的会计单位为 USD，但原始 `network_fee_sol`、手续费发生时 `sol_usd_price` 与折算后的 `network_fee_usd` 同时保留。模型标签效用仍按用户冻结的“忽略交易费”口径计算，二者用途不同。2026-08-13 起交易历史只保留 `h1_route_aware_v1` simulation 成交事实，旧 legacy simulation position/trade 已清除。

### 4.2 持仓和退出

所有账户共同执行：

- 单笔投入：`min(1% × entry liquidity, 50 USD)`；
- 最大同时持仓：每个账户 10 个；
- 止损：`0.9x` 全仓退出；
- 止盈：`1.6x` 全仓退出；
- 1 小时未触发：按到期市价全仓退出；
- 一键清仓先暂停新买入，已有仓位的退出不能被“暂停开仓”反向阻断。

固定模拟账户只使用自己的 `1000 USD` 单一 USD 独立账本；同 Token 可有多个独立批次。模拟盘不维护任何 SOL reserve，也不会因为“模拟 SOL 不足”阻塞交易；网络费只按实际发生时的 SOL/USD 折算为 USD 成本。如果该时点缺少足够新鲜、可审计的 SOL/USD 事实，模拟执行明确返回 `sol_usd_price_unavailable`，不会拿当前价、日终价或事后价格补算。以下规则属于**实盘钱包专属风控**，与模拟收益统计彻底分离：

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
│  ├─ scheduler\           # 16:00 换模冻结、17:00 日/周训练与启动补训
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

Windows 桌面日常使用可直接双击根目录 `一键启动系统.bat`。它会把后端 supervisor/Uvicorn 与 Vite 前端都以无控制台窗口的后台进程启动，服务就绪后自动打开 `http://127.0.0.1:5173/` 并退出启动器，因此成功启动后任务栏只保留浏览器，不留下 `cmd.exe` / `python.exe` 黑窗口；异常时启动器才停留并提示检查 `logs/backend.err.log` 或 `logs/frontend.err.log`。

本地开发也可以单独使用项目启动器开启后端热更新：

```powershell
Set-Location D:\meme
.\.venv\Scripts\python.exe scripts\start_runtime.py --reload
```

`--reload` 启动项目自己的 Windows 开发 supervisor，仅监控 `backend/**/*.py`。检测到源码变化后会终止旧 Uvicorn 子进程树并启动新的单 worker，PID 仍写入 `logs/backend.pid`；2026-08-11 已实测子进程可从旧 PID 自动切换到新 PID，随后 Collector/TrainingWorker 恢复运行。非开发常驻运行去掉 `--reload`。

启动时会：

1. 创建/迁移 `data/meme_quant.db`（当前 schema v12）；v8 完成 `profile`/三档账户迁移，v9 增加可审计的 SOL/USD 价格表及每笔交易的 `platform_fee_usd / sol_usd_price / network_fee_usd / slippage_cost_usd / fee_occurred_at`，v10 将 `daily` 纳入 durable training trigger 并支持候选 Top 3 跨重启等待空仓，v11 增加 H1 审计字段并用数据库 trigger 禁止 `completed` 生命周期重新写入，v12 增加 feature generation / `feature_snapshot_at`、Market Regime、adaptive decision/feedback 与 neutral counterfactual 审计字段；旧交易没有当时 FX 事实时不会用今天价格回填；
2. **不再自动导入根目录 `meme数据.csv`**。legacy CSV importer 仅保留为显式维护 API/脚本能力，启动链禁止把 pre-v3 样本重新灌回 current-generation-only 运行库；
3. 在任何新信号 worker 启动前运行一次订单 journal 对账：有 `provider_order_id` 只查询原单，无法确认的 submit 保持 `submission_unknown` 并暂停新开仓；
4. 启动唯一 `TrainingWorker`；任何非 manual 训练任务在真正执行前都先复核当前代 1000 mature+tag 门槛，未达标即 `skipped`，因此历史 queued/running 状态不能在重启后绕过门槛；达到门槛后才恢复既有 durable 训练与四策略全平换代链；
5. 启动模型调度器与 Prediction worker：当前代 `<1000` 时 scheduler=`data_collection_only`、Top3 prediction/model simulation 全停，仅运行 `rules_only`；达到门槛后才恢复健康感知的日/周 16:00-17:00 调度与三模型 prediction。liquidation/reconciliation 仍按既有持久化安全边界运行；
6. 独立启动 `PositionMonitorWorker`：默认每 2 秒（配置页可改）获取 simulation/live 持仓的 current price/liquidity，同 Token 合并请求；GMGN 使用动态 Key 池并与 Collector 共享总 RPS 预算。simulation 命中退出条件时先冻结同一 current-price 触发事实，再按 Jupiter Key 数进行受控并发 SELL quote，避免四策略串行报价把同一触发事件人为拉开数秒；live 仅在既有安全门禁已武装时执行退出。运行态同时记录目标周期、单次 cycle 耗时和真实 start-to-start 周期。`COLLECTOR_ENABLED=true` 时 Collector 只负责 SOL/USD 费用事实刷新、discovery、enrichment 与 T+1h 标签补齐，不再承载持仓退出。
7. Windows 上默认启用 `PREVENT_SLEEP_WHILE_COLLECTING=true`：Collector 运行且机器接交流电时，后端持有 Win32 `ES_SYSTEM_REQUIRED`，允许显示器正常熄灭但阻止 idle-triggered Modern Standby 暂停桌面进程；拔掉交流电、关闭 Collector 或后端退出时自动释放。运行态写入 `system_awake_request`，用于区分“采集器停机”和“系统进入 S0 待机”。

健康检查：`http://127.0.0.1:8000/health`；OpenAPI：`http://127.0.0.1:8000/docs`。

### 6.3 启动前端

```powershell
Set-Location D:\meme\frontend
npm run dev
```

访问 `http://127.0.0.1:5173`。Vite 会把 `/api` 和 `/health` 代理到 `127.0.0.1:8000`，开发模式自带前端 HMR。

左侧导航固定为：`总览 → 持仓 → 样本采集 → 运行监控 → 模型中心 → Agent审批`。`/signals` 路由保留，页面标题为“样本预览”，可按 `model_1 / model_2 / model_3` 过滤；模型列使用 `YYYYMMDD-算法全称`（如 `20260812-Decision Tree`），不再使用 DT/RF/GB 等缩写。“模型分 / 入选线”表示该模型对样本的概率与自己的单一决策线。`rules_only` 不产生模型预测记录；右上角可一键导出数据库全部 mature/tagged 样本为 `meme数据.csv`。

“持仓”页采用两层视图：先切换 `模拟仓 / 实盘`，模拟仓再选择 `model_1 / model_2 / model_3 / rules_only`。模型策略卡按两列排版、每卡 2×4 展示 **8 个上线期指标**：当前余额、持仓本金、已实现收益、累计手续费、持仓数、交易数、盈利数、亏损数。`当前余额` 明确定义为**可用 USD 现金**，已开仓本金在 BUY 时已经从现金扣除，绝不能再次包含在余额里；`已实现收益` 为已经平仓交易的 `net_pnl_usd`，买卖两侧平台费和按费时 SOL/USD 折算的网络费均已扣除，滑点通过实际 fill price 进入 gross/net PnL，不再重复扣一次。`交易数` 统计当前 session/generation 范围内已经终结的完整仓位，正常退出和终态卖出失败都计 1 次；`盈利数` 是其中 `net_pnl_usd >= 20% × invested_usd` 的交易数，`亏损数` 是其中 `net_pnl_usd < 5% × invested_usd` 的交易数，所以 0%～不足 5% 的小幅盈利也归入亏损数，5%～不足 20% 的交易两边都不计，20% 恰好计入盈利。训练、总览和模型中心仍保留原 Precision/Recall 定义，不受持仓卡片展示变更影响。每次自动换模或人工回滚成功前必须等待 `model_1/2/3/rules_only` 四策略全部空仓；切换时创建新的 simulation session，四个策略都重新从 `1000 USD`、0 持仓、0 交易、0 PnL/手续费开始。当前策略卡下方仍显示平台费、网络费 USD（同时保留原始 SOL 数量）和滑点损耗明细。 `交易历史 / 当前持仓 / 交易审计` 统一受当前 simulation session 边界约束；`model_1/2/3` 额外按当前 `model_id + active_model_slots.selected_at` 隔离。数据库已删除所有非 `h1_route_aware_v1` 的 legacy simulation 成交，因此页面、卡片和审计不会再混入旧执行模型。

总览页展示当前 Top 3 模型的排名、算法全称、特征数、决策线、Precision/Recall、理论收益、E/G/S，并单独显示四策略模拟实际 PnL 对比；模型显示名如 `20260812-Decision Tree`，内部长 model id 与 EARLY 状态仍保留。“实盘收益”只累计 `account_kind='live'` 且已经 `closed` 的已实现 PnL，不混入模拟盘。

卖出失败口径已经显式冻结：模拟仓到 1 小时后确认 `no_route`，或一般卖出失败累计达到 7 次重试，转为 `closed` 的失败平仓；模拟盘不存在 SOL 储备耗尽这一失败类型。实盘强制退出明确返回 `NO_ROUTE`、最终 `FAILED` 或 `EXPIRED` 时也转为失败平仓。失败平仓按 `-投入本金 - 已实际支付的平台费 - 已实际发生并按费时汇率折算的网络费` 计入已实现 PnL；滑点已通过实际 fill price 进入成交结果，并保留 `sell_failure_reason` 审计。`submission_unknown`、缺少 token 原子数量、钱包事实缺失等无法确认是否真实成交的系统问题仍 fail-closed，不得伪造为交易亏损。

“运行监控”页提供 Collector 的结构化实时终端：后端保留最近 250 条采集事件，前端约每 1.5 秒刷新，按 `new_creation / near_completion` 双生命周期分别显示 returned / accepted / rejected / duplicate，并逐条展示候选二筛拒绝原因、成功入样、T+1h 标签补齐和阶段异常。该终端来自 Collector 的实际事件流，不是对静态日志文件的装饰性回放。

### 6.4 训练、自更新与自选特征

“模型中心”会同时展示当前 Top 3、完整候选池、E/G/S、最终 holdout 审计、“不用模型”基线、成熟样本覆盖率和 Top 3 多样性审计。默认候选特征池仍是 31 个入场时特征；勾选/取消勾选会立即保存到 SQLite runtime state，重新进入页面会恢复上次选择，之后的手动训练和所有自动训练都从这份长期候选池重新评估。传统候选从最小可行维度开始逐维自适应比较；TabPFN 因 CPU foundation-model 推理成本采用 4/8/16/24/全量的稀疏维度网格。每个 chronological fold 的特征排序只读取该 fold 训练段，由浅层 XGBoost TreeSHAP 交互贡献排序，失败时才回退单变量 mutual information。所有算法的奥卡姆选择都必须同时落在 one-standard-error 区间内，且相对该算法最佳开发期 `S` 的损失不超过 8%。Top 3 在最佳模型 8% 性能保护带内优先选择不同 model family，TabPFN 作为独立 `foundation` family 可自动进入并替换 Top 3；最终 holdout 上的概率相关、决策一致率和买入集合 Jaccard 只作 certification 审计，不反向参与选模。`launchpad` 不进入模型；`ln(liquidity_usd)` 从新样本开始持续采集，覆盖率足够后可手动加入候选池。API 也支持显式提交候选特征列表：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/api/models/train `
  -ContentType application/json `
  -Body '{"reason":"manual","features":["ln(price+1)","price_change_1h"]}'
```

HTTP 手动训练仍只创建 durable `training_runs` 队列项，真正训练由单一 `TrainingWorker` 串行执行；自动训练则先经过 **1000 条当前代 mature+tag 样本硬门槛**。门槛未满足时 scheduler 固定为 `data_collection_only`，不会创建 daily / weekly / startup-catchup run，Worker 对任何历史遗留的非 manual queued run 还会再次检查并将其 `skipped`，因此重启也不能绕过门槛。达到门槛后才恢复原自动调度：北京时间 17:00，7 日模型健康为 `insufficient_data` 时每天训练，否则按每周约定日训练；到期日 16:00 先冻结四策略新买入，完成训练后候选 Top 3 持久等待全平再原子切换。模型健康服务也服从相同样本门槛，不得另开降级重训旁路。自动训练读取模型中心持久化的长期候选池；实际胜出模型可在 one-SE + 8% 相对性能保护下缩减到更小特征子集。

2026-08-13 age<240 迁移后执行正式手动重训 run `3aa752e0-7557-4602-aa79-22d9b5381c7f`。首轮本地训练进程因工具 180 秒调用上限被中断，durable TrainingWorker 按既有恢复规则将同一 run `retry_count=1` 后继续执行，没有创建重复任务。训练集为 1856 条 mature H1 v4 样本；完成于 `2026-08-13T03:35:38.461429+00:00`，四策略当时全平，因此于 `2026-08-13T03:35:38.499689+00:00` 立即激活并创建 `sim_ed9d999af1964ee194c9bfdd74fbb12d`（`created_reason=model_generation_upgrade`）。该代 Top 3 为 Random Forest / AdaBoost / HistGradientBoosting，分别使用 8 / 8 / 6 个特征。

2026-08-13 TabPFN 正式接入后的 durable run `ed4996ef-aff7-4079-9a0e-d8a288c093e6` 使用 1687 条 mature H1 v4 样本和模型中心持久化的 31 个候选特征，完整执行 fold-train-only TreeSHAP 交互排序、one-SE + 8% 相对 S 损失奥卡姆和 8% 性能带内 model-family 多样性选择。训练完成后因旧 generation 四策略仍持有同一批仓位，run 以 `waiting_for_flat` 持久等待，没有强制平仓；旧仓在 1h timeout 后由 PositionMonitor 正常退出，TrainingWorker 于 `2026-08-13T11:56:51.005533+00:00` 原子激活新 generation，并创建 `sim_425e0aaa067d427a8de69662d1411ed9`。该代 Top 3：Rank 1 **Gradient Boosting**（4 features，threshold `0.13`，E `0.412130`，G `0.464015`，S `0.432884`）；Rank 2 **Random Forest**（4 features，threshold `0.1159806`，E `0.402369`，G `0.457405`，S `0.424383`）；Rank 3 **TabPFN**（4 features，threshold `0.1123239`，E `0.376403`，G `0.453300`，S `0.407162`）。三者统一使用 `price_change_1h / ln(top_wallets+1) / fresh_wallet_rate / dev_team_hold_rate`；TabPFN 本轮实际使用 `V2 / CPU / 1 estimator`（V3 checkpoint 未完成一次性授权/缓存时后台自动回退 V2）。新 session 四策略均从 1000 USD / 0 交易开始。

2026-08-14 将模型排名经济代理正式从 `+6/-1` 改为摩擦调整后的 `+5/-1` 后，立即执行 durable run `ee607aa2-0791-477d-9254-f5331a781a38`，使用 **1726 条 mature H1 v4 样本 + 持久化 31 特征候选池**重新搜索全部候选算法、阈值和维度；四策略当时全平，因此于 `2026-08-14T03:12:41.347438+00:00` 直接激活并创建 `sim_e8afedb223ee48f2a958229763ebb63a`。该代 active Top 3 为：Rank 1 **LightGBM**（6 features，threshold `0.1339322`，E `0.361143`，G `0.454708`，S `0.398569`）；Rank 2 **Random Forest**（5 features，threshold `0.1592411`，E `0.321905`，G `0.457842`，S `0.376280`）；Rank 3 **Gradient Boosting**（4 features，threshold `0.1296533`，E `0.317905`，G `0.466934`，S `0.377516`）。Rank 2 的 S 略低于 Rank 3，是因为 Top-3 选择先在最佳 S 的 8% 性能保护带内优先补入不同 model family；Random Forest 提供 bagging family，随后才回补 boosting family。TabPFN 本轮仍正常参赛，但 S=`0.346826`，低于最佳 LightGBM 的 8% 保护下限约 `0.366683`，因此没有特殊保留。final holdout 仅作审计：三对模型最大概率相关约 `0.874`，最大买入集合 Jaccard 约 `0.664`，不参与选模。

2026-08-14 CatBoost / FLAML 正式启用后的 durable run `69ef1370-55c4-42b8-aa15-ae8944211f83` 使用 **1729 条 mature H1 v4 样本 + 31 特征长期候选池**完成全候选重训，`retry_count=0`、无训练错误，原先两项 optional skip 已消失：**CatBoost** `status=ok`（5 features，threshold `0.1537582`，E `0.286988`，S `0.357498`）；**FLAML AutoML** `status=ok`（4 features，threshold `0.1317023`，E `0.324919`，G `0.440288`，S `0.371067`），并自然进入候选 Rank 3。该次候选 Top 3 为 LightGBM（S `0.397621`，boosting）/ Random Forest（S `0.373483`，bagging）/ FLAML AutoML（S `0.371067`，automl）；FLAML production refit 的内部最佳 learner 为 `rf`，最佳配置为 `n_estimators=4 / max_features≈0.402995 / max_leaves=7 / criterion=gini`，工件同时记录 `split_type=time / eval_method=holdout / validation_fraction=0.20 / time_budget=5s`。final holdout 上 LightGBM×FLAML 概率相关约 `0.664`、selected Jaccard≈`0.612`；三者最大概率相关约 `0.858`、最大 Jaccard≈`0.711`。run 于 `2026-08-14T05:29:57.194257+00:00` 完成时旧 generation 的 `rules_only` 仍有 1 个自然持仓，因此候选以 `activation.waiting_for_flat` 持久化，不强制平仓；TrainingWorker 将继续遵守既有四策略全平 gate 后才原子切换。


### 6.5 配置中心

左侧新增 `/configuration`「配置」页面，统一管理当前外部 Provider 资源池和运行频率：

- GMGN Data：支持任意数量 `GMGN_API_KEY_n` 动态增删/重编号、Base URL、共享 IP 总 RPS；默认总预算为 10 req/s；部分最新官方 Skill 文档标注 20/capacity 20，实际账号确认后可在配置页覆盖，增加 Key 默认只增强轮换与 fallback，不自动放大 IP 吞吐。
- Jupiter Quote：支持任意数量 `JUPITER_API_KEY_n`，同一批模拟 SELL 的并发上限自动取 `min(8, 当前 Key 数)`；Key 数减少时并发自动收缩。
- TabPFN：可维护可选授权 Token，下一轮训练直接读取最新配置。
- Position Monitor：默认目标周期 3.0 秒，可在 1–60 秒间调整；页面同时显示当前唯一持仓 Token 数、按 GMGN 总预算估算的最短周期和 `within_target / budget_limited` 状态。
- 所有凭据只写本机 `.env`；GET/API/前端只返回掩码和数量，不回传明文，增删操作写审计但不记录 secret。

配置文件使用原子替换写入并保留非受管配置/注释。Collector 与 PositionMonitor 检测 `.env` 变更后重建 Provider；持仓周期与 GMGN RPS 在运行中热读取，无需为普通参数修改重启系统。

## 7. 测试与构建

```powershell
Set-Location D:\meme
.\.venv\Scripts\python.exe -m pytest

Set-Location D:\meme\frontend
npm run build
```

2026-08-16 当前基线：后端 `pytest -q` **165/165 通过**；前端 `tsc -b && vite build` 通过。当前正式代际为 `event1m_regime_v3`：严格 `2 < age < 300`，可选 catalog 44 / 生产默认 31，PIT-safe 1m Event 特征、GMGN event contract、DEX Screener 非关键 fallback、Coinbase BTC/SOL public market fallback、4×Alchemy→Solana Public emergency、Market Regime、neutral counterfactual、Adaptive Shadow/OPE evidence gate、family-audit fail-closed，以及 **1000 current-generation mature 样本建模门槛 / rules-only-only 低样本模式**均受测试覆盖。外部真实只读探针与 mock 测试都不关闭 DRY RUN、不签名、不广播交易。

部署环境还有一个只读数据链 smoke：`.\.venv\Scripts\python.exe scripts\collector_smoke.py`。它只构造现有 GMGN data adapter、执行 `new_creation` discovery 和至多一个 enrichment，不写 SQLite、不签名、不交易、也不打印 API Key/token address。2026-08-10 当前环境已实测 discovery/enrichment 通路可达；同时发现 GMGN 可能返回超过请求 limit 的候选，因此 `DiscoveryService` 还会在本地再次按 limit 截断。

外部交易接口自动测试必须使用 mock/fixture。除非用户在当前任务中明确批准小额真实验收，否则测试、代码审查和 CI 都不得关闭 DRY RUN 或广播交易。

## 8. 实盘启用门禁

以下条件缺一不可：

1. `pytest` 和前端构建通过；
2. `.env`、Git、日志、前端产物和交付 ZIP 的秘密扫描通过；
3. GMGN quote/swap/status、鉴权、状态枚举、429/reset、金额最小单位和 gmgn-cli 参数已用脱敏 fixture/小额授权测试验证；
4. 实盘钱包余额、USD/SOL 估值、Token decimals 与真实钱包至少 `0.1 SOL` 的手续费 reserve 已经对账；该 reserve 只属于实盘可执行性风控，不进入模拟账本；
5. pending/processed/提交结果未知不会重复下单；
6. 10 仓、同币唯一、日损 20%、连续 5 亏和退出不受熔断阻断的测试通过；
7. 一键清仓可串行、可恢复且达到硬上限后进入人工介入，不无限加费；
8. 后端 `DRY_RUN=false` 后，仍需在前端完成“准备 → 点击确认”的一次性挑战；页面刷新或后端重启不能隐式恢复实盘。

## 9. 当前完成边界与已知限制

### 9.1 非实盘本地版

截至 2026-08-16，单机/单用户/localhost 范围内的非实盘主链已切换到 **current-generation only**：`event1m_regime_v3` 持续采集/入场特征 → T+1h 标签 → 当前代成熟样本计数门槛 → 达到 1000 后才允许 chronological OOS + 奥卡姆候选训练 → Top 3 + rules-only → 四策略 USD-only 模拟。门槛前系统刻意停在“采集 + 打 Tag + rules-only 可执行性基线”阶段，不加载旧 Top3、不产生模型 predictions/positions，也不会日训、周训或启动补训。旧模型代的持久化训练/交易/模拟会话已作为无效历史清空；未来达到 1000 条后，新的模型、会话与交易历史只从 v3 同代证据重新建立。

当前数据边界：

- legacy / `event2m_regime_v1` / `event2m_regime_v2` 均缺失或不满足当前 v3 特征契约，已从主库删除，不再保留为训练、Model Health、自适应策略或旧 Top3 scorer 的输入；旧模型 registry、active slots、模型工件、training runs、predictions、simulation sessions、positions、trades、旧 collector cycle 和旧 SQLite 备份也同步清除；
- 不对旧样本做零填充、均值填充或事后特征重构；`event1m_regime_v3` 是新的数据起点，成熟样本数只从同代真实采集与 T+1h 标签重新累计；
- `ln(liquidity_usd)` 继续随 v3 新样本采集并留在模型中心可选 Shadow catalog，生产默认 31 暂不启用；达到 1000 条只是“允许重新训练”的下限，不等于任何 Shadow 特征自动晋级，仍须交由同代 chronological OOS / one-SE / final certification 判断；
- 本项目按 localhost 单用户交付。公网认证、session/CSRF、多租户不是当前本地版的“漏开发功能”；如果未来改成公网服务，必须先补正式身份认证与 CSRF/权限边界；
- SQLite 适合当前单进程第一版；跨进程/多服务器扩展时再引入正式 migration/lease/PostgreSQL，不把扩展架构伪装成当前版本阻塞项。

### 9.2 2026-08-16 Event/Regime 自适应层

市场状态与 Token Alpha 分层：Token 局部新特征只描述入场前已发生的 1m 事件，`price_change_1m` 的历史 fallback 仅使用目标时点前已闭合的 1m bar；Regime 则允许使用截至决策时点的 SOL 5m/15m/1h、Solana TPS/priority fee、Meme breadth、GMGN Hot Search/Smart Money/KOL/Dex promotion、执行质量及三模型近期聚类后表现。`rules_only` 永久保持固定规则基线；`model_1/2/3` 记录 `DEFENSIVE / NEUTRAL / EXPANSIVE` 的 logit 阈值反事实。当前策略默认处于 **shadow gate**：即使 Regime 推荐收紧或放宽，实际阈值仍为 Neutral，直到当前 feature generation 至少获得 120 个独立 sample cluster、至少 40 个动作会改变选择的 cluster，并且 chronological 70/30 development/certification 的 +3/-1 excess utility 满足 development>0、certification 95% LCB>0、三个模型各自 certification 均不为负。在线探索另有第二道 `adaptive_exploration_ready` 门，且最大 5%；未通过证据门时 `.env` 单独设置探索率也不能启用探索。

Provider fallback 采用“主源优先、缺失不伪造为 0”：GMGN Key Pool 是 Token/Trending/Hot Search/Smart Money/KOL/DexScreener 集成字段主源；GMGN 整体不可用，或其 Signal 中 Dex promotion 子族单独缺失时，才以 DEX Screener Public API 的最新 Solana Ads/Boosts 做低频、家族级 attention fallback。Solana Network 状态按 `4×Alchemy 独立 Free account → Solana Public RPC emergency` 降级；Public RPC 只允许应急且降低 Regime confidence。Alchemy Free 当前为每账号 30M CU，`getRecentPerformanceSamples=20 CU`、`getRecentPrioritizationFees=10 CU`，因此一分钟一次网络状态远低于免费预算。DEX Screener 官方 Public API 仍为无鉴权免费接口，Ads/Boost 类 60 RPM、Pair/Token 类 300 RPM；官方 API Terms 同时确认存在付费 API，但公开网页未给出可验证的个人免费 Key/付费价目表，因此当前不把它设为关键生产依赖。

投研依据只用于确定“应研究什么”，不直接授予生产权：2025 *Finance Research Letters* DOI `10.1016/j.frl.2025.108356` 显示 Crypto momentum 明显依赖市场状态；2025 *Journal of Banking & Finance* DOI `10.1016/j.jbankfin.2025.107518` 显示异常社交注意力与当期/下一日收益有关且主要来自用户 ticker 帖；2026 *International Review of Financial Analysis* DOI `10.1016/j.irfa.2026.105137` 的 Crypto Factor Zoo 显示少数流动性/交易成本和 blockchain-native 因子已能压缩大部分 factor zoo。以上均非 1 分钟入场特征下 Solana Meme H1 的直接证据，所以所有 Event/Regime family 仍必须经过本系统 chronological OOS / shadow / certification。

### 9.3 2026-08-18 执行摩擦复审

对当前 active simulation 的 rules-only 账本做了逐笔会计重建。审计时 176 笔 closed 中 132 笔为 `stop_loss_0_9x`；每笔按 `token_amount × exit_price - invested_usd - platform_fee - network_fee` 重算，与 `positions.net_pnl_usd` 176/176 一致，最大差异仅浮点误差约 `2.84e-14`，因此不存在滑点或手续费重复扣款。0.9x 是**触发线**而不是保证成交价：高波动 Meme 可能在两次 current-price 观测之间直接跌穿；历史止损成交价中位约为入场价 `0.826x`，说明不少额外损失在第一次观察到止损条件时已经发生。

同时修复两项执行/审计缺陷：第一，旧 `trades.slippage_*` 把 GMGN current price 到 Jupiter fill 的跨源/跨时点差异当成滑点；170 笔 route-validated 历史 SELL 的该字段由 `$393.28` 按 Jupiter 自带 `priceImpact` 重分类为 `$96.72`，约 `$296.56` 被纠正为非 slippage execution deviation。该历史修复不改变任何 fill、现金或 PnL，旧 deviation 保留在 trade `response_json`。第二，旧 PositionMonitor 会等待整批 GMGN 持仓行情全部返回后才处理 paper SELL，最慢 Token 会阻塞其它 Token；现在每个 market response 一到即按实际响应时刻判断，并立刻启动受 Jupiter-key semaphore 限制的退出任务。默认轮询目标同时从 3s 收紧到 **2s**，生产已观测 `target_poll_seconds=2.0 / last_start_interval_seconds=2.0`。

2026-08-18 当时模型经济目标从 `+5/-1` 收紧为 **`+4/-1`**：`U=4TP-FP`，固定 $50 代理为赢 +$20 / 输 -$5，盈亏平衡 Precision=20%；H1 `1.6x/0.9x` 标签与实际交易规则不变。该阶段新模型写入 `economic_objective_version=fixed_4_to_1_v2`。

**2026-08-24 当前口径**进一步收紧为 **`+3/-1`**：`U=3TP-FP=N+×recall×(4-1/precision)`，固定 $50 代理为赢 +$15 / 输 -$5，盈亏平衡 Precision=25%，经济得分归一化为 `E=mean(clip((3TP-FP)/(3N+),-1,1))`。新模型写入 `economic_objective_version=fixed_3_to_1_v3`；Prediction 对旧 objective 模型继续 fail-closed，必须使用本口径重新训练的模型工件。

本次热更新后正式手动重训 run `b040425e-3043-4dd5-9752-f6685d83f4b9` 使用 **1080 条 current-generation mature 样本 + 当前持久化 44 特征候选池**，`retry_count=0`、无训练错误。开发期选出的新 Top 3 为：Rank 1 **Extra Trees**（35 features，threshold `0.2454529`，E `0.139676`，G `0.459697`，S `0.267685`）；Rank 2 **Decision Tree**（4 features，threshold `0.42`，E `0.124905`，G `0.437260`，S `0.249847`）；Rank 3 **Random Forest**（20 features，threshold `0.2763549`，E `0.118388`，G `0.447422`，S `0.250002`）。三者工件均写入 `fixed_3_to_1_v3`。final holdout 继续只做 certification：Extra Trees / Decision Tree / Random Forest 的 Precision 分别约 `28.57% / 20.00% / 19.51%`；该结果不反向改变开发期 Top 3。训练完成后候选按既有 staged rollover 进入 `waiting_for_flat`，并冻结四策略新开仓；已有 `rules_only` 仓位继续按原 TP/SL/1h 路径自然退出，禁止为换模强平。

### 9.4 2026-08-25 Phase16 当前生产态

当前非实盘生产合同已从 H1 1.6x 切换到 **`SL 0.9 / TP 1.8 / 90min / sl090_tp180_m90_binary_v5`**。新模拟仓使用不可变 `m90_tp180_sl090_v2` 快照，旧仓继续按各自 legacy snapshot 退出；标签层使用 generic barrier audit，避免覆盖历史 `price_1h_*`。生产迁移时 1216 条 current-generation mature 全量重标，Kline coverage 100%，FK/label/tag violation 均为 0。

Phase16 模型层新增 chronological sigmoid calibration、model-specific sparse budget、age-aware threshold、execution-risk head、development-reference PSI drift 与 final deployment certification。2026-08-25 durable run `e83855f9-158d-4061-ab80-ba7aaa49c2a6` retry=0 完成，候选 Top3 为 Gradient Boosting / CatBoost / LightGBM，risk head 认证通过；但 final recent 发生真实流动性分布漂移（`ln(liquidity_usd)` PSI≈0.462）且完整 model-policy selected 不足，因此本代 **blocked_certification，未激活**。旧 Top3 虽仍保留在 active slot 作为历史对象，但 Prediction 因 Phase16 provenance stale fail-closed，不再开 model 仓。

fallback 已固定：active Top3 not-ready/stale 或 certification blocked 时，scheduler 自动进入 **daily 17:00 BJT** recovery cadence；通过认证并激活后恢复 weekly。`rules_only` 继续作为无模型可执行性基线。运行收口还修复了 `.env` 中误留的 `SIMULATION_ENABLED=false`；当前 `SIMULATION_ENABLED=true`、`DRY_RUN=true`，已实测新 rules-only 1.8/90m 仓正常建立。

Windows 侧对 2026-08-24/25 停采事故的根因定位为 Modern Standby；防休眠从线程级 `SetThreadExecutionState` 升级为持久 Power Request (`SystemRequired + ExecutionRequired`)。异常 `+1716 USD` 延迟退出仓保留原始成交事实，但通过 `performance_excluded=true` 从收益、现金重算、胜负与训练执行证据中隔离。

本轮最终后端 `pytest -q` **204/204**，scheduler recovery targeted 12/12，前端 `tsc -b && vite build` 通过。

### 9.5 实盘接口刻意停放

实盘代码保留 quote/swap/status、幂等 journal、启动对账、二次确认和持久化清仓接口；自动 live BUY 当前保持停放。未来实盘使用当时 active Top 3 的 Rank 1 模型及其单一决策线，不再存在 `profile`。真正启用实盘前仍必须接入并现场验收：

- 真实 wallet snapshot：available USD、总权益、SOL balance、SOL/USD、Token balance/decimals；
- 无 `provider_order_id` 的钱包/代币余额对账，以及 Rank 1 实盘策略与真实钱包共同资金闸门；
- GMGN 真实成交 `output_amount_raw` / decimals / status 契约；
- 以日初真实钱包总权益（含未实现盈亏）为基线的 20% 日损门禁、连续 5 亏门禁及其真实钱包 E2E；
- 小额授权环境下的 quote → submit → poll → restart reconciliation → exit 全链验收。

在这些真实资金事实缺失时，系统继续 `DRY_RUN=true`、没有自动 live BUY pipeline；不会用模拟数值绕过门禁。

更详细的数据库、API、错误分类、调试流程和 AI 接手规范见 [开发文档.md](./开发文档.md)。

## 10. 致谢

量化系统最难的，往往不是写出第一笔模拟成交，而是在漫长夜里仍然愿意把边界守住：哪些信号该进样本，哪些冲动必须被规则按住，哪些“看起来能赚钱”的捷径其实会毁掉整条可信链路。本项目能从零散脚本走到今天这条可审计的本地闭环，靠的不只是代码堆叠，更是一路并肩推敲时那种不肯含糊的认真。

特别感谢 [tangerinepith](https://github.com/tangerinepith)。在前后端工程上，他像把散落的零件重新装回同一台机器：从 FastAPI 生命周期、SQLite schema 迁移与训练任务持久化，到 React 控制台左侧导航的产品化拆分（总览、持仓、样本采集、运行监控、模型中心、Agent 审批）；从持仓页的模拟/实盘与四策略对照，到交易历史真分页、退出时间筛选，再到采集事件流里 returned / accepted / rejected / duplicate 被一笔笔点亮——那些原本只存在于讨论里的策略，终于有了可以打开、可以核对、可以复盘的面孔。每一次页面刷新、每一次日志回看，都像在说：判断不必靠感觉，证据就在这里。

在算法与交易策略上，他给出的建议同样带着温度，却始终落在刀刃上：守住 1 小时策略窗口与 T+1h 标签补齐；先用规则初筛挡住明显不安全的样本，再用严格 chronological OOS、经济得分与泛化稳定性筛选模型，而不是被某一次漂亮的最终测试成绩牵着走；强调奥卡姆剃刀、最终 holdout 隔离、退化监控与“宁可安全拒绝、也不盲目上线”，好让早期 Pump.fun legacy 样本不至于被夸大成全市场的幻觉；在退出与风控上，推动把 current-price TP/SL、executable quote、仓位上限、同币唯一、日损与连亏门禁写进可测试的约束；并一次次提醒——实盘必须服从 `DRY_RUN`、幂等 journal 与二次确认，绝不能用漂亮的模拟 PnL 去绕过真实的资金事实。

截至 2026-08-14，仓库里的非实盘主链已经完成 schema v11 H1/no-completed、扩展候选池、自适应且 fold-train-only 的特征选择、Top 3 + `rules_only`、四策略 USD-only 模拟账本、手续费发生时 SOL/USD 冻结折算、默认 3s（可配置）current-price PositionMonitor、Jupiter Token→USDC executable quote-only 卖出验证、重大模型换代新 simulation session、H1-only simulation 历史与重启可恢复的 workers，以及 143/143 后端测试与前端 production build。现役 Top 3 已在真实样本上完成训练与激活，最终 holdout 被严格保留为 certification；`E_exec` 仅作为权重为 0 的执行影子指标。写在这里的致谢不是客套，而是一份公开的记念——没有这些前后端支撑，没有那些在策略分叉口给出的清醒建议，本项目很难同时站在“可演示”与“可负责”之间。



### 2026-08-25 GMGN 429 circuit-breaker / position fallback

- 现场进一步复现了 GMGN 多 Key 429：即使独立探针降到 1 RPS，多把 Key 仍返回带约 100–155 秒 reset 窗口的 429；旧逻辑把单 Key `reset_at` 写进共享 limiter，导致一把 Key 限流会把 Collector + PositionMonitor 的其它 Key 一并暂停，PositionMonitor 曾出现约 47–49 秒 start interval。
- `GMGNDataClient` 现在把 429 cooldown 绑定到 **slot 本身**，共享 limiter 只负责总 RPS，不再承载单 Key reset。PositionMonitor 遇单 Key 429 立即换下一 Key；若整池均 429，则打开 full-pool circuit breaker，在 reset window 内不再触碰 GMGN，直接进入只读 fallback。
- PositionMonitor 新增 **DexScreener Public `/token-pairs/v1/solana/{mint}`** 兜底，仅用于已经打开的持仓 current-price 监控：只接受目标 Mint 作为 `baseToken` 的 Solana pair，并选流动性最高的 pair；fallback source 持久化为 `dexscreener_public_token_pairs`。该路径不参与 Collector admission、标签、模型训练或新仓选股。fallback 自身限速 4 RPS，低于其 Public pairs 类接口公开 300 RPM 上限。
- Collector 仍 fail-closed：GMGN 缺关键准入/Kline 事实时不使用 DexScreener 补造样本。Discovery 对 429 不再重复打同一 primary；已冷却的 primary/fallback 直接跳过；Realtime/Kline enrichment 跳过 cooldown slot，连续 429 之间不再人为 sleep。目标是让 server reset window 真正到期，而不是每几十秒探测一次把滚动限流续上。
- 生产 `GMGN_GLOBAL_RPS` 从 10 保守降为 **2 RPS**；不改变任何业务筛选阈值、LabelPolicy 或 DRY_RUN 边界。后续只有在持续运行证据证明无 429 且采样吞吐不足时，才允许逐级上调。

- **Coinbase Exchange Public SOL/USD fee fallback**：PositionMonitor 的手续费会计仍以 GMGN SOL 1m 为主；若 GMGN Kline 因 429/网络不可用，则读取 Coinbase Exchange 无鉴权 `SOL-USD` 1m candles，只接受已经闭合的 minute bucket，并以 candle close 时刻写入 `asset_usd_prices`，source=`coinbase_exchange_public_sol_usd_1m`。该 fallback 已真实解除一笔长期 `closing` rules-only 仓位；退出价、PnL、Jupiter quote 均保持真实执行事实。
- 最终自动化门：后端 `pytest -q` **204/204**、`py_compile`、`git diff --check` 与前端 `tsc -b && vite build` 均通过。

- **Collector worker 级长熔断**：仅 per-slot cooldown 仍不足以处理 GMGN 的滚动 reset。Collector 现将 rate-limit circuit 持久化到 SQLite；首次确认 discovery 429 后静默至少 300s，再次 half-open 失败按 600/1200/2400/3600s 指数退避。backend 重启不会清除 `next_probe_at`。backoff 内不请求 GMGN；half-open 只优先执行真实 `collect_once`，成功并持久化 cycle snapshot 后才关闭 circuit，下一正常周期再恢复 SOL/Kline/Regime/label 辅助负载。
- 生产现场曾在故障恢复中出现一次 partial cycle：New Creation 与 Near Completion discovery 已返回，Near Completion 新样本 `4104/4105` 于 13:38–13:39 BJT 成功入库，但后续阶段失败导致该轮没有 cycle snapshot。样本事实保留有效；最终验收只认 circuit 关闭后的完整成功周期。

- **GMGN Key 角色隔离**：当配置 Key>=6 时，slot 0/1 专供 New Creation/Near Completion discovery，slot 2 只作 discovery fallback；slot 3+ 承担 realtime enrichment、Kline 与 PositionMonitor。`kline_fallback` 不再借用 discovery fallback。少 Key 环境保持旧共享布局。生产当前 12 Key，因此实际为 3 把 Discovery 保留容量 + 9 把 auxiliary。
- **恢复 probation**：half-open collection 成功后保留前一 streak 15 分钟；probation 内若正常全链周期再次 429，则从上一 streak 继续加倍，而不是错误地重置回 5 分钟。现场 13:49 恢复后约 2 分钟复发已按新语义升级为 streak=2 / 600s，下一半开点 14:01:38 BJT。

- **GMGN 429 最终生产验收**：在 12-Key 角色隔离（0/1/2 Discovery；3–11 auxiliary）、`GMGN_GLOBAL_RPS=2`、per-slot cooldown、worker 持久化 circuit 与 15 分钟 probation 同时生效后，生产连续完成 **4062 / 4063 / 4064** 三个 Collector cycle，均 `discovered=120 / errors=[]`；4062 与 4064 各新增 1 条样本。最终样本数 **1279（1275 mature / 4 pending，200 positive）**。PositionMonitor 保持约 2s、0 blocked/0 market/network failure；持仓 current-price DexScreener fallback、fee-time SOL/USD Coinbase fallback 均已真实验证并可在 GMGN 恢复后自动回主源。
