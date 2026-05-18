import { useEffect, useRef, useState } from 'react';
import { api, FilterStats, PortfolioRow, RuntimeStatus } from '../api/client';

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
    creator_token_status_or_dev_team_hold_rate: 'creator未关仓/dev持币',
    burn_status: 'burn状态不符',
    sniper_count: 'sniper数量超标',
    platform: '平台不在白名单',
    volume_1m: 'volume_1m不达标',
    close_gt_open_scaled: 'close未跑赢open',
    candle_position: '1m candle位置偏低',
    price_gt_high_over_y: '价格未突破high5/y',
    price_lt_low_times_y: '价格未跌破low5*y',
    fraction_range: '价格分位不在区间',
    top1_holder: 'TOP1持仓超标',
  };
  return map[name] || name;
}

export default function Portfolio() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [tab, setTab] = useState<AccountTab>('LIVE');
  const [rows, setRows] = useState<PortfolioRow[]>([]);
  const [fstats, setFstats] = useState<FilterStats | null>(null);
  const [message, setMessage] = useState('');
  const tabRef = useRef<AccountTab>('LIVE');

  // keep ref in sync with state so interval always passes current tab
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
    // filter stats only change when discovery runs (every POLL_INTERVAL_SECONDS)
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
  const passTotal = fstats?.trench_history?.reduce((s, i) => s + i.passed, 0) ?? 0;

  return (
    <section className="page-stack">
      <div className="card">
        <h2>交易看板</h2>
        <p className="hint">
          LIVE 展示实盘策略持仓，SIM 展示模拟盘策略持仓；系统处于模拟交易时默认进入 SIM，因为 LIVE 通常为空。
        </p>
        <div className="button-row">
          <button className={tab === 'LIVE' ? 'primary' : ''} onClick={() => switchTab('LIVE')}>LIVE 实盘策略</button>
          <button className={tab === 'SIM' ? 'primary' : ''} onClick={() => switchTab('SIM')}>SIM 模拟盘策略</button>
        </div>
        <p className="hint">当前系统状态：{status?.user_mode ?? '加载中'}</p>
        <div className="metric-row">
          <span>近{fstats?.trench_history?.length ?? 0}次 trench 拉回池子总数</span>
          <strong>{trenchTotal}</strong>
        </div>
        <div className="metric-row">
          <span>其中通过初筛数</span>
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

      {fstats && (
        <div className="card">
          <h2>淘汰指标排行</h2>
          <p className="hint">风控+价格面筛选各指标不满足次数（降序）</p>
          {fstats.filter_fails.length === 0 && (
            <p className="empty">暂无淘汰数据</p>
          )}
          {fstats.filter_fails.map((item) => (
            <div className="metric-row" key={item.rule}>
              <span>{ruleLabel(item.rule)}</span>
              <strong>{item.count}</strong>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
