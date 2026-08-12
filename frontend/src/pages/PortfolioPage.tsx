import { ChevronLeft, ChevronRight, Copy, RefreshCcw, ShieldAlert, WalletCards } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import type { PortfolioView, PreparedAction, StrategyKey } from "../api/types";
import { useApiData } from "../api/useApiData";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { PageError, PageLoading } from "../components/PageState";
import { StatusBadge } from "../components/StatusBadge";

type PortfolioMode = "simulation" | "live";
const strategyOrder: StrategyKey[] = ["model_1", "model_2", "model_3", "rules_only"];
const pageSizeOptions = [10, 30, 50, 100];
const money = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol" }).format(value);
const compactMoney = (value: number | null | undefined) => value == null ? "—" : new Intl.NumberFormat("zh-CN", { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol", notation: Math.abs(value) >= 10_000 ? "compact" : "standard", maximumFractionDigits: 2 }).format(value);
const priceMultiple = (currentPrice: number | null | undefined, entryPrice: number | null | undefined) => currentPrice == null || entryPrice == null || entryPrice <= 0 ? "—" : `${(currentPrice / entryPrice).toFixed(2)}x`;
const pct = (value: number | null | undefined) => value == null || !Number.isFinite(value) ? "—" : `${(value * 100).toFixed(1)}%`;
const beijingTime = (value: string | null | undefined) => {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(value));
};
const failureReasonName: Record<string, string> = { no_route: "无可用卖出路由", quote_failed: "卖出报价失败", network: "网络请求失败", api: "交易 API 失败", rate_limit: "接口限流", chain_rejected: "链上拒绝", insufficient_funds: "余额不足", risk_rejected: "风控拒绝", sol_usd_price_unavailable: "SOL/USD 手续费汇率暂不可用", order_failed: "链上订单失败", order_expired: "订单过期" };
const exitReasonName: Record<string, string> = { stop_loss_0_9x: "止损 0.9x", take_profit_1_6x: "止盈 1.6x", timeout_2h: "持仓满 2 小时", liquidate_all: "一键清仓" };
const reasonText = (exitReason: string | null, sellFailureReason?: string | null) => sellFailureReason ? (failureReasonName[sellFailureReason] ?? sellFailureReason) : (exitReason ? (exitReasonName[exitReason] ?? exitReason) : "—");
const filterIso = (value: string, endOfMinute = false) => {
  if (!value) return undefined;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return undefined;
  const [, year, month, day, hour, minute] = match;
  return new Date(Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour) - 8, Number(minute), endOfMinute ? 59 : 0, endOfMinute ? 999 : 0)).toISOString();
};
const initialPageSize = () => {
  const stored = Number(window.localStorage.getItem("portfolio.history.pageSize"));
  return pageSizeOptions.includes(stored) ? stored : 30;
};

