import { Routes, Route, NavLink } from 'react-router-dom'
import ControlCenter from './pages/ControlCenter'
import Portfolio from './pages/Portfolio'
import Operations from './pages/Operations'

const nav = [
  { to: '/', label: 'Control Center' },
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/ops', label: 'Ops & Emergency' },
]

export default function App() {
  return (
    <div className="min-h-screen flex flex-col bg-gray-950 text-gray-200">
      <nav className="bg-gray-900 border-b border-gray-700 px-4 py-2 flex gap-6 text-sm">
        {nav.map(n => (
          <NavLink key={n.to} to={n.to} end={n.to === '/'} className={({ isActive }) =>
            `hover:text-cyan-400 transition-colors ${isActive ? 'text-cyan-400 font-bold' : 'text-gray-400'}`
          }>
            {n.label}
          </NavLink>
        ))}
      </nav>
      <main className="flex-1 p-4 overflow-auto">
        <Routes>
          <Route path="/" element={<ControlCenter />} />
          <Route path="/portfolio" element={<Portfolio />} />
          <Route path="/ops" element={<Operations />} />
        </Routes>
      </main>
    </div>
  )
}
