import { useEffect, useRef, useState } from 'react';
import { api, FilterStats, PortfolioRow, RuntimeStatus, RuleFailItem, EndpointHealthItem, FieldHealthItem, PlatformHealthItem, DataSourceHealth, getTradeEventsLedger, TradeEventsLedgerRow } from '../api/client';

type AccountTab = 'LIVE' | 'SIM';
type PortfolioTab = 'CURRENT' | 'HISTORY';

function usd(value: unknown) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : '$0.00';
}

function pct(value: unknown) {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '-';
}

function ruleLabel(name: string): string {
  const map: Record<string, string> = {
    type_new_creation: 'type≠new_creation',
    min_liquidity_usd: '流动性不足',
    top_10_holder_rate_range: 'top10持仓比超限',
    renounced_mint: 'mint未renounce',
    renounced_freeze_account: 'freeze未renounce',
    rug_ratio: 'rug比例超标',
    entrapment_ratio: 'entrapment超标',
    is_wash_trading: '疑似wash trading',
    rat_trader_amount_rate: 'rat trader超标',
    suspected_insider_hold_rate: '疑似内幕持仓',
    bundler_trader_amount_rate: 'bundler比例超标',
    fresh_wallet_rate: '新钱包比例超标',
    sell_tax: 'sell_tax超标',
    has_at_least_one_social: '缺少社交(仅x<0.15)',
    burn_status: 'burn状态不符',
    sniper_count: 'sniper数量超标',
    platform: '平台不在白名单',
    top1_holder: 'TOP1持仓超标',
    swaps_5m_scaled: '过去一小时交易数',
    price_change_1h: '1h价格涨幅不足',
    smart_degen: '聪明钱持仓过少',
  };
  return map[name] || name;
}

function exitReasonLabel(code: string): string {
  const map: Record<string, string> = {
    HARD_TP_160: '止盈160%',
    HARD_TP_210: '止盈210%全平',
    HARD_SL_45: '硬止损45%全平',
    HARD_SL_70: '硬止损70%一半',
    PRICE_API_UNAVAILABLE_EXIT_PENDING: '价格接口不可用待平',
    DUST_FORCE_EXIT: '尘埃仓强制清仓',
    HOLDER_RISK: '持仓风险',
    MANUAL: '手动平仓',
  };
  return map[code] || code;
}

function severityDot(sev: string) {
  if (sev === 'critical') return <span title="严重" className="tag live">!</span>;
  if (sev === 'warn') return <span title="警告" style={{display:'inline-flex',borderRadius:999,background:'#6e7a1b',color:'#d8e389',padding:'3px 9px',fontSize:12,fontWeight:700}}>!</span>;
  return <span title="正常" className="tag sim">&#10003;</span>;
}

