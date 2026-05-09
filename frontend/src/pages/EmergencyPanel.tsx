import { useState } from 'react'
import { api } from '../api/client'

export default function EmergencyPanel() {
  const [msg, setMsg] = useState('')

  return (
    <div>
      <h1 className="text-xl font-bold mb-4 text-red-500">Emergency Panel</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-gray-900 border border-red-700 rounded p-4">
          <h2 className="text-sm text-gray-400 mb-3">New Entries Control</h2>
          <div className="flex gap-2">
            <button onClick={async () => { await api.pauseNewEntries(); setMsg('New entries PAUSED') }} className="bg-red-800 hover:bg-red-700 px-4 py-2 rounded text-sm">Pause New Entries</button>
            <button onClick={async () => { await api.resumeNewEntries(); setMsg('New entries RESUMED') }} className="bg-green-800 hover:bg-green-700 px-4 py-2 rounded text-sm">Resume New Entries</button>
          </div>
        </div>
        <div className="bg-gray-900 border border-red-700 rounded p-4">
          <h2 className="text-sm text-gray-400 mb-3">Kill Switch</h2>
          <button onClick={async () => { await api.resetKillSwitch(); setMsg('Kill switch RESET') }} className="bg-yellow-800 hover:bg-yellow-700 px-4 py-2 rounded text-sm">Reset Kill Switch</button>
        </div>
        <div className="bg-gray-900 border border-red-700 rounded p-4">
          <h2 className="text-sm text-gray-400 mb-3">Simulation</h2>
          <button onClick={async () => { await api.mockRunOnce(); setMsg('Mock lifecycle triggered') }} className="bg-gray-700 hover:bg-gray-600 px-4 py-2 rounded text-sm">Run Mock Lifecycle</button>
        </div>
      </div>
      {msg && <p className="mt-4 text-sm text-cyan-400">{msg}</p>}
    </div>
  )
}
