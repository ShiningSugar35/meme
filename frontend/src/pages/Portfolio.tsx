import { useEffect, useRef, useState } from 'react';
import { api, FilterStats, PortfolioRow, RuntimeStatus, RuleFailItem, EndpointHealthItem, FieldHealthItem, PlatformHealthItem, DataSourceHealth } from '../api/client';

type AccountTab = 'LIVE' | 'SIM';

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
    swaps_5m_scaled: 'swaps_5m不达标',
    price_change_1h: '1h价格涨幅不足',
    smart_degen: '聪明钱指标不满足',
  };
  return map[name] || name;
}

function severityDot(sev: string) {
  if (sev === 'critical') return <span title="严重" className="tag live">!</span>;
  if (sev === 'warn') return <span title="警告" style={{display:'inline-flex',borderRadius:999,background:'#6e7a1b',color:'#d8e389',padding:'3px 9px',fontSize:12,fontWeight:700}}>!</span>;
  return <span title="正常" className="tag sim">&#10003;</span>;
}

function rateFmt(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

export default function Portfolio() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [tab, setTab] = useState<AccountTab>('LIVE');
  const [rows, setRows] = useState<PortfolioRow[]>([]);
  const [fstats, setFstats] = useState<FilterStats | null>(null);
  const [message, setMessage] = useState('');
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
    loadFilterStats();
    const timer = window.setInterval(() => load(tabRef.current).catch(() => undefined), 5000);
    const statsTimer = window.setInterval(loadFilterStats, 60000);
    return () => { window.clearInterval(timer); window.clearInterval(statsTimer); };
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

  const trenchTotal = fstats?.trench_history?.reduce((s, i) => s + i.count, 0) ?? 0;
  const rawTotal = fstats?.trench_history?.reduce((s, i) => s + (i.raw_count || i.count || 0), 0) ?? 0;
  const uniqueTotal = fstats?.trench_history?.reduce((s, i) => s + (i.unique_count || i.count || 0), 0) ?? 0;
  const dupTotal = fstats?.trench_history?.reduce((s, i) => s + (i.duplicate_count_estimate || 0), 0) ?? 0;
  const passTotal = fstats?.trench_history?.reduce((s, i) => s + i.passed, 0) ?? 0;
  const dsh = fstats?.data_source_health;

  return (
    <section className="page-stack">
      <div className="card">
        <h2>交易看板</h2>
        <p className="hint">
          LIVE 展示实盘策略持仓，SIM 展示模拟盘策略持仓；系统处于模拟交易时默认进入 SIM。
        </p>
        <div className="button-row">
          <button className={tab === 'LIVE' ? 'primary' : ''} onClick={() => switchTab('LIVE')}>LIVE 实盘策略</button>
          <button className={tab === 'SIM' ? 'primary' : ''} onClick={() => switchTab('SIM')}>SIM 模拟盘策略</button>
        </div>
        <p className="hint">当前系统状态：{status?.user_mode ?? '加载中'}</p>
        {rawTotal > 0 && rawTotal !== trenchTotal && (
          <div className="metric-row">
            <span>近{fstats?.trench_history?.length ?? 0}次 trench 原始拉回</span>
            <strong>{rawTotal}</strong>
          </div>
        )}
        <div className="metric-row">
          <span>近{fstats?.trench_history?.length ?? 0}次 trench 拉回池子总数</span>
          <strong>{rawTotal > 0 && rawTotal !== trenchTotal ? uniqueTotal : trenchTotal}</strong>
        </div>
        {dupTotal > 0 && (
          <div className="metric-row">
            <span>估算重复</span>
            <strong>{dupTotal}</strong>
          </div>
        )}
        <div className="metric-row">
          <span>其中通过风控指标</span>
          <strong>{passTotal}</strong>
        </div>
        {message && <p className="message">{message}</p>}
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>仓位ID</th>
              <th>交易属性</th>
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
            {rows.length === 0 && (
              <tr><td colSpan={9} className="empty">当前看板暂无持仓。</td></tr>
            )}
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.id}</td>
                <td><span className={tab === 'LIVE' ? 'tag live' : 'tag sim'}>{tab}</span></td>
                <td>{row.strategy_name || row.strategy_id || '-'}</td>
                <td title={row.token_mint}>{row.mint_short || row.token_mint || '-'}</td>
                <td>{row.status}</td>
                <td>{usd(row.remaining_value_usd ?? row.remaining)}</td>
                <td>{pct(row.pnl_pct)}</td>
                <td>{row.ratio ?? '-'}</td>
                <td>{row.updated_at || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid two">
        {fstats && (
          <div className="card">
            <h2>淘汰指标排行</h2>
            <p className="hint">风控+价格面筛选各指标不满足比例（按实际检查次数计算）</p>
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
                <div className="metric-row">
                  <span>窗口轮次</span>
                  <strong>{String(dsh.summary.window_run_count || 0)}</strong>
                </div>
                <div className="metric-row">
                  <span>危险筛选数</span>
                  <strong>{String(dsh.summary.risk_match_count ?? '-')}</strong>
                </div>
                <div className="metric-row">
                  <span>危险通过</span>
                  <strong>{String(dsh.summary.risk_pass_count ?? '-')}</strong>
                </div>
                <div className="metric-row">
                  <span>价格筛选数</span>
                  <strong>{String(dsh.summary.price_match_count ?? '-')}</strong>
                </div>
                <div className="metric-row">
                  <span>价格通过</span>
                  <strong>{String(dsh.summary.price_pass_count ?? '-')}</strong>
                </div>
              </>
            )}

            {dsh.endpoint_health && dsh.endpoint_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>API 端点健康</h3>
                {dsh.endpoint_health.map((ep) => (
                  <div className="metric-row" key={ep.endpoint}>
                    <span>
                      {severityDot(ep.severity)} {ep.endpoint}
                      <br/><small className="hint">{ep.method} · 共{ep.calls}次 · {rateFmt(ep.ok_rate)}成功 · {ep.avg_latency_ms}ms</small>
                    </span>
                    <strong>{ep.ok_rate != null ? rateFmt(ep.ok_rate) : '-'}</strong>
                  </div>
                ))}
              </>
            )}

            {dsh.platform_health && dsh.platform_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>Platform 分片拉取</h3>
                <table style={{fontSize:12,marginTop:4}}>
                  <thead>
                    <tr>
                      <th>Platform</th><th>状态</th><th>原始</th><th>去重</th><th>备注</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dsh.platform_health.map((ph, idx) => (
                      <tr key={idx}>
                        <td>{ph.platform}</td>
                        <td>{severityDot(ph.severity)} {ph.ok ? 'OK' : 'FAIL'}{ph.fallback_used ? ' (备用API)' : ''}</td>
                        <td>{ph.raw_count >= 0 ? ph.raw_count : '-'}</td>
                        <td>{ph.unique_count != null ? ph.unique_count : '-'}</td>
                        <td style={{fontSize:11,color:'#8892ae'}}>{ph.error ? String(ph.error).substring(0, 60) : (ph.raw_count === 0 && ph.ok ? '返回0条' : '')}{ph.fallback_used ? ` 备用slot ${ph.used_slot}` : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            {dsh.field_health && dsh.field_health.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>字段异常（全部 critical + warn）</h3>
                {dsh.field_health.filter((fh) => fh.severity !== 'ok').map((fh, idx) => (
                  <div className="metric-row" key={idx}>
                    <span>
                      {severityDot(fh.severity)} {fh.label} ({fh.field})
                      <br/><small className="hint">
                        缺失 {rateFmt(fh.missing_rate)}
                        {fh.zero_count > 0 ? ` · 零值 ${rateFmt(fh.zero_rate)}` : ''}
                        {fh.note ? ` · ${fh.note.substring(0, 50)}` : ''}
                      </small>
                    </span>
                    <strong>{rateFmt(fh.missing_rate)}</strong>
                  </div>
                ))}
              </>
            )}

            {dsh.price_age_health && dsh.price_age_health.warnings?.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#8892ae'}}>价格年龄诊断</h3>
                {dsh.price_age_health.warnings.map((w, idx) => (
                  <p className="message" key={idx} style={{fontSize:12}}>{w}</p>
                ))}
                <div className="metric-row">
                  <span>未满60分钟记录</span>
                  <strong>{dsh.price_age_health.under_60m_count ?? 0}</strong>
                </div>
                <div className="metric-row">
                  <span>年龄解析缺失</span>
                  <strong>{dsh.price_age_health.age_parse_missing_count ?? 0}</strong>
                </div>
              </>
            )}

            {dsh.system_event_warnings && dsh.system_event_warnings.length > 0 && (
              <>
                <h3 style={{fontSize:15,margin:'10px 0 4px',color:'#f85149'}}>系统错误摘要</h3>
                {dsh.system_event_warnings.slice(0, 3).map((w, idx) => (
                  <p className="message" key={idx} style={{fontSize:12}}>
                    {String((w as Record<string,unknown>).message || '').substring(0, 200)}
                  </p>
                ))}
              </>
            )}
          </div>
        )}
      </div>

      {fstats && !dsh && (
        <div className="card">
          <h2>淘汰指标排行</h2>
          <p className="hint">风控+价格面筛选各指标不满足比例（按实际检查次数计算）</p>
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
    </section>
  );
}
