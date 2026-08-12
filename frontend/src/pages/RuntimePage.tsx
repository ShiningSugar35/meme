import { Activity, BrainCircuit, PauseCircle, PlayCircle, Radio, RefreshCw, ServerCog, ShieldCheck, Terminal } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { CollectorEvent, PreparedAction } from "../api/types";
import { useApiData } from "../api/useApiData";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { MetricCard } from "../components/MetricCard";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const text = (value: unknown, fallback = "—") => value == null || value === "" ? fallback : String(value);
const number = (value: unknown) => typeof value === "number" ? value : Number(value ?? 0);
const workerTone = (state: unknown) => ["degraded", "blocked", "failed"].includes(String(state)) ? "orange" : "neutral";
const lifecycleLabel = (value: unknown) => ({
  new_creation: "New Creation",
  near_completion: "Near Completion"
}[String(value)] ?? "Collector");
const eventTime = (value: string) => new Date(value).toLocaleTimeString("zh-CN", { hour12: false });

export function RuntimePage() {
  const loader = useCallback(() => api.runtime(), []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [prepared, setPrepared] = useState<PreparedAction | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [collectorEvents, setCollectorEvents] = useState<CollectorEvent[]>([]);
  const [eventError, setEventError] = useState<string | null>(null);
  const [autoFollow, setAutoFollow] = useState(true);
  const terminalRef = useRef<HTMLDivElement | null>(null);

  const refreshEvents = useCallback(async () => {
    try {
      const result = await api.collectorEvents(220);
      setCollectorEvents(result.items);
      setEventError(null);
    } catch (cause) {
      setEventError(cause instanceof Error ? cause.message : "实时日志读取失败");
    }
  }, []);

  useEffect(() => {
    void refreshEvents();
    const timer = window.setInterval(() => void refreshEvents(), 1500);
    return () => window.clearInterval(timer);
  }, [refreshEvents]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!autoFollow || !terminalRef.current) return;
    terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
  }, [collectorEvents, autoFollow]);

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
  const rawTypeStats = runtime.collector.type_stats && typeof runtime.collector.type_stats === "object"
    ? runtime.collector.type_stats as Record<string, Record<string, unknown>>
    : {};
  const lifecycleStats = ["new_creation", "near_completion"].map((key) => ({
    key,
    label: lifecycleLabel(key),
    stats: rawTypeStats[key] ?? {}
  }));

  const workers = [
    ["Collector", "Trenches → enrichment → 标签补齐", runtime.collector, RefreshCw],
    ["Prediction", "Champion 评分 → 三档模拟信号", runtime.prediction_worker, BrainCircuit],
    ["Paper Market Monitor", "1m K 线 first-touch → 模拟退出", runtime.paper_monitor, Activity],
    ["Training Worker", "持久队列 → 串行训练 → 崩溃恢复", runtime.training_worker, BrainCircuit],
    ["Model Trainer", "16:00 冻结模型新开仓 → 17:00 日/周训练 → 空仓换模", runtime.scheduler, ServerCog],
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

      <section className="panel collector-cycle-panel">
        <div className="panel-heading">
          <div>
            <h2>双生命周期采集</h2>
            <p>每轮固定按 New Creation → Near Completion 扫描；Completed 已永久退出采样、训练和交易；单类请求上限 {number(runtime.collector.requested_limit_per_type) || 80}</p>
          </div>
          <StatusBadge tone={text(runtime.collector.cycle_state) === "in_progress" ? "gold" : "blue"} label={text(runtime.collector.cycle_state, "idle")} />
        </div>
        <div className="collector-lifecycle-grid">
          {lifecycleStats.map(({ key, label, stats }) => (
            <article className="collector-lifecycle-card" key={key}>
              <div className="collector-lifecycle-title"><span className="collector-live-dot" /><strong>{label}</strong></div>
              <div className="collector-lifecycle-count">{number(stats.returned)}</div>
              <span className="collector-lifecycle-caption">最近完整周期返回</span>
              <div className="collector-lifecycle-meta">
                <span>入样 <b>{number(stats.accepted)}</b></span>
                <span>拒绝 <b>{number(stats.rejected)}</b></span>
                <span>重复 <b>{number(stats.duplicates)}</b></span>
              </div>
            </article>
          ))}
        </div>
        <div className="collector-summary-strip">
          <span>发现 <strong>{number(runtime.collector.discovered)}</strong></span>
          <span>入样 <strong>{number(runtime.collector.accepted)}</strong></span>
          <span>拒绝 <strong>{number(runtime.collector.rejected)}</strong></span>
          <span>重复 <strong>{number(runtime.collector.duplicates)}</strong></span>
          <span>耗时 <strong>{number(runtime.collector.last_cycle_duration_seconds).toFixed(1)}s</strong></span>
        </div>
        <div className="collector-reasons">
          <span className="collector-reasons-label">TOP REJECTION REASONS</span>
          <div>
            {rejectionReasons.length ? rejectionReasons.map(([reason, count]) => <span className="collector-reason-chip" key={reason}><code>{reason}</code><b>{number(count)}</b></span>) : <span className="muted-cell">暂无拒绝原因</span>}
          </div>
        </div>
      </section>

      <section className="panel collector-console-panel">
        <div className="collector-console-header">
          <div className="collector-console-heading">
            <span className="collector-console-icon"><Terminal size={17} /></span>
            <div><h2>后端实时采集日志</h2><p>1.5 秒轮询后端事件缓冲区，展示拉池、二筛、入样、标签补齐与异常</p></div>
          </div>
          <div className="collector-console-controls">
            <span className="collector-stream-status"><Radio size={13} /> LIVE</span>
            <button className={`collector-follow-button ${autoFollow ? "active" : ""}`} onClick={() => setAutoFollow((value) => !value)}>{autoFollow ? "自动跟随" : "暂停跟随"}</button>
          </div>
        </div>
        {eventError && <div className="collector-console-error">{eventError}</div>}
        <div className="collector-console" ref={terminalRef}>
          {collectorEvents.length ? collectorEvents.map((event) => {
            const tokenType = event.details?.token_type;
            return (
              <div className={`collector-log-row collector-log-${event.level}`} key={event.id}>
                <time>{eventTime(event.created_at)}</time>
                <span className="collector-log-level">{event.level.toUpperCase()}</span>
                <span className="collector-log-source">{lifecycleLabel(tokenType)}</span>
                <span className="collector-log-message">{event.message}</span>
              </div>
            );
          }) : <div className="collector-log-empty">等待 Collector 产生实时事件…</div>}
        </div>
        <div className="collector-console-footer">
          <span>缓存 {collectorEvents.length}/250 条</span>
          <span>Cycle <code>{text(runtime.collector.cycle_id)}</code></span>
          <span>最后完整周期 {text(runtime.collector.last_cycle_at)}</span>
        </div>
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
