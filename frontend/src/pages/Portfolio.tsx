import { useEffect, useState, useCallback } from 'react'
import { api } from '../api/client'

interface PosRow {
  id: number
  status: string
  entry_usd: number
  remaining: number
  price: number
  liquidity: number | null
  pnl_pct: number | null
  market_cap: number | null
  token_symbol: string | null
  mint_short: string
  mint: string
  account_type: string
  updated_at: string
}

function PosTable({ accountType, title, colorClass }: { accountType: string, title: string, colorClass: string }) {
  const [data, setData] = useState<PosRow[]>([])
  const [expanded, setExpanded] = useState<number | null>(null)
  const [sortKey, setSortKey] = useState<string>('')
  const [sortDir, setSortDir] = useState<1 | -1>(-1)

  const load = useCallback(() => {
    api.getPortfolioTable(accountType).then(r => setData(r || [])).catch(() => {})
  }, [accountType])

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  const sorted = [...data].sort((a, b) => {
    if (!sortKey) return 0
    const av = (a as any)[sortKey] ?? 0
    const bv = (b as any)[sortKey] ?? 0
    if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * sortDir
    return String(av).localeCompare(String(bv)) * sortDir
  })

  const doSort = (key: string) => {
    if (sortKey === key) setSortDir(d => d === 1 ? -1 : 1)
    else { setSortKey(key); setSortDir(-1) }
  }

  const SortBtn = ({ label }: { label: string }) => (
    <span className="cursor-pointer hover:text-cyan-400 select-none" onClick={() => doSort(label)}>{label}</span>
  )

  const StatusBadge = ({ status }: { status: string }) => {
    const map: Record<string, string> = {
      'POSITION_OPEN': 'bg-green-900 text-green-300',
      'SIM_OPEN': 'bg-blue-900 text-blue-300',
      'CLOSED': 'bg-gray-700 text-gray-400',
      'PENDING_ENTRY': 'bg-yellow-900 text-yellow-300',
      'PENDING_EXIT': 'bg-orange-900 text-orange-300',
      'FAILED': 'bg-red-900 text-red-300',
      'BLOCKED': 'bg-red-900 text-red-400',
      'LEGACY_INVALID_CONFIG': 'bg-purple-900 text-purple-300',
      'MIGRATION_NEEDED': 'bg-pink-900 text-pink-300',
    }
    return <span className={`px-1.5 py-0.5 rounded text-xs ${map[status] || 'bg-gray-800 text-gray-400'}`}>{status}</span>
  }

  return (
    <div className="mb-6">
      <h2 className={`text-sm font-bold mb-2 ${colorClass}`}>{title} ({data.length})</h2>
      <div className="bg-gray-900 border border-gray-700 rounded overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="text-left p-1.5"><SortBtn label="status" /></th>
              <th className="text-right p-1.5"><SortBtn label="entry_usd" /></th>
              <th className="text-right p-1.5"><SortBtn label="remaining" /></th>
              <th className="text-right p-1.5"><SortBtn label="price" /></th>
              <th className="text-right p-1.5"><SortBtn label="liquidity" /></th>
              <th className="text-right p-1.5"><SortBtn label="pnl_pct" /></th>
              <th className="text-right p-1.5"><SortBtn label="market_cap" /></th>
              <th className="w-4"></th>
            </tr>
          </thead>
          <tbody>
            {sorted.map(p => (
              <>
                <tr key={p.id} className="border-b border-gray-800 hover:bg-gray-850 cursor-pointer"
                  onClick={() => setExpanded(expanded === p.id ? null : p.id)}>
                  <td className="p-1.5">
                    <StatusBadge status={p.status} />
                    <span className="text-gray-500 ml-1 font-mono">{p.mint_short}</span>
                  </td>
                  <td className="text-right p-1.5">{p.entry_usd?.toFixed(6) || '-'}</td>
                  <td className="text-right p-1.5">{p.remaining?.toFixed(2) || '-'}</td>
                  <td className="text-right p-1.5">{p.price?.toFixed(6) || '-'}</td>
                  <td className="text-right p-1.5">{p.liquidity?.toFixed(0) || '-'}</td>
                  <td className={`text-right p-1.5 ${(p.pnl_pct || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {p.pnl_pct != null ? `${p.pnl_pct.toFixed(1)}%` : '-'}
                  </td>
                  <td className="text-right p-1.5">{p.market_cap?.toFixed(0) || '-'}</td>
                  <td className="p-1.5 text-gray-600">{expanded === p.id ? '▼' : '▶'}</td>
                </tr>
                {expanded === p.id && (
                  <tr key={`${p.id}-detail`} className="bg-gray-800/50">
                    <td colSpan={8} className="p-3 text-xs text-gray-400">
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                        <div><span className="text-gray-500">Mint:</span> <span className="font-mono text-gray-300">{p.mint}</span></div>
                        <div><span className="text-gray-500">Account:</span> <span className={p.account_type === 'LIVE' ? 'text-red-400' : 'text-blue-400'}>{p.account_type}</span></div>
                        <div><span className="text-gray-500">Status:</span> {p.status}</div>
                        <div><span className="text-gray-500">Updated:</span> {p.updated_at?.slice(0, 19) || '-'}</div>
                      </div>
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
        {data.length === 0 && <p className="text-gray-500 text-xs py-4 text-center">No positions yet.</p>}
      </div>
    </div>
  )
}

export default function Portfolio() {
  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Portfolio</h1>
      <PosTable accountType="LIVE" title="LIVE Positions" colorClass="text-red-400" />
      <PosTable accountType="SIM" title="SIM Positions" colorClass="text-blue-400" />
    </div>
  )
}
