import { useEffect, useState } from 'react'
import { api } from '../api/client'

export default function Dashboard() {
  const [health, setHealth] = useState<any>(null)
  const [providers, setProviders] = useState<any[]>([])
  const [ks, setKs] = useState<any>(null)
  const [logs, setLogs] = useState<any[]>([])
  const [positions, setPositions] = useState<any[]>([])

  useEffect(() => {
    api.health().then(setHealth).catch(() => {})
    api.getProviderHealth().then(r => setProviders(r?.providers || [])).catch(() => {})
    api.getKillSwitch().then(setKs).catch(() => {})
    api.getRecentLogs().then(setLogs).catch(() => {})
    api.getPositions().then(setPositions).catch(() => {})
  }, [])

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Dashboard</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-gray-900 border border-gray-700 rounded p-3">
          <h2 className="text-sm text-gray-400 mb-2">System Status</h2>
          <pre className="text-xs text-green-400">{JSON.stringify(health, null, 2)}</pre>
        </div>
        <div className="bg-gray-900 border border-gray-700 rounded p-3">
          <h2 className="text-sm text-gray-400 mb-2">Risk / Kill Switch</h2>
          <pre className="text-xs">{JSON.stringify(ks, null, 2)}</pre>
        </div>
        <div className="bg-gray-900 border border-gray-700 rounded p-3">
          <h2 className="text-sm text-gray-400 mb-2">Provider Health</h2>
          {providers.map((p: any, i: number) => (
            <div key={i} className="text-xs mb-1">
              <span className={p.ok ? 'text-green-400' : 'text-red-400'}>{p.provider}</span>
              <span className="text-gray-500 ml-2">{p.ok ? 'OK' : 'DEGRADED'} {p.latency_ms}ms</span>
            </div>
          ))}
        </div>
        <div className="bg-gray-900 border border-gray-700 rounded p-3">
          <h2 className="text-sm text-gray-400 mb-2">Open Positions ({positions.length})</h2>
          {positions.slice(0, 5).map((p: any) => (
            <div key={p.id} className="text-xs text-gray-300 border-b border-gray-800 py-1">
              {p.token_mint?.slice(0, 12)}... {p.status} rem={p.remaining_value_usd?.toFixed(2)} pnl={p.realized_pnl_pct?.toFixed(1)}%
            </div>
          ))}
        </div>
        <div className="bg-gray-900 border border-gray-700 rounded p-3 md:col-span-2">
          <h2 className="text-sm text-gray-400 mb-2">Recent Logs ({logs.length})</h2>
          <div className="max-h-40 overflow-y-auto text-xs">
            {logs.slice(0, 20).map((l: any, i: number) => (
              <div key={i} className={`py-0.5 ${l.level === 'ERROR' ? 'text-red-400' : l.level === 'WARN' ? 'text-yellow-400' : 'text-gray-400'}`}>
                [{l.level}] {l.category}: {l.message}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
