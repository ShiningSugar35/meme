import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'

const MODE_LABELS: Record<string, string> = {
  'IDLE': 'IDLE',
  'SIM_TEST': '模拟交易',
  'FORMAL_SIM_LIVE': '实盘交易',
}

export default function ControlCenter() {
  const [runtime, setRuntime] = useState<any>(null)
  const [providers, setProviders] = useState<any[]>([])
  const [strategies, setStrategies] = useState<any[]>([])
  const [summary, setSummary] = useState<any>({})
  const [msg, setMsg] = useState('')
  const [confirmLive, setConfirmLive] = useState(false)

  const [form, setForm] = useState({
    name: '', x: 0.15, y: 2.25, t_seconds: 3600, is_live: false
  })

  const refresh = useCallback(() => {
    api.getRuntimeStatus().then(setRuntime).catch(() => {})
    api.getProviderHealth().then(r => setProviders(r?.providers || [])).catch(() => {})
    api.getStrategies().then(r => setStrategies(r || [])).catch(() => {})
    api.getPositionsSummary().then(setSummary).catch(() => {})
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 3000)
    return () => clearInterval(t)
  }, [refresh])

  const mode = runtime?.user_mode || 'IDLE'
  const canLiveTrade = runtime?.can_live_trade || false
  const liveReady = runtime?.live_readiness || {}

  const switchToMode = async (newMode: string) => {
    if (newMode === mode) {
      const r = await api.switchMode('IDLE')
      if (r.ok) { setMsg('Switched to IDLE'); refresh() }
      else setMsg(`Failed: ${r.error || 'unknown error'}`)
      return
    }
    if (newMode === 'FORMAL_SIM_LIVE') {
      setConfirmLive(true)
      return
    }
    const r = await api.switchMode(newMode)
    if (r.ok) { setMsg(`Switched to ${MODE_LABELS[newMode] || newMode}`); refresh() }
    else setMsg(`Failed: ${r.error || JSON.stringify(r.missing)}`)
  }

  const confirmSwitchToLive = async () => {
    setConfirmLive(false)
    const r = await api.switchMode('FORMAL_SIM_LIVE')
    if (r.ok) { setMsg('Switched to 实盘交易'); refresh() }
    else setMsg(`Failed: ${r.error || JSON.stringify(r.missing)}`)
  }

  const createStrategy = async () => {
    await api.createStrategy(form)
    setForm({ name: '', x: 0.15, y: 2.25, t_seconds: 3600, is_live: false })
    refresh()
  }

  const Badge = ({ ok }: { ok: boolean }) => (
    <span className={`inline-block w-2 h-2 rounded-full ${ok ? 'bg-green-500' : 'bg-red-500'}`} />
  )

  const Card = ({ title, children, className = '' }: { title: string, children: React.ReactNode, className?: string }) => (
    <div className={`bg-gray-900 border border-gray-700 rounded p-3 ${className}`}>
      <h3 className="text-sm text-gray-400 mb-2">{title}</h3>
      {children}
    </div>
  )

  const fmtUsd = (sol: number | undefined | null) => {
    if (sol == null) return '$0.00'
    const usd = sol * 150
    return `$${usd.toFixed(2)}`
  }

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Control Center</h1>

      {msg && <div className="bg-gray-800 border border-cyan-700 rounded p-2 mb-3 text-sm text-cyan-400">{msg}
        <button onClick={() => setMsg('')} className="ml-3 text-gray-500 hover:text-white">x</button>
      </div>}

      {confirmLive && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
          <div className="bg-gray-900 border border-red-600 rounded-lg p-6 max-w-md">
            <h2 className="text-lg text-red-400 font-bold mb-3">Confirm Live Trading Mode</h2>
            <p className="text-sm text-gray-300 mb-4">This will enable REAL transaction broadcasting. Your wallet will execute actual trades on Solana mainnet.</p>
            <div className="text-xs text-gray-400 mb-4">
              {Object.entries(liveReady).filter(([k]) => !['ready', 'missing'].includes(k)).map(([k, v]) => (
                <div key={k} className="flex items-center gap-2 py-0.5">
                  <Badge ok={!!v} /> {k}: {v ? 'OK' : 'MISSING'}
                </div>
              ))}
              {liveReady.missing?.length > 0 && (
                <div className="mt-2 text-red-400">Missing: {liveReady.missing.join(', ')}</div>
              )}
            </div>
            <div className="flex gap-3">
              <button onClick={confirmSwitchToLive} className="bg-red-700 hover:bg-red-600 px-4 py-2 rounded text-sm flex-1"
                disabled={!canLiveTrade}>Confirm Live Trading</button>
              <button onClick={() => setConfirmLive(false)} className="bg-gray-700 hover:bg-gray-600 px-4 py-2 rounded text-sm">Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Mode Switch Buttons */}
      <div className="flex gap-3 mb-4">
        <button onClick={() => switchToMode('SIM_TEST')}
          className={`px-4 py-2 rounded text-sm ${mode === 'SIM_TEST' ? 'bg-cyan-700 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'}`}>
          模拟交易
        </button>
        <button onClick={() => switchToMode('FORMAL_SIM_LIVE')}
          className={`px-4 py-2 rounded text-sm ${mode === 'FORMAL_SIM_LIVE' ? 'bg-red-700 text-white' : 'bg-gray-700 text-gray-400 hover:bg-gray-600'}`}>
          实盘交易
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3 mb-4">
        <Card title="模拟盘">
          <span className={`text-lg font-bold ${mode === 'FORMAL_SIM_LIVE' ? 'text-red-400' : mode === 'SIM_TEST' ? 'text-cyan-400' : 'text-gray-500'}`}>
            {MODE_LABELS[mode] || mode}
          </span>
        </Card>
        <Card title="实盘开仓">
          <span className="text-lg">{summary.live_open_count ?? 0}</span>
        </Card>
        <Card title="模拟盘开仓">
          <span className="text-lg">{summary.sim_open_count ?? 0}</span>
        </Card>
        <Card title="实盘收益">
          <span className={`text-lg ${(summary.live_pnl_sol || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {fmtUsd(summary.live_pnl_sol)}
          </span>
        </Card>
        <Card title="模拟盘收益">
          <span className={`text-lg ${(summary.sim_pnl_sol || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {fmtUsd(summary.sim_pnl_sol)}
          </span>
        </Card>
      </div>

      {/* Provider Health */}
      <Card title="Provider Health" className="mb-4">
        {providers.map((p: any, i: number) => (
          <div key={i} className="text-xs mb-1 flex items-center gap-2">
            <Badge ok={p.ok} />
            <span className="text-gray-300">{p.provider}</span>
            <span className={`${p.ok ? 'text-green-400' : 'text-red-400'}`}>{p.ok ? 'OK' : p.summary || 'DEGRADED'}</span>
            {p.latency_ms > 0 && <span className="text-gray-500 ml-auto">{p.latency_ms}ms</span>}
          </div>
        ))}
        {providers.length === 0 && <p className="text-xs text-gray-500">No provider data</p>}
      </Card>

      {/* Strategy Groups */}
      <Card title="Strategy Groups" className="mb-4">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs mb-2">
          <input placeholder="Name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <input type="number" placeholder="X" value={form.x} step={0.01}
            onChange={e => setForm({ ...form, x: +e.target.value })}
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <input type="number" placeholder="Y" value={form.y} step={0.01}
            onChange={e => setForm({ ...form, y: +e.target.value })}
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <input type="number" placeholder="T seconds" value={form.t_seconds}
            onChange={e => setForm({ ...form, t_seconds: +e.target.value })}
            className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <label className="flex items-center gap-1 text-gray-400">
            <input type="checkbox" checked={form.is_live} onChange={e => setForm({ ...form, is_live: e.target.checked })} /> Live
          </label>
        </div>
        <div className="flex gap-2">
          <button onClick={createStrategy} className="bg-cyan-700 hover:bg-cyan-600 px-3 py-1 rounded text-xs">Create</button>
          <button onClick={() => { api.applyConfig(); refresh() }} className="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded text-xs">Apply Config</button>
        </div>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-gray-400 border-b border-gray-700">
              <th className="text-left p-1">ID</th><th className="text-left">Name</th><th>X</th><th>Y</th><th>T(s)</th><th>Live</th><th>Enabled</th>
            </tr></thead>
            <tbody>
              {strategies.map(s => (
                <tr key={s.id} className="border-b border-gray-800 hover:bg-gray-850">
                  <td className="p-1">{s.id}</td><td>{s.name}</td><td>{s.x}</td><td>{s.y}</td><td>{s.t_seconds}</td>
                  <td className={s.is_live ? 'text-green-400' : 'text-gray-500'}>{s.is_live ? 'LIVE' : 'sim'}</td>
                  <td>{s.enabled ? <Badge ok={true} /> : <Badge ok={false} />}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {strategies.length === 0 && <p className="text-gray-500 text-xs py-4 text-center">No strategies configured. Create one above.</p>}
        </div>
      </Card>
    </div>
  )
}
