import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'

export default function Operations() {
  const [runtime, setRuntime] = useState<any>(null)
  const [logs, setLogs] = useState<any[]>([])
  const [trades, setTrades] = useState<any[]>([])
  const [reqs, setReqs] = useState<any[]>([])
  const [msg, setMsg] = useState('')
  const [filterLevel, setFilterLevel] = useState('')
  const [filterCat, setFilterCat] = useState('')
  const [tab, setTab] = useState<'logs' | 'trades' | 'providers'>('logs')
  const [confirmAction, setConfirmAction] = useState<{ label: string, action: () => void } | null>(null)

  const [killActive, setKillActive] = useState(false)
  const [liveStopped, setLiveStopped] = useState(false)

  const refresh = useCallback(() => {
    api.getRuntimeStatus().then(setRuntime).catch(() => {})
    api.getRecentLogs(filterLevel || undefined, filterCat || undefined).then(r => setLogs(r || [])).catch(() => {})
    api.getTrades().then(r => setTrades(r || [])).catch(() => {})
    api.getProviderRequests().then(r => setReqs(r || [])).catch(() => {})
  }, [filterLevel, filterCat])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  const doConfirm = (label: string, action: () => void) => {
    setConfirmAction({ label, action })
  }

  const executeConfirm = async () => {
    if (!confirmAction) return
    await confirmAction.action()
    setConfirmAction(null)
    setMsg(`${confirmAction.label} completed`)
    refresh()
  }

  const LevelBadge = ({ level }: { level: string }) => {
    const map: Record<string, string> = {
      'ERROR': 'bg-red-900 text-red-300', 'WARN': 'bg-yellow-900 text-yellow-300',
      'INFO': 'bg-blue-900 text-blue-300', 'DEBUG': 'bg-gray-700 text-gray-400'
    }
    return <span className={`px-1 py-0.5 rounded text-xs ${map[level] || 'bg-gray-800 text-gray-400'}`}>{level}</span>
  }

  const CATEGORIES = ['', 'DISCOVERY', 'SECOND_FILTER', 'RISK', 'TRADE', 'JITO', 'GMGN', 'JUPITER', 'RPC', 'WORKER', 'EMERGENCY', 'CONFIG', 'DB']
  const LEVELS = ['', 'INFO', 'WARN', 'ERROR']

  const mode = runtime?.user_mode || 'IDLE'
  const modeLabel = mode === 'FORMAL_SIM_LIVE' ? '实盘' : mode === 'SIM_TEST' ? '模拟盘' : 'IDLE'
  const liveReady = runtime?.can_live_trade ? 'Ready' : 'Blocked'

  const handleKill = () => {
    if (killActive) {
      doConfirm('Resume (disable kill switch)', async () => {
        await api.toggleKill(false)
        setKillActive(false)
        setMsg('Kill switch disabled')
      })
    } else {
      doConfirm('Kill switch - block all new entries', async () => {
        await api.toggleKill(true)
        setKillActive(true)
        setMsg('Kill switch enabled')
      })
    }
  }

  const handleStopLive = () => {
    if (liveStopped) {
      doConfirm('Resume live mode trading', async () => {
        await api.resumeLiveMode()
        setLiveStopped(false)
        setMsg('Live trading resumed')
      })
    } else {
      doConfirm('Stop all live mode trading', async () => {
        await api.stopLiveMode()
        setLiveStopped(true)
        setMsg('Live trading stopped')
      })
    }
  }

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Operations &amp; Emergency</h1>

      {msg && <div className="bg-gray-800 border border-cyan-700 rounded p-2 mb-3 text-sm text-cyan-400">{msg}
        <button onClick={() => setMsg('')} className="ml-3 text-gray-500 hover:text-white">x</button>
      </div>}

      {confirmAction && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-red-600 rounded-lg p-6 max-w-sm">
            <h2 className="text-lg text-red-400 font-bold mb-3">Confirm Action</h2>
            <p className="text-sm text-gray-300 mb-4">{confirmAction.label}</p>
            <div className="flex gap-3">
              <button onClick={executeConfirm} className="bg-red-700 hover:bg-red-600 px-4 py-2 rounded text-sm flex-1">Confirm</button>
              <button onClick={() => setConfirmAction(null)} className="bg-gray-700 hover:bg-gray-600 px-4 py-2 rounded text-sm">Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Status bar */}
      <div className="flex items-center gap-4 mb-4 text-xs text-gray-400">
        <span>Mode: <span className={mode === 'FORMAL_SIM_LIVE' ? 'text-red-400' : 'text-cyan-400'}>{modeLabel}</span></span>
        <span>Live: <span className={runtime?.can_live_trade ? 'text-green-400' : 'text-red-400'}>{liveReady}</span></span>
      </div>

      {/* Emergency Actions */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
        <button onClick={handleKill}
          className={`px-3 py-2 rounded text-xs text-white ${killActive ? 'bg-gray-600 hover:bg-gray-500' : 'bg-red-800 hover:bg-red-700'}`}>
          {killActive ? 'Resume' : 'Kill'}
        </button>
        <button onClick={handleStopLive}
          className={`px-3 py-2 rounded text-xs text-white ${liveStopped ? 'bg-green-800 hover:bg-green-700' : 'bg-orange-800 hover:bg-orange-700'}`}>
          {liveStopped ? '恢复实盘' : '停止实盘'}
        </button>
        <button onClick={() => doConfirm('Export losing positions', () => api.exportLosing())}
          className="bg-gray-700 hover:bg-gray-600 px-3 py-2 rounded text-xs">
          亏钱盘导出
        </button>
        <button onClick={() => doConfirm('Repair legacy DB configs', () => api.repairLegacyDb())}
          className="bg-purple-900 hover:bg-purple-800 px-3 py-2 rounded text-xs text-white">Repair Legacy DB</button>
        <button onClick={() => { api.backupDb(); setMsg('DB backup started') }}
          className="bg-gray-700 hover:bg-gray-600 px-3 py-2 rounded text-xs">Backup DB</button>
      </div>

      {/* Tabs */}
      <div className="flex gap-4 mb-3 border-b border-gray-700">
        {(['logs', 'trades', 'providers'] as const).map(t => (
          <button key={t} onClick={() => setTab(t)}
            className={`pb-2 px-2 text-sm capitalize transition-colors ${tab === t ? 'text-cyan-400 border-b-2 border-cyan-400' : 'text-gray-500 hover:text-gray-300'}`}>
            {t === 'providers' ? 'Provider Requests' : t}
          </button>
        ))}
      </div>

      {/* Logs tab */}
      {tab === 'logs' && (
        <div>
          <div className="flex gap-2 mb-2 flex-wrap">
            <select value={filterLevel} onChange={e => setFilterLevel(e.target.value)}
              className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs">
              {LEVELS.map(l => <option key={l} value={l}>{l || 'All Levels'}</option>)}
            </select>
            <select value={filterCat} onChange={e => setFilterCat(e.target.value)}
              className="bg-gray-800 border border-gray-600 rounded px-2 py-1 text-xs">
              {CATEGORIES.map(c => <option key={c} value={c}>{c || 'All Modules'}</option>)}
            </select>
          </div>
          <div className="bg-gray-900 border border-gray-700 rounded max-h-96 overflow-y-auto">
            {logs.slice(0, 100).map((l: any, i: number) => (
              <div key={i} className={`text-xs py-1 px-2 border-b border-gray-800 flex items-center gap-2 ${l.level === 'ERROR' ? 'bg-red-900/20' : l.level === 'WARN' ? 'bg-yellow-900/20' : ''}`}>
                <span className="text-gray-600 w-16">{l.created_at?.slice(11, 19)}</span>
                <LevelBadge level={l.level} />
                <span className="text-gray-500 w-20">{l.category}</span>
                <span className="text-gray-300 flex-1">{l.message}</span>
                <span className="text-gray-600">{l.account_type}</span>
              </div>
            ))}
            {logs.length === 0 && <p className="text-gray-500 text-xs py-4 text-center">No logs.</p>}
          </div>
        </div>
      )}

      {/* Trades tab */}
      {tab === 'trades' && (
        <div className="bg-gray-900 border border-gray-700 rounded overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-gray-400 border-b border-gray-700">
              <th className="text-left p-1.5">ID</th><th>Side</th><th>Type</th><th>Status</th><th>Impact</th><th>Sig</th><th>Account</th>
            </tr></thead>
            <tbody>
              {trades.slice(0, 50).map((t: any) => (
                <tr key={t.id} className="border-b border-gray-800">
                  <td className="p-1.5">{t.id}</td>
                  <td>{t.side}</td>
                  <td className="text-gray-500">{t.event_type}</td>
                  <td className={t.status === 'CONFIRMED' ? 'text-green-400' : t.status === 'FAILED' ? 'text-red-400' : 'text-yellow-400'}>{t.status}</td>
                  <td className="text-right">{t.price_impact_pct?.toFixed(4) || '-'}</td>
                  <td className="font-mono text-gray-500 text-[10px]">{t.tx_signature?.slice(0, 12) || '-'}</td>
                  <td className={t.account_type === 'LIVE' ? 'text-red-400' : 'text-blue-400'}>{t.account_type || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {trades.length === 0 && <p className="text-gray-500 text-xs py-4 text-center">No trades recorded.</p>}
        </div>
      )}

      {/* Provider Requests tab */}
      {tab === 'providers' && (
        <div className="bg-gray-900 border border-gray-700 rounded overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-gray-400 border-b border-gray-700">
              <th className="text-left p-1.5">ID</th><th>Provider</th><th>Endpoint</th><th>Status</th><th>Latency</th><th>Error</th>
            </tr></thead>
            <tbody>
              {reqs.slice(0, 50).map((r: any) => (
                <tr key={r.id} className="border-b border-gray-800">
                  <td className="p-1.5">{r.id}</td>
                  <td>{r.provider}</td>
                  <td className="font-mono text-gray-500 text-[10px] max-w-[200px] truncate" title={r.endpoint}>{r.endpoint?.slice(0, 30)}</td>
                  <td className={r.ok ? 'text-green-400' : 'text-red-400'}>{r.ok ? 'OK' : 'ERR'}</td>
                  <td className="text-right">{r.latency_ms}ms</td>
                  <td className="text-red-400 text-[10px]">{r.error_summary?.slice(0, 30) || '-'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {reqs.length === 0 && <p className="text-gray-500 text-xs py-4 text-center">No provider requests recorded.</p>}
        </div>
      )}
    </div>
  )
}
