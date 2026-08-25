# 2026-08-24 Collector 停采事故记录

## 现象

- 最后一轮正常 Collector cycle：2026-08-24 09:36（Asia/Shanghai）附近。
- 此后前端/runtime 仍残留 `collector_status.state=running`，但 `last_cycle_at` 不再推进，且无新样本。

## 根因

本次离线 2h Kline 研究流程为了隔离模拟交易，临时修改了运行状态，但没有完整恢复：

1. `.env` 中 `COLLECTOR_ENABLED=false`，文件 mtime 为约 09:38:05；后端热重载后启动逻辑因此没有重新创建 CollectorWorker。
2. durable `runtime_state.simulation_entries_paused=true` 同样残留。
3. `collector_status` 存储在 SQLite，Collector 未启动时旧的 `running` 快照没有被覆盖，造成“看起来 running，实际无 worker”的误导。
4. 更深层耦合：SOL/USD fee-time 事实此前由 Collector 周期刷新。Collector 停止后，PositionMonitor 虽仍运行，但已有 simulation 仓位在 exit route quote 后因 `sol_usd_price_unavailable` 被卡住，无法完成退出。

## 影响

- 09:36 后 discovery/sample collection 停止，直到约 14:04 恢复。
- 2 个 rules-only 仓位跨越事故窗口，退出延迟约 3.69h / 4.34h；后者因延迟期间价格暴涨产生约 +3432% 的污染收益，不能进入执行策略统计。
- 事故影响仓位：
  - `rules_only-4ff37ddee6cf43cb`
  - `rules_only-30795ca0d9114ec6`

## 修复

1. 恢复 `.env`：`COLLECTOR_ENABLED=true`。
2. 恢复 `simulation_entries_paused=false`。
3. `backend/app/main.py`：Collector 被配置为 disabled 时，启动过程显式写入 `collector_status.state=disabled`，禁止保留旧 running 快照。
4. `PositionMonitorWorker`：新增独立 SOL/USD 刷新链。有 simulation open/closing position 时，PositionMonitor 自行保证约 60s 内新鲜的 SOL/USD fee fact；失败时显式 degraded/audit。退出链不再依赖 Collector discovery 存活。
5. 新增 PositionMonitor 回归：有 open paper position 且 SOL/USD stale 时自主 refresh；refresh 后不重复请求。

## 验收

- 恢复后首轮 Collector：发现 120，accepted=3，真实新增样本。
- 后续连续 cycle 正常推进；accepted=0 的轮次均为严格准入正常拒绝，`errors=[]`。
- `/health=ok`。
- PositionMonitor：`state=running`、约 2.0s cadence、`sol_usd_refresh_error=null`。
- 修复后 collector/trading warning/error=0（验收窗口）。
- PositionMonitor 定向回归 7/7 通过。
- 全量 pytest 172/172 通过。

## 后续审计口径

任何执行/PnL 统计必须排除上述 2 个 incident-contaminated positions，除非专门分析故障恢复行为。
