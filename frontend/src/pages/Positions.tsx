import { useEffect, useState } from 'react'
import { api } from '../api/client'

export default function Positions() {
  const [positions, setPositions] = useState<any[]>([])
  const load = () => api.getPositions().then(r => setPositions(r || [])).catch(() => {})
  useEffect(() => { load() }, [])

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Positions</h1>
      <div className="bg-gray-900 border border-gray-700 rounded p-3">
        <table className="w-full text-xs">
          <thead><tr className="text-gray-400 border-b border-gray-700"><th className="text-left p-1">ID</th><th>Token</th><th>Status</th><th>Live</th><th>Entry $</th><th>Remaining</th><th>PnL %</th><th>Reason</th><th></th></tr></thead>
          <tbody>
            {positions.map(p => (
              <tr key={p.id} className="border-b border-gray-800">
                <td className="p-1">{p.id}</td>
                <td className="font-mono">{p.token_mint?.slice(0, 8)}...</td>
                <td className={p.status === 'POSITION_OPEN' ? 'text-green-400' : 'text-gray-500'}>{p.status}</td>
                <td>{p.is_live ? 'LIVE' : 'SIM'}</td>
                <td className="text-right">{p.entry_price_usd?.toFixed(6)}</td>
                <td className="text-right">{p.remaining_value_usd?.toFixed(2)}</td>
                <td className={`text-right ${(p.realized_pnl_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {p.realized_pnl_pct?.toFixed(1) ?? '-'}%
                </td>
                <td className="text-gray-500">{p.close_reason || '-'}</td>
                <td>{p.status !== 'CLOSED' && <button onClick={async () => { await api.manualClose(p.id); load() }} className="bg-red-800 hover:bg-red-700 px-2 py-0.5 rounded text-xs">Close</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
