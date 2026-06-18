import { useEffect, useState } from 'react';
import { api, RuntimeStatus } from '../api/client';

export default function Operations({ active }: { active: boolean }) {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [statusError, setStatusError] = useState('');
  const [initialLoading, setInitialLoading] = useState(true);
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    try {
      const s = await api.getRuntimeStatus();
      setStatus(s);
      setStatusError('');
    } catch (e) {
      setStatusError((e as Error).message);
    } finally {
      setInitialLoading(false);
    }
  };

  useEffect(() => {
    if (!active) return;
    load();
    const timer = window.setInterval(() => {
      api.getRuntimeStatus().then(setStatus).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [active]);

  const run = async (action: () => Promise<Record<string, unknown>>, success: (r: Record<string, unknown>) => string) => {
    setLoading(true);
    setMessage('');
    try {
      const res = await action();
      setMessage(success(res));
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const liveMode = status?.user_mode === 'FORMAL_SIM_LIVE';
  const showSellAll = liveMode && Boolean(status?.has_live_positions);

  return (
    <section className="page-stack">
      <div className="card danger-zone">
        <h2>Ops & Emergency</h2>
        <p className="hint">该页只保留实盘安全、数据备份与导出功能。</p>
        <div className="metric-row"><span>当前状态</span><strong>{status?.user_mode ?? (statusError ? '状态接口异常' : (initialLoading ? '加载中' : '未知'))}</strong></div>
        <div className="metric-row"><span>实盘持仓数</span><strong>{status?.live_open_count ?? 0}</strong></div>

        <div className="button-row">
          {showSellAll && (
            <button className="danger" disabled={loading} onClick={() => run(api.sellAllLive, (r) => `一键卖出完成，处理实盘持仓 ${(r as Record<string, unknown>).sold_count ?? 0} 个。`)}>
              一键卖出
            </button>
          )}

          {liveMode ? (
            <button className="danger" disabled={loading} onClick={() => run(api.stopLive, () => '已停止实盘，系统切换为模拟交易状态。')}>停止实盘</button>
          ) : (
            <button className="success" disabled={loading} onClick={() => run(api.resumeLive, () => '已恢复实盘，系统切换为实盘交易状态。')}>恢复实盘</button>
          )}
        </div>
        <p className="hint">"一键卖出"仅在实盘交易状态且存在实盘持仓时显示。</p>
      </div>

      <div className="card">
        <h2>数据导出</h2>
        <p className="hint">交易审计默认过去24小时（北京时间），日志导出固定过去12小时。</p>
        <div className="button-row">
          <button disabled={loading} onClick={() => run(api.backupDb, (r) => `已备份数据库：${(r as Record<string, unknown>).export_path}`)}>备份数据库</button>
          <button disabled={loading} onClick={() => run(api.exportTradeAudit, (r) => `已导出交易审计：${(r as Record<string, unknown>).export_path}`)}>导出交易审计（24h）</button>
          <button disabled={loading} onClick={() => run(api.exportLogs, (r) => `已导出日志：${(r as Record<string, unknown>).export_path}，WARNING ${(r as Record<string, unknown>).warning_count ?? 0} 条，ERROR ${(r as Record<string, unknown>).error_count ?? 0} 条`)}>导出日志（过去12小时）</button>
        </div>
        <p className="hint">日志导出仅保留过去12小时，过滤Duplicate tokens等无意义噪声。</p>
        {message && <p className="message">{message}</p>}
      </div>
    </section>
  );
}
