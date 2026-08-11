import { ChevronLeft, ChevronRight, Copy, RefreshCcw, ShieldAlert, WalletCards } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import type { PortfolioView, PreparedAction, Profile } from "../api/types";
import { useApiData } from "../api/useApiData";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

type PortfolioMode = "simulation" | "live";

const profileOrder: Profile[] = ["balanced", "aggressive", "conservative"];
const profileName: Record<Profile, string> = {
  balanced: "平衡",
  aggressive: "激进",
  conservative: "保守"
};
const pageSizeOptions = [10, 30, 50, 100];

const money = (value: number | null | undefined) => value == null
  ? "—"
  : new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD" }).format(value);

const compactMoney = (value: number | null | undefined) => value == null
  ? "—"
  : new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: "USD",
      notation: Math.abs(value) >= 10_000 ? "compact" : "standard",
      maximumFractionDigits: 2
    }).format(value);

const beijingTime = (value: string | null | undefined) => {
  if (!value) return "—";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false
  }).formatToParts(new Date(value));
  const pick = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? "";
  return `${pick("month")}/${pick("day")} ${pick("hour")}:${pick("minute")}`;
};

const filterIso = (value: string, endOfMinute = false) => {
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return undefined;
  if (endOfMinute) date.setSeconds(59, 999);
  return date.toISOString();
};

const initialPageSize = () => {
  const stored = Number(window.localStorage.getItem("portfolio.history.pageSize"));
  return pageSizeOptions.includes(stored) ? stored : 30;
};

