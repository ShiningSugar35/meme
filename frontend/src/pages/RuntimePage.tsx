import { Activity, BrainCircuit, PauseCircle, PlayCircle, RefreshCw, ServerCog, ShieldCheck } from "lucide-react";
import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { PreparedAction } from "../api/types";
import { useApiData } from "../api/useApiData";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { MetricCard } from "../components/MetricCard";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const text = (value: unknown, fallback = "—") => value == null || value === "" ? fallback : String(value);
const number = (value: unknown) => typeof value === "number" ? value : Number(value ?? 0);
const workerTone = (state: unknown) => ["degraded", "blocked", "failed"].includes(String(state)) ? "orange" : "neutral";

export function RuntimePage() {
  const loader = useCallback(() => api.runtime(), []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [prepared, setPrepared] = useState<PreparedAction | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const prepareLive = async () => {
    setBusy(true);
    setNotice(null);
    try {
      setPrepared(await api.prepareLive());
      setDialogOpen(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "实盘准备失败");
    } finally {
      setBusy(false);
    }
  };
  const confirmLive = async () => {
    if (!prepared) return;
    setBusy(true);
    try {
      await api.confirmLive(prepared.challenge);
      setDialogOpen(false);
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "实盘确认失败");
    } finally {
      setBusy(false);
    }
  };
  const stopLive = async () => {
    setBusy(true);
    try { await api.stopLive(); await refresh(); } finally { setBusy(false); }
  };
  const resumeRisk = async () => {
    setBusy(true);
    try { await api.resumeRisk(); await refresh(); } finally { setBusy(false); }
  };

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;
  const { runtime, risk } = data;
  const health = runtime.model_health;
  const healthState = text(health.state, "not_evaluated");
  const healthReason = text(health.reason, "尚未形成足够的近期成熟 OOS 样本");
  const rejectionReasons = runtime.collector.rejection_reasons && typeof runtime.collector.rejection_reasons === "object"
    ? Object.entries(runtime.collector.rejection_reasons as Record<string, unknown>).sort((a, b) => number(b[1]) - number(a[1])).slice(0, 8)
    : [];

  const workers = [
    ["Collector", "Trenches → enrichment → 标签补齐", runtime.collector, RefreshCw],
    ["Prediction", "Champion 评分 → 三档模拟信号", runtime.prediction_worker, BrainCircuit],
    ["Paper Market Monitor", "1m K 线 first-touch → 模拟退出", runtime.paper_monitor, Activity],
    ["Training Worker", "持久队列 → 串行训练 → 崩溃恢复", runtime.training_worker, BrainCircuit],
    ["Weekly Trainer", "周日 03:00、启动补跑、有限重试", runtime.scheduler, ServerCog],
    ["Model Health", "7 日 OOS 退化检测 → 安全重训", runtime.model_health_worker, ShieldCheck],
    ["Order Reconciliation", "未决订单只查原单，禁止重复提交", runtime.reconciliation_worker, RefreshCw],
    ["Liquidation", "冻结快照 → 串行退出 → 重启恢复", runtime.liquidation_worker, Activity]
  ] as const;

  return (
    <div className="page-stack">
      <section className="page-heading">
        <div>
          <p className="eyebrow">RUNTIME CONTROL</p>
          <h1>运行监控</h1>
          <p>当前重点运行模拟交易和模型自训练；实盘接口保留，但仍受 DRY_RUN、二次确认和未决订单门禁约束。</p>
        </div>
        <div className="heading-actions">
          {runtime.live_trading_enabled ? (
            <button className="button button-secondary" disabled={busy} onClick={() => void stopLive()}><PauseCircle size={17} />停止实盘新开仓</button>
          ) : (
            <button className="button button-primary" disabled={busy} onClick={() => void prepareLive()}><PlayCircle size={17} />实盘接口</button>
          )}
        </div>
      </section>
      {notice && <div className="inline-notice">{notice}</div>}

      <section className="metric-grid runtime-metrics">
        <MetricCard title="运行环境" value={runtime.app_env} context={runtime.dry_run ? "DRY_RUN 已开启" : "真实执行门禁可用"} icon={ServerCog} tone={runtime.dry_run ? "blue" : "orange"} />
        <MetricCard title="采集器" value={text(runtime.collector.state, "stopped")} context={`最近补标签 ${number(runtime.collector.finalized)} 条`} icon={RefreshCw} tone="gold" />
        <MetricCard title="模型健康" value={healthState} context={healthReason} icon={BrainCircuit} tone={healthState.startsWith("degraded") ? "orange" : "blue"} />
      </section>

      <section className="runtime-grid">
        <article className="panel">
          <div className="panel-heading">
            <div><h2>风险门禁</h2><p>实盘入口保持受控；模拟盘继续独立运行和退出</p></div>
            <StatusBadge tone={risk.new_entries_paused ? "orange" : "blue"} label={risk.new_entries_paused ? "PAUSED" : "ACTIVE"} />
          </div>
          <dl className="health-list">
            <div><dt>暂停原因</dt><dd>{risk.pause_reason ?? "—"}</dd></div>
            <div><dt>实盘持仓</dt><dd>{risk.open_live_positions} / {risk.max_open_positions}</dd></div>
            <div><dt>连续亏损</dt><dd>{risk.consecutive_live_losses} / {risk.consecutive_loss_limit}</dd></div>
            <div><dt>单日亏损上限</dt><dd>{(risk.max_daily_loss_fraction * 100).toFixed(0)}%</dd></div>
            <div><dt>SOL 保留</dt><dd>{risk.wallet_sol_reserve} SOL</dd></div>
          </dl>
          {risk.new_entries_paused && <button className="button button-secondary full-width" disabled={busy} onClick={() => void resumeRisk()}>人工恢复新开仓</button>}
        </article>

        <article className="panel">
          <div className="panel-heading"><div><h2>模型自更新</h2><p>只用成熟 OOS 事实判断，不混用 legacy proxy 与真实美元 PnL</p></div></div>
          <dl className="health-list">
            <div><dt>状态</dt><dd>{healthState}</dd></div>
            <div><dt>成熟预测</dt><dd>{text(health.mature_predictions, "0")}</dd></div>
            <div><dt>入选交易</dt><dd>{text(health.selected_trades, "0")}</dd></div>
            <div><dt>Precision</dt><dd>{typeof health.precision === "number" ? `${(health.precision * 100).toFixed(1)}%` : "—"}</dd></div>
            <div><dt>最近 ROI</dt><dd>{typeof health.recent_roi === "number" ? `${(health.recent_roi * 100).toFixed(2)}%` : "—"}</dd></div>
            <div><dt>训练任务</dt><dd className="mono">{text(health.training_run_id)}</dd></div>
          </dl>
        </article>
      </section>

      <section className="panel">
        <div className="panel-heading"><div><h2>最近采集周期</h2><p>只增加可观测性，不改变任何准入阈值；拒绝原因按本轮候选命中次数排序</p></div><StatusBadge label={text(runtime.collector.mode, "collector")} /></div>
        <dl className="health-list">
          <div><dt>发现候选</dt><dd>{number(runtime.collector.discovered)}</dd></div>
          <div><dt>成功入样</dt><dd>{number(runtime.collector.accepted)}</dd></div>
          <div><dt>规则拒绝</dt><dd>{number(runtime.collector.rejected)}</dd></div>
          <div><dt>未成熟重复</dt><dd>{number(runtime.collector.duplicates)}</dd></div>
          {rejectionReasons.length ? rejectionReasons.map(([reason, count]) => <div key={reason}><dt className="mono">{reason}</dt><dd>{number(count)}</dd></div>) : <div><dt>拒绝原因</dt><dd>—</dd></div>}
        </dl>
      </section>

      <section className="panel">
        <div className="panel-heading"><div><h2>后台工作器</h2><p>关键任务状态均写入 SQLite runtime state，进程重启后可继续恢复</p></div></div>
        <dl className="worker-list">
          {workers.map(([name, detail, state, Icon]) => (
            <div key={name}>
              <span className="worker-icon"><Icon size={18} /></span>
              <span><strong>{name}</strong><small>{detail}</small></span>
              <StatusBadge tone={workerTone(state.state)} label={text(state.state, "stopped")} />
            </div>
          ))}
        </dl>
      </section>

      <ConfirmDialog open={dialogOpen} title="确认启动实盘交易" description="实盘链路当前仅保留接口。只有 DRY_RUN 已关闭、钱包与 GMGN 合约验收完成、未决订单已对账且全部风控通过时才应启用。" actionLabel="确认启动实盘" prepared={prepared} busy={busy} onCancel={() => setDialogOpen(false)} onConfirm={() => void confirmLive()} />
    </div>
  );
}
