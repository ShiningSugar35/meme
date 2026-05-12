import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'

const fmtSol = (n: any) => {
  const v = Number(n || 0)
  return `${v >= 0 ? '+' : ''}${v.toFixed(4)} SOL`
}

const fmtBool = (v: any) => v ? 'ON' : 'OFF'

function StatCard({ title, value, color }: { title: string, value: string, color?: string }) {
  return <div className="bg-gray-900 border border-gray-700 rounded p-3">
    <div className="text-xs text-gray-500 mb-1">{title}</div>
    <div className={`text-lg font-bold ${color || 'text-cyan-400'}`}>{value}</div>
  </div>
}

export default function ControlCenter() {
  const [runtime, setRuntime] = useState<any>(null)
  const [summary, setSummary] = useState<any>(null)
  const [workers, setWorkers] = useState<any>({})
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const [r, s, w] = await Promise.all([
        api.getRuntimeStatus(),
        api.getPositionsSummary(),
        api.getWorkersStatus().catch(() => ({})),
      ])
      setRuntime(r || {})
      setSummary(s || {})
      setWorkers(w || {})
    } catch (e: any) {
      setMsg(e?.message || 'Refresh failed')
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 4000)
    return () => clearInterval(t)
  }, [refresh])

  const run = async (label: string, fn: () => Promise<any>) => {
    setBusy(true)
    try {
      const r = await fn()
      if (r?.ok === false) {
        setMsg(`${label} failed: ${r.error || 'unknown error'}`)
      } else {
        setMsg(`${label} completed`)
      }
      await refresh()
    } catch (e: any) {
      setMsg(`${label} failed: ${e?.message || e}`)
    } finally {
      setBusy(false)
    }
  }

  const mode = runtime?.user_mode || 'IDLE'
  const modeLabel = mode === 'FORMAL_SIM_LIVE' ? '实盘模式' : mode === 'SIM_TEST' ? '模拟模式' : '空闲'
  const killActive = !!runtime?.kill_switch_active || !!runtime?.pause_new_entries
  const workerList = Object.entries(workers || {}) as [string, any][]
  const runningCount = workerList.filter(([, w]) => w?.running).length
  const liveReady = !!runtime?.live_readiness?.ready

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Control Center</h1>

      {msg && <div className="bg-gray-800 border border-cyan-700 rounded p-2 mb-3 text-sm text-cyan-400">
        {msg}<button onClick={() => setMsg('')} className="ml-3 text-gray-500 hover:text-white">x</button>
      </div>}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
        <StatCard title="Runtime Mode" value={modeLabel} color={mode === 'FORMAL_SIM_LIVE' ? 'text-red-400' : mode === 'SIM_TEST' ? 'text-blue-400' : 'text-gray-400'} />
        <StatCard title="Workers" value={`${runningCount}/${workerList.length || 0}`} color={runningCount > 0 ? 'text-green-400' : 'text-gray-400'} />
        <StatCard title="Kill Switch" value={fmtBool(killActive)} color={killActive ? 'text-red-400' : 'text-green-400'} />
        <StatCard title="Live Readiness" value={liveReady ? 'READY' : 'BLOCKED'} color={liveReady ? 'text-green-400' : 'text-red-400'} />
        <StatCard title="LIVE Realized PnL" value={fmtSol(summary?.live_pnl_sol)} color={(summary?.live_pnl_sol || 0) >= 0 ? 'text-green-400' : 'text-red-400'} />
        <StatCard title="LIVE Open Value" value={`${Number(summary?.live_open_value_sol || 0).toFixed(4)} SOL`} color="text-red-300" />
        <StatCard title="SIM Realized PnL" value={fmtSol(summary?.sim_pnl_sol)} color={(summary?.sim_pnl_sol || 0) >= 0 ? 'text-green-400' : 'text-red-400'} />
        <StatCard title="SIM Open Value" value={`${Number(summary?.sim_open_value_sol || 0).toFixed(4)} SOL`} color="text-blue-300" />
      </div>

      <div className="bg-gray-900 border border-gray-700 rounded p-4 mb-4">
        <h2 className="text-sm font-bold mb-3 text-gray-300">Mode Switch</h2>
        <div className="flex gap-2 flex-wrap">
          <button disabled={busy} onClick={() => run('Switch to IDLE', () => api.switchMode('IDLE'))}
            className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-4 py-2 rounded text-sm">停止/空闲</button>
          <button disabled={busy} onClick={() => run('Switch to SIM_TEST', () => api.switchMode('SIM_TEST'))}
            className="bg-blue-800 hover:bg-blue-700 disabled:opacity-50 px-4 py-2 rounded text-sm text-white">模拟交易</button>
          <button disabled={busy || !liveReady || killActive} onClick={() => run('Switch to FORMAL_SIM_LIVE', () => api.switchMode('FORMAL_SIM_LIVE'))}
            className="bg-red-800 hover:bg-red-700 disabled:opacity-50 px-4 py-2 rounded text-sm text-white">实盘交易</button>
        </div>
        {!liveReady && <p className="text-xs text-red-400 mt-2">Live missing: {(runtime?.live_readiness?.missing || []).join(', ') || '-'}</p>}
      </div>

      <div className="bg-gray-900 border border-gray-700 rounded p-4 mb-4">
        <h2 className="text-sm font-bold mb-3 text-gray-300">Emergency</h2>
        <div className="flex gap-2 flex-wrap">
          <button disabled={busy} onClick={() => run(killActive ? 'Disable kill switch' : 'Enable kill switch', () => api.toggleKill(!killActive))}
            className={`${killActive ? 'bg-green-800 hover:bg-green-700' : 'bg-red-800 hover:bg-red-700'} disabled:opacity-50 px-4 py-2 rounded text-sm text-white`}>
            {killActive ? '解除 Kill Switch' : '启动 Kill Switch'}
          </button>
          <button disabled={busy} onClick={() => run('Stop live mode', () => api.stopLiveMode())}
            className="bg-orange-800 hover:bg-orange-700 disabled:opacity-50 px-4 py-2 rounded text-sm text-white">停止实盘并转模拟</button>
          <button disabled={busy || !liveReady} onClick={() => run('Resume live mode', () => api.resumeLiveMode())}
            className="bg-green-800 hover:bg-green-700 disabled:opacity-50 px-4 py-2 rounded text-sm text-white">恢复实盘入口</button>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-700 rounded p-4">
        <h2 className="text-sm font-bold mb-3 text-gray-300">Workers</h2>
        <div className="flex gap-2 mb-3">
          <button disabled={busy} onClick={() => run('Start workers', () => api.startWorkers())}
            className="bg-cyan-800 hover:bg-cyan-700 disabled:opacity-50 px-3 py-1.5 rounded text-xs text-white">Start All</button>
          <button disabled={busy} onClick={() => run('Stop workers', () => api.stopWorkers())}
            className="bg-gray-700 hover:bg-gray-600 disabled:opacity-50 px-3 py-1.5 rounded text-xs">Stop All</button>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-xs">
          {workerList.map(([name, w]) => <div key={name} className="bg-gray-800 rounded p-2 flex justify-between">
            <span className="text-gray-300">{name}</span>
            <span className={w?.running ? 'text-green-400' : 'text-gray-500'}>{w?.running ? 'running' : 'stopped'} · {w?.interval_sec ?? '-'}s</span>
          </div>)}
          {workerList.length === 0 && <p className="text-gray-500">No worker status.</p>}
        </div>
      </div>
    </div>
  )
}
