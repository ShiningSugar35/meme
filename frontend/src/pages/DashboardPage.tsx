import { Activity, Bot, CircleDollarSign, Database, Layers3 } from "lucide-react";
import { useCallback } from "react";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import { useApiData } from "../api/useApiData";
import { EmptyState } from "../components/EmptyState";
import { MetricCard } from "../components/MetricCard";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const money = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol", maximumFractionDigits: 2 }).format(value);
const percent = (value: number | null | undefined) => value == null ? "—" : `${(value * 100).toFixed(1)}%`;
const score = (value: unknown) => typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "—";
const strategyName: Record<string, string> = { model_1: "模型1", model_2: "模型2", model_3: "模型3", rules_only: "不用模型", live: "实盘" };

function pivot<T extends { date: string }>(rows: T[], category: (row: T) => string, value: (row: T) => number) {
  const result = new Map<string, Record<string, string | number>>();
  rows.forEach((row) => {
    const current = result.get(row.date) ?? { date: row.date };
    current[strategyName[category(row)] ?? category(row)] = value(row);
    result.set(row.date, current);
  });
  return [...result.values()];
}

export function DashboardPage() {
  const loader = useCallback(() => api.dashboard(), []);
  const { data, loading, error, refresh } = useApiData(loader);
  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  const simulationOpen = Object.values(data.simulation.accounts).reduce((sum, account) => sum + Number(account.open_positions || 0), 0);
  const selectedSignals = data.signal_activity.reduce((sum, row) => sum + Number(row.selected), 0);
  const rank1 = data.active_models.find((model) => Number(model.active_slot) === 1) ?? data.model;
  const equity = pivot(data.equity_curve, (row) => row.strategy_key, (row) => Number(row.daily_pnl));
  const signalActivity = pivot(data.signal_activity, (row) => row.strategy_key, (row) => Number(row.selected));
  const equitySeries = [...new Set(data.equity_curve.map((row) => strategyName[row.strategy_key] ?? row.strategy_key))];
  const signalSeries = [...new Set(data.signal_activity.map((row) => strategyName[row.strategy_key] ?? row.strategy_key))];
  const colors = ["#3668b2", "#c68a2b", "#d56a3a", "#6f7c8c"];

  return <div className="page-stack">
    <section className="page-heading"><div><p className="eyebrow">STATUS OVERVIEW</p><h1>今天的系统状态</h1><p>Top 3 模型、纯规则基线、采集和资金曲线的一屏概览。</p></div><div className="heading-actions"><StatusBadge tone={data.runtime.live_trading_enabled ? "orange" : "blue"} label={data.runtime.live_trading_enabled ? "实盘已启用" : data.runtime.dry_run ? "DRY RUN" : "模拟模式"} /><button className="button button-secondary" onClick={() => void refresh()}>刷新</button></div></section>

    <section className="metric-grid">
      <MetricCard title="实盘收益" value={money(data.live_realized_pnl_usd)} icon={CircleDollarSign} tone={data.live_realized_pnl_usd < 0 ? "orange" : "blue"} />
      <MetricCard title="Rank 1 模型" value={data.strategies.find((item) => item.strategy_key === "model_1")?.label ?? "尚未训练"} icon={Bot} tone="gold" />
      <MetricCard title="Rank 1 综合分" value={score(rank1?.metrics.composite_score ?? rank1?.active_composite_score)} context={`Precision ${percent(typeof rank1?.metrics.precision === "number" ? rank1.metrics.precision : null)} · Recall ${percent(typeof rank1?.metrics.recall === "number" ? rank1.metrics.recall : null)}`} icon={Activity} tone="blue" />
      <MetricCard title="四策略模拟持仓" value={`${simulationOpen} / ${data.risk.max_open_positions * 4}`} context={`过去7日模型入选 ${selectedSignals} 条`} icon={Layers3} />
      <MetricCard title="成熟训练样本" value={new Intl.NumberFormat("zh-CN").format(data.dataset.mature ?? 0)} context={`正类率 ${percent(data.dataset.positive_rate)}`} icon={Database} />
    </section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>Top 3 模型</h2><p>训练排名只使用开发期时间外综合分；最近最终留出用于独立审计。</p></div></div>{data.active_models.length ? <div className="table-scroll"><table><thead><tr><th>排名</th><th>模型</th><th>特征</th><th>决策线</th><th>Precision</th><th>Recall</th><th>理论收益</th><th>E</th><th>G</th><th>S</th></tr></thead><tbody>{[...data.active_models].sort((a, b) => Number(a.active_slot ?? 99) - Number(b.active_slot ?? 99)).map((model) => <tr key={model.id}><td><StatusBadge tone={model.active_slot === 1 ? "gold" : "blue"} label={`Top ${model.active_slot}`} /></td><td>{data.strategies.find((item) => item.model_id === model.id)?.label ?? model.algorithm}</td><td>{model.feature_names.length}</td><td>{typeof model.active_threshold === "number" ? model.active_threshold.toFixed(3) : "—"}</td><td>{percent(typeof model.metrics.precision === "number" ? model.metrics.precision : null)}</td><td>{percent(typeof model.metrics.recall === "number" ? model.metrics.recall : null)}</td><td>{money(typeof model.metrics.fixed_profit_usd === "number" ? model.metrics.fixed_profit_usd : null)}</td><td>{score(model.metrics.economic_score)}</td><td>{score(model.metrics.generalization_score)}</td><td><strong>{score(model.metrics.composite_score)}</strong></td></tr>)}</tbody></table></div> : <EmptyState title="Top 3 尚未生成" detail="完成一次新训练后显示。" />}</section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>四策略模拟收益</h2><p>三个模型与“不用模型”各用独立账户执行同样的交易/退出规则。</p></div></div><div className="table-scroll"><table><thead><tr><th>策略</th><th>交易</th><th>持仓</th><th>已平仓</th><th>已实现 PnL</th></tr></thead><tbody>{data.strategy_performance.map((item) => <tr key={item.strategy_key}><td><strong>{data.strategies.find((strategy) => strategy.strategy_key === item.strategy_key)?.label ?? strategyName[item.strategy_key] ?? item.strategy_key}</strong></td><td>{item.positions}</td><td>{item.open_positions}</td><td>{item.closed_positions}</td><td className={item.realized_pnl_usd < 0 ? "number-negative" : "number-positive"}>{money(item.realized_pnl_usd)}</td></tr>)}</tbody></table></div></section>

    <section className="chart-grid">
      <article className="panel chart-panel panel-wide"><div className="panel-heading"><div><h2>近7日已实现损益</h2><p>单位：$；按策略和平仓日期聚合</p></div><span className="source-note">SQLite · {new Date(data.as_of).toLocaleString("zh-CN")}</span></div>{equity.length ? <div className="chart-box"><ResponsiveContainer width="100%" height="100%"><LineChart data={equity} margin={{ top: 8, right: 18, left: 4, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e5e8ec" /><XAxis dataKey="date" tickLine={false} axisLine={{ stroke: "#cbd1d8" }} /><YAxis tickLine={false} axisLine={false} tickFormatter={(v) => `$${v}`} /><Tooltip formatter={(v) => money(Number(v))} /><Legend />{equitySeries.map((series, index) => <Line key={series} dataKey={series} type="monotone" stroke={colors[index % colors.length]} strokeWidth={2} dot={{ r: 2 }} connectNulls />)}</LineChart></ResponsiveContainer></div> : <EmptyState title="暂无损益曲线" detail="新四策略模拟产生平仓后显示。" />}</article>
      <article className="panel chart-panel"><div className="panel-heading"><div><h2>近7日模型入选</h2><p>按 Top 3 模型分别统计</p></div></div>{signalActivity.length ? <div className="chart-box"><ResponsiveContainer width="100%" height="100%"><BarChart data={signalActivity}><CartesianGrid vertical={false} stroke="#e5e8ec" /><XAxis dataKey="date" tickLine={false} axisLine={{ stroke: "#cbd1d8" }} /><YAxis allowDecimals={false} tickLine={false} axisLine={false} /><Tooltip /><Legend />{signalSeries.map((series, index) => <Bar key={series} dataKey={series} fill={colors[index % colors.length]} radius={[4, 4, 0, 0]} />)}</BarChart></ResponsiveContainer></div> : <EmptyState title="暂无模型信号" detail="Top 3 模型开始评分后显示。" />}</article>
      <article className="panel health-panel"><div className="panel-heading"><div><h2>运行与风险</h2><p>开仓门禁的实时摘要</p></div></div><dl className="health-list"><div><dt>采集器</dt><dd><StatusBadge label={String(data.runtime.collector.state ?? "stopped")} /></dd></div><div><dt>新开仓</dt><dd><StatusBadge tone={data.risk.new_entries_paused ? "orange" : "blue"} label={data.risk.new_entries_paused ? "已暂停" : "允许"} /></dd></div><div><dt>连续亏损</dt><dd>{data.risk.consecutive_live_losses} / {data.risk.consecutive_loss_limit}</dd></div><div><dt>今日实盘亏损</dt><dd>{money(data.risk.daily_live_loss_usd)}</dd></div><div><dt>SOL保留</dt><dd>{data.risk.wallet_sol_reserve} SOL</dd></div></dl></article>
    </section>
  </div>;
}
