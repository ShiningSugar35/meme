import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'

interface WorkerState {
  running?: boolean
  interval_sec?: number
  last_run_at?: string | null
  last_error?: string | null
  run_count?: number
}

export default function Operations() {
  const [runtime, setRuntime] = useState<any>({})
  const [workers, setWorkers] = useState<Record<string, WorkerState>>({})
  const [providerHealth, setProviderHealth] = useState<any>({})
  const [logs, setLogs] = useState<any[]>([])
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [r, w, h, l] = await Promise.all([
        api.getRuntimeStatus().catch(() => ({})),
        api.getWorkersStatus().catch(() => ({})),
        api.getProviderHealth().catch(() => ({})),
        api.getRecentLogs(undefined, undefined).catch(() => []),
      ])
      setRuntime(r || {})
      setWorkers(w || {})
      setProviderHealth(h || {})
      setLogs(Array.isArray(l) ? l : [])
    } catch (e: any) {
      setMsg(e?.message || 'Refresh failed')
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const run = async (label: string, fn: () => Promise<any>) => {
    setBusy(true)
    try {
      const r = await fn()
      if (r?.ok === false) setMsg(`${label} failed: ${r.error || 'unknown error'}`)
      else setMsg(`${label} completed`)
      await refresh()
    } catch (e: any) {
      setMsg(`${label} failed: ${e?.message || e}`)
    } finally {
      setBusy(false)
    }
  }

  const killActive = !!runtime?.kill_switch_active || !!runtime?.pause_new_entries
  const workerList = Object.entries(workers || {}) as [string, WorkerState][]
  const providerEntries = Object.entries(providerHealth || {}).filter(([k]) => k !== 'ok' && k !== 'timestamp')

  const badge = (ok: boolean, trueText = 'OK', falseText = 'FAIL') => (
    <span className={`px-2 py-0.5 rounded text-xs ${ok ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'}`}>
      {ok ? trueText : falseText}
    </span>
  )

  return <div>
    <h1 className="text-xl font-bold mb-4 text-cyan-400">Operations</h1>

    {msg && <div className="bg-gray-800 border border-cyan-700 rounded p-2 mb-3 text-sm text-cyan-400">
      {msg}<button onClick={() => setMsg('')} className="ml-3 text-gray-500 hover:text-white">x</button>
    </div>}

    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
      <div className="bg-gray-900 border border-gray-700 rounded p-4">
        <h2 className="text-sm font-bold mb-3 text-gray-300">Runtime Guardrails</h2>
        <div className="space-y-2 text-xs">
          <div className="flex justify-between"><span className="text-gray-500">User Mode</span><span className="text-cyan-300">{runtime?.user_mode || '-'}</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Provider Mode</span><span>{runtime?.provider_mode || '-'}</span></div>
          <div className="flex justify-between"><span className="text-gray-500">Workers Enabled</span>{badge(!!runtime?.workers_enabled, 'ON', 'OFF')}</div>
          <div className="flex justify-between"><span className="text-gray-500">Live Entries</span>{badge(!!runtime?.live_entries_enabled, 'ON', 'OFF')}</div>
          <div className="flex justify-between"><span className="text-gray-500">Kill Switch</span>{badge(!killActive, 'OFF', 'ON')}</div>
          <div className="flex justify-between"><span className="text-gray-500">Live Readiness</span>{badge(!!runtime?.live_readiness?.ready, 'READY', 'BLOCKED')}</div>
          {!runtime?.live_readiness?.ready && <div className="text-red-400">Missing: {(runtime?.live_readiness?.missing || []).join(', ') || '-'}</div>}
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-700 rounded p-4">
        <h2 className="text-sm font-bold mb-3 text-gray-300">Emergency Actions</h2>
        <div className="flex gap-2 flex-wrap">
          <button disabled={busy} onClick={() => run('Toggle kill switch', () => api.toggleKill(!killActive))}
            className={`${killActive ? 'bg-green-800 hover:bg-green-700' : 'bg-red-800 hover:bg-red-700'} disabled:opacity-50 px-3 py-2 rounded text-sm text-white`}>
            {killActive ? '解除 Kill Switch' : '启动 Kill Switch'}
          </button>
          <button disabled={busy} onClick={() => run('Stop live mode', () => api.stopLiveMode())}
            className="bg-orange-800 hover:bg-orange-700 disabled:opacity-50 px-3 py-2 rounded text-sm text-white">停止实盘</button>
          <button disabled={busy || !runtime?.live_readiness?.ready} onClick={() => run('Resume live mode', () => api.resumeLiveMode())}
            className="bg-green-800 hover:bg-green-700 disabled:opacity-50 px-3 py-2 rounded text-sm text-white">恢复实盘</button>
          <button disabled={busy} onClick={() => run('Backup DB', () => api.backupDb())}
            className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-3 py-2 rounded text-sm">备份数据库</button>
          <button disabled={busy} onClick={() => run('Export losing trades', () => api.exportLosing())}
            className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-3 py-2 rounded text-sm">导出亏损交易</button>
          <button disabled={busy} onClick={() => run('Repair legacy DB', () => api.repairLegacyDb())}
            className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-3 py-2 rounded text-sm">标记旧配置</button>
        </div>
      </div>
    </div>

    <div className="bg-gray-900 border border-gray-700 rounded p-4 mb-4">
      <div className="flex justify-between items-center mb-3">
        <h2 className="text-sm font-bold text-gray-300">Workers</h2>
        <div className="flex gap-2">
          <button disabled={busy} onClick={() => run('Start workers', () => api.startWorkers())}
            className="bg-cyan-800 hover:bg-cyan-700 disabled:opacity-50 px-3 py-1 rounded text-xs text-white">Start All</button>
          <button disabled={busy} onClick={() => run('Stop workers', () => api.stopWorkers())}
            className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-3 py-1 rounded text-xs">Stop All</button>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead><tr className="text-gray-500 border-b border-gray-700"><th className="text-left p-1.5">Worker</th><th>Status</th><th>Interval</th><th>Runs</th><th>Last Run</th><th>Error</th></tr></thead>
          <tbody>{workerList.map(([name, w]) => <tr key={name} className="border-b border-gray-800">
            <td className="p-1.5 text-gray-300">{name}</td>
            <td className="text-center">{badge(!!w.running, 'running', 'stopped')}</td>
            <td className="text-center">{w.interval_sec ?? '-' }s</td>
            <td className="text-center">{w.run_count ?? 0}</td>
            <td className="text-center text-gray-400">{w.last_run_at ? String(w.last_run_at).slice(0, 19) : '-'}</td>
            <td className="text-red-400 max-w-xs truncate">{w.last_error || '-'}</td>
          </tr>)}</tbody>
        </table>
        {workerList.length === 0 && <p className="text-gray-500 text-xs py-4 text-center">No workers.</p>}
      </div>
    </div>

    <div className="bg-gray-900 border border-gray-700 rounded p-4 mb-4">
      <h2 className="text-sm font-bold mb-3 text-gray-300">Provider Health</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
        {providerEntries.map(([name, v]: any) => <div key={name} className="bg-gray-800 rounded p-2">
          <div className="flex justify-between mb-1"><span className="text-gray-300">{name}</span>{badge(!!v?.ok, 'OK', 'FAIL')}</div>
          <div className="text-gray-500 truncate">{v?.error || v?.status || '-'}</div>
        </div>)}
        {providerEntries.length === 0 && <p className="text-gray-500">No provider health data.</p>}
      </div>
    </div>

    <div className="bg-gray-900 border border-gray-700 rounded p-4">
      <h2 className="text-sm font-bold mb-3 text-gray-300">Recent Events</h2>
      <div className="space-y-1 text-xs max-h-72 overflow-y-auto">
        {logs.slice(0, 80).map((l: any) => <div key={l.id || `${l.created_at}-${l.message}`} className="border-b border-gray-800 pb-1">
          <span className={l.level === 'ERROR' ? 'text-red-400' : l.level === 'WARN' || l.level === 'WARNING' ? 'text-yellow-400' : 'text-gray-400'}>{l.level}</span>
          <span className="text-gray-600 ml-2">{l.category}</span>
          <span className="text-gray-300 ml-2">{l.message}</span>
          <span className="text-gray-600 ml-2">{l.created_at?.slice(0, 19)}</span>
        </div>)}
        {logs.length === 0 && <p className="text-gray-500">No events.</p>}
      </div>
    </div>
  </div>
}
