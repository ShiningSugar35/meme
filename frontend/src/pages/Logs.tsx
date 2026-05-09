import { useEffect, useState, useRef } from 'react'
import { api } from '../api/client'

export default function Logs() {
  const [recent, setRecent] = useState<any[]>([])
  const [stream, setStream] = useState<string[]>([])
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    api.getRecentLogs().then(r => setRecent(r || [])).catch(() => {})

    const es = new EventSource('/api/logs/stream')
    esRef.current = es
    es.onmessage = (e) => {
      try { const d = JSON.parse(e.data); setStream(prev => [...prev.slice(-99), `[${d.level || 'INFO'}] ${d.category}: ${d.message}`]) } catch {}
    }
    es.onerror = () => {}
    return () => { es.close() }
  }, [])

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Logs</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-gray-900 border border-gray-700 rounded p-3">
          <h2 className="text-sm text-gray-400 mb-2">Recent ({recent.length})</h2>
          <div className="max-h-96 overflow-y-auto text-xs">
            {recent.map((l: any, i: number) => (
              <div key={i} className={`py-0.5 ${l.level === 'ERROR' ? 'text-red-400' : l.level === 'WARN' ? 'text-yellow-400' : 'text-gray-400'}`}>
                <span className="text-gray-600">{l.created_at?.slice(11, 19)}</span> [{l.level}] {l.category}: {l.message}
              </div>
            ))}
          </div>
        </div>
        <div className="bg-gray-900 border border-gray-700 rounded p-3">
          <h2 className="text-sm text-cyan-400 mb-2">Live Stream (SSE)</h2>
          <div className="max-h-96 overflow-y-auto text-xs">
            {stream.map((s, i) => <div key={i} className="py-0.5 text-gray-400">{s}</div>)}
          </div>
        </div>
      </div>
    </div>
  )
}
