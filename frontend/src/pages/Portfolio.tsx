import { useEffect, useState } from 'react';
import { api, PortfolioRow, RuntimeStatus } from '../api/client';

type AccountTab = 'LIVE' | 'SIM';

function usd(value: unknown) {
  const n = Number(value || 0);
  return Number.isFinite(n) ? `$${n.toFixed(2)}` : '$0.00';
}

function pct(value: unknown) {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(2)}%` : '-';
}

export default function Portfolio() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [tab, setTab] = useState<AccountTab>('LIVE');
  const [rows, setRows] = useState<PortfolioRow[]>([]);
  const [message, setMessage] = useState('');

  const load = async (preferred?: AccountTab) => {
    const runtime = await api.getRuntimeStatus();
    setStatus(runtime);
    const nextTab = preferred ?? (runtime.user_mode === 'SIM_TEST' ? 'SIM' : 'LIVE');
    setTab(nextTab);
    const data = await api.getPortfolio(nextTab);
    setRows(data);
  };

  useEffect(() => {
    load().catch((e) => setMessage(e.message));
    const timer = window.setInterval(() => load(tab).catch(() => undefined), 5000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const switchTab = async (next: AccountTab) => {
    setTab(next);
    setMessage('');
    try {
      setRows(await api.getPortfolio(next));
    } catch (e) {
      setMessage((e as Error).message);
    }
  };

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
    </section>
  );
}
