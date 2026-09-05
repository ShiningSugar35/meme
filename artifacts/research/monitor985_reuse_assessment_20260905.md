# 985monitor.xyz 复用评估与 v4 特征落地（2026-09-05）

## 结论

985monitor 对本项目最有价值的不是“Agent 交易”，而是把分散的社交/资讯/交易所广场/钱包追踪信号收敛为带事件时间的实时流。该方法可以在不引入 LLM Agent、不购买付费情绪 API 的前提下复用。

本轮已落地的原则：

1. 只使用可自动化的免费只读来源；当前生产代码不读取浏览器 Cookie、不保存站点 Token、不调用 LLM。
2. 所有 token-local 事件必须满足 `event_time <= sample.entry_time`；未来事件永不回填入场特征。
3. 远端列表达到最大行数时，必须确认最老事件已覆盖完整 5m/15m 窗口；否则该来源标记 incomplete，不能把“未看到 CA”当作 0 提及。
4. 单源失败或超时只降低 `monitor_source_coverage`，不阻断 Collector，也不伪造为 0。
5. 985monitor 抓取放在常规安全门和新增链上门之后，并用 20 秒共享缓存，避免每个候选重复拉全站事件。
6. 新增信号进入 v4 训练候选池，由 chronological development OOS 决定是否保留；不直接晋级最终模型。

## 已验证的公开只读来源

本机脱敏探针 `monitor985_public_probe_20260905.json` 已实际读取以下公开流，不保存事件正文：

| 来源 | 实测返回 | 主要可用信息 | Solana 用法 |
|---|---:|---|---|
| Binance Square | 200 | createdAt/content/account | CA 提及、跨来源共振、事件速度 |
| Binance Alpha | 200 | createdAt/content | 上币/Alpha 事件、CA 提及 |
| Binance Web3 | 200 | createdAt/content | Web3 广场事件、CA 提及 |
| OKX Board | 34 | createdAt/content | 交易所广场事件 |
| News | 54 | createdAt/content | 资讯事件、CA 提及 |
| Truth Social | 200 | createdAt/content/account | 社交提及 |
| DEX events | 200 | createdAt/content | DEX 事件、市场关注 |
| FOMO static feed | 800 | side/usd/tokenAddress/followers/marketCap/ts | 买卖笔数、金额不平衡、人物覆盖 |
| Telegram static feed | 600 | createdAt/content/channel | TG 喊单/讨论提及 |

`985monitor.xyz` 主站同时展示 X、广场、Truth、资讯、WSS、TG、fomo、j7、gmgn，并通过 SSE 维持实时连接。公开端点属于站内只读接口而非有 SLA 的正式数据 API：多次探针曾出现 9/9 成功，也出现 7/9 成功（Binance Square/OKX 临时失败），因此只作为可缺失模型特征，绝不升级为硬准入依赖。当前 X/Pump 直接 REST 在本机表现不稳定或需要站点身份，因此本轮没有把它们伪装成公共稳定源。

## 已落地的 v4 候选特征

### Token-local 注意力与共振

- `ln(monitor_mentions_5m+1)`
- `ln(monitor_mentions_15m+1)`
- `ln(monitor_unique_authors_15m+1)`
- `monitor_unique_sources_15m`
- `ln(monitor_follower_reach_15m+1)`
- `monitor_mention_accel_5m_vs_15m`
- `ln(monitor_latest_mention_age_s+1)`
- `monitor_exchange_hits_15m`
- `monitor_news_hits_15m`
- `monitor_social_hits_15m`

这些字段不需要主观“情绪正负词典”：它们表达注意力强度、速度、独立来源数和传播覆盖，更适合 Meme 的短周期入场建模，也更容易审计。

### FOMO 交易情绪/行为

- `monitor_fomo_buy_ratio_15m`
- `monitor_fomo_usd_imbalance_15m`
- `ln(monitor_fomo_usd_15m+1)`

