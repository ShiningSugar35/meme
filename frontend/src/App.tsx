import { Routes, Route, NavLink } from 'react-router-dom'
import Dashboard from './pages/Dashboard'
import StrategyConfig from './pages/StrategyConfig'
import TokenStream from './pages/TokenStream'
import Positions from './pages/Positions'
import TradeLedger from './pages/TradeLedger'
import Logs from './pages/Logs'
import EmergencyPanel from './pages/EmergencyPanel'

const nav = [
  { to: '/', label: 'Dashboard' },
  { to: '/strategies', label: 'Strategies' },
  { to: '/tokens', label: 'Tokens' },
  { to: '/positions', label: 'Positions' },
  { to: '/trades', label: 'Trades' },
  { to: '/logs', label: 'Logs' },
  { to: '/emergency', label: 'Emergency' },
]

export default function App() {
  return (
    <div className="min-h-screen flex flex-col">
      <nav className="bg-gray-900 border-b border-gray-700 px-4 py-2 flex gap-4 text-sm flex-wrap">
        {nav.map(n => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `hover:text-cyan-400 ${isActive ? 'text-cyan-400 font-bold' : 'text-gray-400'}`}>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <main className="flex-1 p-4">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/strategies" element={<StrategyConfig />} />
          <Route path="/tokens" element={<TokenStream />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/trades" element={<TradeLedger />} />
          <Route path="/logs" element={<Logs />} />
          <Route path="/emergency" element={<EmergencyPanel />} />
        </Routes>
      </main>
    </div>
  )
}
