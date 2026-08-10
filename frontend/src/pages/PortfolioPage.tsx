import { RefreshCcw, ShieldAlert, WalletCards } from "lucide-react";
import { useCallback, useState } from "react";
import { api } from "../api/client";
import type { PreparedAction } from "../api/types";
import { useApiData } from "../api/useApiData";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

const money = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD" }).format(value);
const accountName: Record<string, string> = {
  paper: "平衡档",
  shadow_aggressive: "激进档",
  shadow_conservative: "保守档"
};

export function PortfolioPage() {
  const loader = useCallback(async () => {
    const [portfolio, simulation, history] = await Promise.all([
      api.portfolio(),
      api.simulation(),
      api.simulationHistory()
    ]);
    return { portfolio, simulation, history };
  }, []);
  const { data, loading, error, refresh } = useApiData(loader);
  const [prepared, setPrepared] = useState<PreparedAction | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const prepare = async () => {
    setBusy(true);
    setNotice(null);
    try {
      setPrepared(await api.prepareLiquidation());
      setDialogOpen(true);
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "清仓预览失败");
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    if (!prepared) return;
    setBusy(true);
    try {
      await api.confirmLiquidation(prepared.challenge);
      setDialogOpen(false);
      setNotice("清仓任务已进入持久化队列");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "清仓任务创建失败");
    } finally {
      setBusy(false);
    }
  };
  const resetSimulation = async () => {
    if (!window.confirm("确认开始新的模拟会话？历史记录会保留，三个模拟账户会重置为 1000 USD + 0.1 SOL。")) return;
    setBusy(true);
    setNotice(null);
    try {
      await api.resetSimulation();
      setNotice("新的模拟会话已创建");
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "模拟会话重置失败");
    } finally {
      setBusy(false);
    }
  };

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  const items = data.portfolio.items;
  const open = items.filter((item) => !["closed", "failed"].includes(item.status));
  const history = items.filter((item) => ["closed", "failed"].includes(item.status));
  const simulationOpen = Object.values(data.simulation.accounts).reduce((sum, item) => sum + Number(item.open_positions || 0), 0);

  return (
    <div className="page-stack">
      <section className="page-heading">
        <div>
          <p className="eyebrow">PORTFOLIO</p>
          <h1>模拟账户与持仓</h1>
          <p>模型三档信号独立记账；模拟成交包含滑点、价格冲击、平台费、网络费和失败场景。</p>
        </div>
        <div className="heading-actions">
          <button className="button button-secondary" disabled={busy || simulationOpen > 0} onClick={() => void resetSimulation()}>
            <RefreshCcw size={17} />新建模拟会话
          </button>
          <button className="button button-danger-outline" disabled={busy || !open.length} onClick={() => void prepare()}>
            <ShieldAlert size={17} />一键清仓
          </button>
        </div>
      </section>
      {notice && <div className="inline-notice">{notice}</div>}

      <section className="panel simulation-session-panel">
        <div className="panel-heading">
          <div>
            <h2>当前模拟会话</h2>
            <p className="mono">{data.simulation.session.id}</p>
          </div>
          <StatusBadge tone="blue" label={`开始于 ${new Date(data.simulation.session.started_at).toLocaleString("zh-CN")}`} />
        </div>
        <div className="simulation-account-grid">
          {Object.entries(data.simulation.accounts).map(([key, account]) => (
            <article className="simulation-account" key={key}>
              <div className="simulation-account-title"><WalletCards size={17} /><strong>{accountName[key] ?? key}</strong></div>
              <div className="simulation-account-stats">
                <div><span>现金</span><strong>{money(account.cash_usd)}</strong></div>
                <div><span>SOL 储备</span><strong>{account.sol_fee_reserve.toFixed(6)}</strong></div>
                <div><span>持仓</span><strong>{account.open_positions}</strong></div>
                <div><span>已实现 PnL</span><strong className={account.realized_pnl_usd < 0 ? "number-negative" : "number-positive"}>{money(account.realized_pnl_usd)}</strong></div>
              </div>
            </article>
          ))}
        </div>
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>模拟会话历史</h2><p>新建会话只重置资金账本，不删除旧仓位、成交和收益审计</p></div></div>
        {data.history.items.length ? <div className="table-scroll"><table><thead><tr><th>会话</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>已实现 PnL</th><th>来源</th></tr></thead><tbody>{data.history.items.map((session) => <tr key={session.id}><td className="mono">{session.id.slice(0, 16)}…</td><td><StatusBadge tone={session.status === "active" ? "blue" : "neutral"} label={session.status} /></td><td>{new Date(session.started_at).toLocaleString("zh-CN")}</td><td>{session.ended_at ? new Date(session.ended_at).toLocaleString("zh-CN") : "—"}</td><td className={session.realized_pnl_usd < 0 ? "number-negative" : "number-positive"}>{money(session.realized_pnl_usd)}</td><td>{session.created_reason}</td></tr>)}</tbody></table></div> : <EmptyState title="暂无模拟会话历史" detail="首次模拟会话创建后会在这里形成持久化记录。" />}
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>当前持仓</h2><p>实时 K 线 first-touch 监控；同 bar 止损优先，退出失败保持 closing 并按原触发原因重试</p></div><StatusBadge label={`${open.length} 个批次`} /></div>
        {open.length ? <div className="table-scroll"><table><thead><tr><th>Token</th><th>账户</th><th>档位</th><th>状态</th><th>投入</th><th>入场</th><th>到期</th></tr></thead><tbody>{open.map((item) => <tr key={item.id}><td className="mono">{item.token_address.slice(0, 7)}…{item.token_address.slice(-5)}</td><td>{item.account_kind}</td><td>{item.profile}</td><td><StatusBadge tone={item.status === "manual_intervention" ? "orange" : item.status === "closing" ? "gold" : "blue"} label={item.status} /></td><td>{money(item.invested_usd)}</td><td>{new Date(item.entry_time).toLocaleString("zh-CN")}</td><td>{new Date(item.expires_at).toLocaleString("zh-CN")}</td></tr>)}</tbody></table></div> : <EmptyState title="当前没有持仓" detail="Champion 信号达到对应阈值后，模拟交易批次会显示在这里。" />}
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>最近平仓</h2><p>USD 净收益已计入滑点与平台费；网络费独立从上方 SOL 储备扣减，不做虚构的 SOL→USD 换算</p></div></div>
        {history.length ? <div className="table-scroll"><table><thead><tr><th>Token</th><th>账户</th><th>投入</th><th>退出原因</th><th>净收益</th></tr></thead><tbody>{history.map((item) => <tr key={item.id}><td className="mono">{item.token_address.slice(0, 7)}…</td><td>{item.account_kind}</td><td>{money(item.invested_usd)}</td><td>{item.exit_reason ?? item.status}</td><td className={(item.net_pnl_usd ?? 0) < 0 ? "number-negative" : "number-positive"}>{money(item.net_pnl_usd)}</td></tr>)}</tbody></table></div> : <EmptyState title="尚无平仓记录" detail="这里会保留完整的盈亏与退出原因审计。" />}
      </section>

      <ConfirmDialog open={dialogOpen} title="确认一键清仓" description="系统将暂停所有新买入，并按冻结快照逐仓退出。模拟仓位使用最新市场参考；实盘接口仍保持受控状态。" actionLabel="确认开始清仓" prepared={prepared} busy={busy} onCancel={() => setDialogOpen(false)} onConfirm={() => void confirm()} />
    </div>
  );
}