export function PortfolioPage() {
  const [mode, setMode] = useState<PortfolioMode>("simulation");
  const [strategy, setStrategy] = useState<StrategyKey>("model_1");
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
    void api.runtime().then(({ runtime }) => setMode(runtime.live_trading_enabled ? "live" : "simulation")).catch(() => undefined);
  }, []);

  const loader = useCallback(async () => {
    const [view, audit] = await Promise.all([api.portfolioView({ mode, strategy, page, pageSize, startAt, endAt }), api.simulationAudit()]);
    return { view, audit };
  }, [endAt, mode, page, pageSize, startAt, strategy]);
  const { data, loading, error, refresh } = useApiData(loader);

  useEffect(() => {
    const resolved = data?.view.history.page;
    if (resolved && resolved !== page) setPage(resolved);
  }, [data?.view.history.page, page]);

  const totalOpen = useMemo(() => {
    if (!data) return 0;
    if (mode === "live") return Number(data.view.account.open_positions || 0);
    return Object.values(data.view.accounts).reduce((sum, item) => sum + Number(item?.open_positions || 0), 0);
  }, [data, mode]);

  const switchMode = () => { setMode((current) => current === "simulation" ? "live" : "simulation"); setPage(1); setNotice(null); };
  const selectStrategy = (next: StrategyKey) => { setStrategy(next); setPage(1); };
  const updatePageSize = (value: number) => { window.localStorage.setItem("portfolio.history.pageSize", String(value)); setPageSize(value); setPage(1); };
  const applyTimeFilter = () => { setStartAt(filterIso(startDraft)); setEndAt(filterIso(endDraft, true)); setPage(1); };
  const clearTimeFilter = () => { setStartDraft(""); setEndDraft(""); setStartAt(undefined); setEndAt(undefined); setPage(1); };
  const copyToken = async (token: string) => { try { await navigator.clipboard.writeText(token); setNotice("Token 地址已复制"); } catch { setNotice("复制失败，请手动复制 Token 地址"); } };

  const prepare = async () => { setBusy(true); setNotice(null); try { setPrepared(await api.prepareLiquidation(mode)); setDialogOpen(true); } catch (cause) { setNotice(cause instanceof Error ? cause.message : "清仓预览失败"); } finally { setBusy(false); } };
  const confirm = async () => { if (!prepared) return; setBusy(true); try { await api.confirmLiquidation(prepared.challenge, mode); setDialogOpen(false); setNotice("清仓任务已进入持久化队列"); await refresh(); } catch (cause) { setNotice(cause instanceof Error ? cause.message : "清仓任务创建失败"); } finally { setBusy(false); } };
  const resetSimulation = async () => { if (!window.confirm("确认开始新的模拟会话？历史记录会保留，四个策略账户都会重置为 $1000 USD 单一账本。")) return; setBusy(true); try { await api.resetSimulation(); setNotice("新的四策略模拟会话已创建"); setPage(1); await refresh(); } catch (cause) { setNotice(cause instanceof Error ? cause.message : "模拟会话重置失败"); } finally { setBusy(false); } };

  if (loading && !data) return <PageLoading />;
  if (error || !data) return <PageError message={error ?? "无数据"} retry={refresh} />;
  const view: PortfolioView = data.view;
  const isSimulation = mode === "simulation";
  const modeButton = switchHost ? createPortal(<button className="button button-secondary portfolio-mode-button" onClick={switchMode}>{isSimulation ? "切换至实盘" : "切换至模拟仓"}</button>, switchHost) : null;
  const strategyLabel = view.strategy_info?.label ?? strategy;

  return <div className="page-stack">
    {modeButton}
    <section className="page-heading"><div><p className="eyebrow">PORTFOLIO</p><h1>{isSimulation ? "四策略模拟对比" : "实盘账户与持仓"}</h1><p>{isSimulation ? "三个 Top 模型与纯规则策略独立记账、同场对比。" : "实盘接口保持受控停放；当前页面展示持久化账本。"}</p></div><div className="heading-actions">{isSimulation && <button className="button button-secondary" disabled={busy || totalOpen > 0} onClick={() => void resetSimulation()}><RefreshCcw size={17} />新建模拟会话</button>}<button className="button button-danger-outline" disabled={busy || totalOpen === 0} onClick={() => void prepare()}><ShieldAlert size={17} />一键清仓</button></div></section>
    {notice && <div className="inline-notice">{notice}</div>}

    <section className="panel simulation-session-panel"><div className="panel-heading"><div><h2>{isSimulation ? "当前策略" : "当前用于实盘的量化模型"}</h2><p className="mono">{strategy === "rules_only" ? "不用模型 · 仅规则筛选" : strategyLabel}</p></div><StatusBadge tone={isSimulation || view.live_trading_enabled ? "blue" : "neutral"} label={isSimulation ? "模拟运行中" : view.live_trading_enabled ? "实盘运行中" : "实盘未启动"} /></div>
      <div className="simulation-account-grid">{strategyOrder.map((key) => { const info = view.strategies.find((item) => item.strategy_key === key); const account = view.accounts[key] ?? {}; const selected = strategy === key; return <button type="button" className={`simulation-account simulation-account-button ${selected ? "simulation-account-selected" : ""}`} key={key} onClick={() => selectStrategy(key)} aria-pressed={selected}><div className="simulation-account-title"><WalletCards size={17} /><strong>{info?.label ?? key}</strong>{selected && <StatusBadge tone="blue" label="当前" />}</div><div className="simulation-account-stats"><div><span title="可用现金；当前持仓本金已从余额扣除">当前余额</span><strong>{money(account.cash_usd)}</strong></div><div><span>持仓本金</span><strong>{money(Number(account.invested_usd || 0))}</strong></div><div><span title="完整平仓后的净利润；平台费、网络费已扣除，滑点已体现在实际成交价">已实现收益</span><strong className={Number(account.realized_pnl_usd || 0) < 0 ? "number-negative" : "number-positive"}>{money(Number(account.realized_pnl_usd || 0))}</strong></div><div><span>累计手续费</span><strong>{money(Number(account.total_fees_usd || 0))}</strong></div><div><span>持仓数</span><strong>{Number(account.open_positions || 0)}</strong></div><div><span title="当前模型上线以来已经结束的完整交易；终态卖出失败同样计入">交易数</span><strong>{Number(account.trade_count || 0)}</strong></div><div><span title="当前模型上线后的成熟样本：TP / 所有预测为正的样本">Precision</span><strong>{pct(account.precision)}</strong></div><div><span title="当前模型上线后的成熟样本：TP / 所有真实正类样本；不用模型因全部预测为正，存在正类时 Recall=100%">Recall</span><strong>{pct(account.recall)}</strong></div></div></button>; })}</div>
      {isSimulation && <div className="inline-notice">费用明细 · 平台费 {money(Number(view.accounts[strategy]?.platform_fee_usd || 0))} · 网络费 {money(Number(view.accounts[strategy]?.network_fee_usd || 0))}（原始 {Number(view.accounts[strategy]?.network_fee_sol || 0).toFixed(6)} SOL）· 滑点损耗 {money(Number(view.accounts[strategy]?.slippage_cost_usd || 0))}</div>}
    </section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>交易审计</h2></div></div>{isSimulation && data.audit.items.length ? <div className="table-scroll"><table><thead><tr><th>模型</th><th>状态</th><th>买入时间</th><th>卖出时间</th><th>交易</th><th>已实现 PnL</th></tr></thead><tbody>{data.audit.items.filter((item) => item.status === "active").map((item) => <tr key={`${item.session_id}-${item.strategy_key}`}><td>{item.model_label}</td><td><StatusBadge tone={item.status === "active" ? "blue" : "neutral"} label={item.status} /></td><td>{beijingTime(item.first_entry_time)}</td><td>{beijingTime(item.last_exit_time)}</td><td>{item.closed_positions} / {item.positions}</td><td className={item.realized_pnl_usd < 0 ? "number-negative" : "number-positive"}>{money(item.realized_pnl_usd)}</td></tr>)}</tbody></table></div> : <EmptyState title="暂无交易审计" detail="新模拟会话会为 Top 3 模型和不用模型分别记录收益。" />}</section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>当前持仓</h2><p>{strategyLabel}</p></div><StatusBadge label={`${view.current.length} 个批次`} /></div>{view.current.length ? <div className="table-scroll"><table><thead><tr><th>Token</th><th>状态</th><th>投入</th><th>入场</th><th>当前流动性</th><th>市值</th><th>当前涨幅</th><th>到期</th></tr></thead><tbody>{view.current.map((item) => <tr key={item.id}><td className="mono token-cell" title={item.token_address}><span>{item.token_address.slice(0, 7)}…{item.token_address.slice(-5)}</span><button className="token-copy-button" onClick={() => void copyToken(item.token_address)}><Copy size={11} />复制</button></td><td><StatusBadge tone={item.status === "manual_intervention" ? "orange" : item.status === "closing" ? "gold" : "blue"} label={item.status} /></td><td>{money(item.invested_usd)}</td><td>{beijingTime(item.entry_time)}</td><td>{compactMoney(item.current_liquidity_usd)}</td><td>{compactMoney(item.current_market_cap_usd)}</td><td>{priceMultiple(item.current_price, item.entry_price)}</td><td>{beijingTime(item.expires_at)}</td></tr>)}</tbody></table></div> : <EmptyState title="当前没有持仓" detail={`${strategyLabel} 当前没有${isSimulation ? "模拟" : "实盘"}持仓。`} />}</section>

    <section className="panel table-panel"><div className="panel-heading"><div><h2>交易历史</h2><p>已完成平仓的历史记录，净收益已计入滑点、平台费和按手续费发生时 SOL/USD 折算的网络费。</p></div><StatusBadge label={`${view.history.total} 条`} /></div><div className="history-toolbar"><label>开始时间<input type="datetime-local" value={startDraft} onChange={(e) => setStartDraft(e.target.value)} /></label><label>结束时间<input type="datetime-local" value={endDraft} onChange={(e) => setEndDraft(e.target.value)} /></label><button className="button button-secondary" onClick={applyTimeFilter}>筛选</button>{(startAt || endAt) && <button className="button button-secondary" onClick={clearTimeFilter}>清除</button>}<label className="page-size-control">每页<select value={pageSize} onChange={(e) => updatePageSize(Number(e.target.value))}>{pageSizeOptions.map((size) => <option value={size} key={size}>{size} 行</option>)}</select></label></div>
      {view.history.items.length ? <><div className="table-scroll"><table><thead><tr><th>Token</th><th>launch-pad</th><th>买入时间</th><th>平仓时间</th><th>投入</th><th>原因</th><th>净收益</th></tr></thead><tbody>{view.history.items.map((item) => <tr key={item.id}><td className="mono token-cell" title={item.token_address}><span>{item.token_address.slice(0, 7)}…{item.token_address.slice(-5)}</span><button className="token-copy-button" onClick={() => void copyToken(item.token_address)}><Copy size={11} />复制</button></td><td>{item.launchpad ?? "—"}</td><td>{beijingTime(item.entry_time)}</td><td>{item.sell_failed ? "卖出失败" : beijingTime(item.exit_time)}</td><td>{money(item.invested_usd)}</td><td>{reasonText(item.exit_reason, item.sell_failure_reason)}</td><td className={(item.net_pnl_usd ?? 0) < 0 ? "number-negative" : "number-positive"}>{money(item.net_pnl_usd)}</td></tr>)}</tbody></table></div><div className="pagination-bar"><span>共 {view.history.total} 条</span><button className="icon-button pagination-button" disabled={view.history.page <= 1} onClick={() => setPage(Math.max(1, view.history.page - 1))} aria-label="上一页"><ChevronLeft size={16} /></button><label>第<input type="number" min={1} max={view.history.total_pages} value={view.history.page} onChange={(e) => setPage(Math.min(view.history.total_pages, Math.max(1, Number(e.target.value) || 1)))} />页</label><span>/ {view.history.total_pages}</span><button className="icon-button pagination-button" disabled={view.history.page >= view.history.total_pages} onClick={() => setPage(Math.min(view.history.total_pages, view.history.page + 1))} aria-label="下一页"><ChevronRight size={16} /></button></div></> : <EmptyState title="尚无平仓记录" detail="当前策略和时间范围内没有已完成平仓的交易。" />}
    </section>
    <ConfirmDialog open={dialogOpen} title="确认一键清仓" description="系统将暂停新买入，并按冻结快照逐仓退出。" actionLabel="确认开始清仓" prepared={prepared} busy={busy} onCancel={() => setDialogOpen(false)} onConfirm={() => void confirm()} />
  </div>;
}
