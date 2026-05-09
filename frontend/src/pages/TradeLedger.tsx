import { useEffect, useState } from 'react'
import { api } from '../api/client'

export default function TradeLedger() {
  const [trades, setTrades] = useState<any[]>([])
  const [reqs, setReqs] = useState<any[]>([])
  useEffect(() => {
    api.getTrades().then(r => setTrades(r || [])).catch(() => {})
    api.getProviderRequests().then(r => setReqs(r || [])).catch(() => {})
  }, [])

  function TradeTable({ data, title }: { data: any[], title: string }) {
    return (
      <div className="mb-4">
        <h2 className="text-sm text-gray-400 mb-2">{title} ({data.length})</h2>
        <table className="w-full text-xs">
          <thead><tr className="text-gray-400 border-b border-gray-700"><th className="text-left p-1">ID</th><th>Side</th><th>Type</th><th>Status</th><th>Impact</th><th>Sig</th></tr></thead>
          <tbody>
            {data.slice(0, 30).map((t: any) => (
              <tr key={t.id} className="border-b border-gray-800">
                <td className="p-1">{t.id}</td>
                <td>{t.side}</td>
                <td>{t.event_type}</td>
                <td className={t.status === 'CONFIRMED' ? 'text-green-400' : t.status === 'FAILED' ? 'text-red-400' : 'text-yellow-400'}>{t.status}</td>
                <td className="text-right">{t.price_impact_pct?.toFixed(4)}</td>
                <td className="font-mono text-gray-500">{t.tx_signature?.slice(0, 10) || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Trade Ledger</h1>
      <div className="bg-gray-900 border border-gray-700 rounded p-3">
        <TradeTable data={trades} title="Trade Events" />
        <h2 className="text-sm text-gray-400 mb-2 mt-4">Provider Requests ({reqs.length})</h2>
        <table className="w-full text-xs">
          <thead><tr className="text-gray-400 border-b border-gray-700"><th className="text-left p-1">ID</th><th>Provider</th><th>Endpoint</th><th>Status</th><th>Latency</th></tr></thead>
          <tbody>
            {reqs.slice(0, 20).map((r: any) => (
              <tr key={r.id} className="border-b border-gray-800">
                <td className="p-1">{r.id}</td><td>{r.provider}</td><td className="font-mono text-gray-500">{r.endpoint?.slice(0, 40)}</td>
                <td className={r.ok ? 'text-green-400' : 'text-red-400'}>{r.ok ? 'OK' : 'ERR'}</td>
                <td className="text-right">{r.latency_ms}ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
