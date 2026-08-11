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
const algorithmName: Record<string, string> = {
  logistic_regression: "LR",
  hist_gradient_boosting: "HGB",
  xgboost: "XGBoost",
  extra_trees: "ExtraTrees",
  random_forest: "RF"
};

const modelDate = (value: string) => {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(new Date(value));
  const pick = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? "";
  return `${pick("year")}${pick("month")}${pick("day")}`;
};

function pivot<T extends Record<string, unknown>>(rows: T[], category: keyof T, value: keyof T) {
  const result = new Map<string, Record<string, string | number>>();
  rows.forEach((row) => {
    const date = String(row.date);
    const current = result.get(date) ?? { date };
    current[String(row[category])] = Number(row[value] ?? 0);
    result.set(date, current);
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
  const precision = typeof data.model?.metrics?.precision === "number" ? data.model.metrics.precision : null;
  const championName = data.model
    ? `${modelDate(data.model.trained_at)}-${algorithmName[data.model.algorithm] ?? data.model.algorithm}`
    : "尚未训练";
  const equity = pivot(data.equity_curve, "account_kind", "daily_pnl");
  const signalActivity = pivot(data.signal_activity, "profile", "selected");
  const equitySeries = [...new Set(data.equity_curve.map((row) => row.account_kind))];
  const signalSeries = [...new Set(data.signal_activity.map((row) => row.profile))];
  const colors = ["#3668b2", "#c68a2b", "#d56a3a", "#6f7c8c"];

  return (
    <div className="page-stack">
      <section className="page-heading"><div><p className="eyebrow">STATUS OVERVIEW</p><h1>今天的系统状态</h1><p>从采集、模型到资金曲线的一屏运行概览。</p></div><div className="heading-actions"><StatusBadge tone={data.runtime.live_trading_enabled ? "orange" : "blue"} label={data.runtime.live_trading_enabled ? "实盘已启用" : data.runtime.dry_run ? "DRY RUN" : "模拟模式"} /><button className="button button-secondary" onClick={() => void refresh()}>刷新</button></div></section>

      <section className="metric-grid">
        <MetricCard title="实盘收益" value={money(data.live_realized_pnl_usd)} icon={CircleDollarSign} tone={data.live_realized_pnl_usd < 0 ? "orange" : "blue"} />
        <MetricCard title="当前 Champion" value={championName} icon={Bot} tone="gold" />
        <MetricCard title="验证 Precision" value={percent(precision)} context="模型版本记录的时间外结果" icon={Activity} tone="blue" />
        <MetricCard title="当前模拟持仓" value={`${simulationOpen} / ${data.risk.max_open_positions * 3}`} context={`三档账户独立上限；过去7日入选 ${selectedSignals} 条`} icon={Layers3} />
        <MetricCard title="成熟训练样本" value={new Intl.NumberFormat("zh-CN").format(data.dataset.mature ?? 0)} context={`正类率 ${percent(data.dataset.positive_rate)}`} icon={Database} />
      </section>

      <section className="chart-grid">
        <article className="panel chart-panel panel-wide">
          <div className="panel-heading"><div><h2>近7日已实现损益</h2><p>单位：$；按账户类型与平仓日期聚合</p></div><span className="source-note">SQLite · {new Date(data.as_of).toLocaleString("zh-CN")}</span></div>
          {equity.length ? <div className="chart-box"><ResponsiveContainer width="100%" height="100%"><LineChart data={equity} margin={{ top: 8, right: 18, left: 4, bottom: 0 }}><CartesianGrid vertical={false} stroke="#e5e8ec" /><XAxis dataKey="date" tickLine={false} axisLine={{ stroke: "#cbd1d8" }} /><YAxis tickLine={false} axisLine={false} tickFormatter={(v) => `$${v}`} /><Tooltip formatter={(v) => money(Number(v))} /><Legend />{equitySeries.map((series, index) => <Line key={series} dataKey={series} type="monotone" stroke={colors[index % colors.length]} strokeWidth={2} dot={{ r: 2 }} connectNulls />)}</LineChart></ResponsiveContainer></div> : <EmptyState title="暂无损益曲线" detail="完成首笔模拟或实盘交易后，这里会出现按日损益。" />}
        </article>
        <article className="panel chart-panel">
          <div className="panel-heading"><div><h2>近7日入选信号</h2><p>按三档概率阈值分类</p></div></div>
          {signalActivity.length ? <div className="chart-box"><ResponsiveContainer width="100%" height="100%"><BarChart data={signalActivity}><CartesianGrid vertical={false} stroke="#e5e8ec" /><XAxis dataKey="date" tickLine={false} axisLine={{ stroke: "#cbd1d8" }} /><YAxis allowDecimals={false} tickLine={false} axisLine={false} /><Tooltip /><Legend />{signalSeries.map((series, index) => <Bar key={series} dataKey={series} fill={colors[index % colors.length]} radius={[4, 4, 0, 0]} />)}</BarChart></ResponsiveContainer></div> : <EmptyState title="暂无模型信号" detail="模型上线并开始评分后，这里会显示三档入选数量。" />}
        </article>
        <article className="panel health-panel">
          <div className="panel-heading"><div><h2>运行与风险</h2><p>开仓门禁的实时摘要</p></div></div>
          <dl className="health-list"><div><dt>采集器</dt><dd><StatusBadge label={String(data.runtime.collector.state ?? "stopped")} /></dd></div><div><dt>新开仓</dt><dd><StatusBadge tone={data.risk.new_entries_paused ? "orange" : "blue"} label={data.risk.new_entries_paused ? "已暂停" : "允许"} /></dd></div><div><dt>连续亏损</dt><dd>{data.risk.consecutive_live_losses} / {data.risk.consecutive_loss_limit}</dd></div><div><dt>今日实盘亏损</dt><dd>{money(data.risk.daily_live_loss_usd)}</dd></div><div><dt>SOL保留</dt><dd>{data.risk.wallet_sol_reserve} SOL</dd></div></dl>
        </article>
      </section>
    </div>
  );
}

