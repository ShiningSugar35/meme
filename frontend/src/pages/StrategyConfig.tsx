import { useEffect, useState } from 'react'
import { api } from '../api/client'

export default function StrategyConfig() {
  const [strategies, setStrategies] = useState<any[]>([])
  const [form, setForm] = useState({ name: '', x: 0.15, y: 2.25, t_seconds: 3600, is_live: false, priority: 100, raw_config_json: '{}' })

  const load = () => api.getStrategies().then(r => setStrategies(r || [])).catch(() => {})
  useEffect(() => { load() }, [])

  const create = async () => {
    await api.createStrategy(form)
    setForm({ name: '', x: 0.15, y: 2.25, t_seconds: 3600, is_live: false, priority: 100, raw_config_json: '{}' })
    load()
  }

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-cyan-400">Strategy Config</h1>
      <div className="bg-gray-900 border border-gray-700 rounded p-3 mb-4">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
          <input placeholder="Name" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <input type="number" placeholder="X" value={form.x} onChange={e => setForm({ ...form, x: +e.target.value })} className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <input type="number" placeholder="Y" value={form.y} onChange={e => setForm({ ...form, y: +e.target.value })} className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <input type="number" placeholder="T seconds" value={form.t_seconds} onChange={e => setForm({ ...form, t_seconds: +e.target.value })} className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
          <label className="flex items-center gap-1"><input type="checkbox" checked={form.is_live} onChange={e => setForm({ ...form, is_live: e.target.checked })} /> Live</label>
          <input type="number" placeholder="Priority" value={form.priority} onChange={e => setForm({ ...form, priority: +e.target.value })} className="bg-gray-800 border border-gray-600 rounded px-2 py-1" />
        </div>
        <div className="flex gap-2 mt-2">
          <button onClick={create} className="bg-cyan-700 hover:bg-cyan-600 px-3 py-1 rounded text-sm">Create</button>
          <button onClick={() => api.applyConfig()} className="bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded text-sm">Apply</button>
        </div>
      </div>
      <div className="bg-gray-900 border border-gray-700 rounded p-3">
        <table className="w-full text-xs">
          <thead><tr className="text-gray-400 border-b border-gray-700"><th className="text-left p-1">ID</th><th className="text-left">Name</th><th>X</th><th>Y</th><th>T(s)</th><th>Live</th><th>Priority</th></tr></thead>
          <tbody>
            {strategies.map(s => (
              <tr key={s.id} className="border-b border-gray-800">
                <td className="p-1">{s.id}</td><td>{s.name}</td><td>{s.x}</td><td>{s.y}</td><td>{s.t_seconds}</td>
                <td className={s.is_live ? 'text-green-400' : 'text-gray-500'}>{s.is_live ? 'YES' : 'no'}</td>
                <td>{s.priority}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
