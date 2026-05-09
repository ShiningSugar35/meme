import { useEffect, useState } from 'react'
import { api } from '../api/client'

export default function TokenStream() {
  const [tokens, setTokens] = useState<any[]>([])
  useEffect(() => { api.getTokens().then(r => setTokens(r || [])).catch(() => {}) }, [])

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Token Stream</h1>
      <div className="bg-gray-900 border border-gray-700 rounded p-3">
        <table className="w-full text-xs">
          <thead><tr className="text-gray-400 border-b border-gray-700"><th className="text-left p-1">Mint</th><th>Symbol</th><th>Price USD</th><th>Liquidity</th><th>MCap</th><th>State</th></tr></thead>
          <tbody>
            {tokens.map(t => (
              <tr key={t.token_mint} className="border-b border-gray-800">
                <td className="p-1 font-mono">{t.token_mint?.slice(0, 8)}...</td>
                <td>{t.symbol}</td>
                <td className="text-right">{t.latest_price_usd?.toFixed(6)}</td>
                <td className="text-right">{t.latest_liquidity_usd?.toFixed(0)}</td>
                <td className="text-right">{t.latest_market_cap?.toFixed(0)}</td>
                <td>{t.latest_state}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {tokens.length === 0 && <p className="text-gray-500 text-sm py-4 text-center">No tokens yet. Run mock lifecycle or wait for discovery.</p>}
      </div>
    </div>
  )
}
