import { useEffect, useMemo, useState, useCallback } from 'react'
import { api } from '../api/client'

const fmt = (v: any, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '-'
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits })
}
const fmtUsd = (v: any) => v === null || v === undefined ? '-' : `$${fmt(v, 6)}`
const fmtPct = (v: any) => v === null || v === undefined ? '-' : `${Number(v) >= 0 ? '+' : ''}${fmt(v, 2)}%`
const fmtSol = (v: any) => v === null || v === undefined ? '-' : `${fmt(v, 4)} SOL`

export default function Portfolio() {
  const [accountType, setAccountType] = useState('LIVE')
  const [rows, setRows] = useState<any[]>([])
  const [summary, setSummary] = useState<any>({})
  const [sortKey, setSortKey] = useState('updated_at')
  const [sortDesc, setSortDesc] = useState(true)
  const [msg, setMsg] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [table, s] = await Promise.all([
        api.getPortfolioTable(accountType),
        api.getPositionsSummary().catch(() => ({})),
      ])
      setRows(Array.isArray(table) ? table : [])
      setSummary(s || {})
    } catch (e: any) {
      setMsg(e?.message || 'Load portfolio failed')
    }
  }, [accountType])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 5000)
    return () => clearInterval(t)
  }, [refresh])

  const sorted = useMemo(() => {
    const r = [...rows]
    r.sort((a, b) => {
      const av = a?.[sortKey]
      const bv = b?.[sortKey]
      if (av === bv) return 0
      if (av === null || av === undefined) return 1
      if (bv === null || bv === undefined) return -1
      const an = Number(av)
      const bn = Number(bv)
      let cmp = 0
      if (!Number.isNaN(an) && !Number.isNaN(bn)) cmp = an - bn
      else cmp = String(av).localeCompare(String(bv))
      return sortDesc ? -cmp : cmp
    })
    return r
  }, [rows, sortKey, sortDesc])

  const setSort = (key: string) => {
    if (key === sortKey) setSortDesc(!sortDesc)
    else { setSortKey(key); setSortDesc(true) }
  }

  const closePosition = async (id: number) => {
    setBusyId(id)
    try {
      const r = await api.manualClose(id)
      if (r?.ok === false) setMsg(`Close failed: ${r.error || 'unknown error'}`)
      else setMsg(`Close request sent for position ${id}`)
      await refresh()
    } catch (e: any) {
      setMsg(e?.message || 'Close failed')
    } finally {
      setBusyId(null)
    }
  }

  const SortBtn = ({ k, children }: { k: string, children: any }) =>
    <button onClick={() => setSort(k)} className="hover:text-cyan-300">
      {children}{sortKey === k ? (sortDesc ? ' ↓' : ' ↑') : ''}
    </button>

  const pnlColor = (v: any) => Number(v || 0) >= 0 ? 'text-green-400' : 'text-red-400'
  const prefix = accountType.toLowerCase()

  return <div>
    <div className="flex items-center justify-between mb-4">
      <h1 className="text-xl font-bold text-cyan-400">Portfolio</h1>
      <div className="flex gap-2">
        <button onClick={() => setAccountType('LIVE')} className={`px-3 py-1 rounded text-sm ${accountType === 'LIVE' ? 'bg-red-800 text-white' : 'bg-gray-800 text-gray-400'}`}>LIVE</button>
        <button onClick={() => setAccountType('SIM')} className={`px-3 py-1 rounded text-sm ${accountType === 'SIM' ? 'bg-blue-800 text-white' : 'bg-gray-800 text-gray-400'}`}>SIM</button>
      </div>
    </div>

    {msg && <div className="bg-gray-800 border border-cyan-700 rounded p-2 mb-3 text-sm text-cyan-400">
      {msg}<button onClick={() => setMsg('')} className="ml-3 text-gray-500 hover:text-white">x</button>
    </div>}

    <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
      <div className="bg-gray-900 border border-gray-700 rounded p-3"><div className="text-xs text-gray-500">Open Count</div><div className="text-lg font-bold text-cyan-300">{summary?.[`${prefix}_open_count`] ?? 0}</div></div>
      <div className="bg-gray-900 border border-gray-700 rounded p-3"><div className="text-xs text-gray-500">Open Cost</div><div className="text-lg font-bold text-gray-200">{fmtSol(summary?.[`${prefix}_open_cost_sol`])}</div></div>
      <div className="bg-gray-900 border border-gray-700 rounded p-3"><div className="text-xs text-gray-500">Open Value</div><div className="text-lg font-bold text-gray-200">{fmtSol(summary?.[`${prefix}_open_value_sol`])}</div></div>
      <div className="bg-gray-900 border border-gray-700 rounded p-3"><div className="text-xs text-gray-500">Realized PnL</div><div className={`text-lg font-bold ${pnlColor(summary?.[`${prefix}_pnl_sol`])}`}>{fmtSol(summary?.[`${prefix}_pnl_sol`])}</div></div>
    </div>

    <div className="bg-gray-900 border border-gray-700 rounded overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="bg-gray-950 text-gray-500 border-b border-gray-700">
          <tr>
            <th className="text-left p-2"><SortBtn k="id">ID</SortBtn></th>
            <th className="text-left p-2">Token</th>
            <th className="p-2"><SortBtn k="status">Status</SortBtn></th>
            <th className="p-2"><SortBtn k="ratio">倍率</SortBtn></th>
            <th className="p-2"><SortBtn k="remaining_sol">持仓SOL</SortBtn></th>
            <th className="p-2"><SortBtn k="pnl_pct">PnL%</SortBtn></th>
            <th className="p-2"><SortBtn k="pnl_sol">PnL SOL</SortBtn></th>
            <th className="p-2"><SortBtn k="liquidity">Liquidity</SortBtn></th>
            <th className="p-2"><SortBtn k="market_cap">Market Cap</SortBtn></th>
            <th className="p-2"><SortBtn k="updated_at">Updated</SortBtn></th>
            <th className="p-2">Action</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => <tr key={r.id} className="border-b border-gray-800 hover:bg-gray-800/50">
            <td className="p-2 text-gray-400">#{r.id}</td>
            <td className="p-2">
              <div className="text-gray-200">{r.token_symbol || r.mint_short}</div>
              <div className="text-gray-600 font-mono">{r.mint_short}</div>
            </td>
            <td className="p-2 text-center"><span className={`px-2 py-0.5 rounded ${r.status === 'CLOSED' ? 'bg-gray-700 text-gray-400' : 'bg-green-900 text-green-300'}`}>{r.status}</span></td>
            <td className={`p-2 text-center ${Number(r.ratio || 1) >= 1 ? 'text-green-400' : 'text-red-400'}`}>{r.ratio === null || r.ratio === undefined ? '-' : `${fmt(r.ratio, 2)}x`}</td>
            <td className="p-2 text-center text-cyan-300">{fmtSol(r.remaining_sol)}</td>
            <td className={`p-2 text-center ${pnlColor(r.pnl_pct)}`}>{fmtPct(r.pnl_pct)}</td>
            <td className={`p-2 text-center ${pnlColor(r.pnl_sol)}`}>{fmtSol(r.pnl_sol)}</td>
            <td className="p-2 text-center">{fmtUsd(r.liquidity)}</td>
            <td className="p-2 text-center">{fmtUsd(r.market_cap)}</td>
            <td className="p-2 text-center text-gray-500">{r.updated_at ? String(r.updated_at).slice(0, 19) : '-'}</td>
            <td className="p-2 text-center">
              {r.status !== 'CLOSED'
                ? <button disabled={busyId === r.id} onClick={() => closePosition(r.id)} className="bg-red-800 hover:bg-red-700 disabled:opacity-50 px-2 py-1 rounded text-white">Close</button>
                : <span className="text-gray-600">-</span>}
            </td>
          </tr>)}
        </tbody>
      </table>
      {sorted.length === 0 && <div className="text-center text-gray-500 py-8">No positions.</div>}
    </div>
  </div>
}