function rateFmt(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function pnlStyle(value: number | null | undefined): React.CSSProperties {
  if (value == null) return {};
  return { color: value >= 0 ? '#22c55e' : '#ef4444' };
}

export default function Portfolio() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [portfolioTab, setPortfolioTab] = useState<PortfolioTab>('CURRENT');
  const [tab, setTab] = useState<AccountTab>('LIVE');
  const [rows, setRows] = useState<PortfolioRow[]>([]);
  const [fstats, setFstats] = useState<FilterStats | null>(null);
  const [message, setMessage] = useState('');
  const [historyRows, setHistoryRows] = useState<TradeEventsLedgerRow[]>([]);
  const [historyAccount, setHistoryAccount] = useState<string>('ALL');
  const tabRef = useRef<AccountTab>('LIVE');

  useEffect(() => { tabRef.current = tab; }, [tab]);

  const load = async (preferred?: AccountTab) => {
    const runtime = await api.getRuntimeStatus();
    setStatus(runtime);
    const nextTab = preferred ?? (runtime.user_mode === 'SIM_TEST' ? 'SIM' : 'LIVE');
    setTab(nextTab);

    const data = await api.getPortfolio(nextTab);
    setRows(data);
  };

  const loadHistory = async (account = historyAccount) => {
    try {
      const data = await getTradeEventsLedger(account === 'ALL' ? 'ALL' : account);
      setHistoryRows(data);
    } catch (e) {
      setMessage((e as Error).message);
    }
  };

  const loadFilterStats = async () => {
    try {
      const stats = await api.getFilterStats();
      setFstats(stats);
    } catch {
      // stats load is best-effort
    }
  };

  useEffect(() => {
    load().catch((e) => setMessage(e.message));
    loadHistory();
    loadFilterStats();
    const timer = window.setInterval(() => load(tabRef.current).catch(() => undefined), 5000);
    const historyTimer = window.setInterval(() => loadHistory(), 30000);
    const statsTimer = window.setInterval(loadFilterStats, 60000);
    return () => { window.clearInterval(timer); window.clearInterval(historyTimer); window.clearInterval(statsTimer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchTab = async (next: AccountTab) => {
    setTab(next);
    tabRef.current = next;
    setMessage('');
    try {
      setRows(await api.getPortfolio(next));
    } catch (e) {
      setMessage((e as Error).message);
    }
  };

  const switchPortfolioTab = (next: PortfolioTab) => {
    setPortfolioTab(next);
    if (next === 'HISTORY') loadHistory();
  };

  const switchHistoryAccount = (account: string) => {
    setHistoryAccount(account);
    loadHistory(account);
  };

  const currentRows = rows.filter((r) => r.status !== 'CLOSED' && (r.remaining == null || r.remaining > 0));

  const latestTrench = fstats?.trench_history?.[(fstats?.trench_history?.length ?? 0) - 1];
  const rawLast = latestTrench?.raw_count ?? latestTrench?.count ?? 0;
  const uniqueLast = latestTrench?.unique_count ?? latestTrench?.count ?? 0;
  const dupLast = latestTrench?.duplicate_count_estimate ?? 0;
  const passLast = latestTrench?.passed ?? 0;
  const passTotal = passLast;
  const dsh = fstats?.data_source_health;

  return (
    <section className="page-stack">
      <div className="card">
        <h2>交易看板</h2>
        <p className="hint">
          LIVE 展示实盘策略持仓，SIM 展示模拟盘策略持仓；系统处于模拟交易时默认进入 SIM。
        </p>
        <div className="button-row">
          <button className={portfolioTab === 'CURRENT' ? 'primary' : ''} onClick={() => switchPortfolioTab('CURRENT')}>当前持仓</button>
          <button className={portfolioTab === 'HISTORY' ? 'primary' : ''} onClick={() => switchPortfolioTab('HISTORY')}>历史持仓</button>
        </div>
        {portfolioTab === 'CURRENT' ? (
          <>
            <div className="button-row">
              <button className={tab === 'LIVE' ? 'primary' : ''} onClick={() => switchTab('LIVE')}>LIVE 实盘策略</button>
              <button className={tab === 'SIM' ? 'primary' : ''} onClick={() => switchTab('SIM')}>SIM 模拟盘策略</button>
            </div>
            <p className="hint">当前系统状态：{status?.user_mode ?? '加载中'}</p>
            {rawLast > 0 && (
              <div className="metric-row">
                <span>上一次 trench 原始拉回</span>
                <strong>{rawLast}</strong>
              </div>
            )}
            <div className="metric-row">
              <span>上一次 trench 拉回池子数</span>
              <strong>{uniqueLast}</strong>
            </div>
            {dupLast > 0 && (
              <div className="metric-row">
                <span>上一次估算重复</span>
                <strong>{dupLast}</strong>
              </div>
            )}
            <div className="metric-row">
              <span>其中通过风控指标</span>
              <strong>{passTotal}</strong>
            </div>
          </>
        ) : (
          <>
            <div className="button-row">
              <button className={historyAccount === 'ALL' ? 'primary' : ''} onClick={() => switchHistoryAccount('ALL')}>全部</button>
              <button className={historyAccount === 'LIVE' ? 'primary' : ''} onClick={() => switchHistoryAccount('LIVE')}>LIVE</button>
              <button className={historyAccount === 'SIM' ? 'primary' : ''} onClick={() => switchHistoryAccount('SIM')}>SIM</button>
            </div>
          </>
        )}
        {message && <p className="message">{message}</p>}
      </div>

      <div className="card">
        {portfolioTab === 'CURRENT' ? (
          <table>
            <thead>
              <tr>
                <th>仓位ID</th>
                <th>策略</th>
                <th>Token</th>
                <th>状态</th>
                <th>剩余价值</th>
                <th>收益率</th>
                <th>价格倍数</th>
                <th>更新时间</th>
              </tr>
            </thead>
            <tbody>
              {currentRows.length === 0 && (
                <tr><td colSpan={8} className="empty">当前看板暂无持仓。</td></tr>
              )}
              {currentRows.map((row) => (
                <tr key={row.id}>
                  <td>{row.id}</td>
                  <td>{row.strategy_name || row.strategy_id || '-'}</td>
                  <td title={row.token_mint}>{row.mint_short || row.token_mint || '-'}</td>
                  <td>{row.status}{row.last_exit_reason ? ` / ${exitReasonLabel(String(row.last_exit_reason))}` : ''}</td>
                  <td>{usd(row.remaining_value_usd ?? row.remaining)}</td>
                  <td>{pct(row.pnl_pct)}</td>
                  <td>{row.ratio ?? '-'}</td>
                  <td>{row.updated_at || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table>
            <thead>
              <tr>
                <th>方向</th>
                <th>Token</th>
                <th>Symbol</th>
                <th>价格</th>
                <th>数量</th>
                <th>价值(USD)</th>
                <th>退出原因</th>
                <th>北京时</th>
              </tr>
            </thead>
            <tbody>
              {historyRows.length === 0 && (
                <tr><td colSpan={8} className="empty">暂无交易记录。</td></tr>
              )}
              {historyRows.map((row) => (
                <tr key={row.trade_event_id}>
                  <td><span className={row.side === 'BUY' ? 'tag sim' : 'tag live'}>{row.side}</span></td>
                  <td title={row.token_mint}>{row.mint_short || '-'}</td>
                  <td>{row.symbol || '-'}</td>
                  <td>{usd(row.price_usd)}</td>
                  <td>{row.token_amount ?? '-'}</td>
                  <td style={pnlStyle(row.trade_value_usd_net)}>{usd(row.trade_value_usd_net)}</td>
                  <td>{row.exit_reason_label || '-'}</td>
                  <td style={{ fontSize: 12 }}>{(row.created_at_beijing || row.created_at_utc || '-').substring(0, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="grid two">
        {fstats && (
          <div className="card">
            <h2>淘汰指标排行</h2>
            <p className="hint">最近一次 run 中各指标不满足比例；分母为实际进入该项筛选的策略-池子检查次数</p>
            {(!fstats.filter_fails || fstats.filter_fails.length === 0) && (
              <p className="empty">暂无淘汰数据</p>
            )}
            {fstats.filter_fails?.map((item) => (
              <div className="metric-row" key={item.rule}>
                <span title={item.stage ? `[${item.stage}] ${item.section || ''}` : undefined}>
                  {item.label || ruleLabel(item.rule)}
                  <br/><small className="hint">检查{item.checked_count ?? '?'}次 · 失败{item.failed_count ?? '?'}次</small>
                </span>
                <strong>{item.fail_rate_pct != null ? `${item.fail_rate_pct}%` : (item.count != null ? `${item.count}` : '-')}</strong>
              </div>
            ))}
          </div>
        )}

        {dsh && (
          <div className="card">
            <h2>数据源健康诊断</h2>
            {dsh.summary && Object.keys(dsh.summary).length > 0 && (
              <>
                <p className="hint">统计口径：AND流水线(risk+holder+degen→price+kline→create)</p>
                <div className="metric-row"><span>风控面通过(risk+holder+degen)</span><strong>{String(dsh.summary.risk_surface_pass_count ?? '-')}</strong></div>
                <div className="metric-row"><span>价格面通过(price+kline)</span><strong>{String(dsh.summary.price_surface_pass_count ?? '-')}</strong></div>
                <div className="metric-row"><span>就绪可创建</span><strong>{String(dsh.summary.entry_ready_count ?? '-')}</strong></div>
                {dsh.summary.total_429_count as number > 0 && (
                  <div className="metric-row"><span>最近窗口429次数</span><strong style={{color:'#f85149'}}>{String(dsh.summary.total_429_count)}</strong></div>
                )}
              </>)}
            {dsh.discovery_fetch_health && dsh.discovery_fetch_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>Discovery 拉取（最新轮）</h3>
                <table style={{fontSize:12,marginTop:4}}><thead><tr><th>分组</th><th>状态</th><th>原始</th><th>去重</th><th>备注</th></tr></thead><tbody>
                {dsh.discovery_fetch_health.map((dh, idx) => (
                  <tr key={idx}><td>{dh.group_name}{dh.slot != null ? ` [slot ${dh.slot}]` : ''}</td>
                    <td>{severityDot(dh.severity)} {dh.ok ? 'OK' : 'FAIL'}</td>
                    <td>{dh.raw_count}</td><td>{dh.unique_count ?? '-'}</td>
                    <td style={{fontSize:11,color:'#8892ae'}}>{dh.error ? String(dh.error).substring(0,50) : ''}{dh.raw_count===0&&dh.ok?'返回0条':''}</td>
                  </tr>))}</tbody></table></>)}
            {dsh.credential_health && dsh.credential_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>API Slot 状态</h3>
                <table style={{fontSize:12,marginTop:4}}><thead><tr><th>S</th><th>角色</th><th>调用</th><th>成功</th><th>429</th><th>冷却</th><th>禁用</th></tr></thead><tbody>
                {dsh.credential_health.map((ch) => (
                  <tr key={ch.slot}>
                    <td>{severityDot(ch.severity)} {ch.slot}</td>
                    <td style={{fontSize:11}}>{ch.role}</td>
                    <td>{ch.total_calls}</td>
                    <td>{rateFmt(ch.ok_rate)}</td>
                    <td style={{color: ch.rate_limited_count>0?'#f85149':''}}>{ch.rate_limited_count}</td>
                    <td style={{color: ch.cooldown_until?'#f85149':'#8892ae',fontSize:11}}>{ch.cooldown_until ? `${ch.cooldown_remaining_s ?? 0}s` : '-'}</td>
                    <td style={{color: ch.disabled_until?'#f85149':'#8892ae',fontSize:11}}>{ch.disabled_until ? `${ch.disabled_remaining_s ?? 0}s` : '-'}</td>
                  </tr>))}</tbody></table></>)}
            {dsh.feature_stage_health && dsh.feature_stage_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>特征阶段流水线</h3>
                <table style={{fontSize:12,marginTop:4}}><thead><tr><th>阶段</th><th>候选</th><th>通过</th><th>失败</th><th>API次数</th><th>429</th></tr></thead><tbody>
                {dsh.feature_stage_health.map((fs) => (
                  <tr key={fs.stage}><td>{severityDot(fs.severity)} {fs.label}{fs.weight ? ` (w${fs.weight})` : ''}</td>
                    <td>{fs.candidates_in}</td><td>{fs.passed_count}</td><td>{fs.failed_count}</td>
                    <td>{fs.api_calls}</td>
                    <td style={{color:fs.rate_limited_count>0?'#f85149':''}}>{fs.rate_limited_count}</td>
                  </tr>))}</tbody></table></>)}
            {dsh.field_health && dsh.field_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>字段异常</h3>
                {dsh.field_health.filter(fh => fh.severity !== 'ok').slice(0, 6).map((fh, idx) => (
                  <div className="metric-row" key={idx}><span>{severityDot(fh.severity)} {fh.label}<br/><small className="hint">缺失{rateFmt(fh.missing_rate)}</small></span><strong>{rateFmt(fh.missing_rate)}</strong></div>))}</>)}
            {dsh.system_event_warnings && dsh.system_event_warnings.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#f85149'}}>系统错误</h3>
                {dsh.system_event_warnings.slice(0, 3).map((w, idx) => <p className="message" key={idx} style={{fontSize:12}}>{String((w as Record<string,unknown>).message || '').substring(0,200)}</p>)}</>)}
          </div>
        )}
      </div>
    </section>
  );
}
