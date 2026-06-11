import { useEffect, useState } from 'react';
import { api, RuntimeStatus } from '../api/client';

export default function Operations() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => setStatus(await api.getRuntimeStatus());

  useEffect(() => {
    load().catch((e) => setMessage(e.message));
    const timer = window.setInterval(() => load().catch(() => undefined), 5000);
    return () => window.clearInterval(timer);
  }, []);

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
        <p className="hint">该页只保留实盘安全、数据备份与导出功能；Workers看板与手动Start/Stop All已删除。</p>
        <div className="metric-row"><span>当前状态</span><strong>{status?.user_mode ?? '加载中'}</strong></div>
        <div className="metric-row"><span>实盘持仓数</span><strong>{status?.live_open_count ?? 0}</strong></div>

        <div className="button-row">
          {showSellAll && (
            <button className="danger" disabled={loading} onClick={() => run(api.sellAllLive, (r) => `一键卖出完成，处理实盘持仓 ${r.sold_count ?? 0} 个，系统已切换到模拟交易。`)}>
              一键卖出
            </button>
          )}

          {liveMode ? (
            <button className="danger" disabled={loading} onClick={() => run(api.stopLive, () => '已停止实盘，系统切换为模拟交易状态。')}>停止实盘</button>
          ) : (
            <button className="success" disabled={loading} onClick={() => run(api.resumeLive, () => '已恢复实盘，系统切换为实盘交易状态。')}>恢复实盘</button>
          )}
        </div>
        <p className="hint">“一键卖出”仅在实盘交易状态且存在实盘持仓时显示；仅卖出实盘持仓，不影响模拟盘记录。</p>
      </div>

      <div className="card">
        <h2>数据导出</h2>
        <p className="hint">备份与导出均以本次系统启动时间为边界，便于定位单次运行问题。</p>
        <div className="button-row">
          <button disabled={loading} onClick={() => run(api.backupDb, (r) => `已备份本次启动以来数据：${r.export_path}`)}>备份数据库</button>
          <button disabled={loading} onClick={() => run(api.exportTradeAudit, (r) => `已导出交易审计：${r.export_path}`)}>导出交易审计</button>
           <button disabled={loading} onClick={() => run(api.exportLogs, (r) => `已导出日志：${r.export_path}，WARNING ${r.warning_count ?? 0} 条，ERROR ${r.error_count ?? 0} 条，CRITICAL ${r.critical_count ?? 0} 条，去重问题 ${r.issue_count ?? 0} 类。`)}>导出日志</button>
        </div>
        <p className="hint">亏损交易导出包含初筛、二筛、持仓风控、交易事件、GMGN风控原始快照、K线快照和请求日志；日志导出仅保留重点统计。</p>
        {message && <p className="message">{message}</p>}
      </div>
    </section>
  );
}