这些是行为型情绪代理：实际买卖方向与美元金额，不依赖 NLP。

### 全局市场注意力 regime

- `ln(monitor_global_events_5m+1)`
- `monitor_global_source_diversity_5m`
- `monitor_source_coverage`

用于区分“某币自身热”与“全市场都很吵”的状态，并显式告诉模型/审计当前来源覆盖是否完整。

## 985monitor/插件可复用的方法

公开仓库 `0xuezhang985/985gmgn-helper` 证明了以下免费自动化方式可行：

- Manifest V3 扩展直接在 GMGN/DeBot/fomo/985monitor 页面内工作，而非依赖 LLM Agent。
- 985monitor 账号可以在同一 Chrome profile 登录一次后建立独立只读 session；页面之后可关闭，session 失效则停止推送，而不是退回错误的公共个性化结果。
- fomo 登录令牌只在 fomo 页面读取并只发往 fomo 官方 API；GMGN Bearer 也只在 gmgn.ai 页面内使用。
- 插件把 985monitor Pump/FOMO 真实时间事件混排到 GMGN/DeBot，说明“浏览器登录态 -> 确定性本地桥 -> 结构化事件”是可行路线，不需要 Agent skills。

### 适合本项目的下一层浏览器桥

若以后要补 X 私有/个性化流、985monitor 个人关注 Pump/FOMO 或 fomo 个人持仓观点，建议复用同类架构，而不是 Python 直接解密 Chrome Cookie：

```text
Chrome/Edge MV3 content script（站点原域，复用已登录 session）
  -> 只提取白名单结构化字段
  -> Native Messaging / localhost loopback
  -> D:\meme 后端的只读 event ingest
  -> observed_at + source + event_time + token CA
```

这样登录凭据不离开原站域，后端只收到结构化事件。它是普通确定性采集器，不是 LLM/Agent。

## 本轮暂不直接接入的信号

1. **X 私有流**：主站有 X 数据，但公开 REST 在本机不稳定；应等浏览器只读 session 桥，而不是抓 Cookie 或用付费 X API。
2. **Pump 个性化关注流**：公开端点延迟不稳定；扩展仓库表明可以通过 985monitor 只读 session 获取个人关注/过滤后的事件，适合浏览器桥后接入。
3. **fomo 个人持仓、7日盈亏、观点**：需要用户自己的 fomo 登录态。可以免费复用浏览器鉴权，但应保持 token 只在 fomo 原域使用。
4. **文本“正/负情绪分数”**：当前不引入云 LLM，也不新增常驻深度 NLP 模型。短文本 Meme 语境下，通用词典很容易把反讽/黑话误判；本轮优先使用真实交易行为、事件速度、传播覆盖与来源共振。
5. **自动翻译后的文本特征**：插件使用 Chrome 本地翻译，但翻译结果是展示辅助，不应在没有独立 OOS 验证前成为交易特征。

## 失败与降级语义

- 公共事件流全挂：所有 `monitor_*` 事件特征保持 `None`，不写 0。
- 部分来源挂：仍计算完整窗口来源的特征，同时 `monitor_source_coverage<1`。
- 来源列表被截断且不能覆盖目标时间窗：该来源进入 `incomplete_sources`，不参与该窗口统计。
- 985monitor 故障：绝不影响规则准入、链上门、labels、rules_only 或持仓退出。
- 任何事件的 `event_time > entry_time`：无条件丢弃，防未来泄漏。

## 代码与证据

- 实现：`backend/app/services/public_social_signals.py`
- Collector 接入：`backend/app/collector/enrichment.py`
- 候选目录：`backend/app/ml/features.py`
- 单元回归：`tests/test_public_social_signals.py`
- 实机探针：`artifacts/research/monitor985_public_probe_20260905.py`
- 实机结果：`artifacts/research/monitor985_public_probe_20260905.json`
- 参考：`https://985monitor.xyz/`
- 参考：`https://github.com/0xuezhang985/985gmgn-helper`