export function PortfolioPage() {
  const [mode, setMode] = useState<PortfolioMode>("simulation");
  const [profile, setProfile] = useState<Profile>("balanced");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(initialPageSize);
  const [startDraft, setStartDraft] = useState("");
  const [endDraft, setEndDraft] = useState("");
  const [startAt, setStartAt] = useState<string | undefined>();
  const [endAt, setEndAt] = useState<string | undefined>();
  const [switchHost, setSwitchHost] = useState<HTMLElement | null>(null);
  const [prepared, setPrepared] = useState<PreparedAction | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    setSwitchHost(document.getElementById("portfolio-mode-switch"));
    void api.runtime().then(({ runtime }) => {
      setMode(runtime.live_trading_enabled ? "live" : "simulation");
    }).catch(() => undefined);
  }, []);

  const loader = useCallback(async () => {
    const [view, audit] = await Promise.all([
      api.portfolioView({ mode, profile, page, pageSize, startAt, endAt }),
      api.simulationAudit()
    ]);
    return { view, audit };
  }, [endAt, mode, page, pageSize, profile, startAt]);
  const { data, loading, error, refresh } = useApiData(loader);

  useEffect(() => {
    const resolved = data?.view.history.page;
    if (resolved && resolved !== page) setPage(resolved);
  }, [data?.view.history.page, page]);

  const totalOpen = useMemo(
    () => data ? Object.values(data.view.accounts).reduce((sum, item) => sum + Number(item.open_positions || 0), 0) : 0,
    [data]
  );

  const switchMode = () => {
    setMode((current) => current === "simulation" ? "live" : "simulation");
    setPage(1);
    setNotice(null);
  };

  const selectProfile = (next: Profile) => {
    setProfile(next);
    setPage(1);
  };

  const updatePageSize = (value: number) => {
    window.localStorage.setItem("portfolio.history.pageSize", String(value));
    setPageSize(value);
    setPage(1);
  };

  const applyTimeFilter = () => {
    setStartAt(filterIso(startDraft));
    setEndAt(filterIso(endDraft, true));
    setPage(1);
  };

  const clearTimeFilter = () => {
    setStartDraft("");
    setEndDraft("");
    setStartAt(undefined);
    setEndAt(undefined);
    setPage(1);
  };

  const copyToken = async (token: string) => {
    try {
      await navigator.clipboard.writeText(token);
      setNotice("Token 地址已复制");
    } catch {
      setNotice("复制失败，请手动复制 Token 地址");
    }
  };

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
      setPage(1);
      await refresh();
    } catch (cause) {
      setNotice(cause instanceof Error ? cause.message : "模拟会话重置失败");
    } finally {
      setBusy(false);
    }
  };

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;

  const view: PortfolioView = data.view;
  const current = view.current;
  const history = view.history.items;
  const isSimulation = mode === "simulation";

  const modeButton = switchHost ? createPortal(
    <button className="button button-secondary portfolio-mode-button" onClick={switchMode}>
      {isSimulation ? "切换至实盘" : "切换至模拟仓"}
    </button>,
    switchHost
  ) : null;

  return (
    <div className="page-stack">
      {modeButton}
      <section className="page-heading">
        <div>
          <p className="eyebrow">PORTFOLIO</p>
          <h1>{isSimulation ? "模拟账户与持仓" : "实盘账户与持仓"}</h1>
          <p>{isSimulation ? "三档模型独立记账并支持按档位查看持仓与交易历史。" : "实盘视图已按 GMGN Trading API 接口预留；未启用实盘时仅展示持久化账本。"}</p>
        </div>
        <div className="heading-actions">
          {isSimulation && (
            <button className="button button-secondary" disabled={busy || totalOpen > 0} onClick={() => void resetSimulation()}>
              <RefreshCcw size={17} />新建模拟会话
            </button>
          )}
          <button className="button button-danger-outline" disabled={busy || totalOpen === 0} onClick={() => void prepare()}>
            <ShieldAlert size={17} />一键清仓
          </button>
        </div>
      </section>
      {notice && <div className="inline-notice">{notice}</div>}

      <section className="panel simulation-session-panel">
        <div className="panel-heading">
          <div>
            <h2>{isSimulation ? "当前模拟会话" : "当前实盘账户"}</h2>
            <p className="mono">{isSimulation ? view.session?.id : "GMGN Trading API · live ledger"}</p>
          </div>
          <StatusBadge
            tone={isSimulation || view.live_trading_enabled ? "blue" : "neutral"}
            label={isSimulation ? `开始于 ${beijingTime(view.session?.started_at)}` : view.live_trading_enabled ? "实盘运行中" : "实盘未启动"}
          />
        </div>
        <div className="simulation-account-grid">
          {profileOrder.map((key) => {
            const account = view.accounts[key] ?? {};
            const selected = profile === key;
            return (
              <button
                type="button"
                className={`simulation-account simulation-account-button ${selected ? "simulation-account-selected" : ""}`}
                key={key}
                onClick={() => selectProfile(key)}
                aria-pressed={selected}
              >
                <div className="simulation-account-title">
                  <WalletCards size={17} />
                  <strong>{profileName[key]}</strong>
                  {selected && <StatusBadge tone="blue" label="当前" />}
                </div>
                <div className="simulation-account-stats">
                  <div><span>现金</span><strong>{money(account.cash_usd)}</strong></div>
                  <div><span>SOL 储备</span><strong>{account.sol_fee_reserve == null ? "—" : Number(account.sol_fee_reserve).toFixed(6)}</strong></div>
                  <div><span>持仓</span><strong>{Number(account.open_positions || 0)}</strong></div>
                  <div><span>已实现 PnL</span><strong className={Number(account.realized_pnl_usd || 0) < 0 ? "number-negative" : "number-positive"}>{money(Number(account.realized_pnl_usd || 0))}</strong></div>
                </div>
              </button>
            );
          })}
        </div>
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>交易审计</h2></div></div>
        {isSimulation ? (
          data.audit.items.length ? (
            <div className="table-scroll">
              <table>
                <thead><tr><th>档位</th><th>状态</th><th>开始时间</th><th>结束时间</th><th>交易</th><th>已实现 PnL</th><th>来源</th></tr></thead>
                <tbody>{data.audit.items.map((item) => (
                  <tr key={`${item.session_id}-${item.profile}`}>
                    <td>{profileName[item.profile]}</td>
                    <td><StatusBadge tone={item.status === "active" ? "blue" : "neutral"} label={item.status} /></td>
                    <td>{beijingTime(item.started_at)}</td>
                    <td>{beijingTime(item.ended_at)}</td>
                    <td>{item.closed_positions} / {item.positions}</td>
                    <td className={item.realized_pnl_usd < 0 ? "number-negative" : "number-positive"}>{money(item.realized_pnl_usd)}</td>
                    <td>{item.created_reason}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          ) : <EmptyState title="暂无交易审计" detail="首次模拟会话创建后会按平衡、激进、保守三档分别形成审计记录。" />
        ) : <EmptyState title="暂无实盘交易审计" detail="GMGN Trading API 成交回执接入后，实盘审计会按相同布局展示。" />}
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>当前持仓</h2><p>实时 K 线监控</p></div><StatusBadge label={`${current.length} 个批次`} /></div>
        {current.length ? (
          <div className="table-scroll">
            <table>
              <thead><tr><th>Token</th><th>状态</th><th>投入</th><th>入场</th><th>当前流动性</th><th>当前市值</th><th>到期</th></tr></thead>
              <tbody>{current.map((item) => (
                <tr key={item.id}>
                  <td className="mono token-cell" title={item.token_address}>
                    <span>{item.token_address.slice(0, 7)}…{item.token_address.slice(-5)}</span>
                    <button className="token-copy-button" onClick={() => void copyToken(item.token_address)}><Copy size={11} />复制</button>
                  </td>
                  <td><StatusBadge tone={item.status === "manual_intervention" ? "orange" : item.status === "closing" ? "gold" : "blue"} label={item.status} /></td>
                  <td>{money(item.invested_usd)}</td>
                  <td>{beijingTime(item.entry_time)}</td>
                  <td>{compactMoney(item.current_liquidity_usd)}</td>
                  <td>{compactMoney(item.current_market_cap_usd)}</td>
                  <td>{beijingTime(item.expires_at)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <EmptyState title="当前没有持仓" detail={`${profileName[profile]}档当前没有${isSimulation ? "模拟" : "实盘"}持仓。`} />}
      </section>

      <section className="panel table-panel">
        <div className="panel-heading"><div><h2>交易历史</h2><p>已完成平仓的历史记录，计入滑点和平台费。</p></div><StatusBadge label={`${view.history.total} 条`} /></div>
        <div className="history-toolbar">
          <label>开始时间<input type="datetime-local" value={startDraft} onChange={(event) => setStartDraft(event.target.value)} /></label>
          <label>结束时间<input type="datetime-local" value={endDraft} onChange={(event) => setEndDraft(event.target.value)} /></label>
          <button className="button button-secondary" onClick={applyTimeFilter}>筛选</button>
          {(startAt || endAt) && <button className="button button-secondary" onClick={clearTimeFilter}>清除</button>}
          <label className="page-size-control">每页
            <select value={pageSize} onChange={(event) => updatePageSize(Number(event.target.value))}>
              {pageSizeOptions.map((size) => <option value={size} key={size}>{size} 行</option>)}
            </select>
          </label>
        </div>
        {history.length ? (
          <>
            <div className="table-scroll">
              <table>
                <thead><tr><th>Token</th><th>买入时间</th><th>投入</th><th>退出原因</th><th>净收益</th></tr></thead>
                <tbody>{history.map((item) => (
                  <tr key={item.id}>
                    <td className="mono token-cell" title={item.token_address}>
                      <span>{item.token_address.slice(0, 7)}…{item.token_address.slice(-5)}</span>
                      <button className="token-copy-button" onClick={() => void copyToken(item.token_address)}><Copy size={11} />复制</button>
                    </td>
                    <td>{beijingTime(item.entry_time)}</td>
                    <td>{money(item.invested_usd)}</td>
                    <td>{item.exit_reason ?? item.status}</td>
                    <td className={(item.net_pnl_usd ?? 0) < 0 ? "number-negative" : "number-positive"}>{money(item.net_pnl_usd)}</td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
            <div className="pagination-bar">
              <span>共 {view.history.total} 条</span>
              <button className="icon-button pagination-button" disabled={view.history.page <= 1} onClick={() => setPage(Math.max(1, view.history.page - 1))} aria-label="上一页"><ChevronLeft size={16} /></button>
              <label>第<input type="number" min={1} max={view.history.total_pages} value={view.history.page} onChange={(event) => setPage(Math.min(view.history.total_pages, Math.max(1, Number(event.target.value) || 1)))} />页</label>
              <span>/ {view.history.total_pages}</span>
              <button className="icon-button pagination-button" disabled={view.history.page >= view.history.total_pages} onClick={() => setPage(Math.min(view.history.total_pages, view.history.page + 1))} aria-label="下一页"><ChevronRight size={16} /></button>
            </div>
          </>
        ) : <EmptyState title="尚无平仓记录" detail="当前档位和时间范围内没有已完成平仓的交易。" />}
      </section>

      <ConfirmDialog open={dialogOpen} title="确认一键清仓" description="系统将暂停所有新买入，并按冻结快照逐仓退出。模拟仓位使用最新市场参考；实盘接口仍保持受控状态。" actionLabel="确认开始清仓" prepared={prepared} busy={busy} onCancel={() => setDialogOpen(false)} onConfirm={() => void confirm()} />
    </div>
  );
}
