import { useCallback, useEffect, useState } from 'react'
import { api, runtimeStrategyApi, RuntimeStrategyPayload } from '../api/client'

const fmtSol = (n: any) => {
  const v = Number(n || 0)
  return `${v >= 0 ? '+' : ''}${v.toFixed(4)} SOL`
}

function StatCard({ title, value, color }: { title: string, value: string, color?: string }) {
  return <div className="bg-gray-900 border border-gray-700 rounded p-3">
    <div className="text-xs text-gray-500 mb-1">{title}</div>
    <div className={`text-lg font-bold ${color || 'text-cyan-400'}`}>{value}</div>
  </div>
}

type StrategyRow = {
  id?: number
  name?: string
  x: number
  y: number
  t_seconds: number
  enabled?: boolean
  is_live?: boolean
  priority?: number
  config_version?: number
}

const newStrategy = (): StrategyRow => ({
  name: '',
  x: 0.2,
  y: 2.25,
  t_seconds: 150,
  enabled: true,
  is_live: false,
  priority: 100,
})

const normalizeStrategyPayload = (row: StrategyRow): RuntimeStrategyPayload => ({
  name: row.name?.trim() || `x=${Number(row.x)}, y=${Number(row.y)}, t=${Number(row.t_seconds)}s`,
  x: Number(row.x),
  y: Number(row.y),
  t_seconds: Math.max(0, Math.floor(Number(row.t_seconds))),
  enabled: Boolean(row.enabled),
  is_live: Boolean(row.is_live),
  priority: Math.floor(Number(row.priority ?? 100)),
})

export default function ControlCenter() {
  const [runtime, setRuntime] = useState<any>(null)
  const [summary, setSummary] = useState<any>(null)
  const [strategies, setStrategies] = useState<StrategyRow[]>([])
  const [draft, setDraft] = useState<StrategyRow>(newStrategy())
  const [msg, setMsg] = useState<string>('')
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    try {
      const [runtimeStatus, positionsSummary, strategyResp] = await Promise.all([
        api.getRuntimeStatus(),
        api.getPositionsSummary(),
        runtimeStrategyApi.list(),
      ])
      setRuntime(runtimeStatus)
      setSummary(positionsSummary)
      setStrategies((strategyResp?.strategies || []).sort((a: StrategyRow, b: StrategyRow) => Number(a.priority ?? 100) - Number(b.priority ?? 100)))
    } catch (e: any) {
      setMsg(`加载失败：${e?.message || e}`)
    }
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [load])

  const switchMode = async (mode: string) => {
    setLoading(true)
    setMsg('')
    try {
      const r = await api.switchMode(mode)
      if (r?.ok === false) setMsg(r.error || '切换失败')
      else setMsg(`模式已切换为 ${mode}`)
      await load()
    } catch (e: any) {
      setMsg(`切换失败：${e?.message || e}`)
    } finally {
      setLoading(false)
    }
  }

  const patchStrategy = (id: number | undefined, patch: Partial<StrategyRow>) => {
    if (id === undefined) return
    setStrategies(prev => prev.map(row => row.id === id ? { ...row, ...patch } : row))
  }

  const saveStrategy = async (row: StrategyRow) => {
    if (!row.id) return
    setLoading(true)
    setMsg('')
    try {
      await runtimeStrategyApi.update(row.id, normalizeStrategyPayload(row))
      setMsg('策略组已保存；下一轮 trench 轮询自动生效')
      await load()
    } catch (e: any) {
      setMsg(`保存失败：${e?.message || e}`)
    } finally {
      setLoading(false)
    }
  }

  const addStrategy = async () => {
    setLoading(true)
    setMsg('')
    try {
      await runtimeStrategyApi.create(normalizeStrategyPayload(draft))
      setDraft(newStrategy())
      setMsg('策略组已新增；下一轮 trench 轮询自动生效')
      await load()
    } catch (e: any) {
      setMsg(`新增失败：${e?.message || e}`)
    } finally {
      setLoading(false)
    }
  }

  const deleteStrategy = async (id?: number) => {
    if (!id) return
    if (!window.confirm('确定删除这个策略组？删除后下一轮 trench 轮询不再使用它。')) return
    setLoading(true)
    setMsg('')
    try {
      await runtimeStrategyApi.remove(id)
      setMsg('策略组已删除；下一轮 trench 轮询自动生效')
      await load()
    } catch (e: any) {
      setMsg(`删除失败：${e?.message || e}`)
    } finally {
      setLoading(false)
    }
  }

  const mode = runtime?.user_mode || 'IDLE'
  const liveReady = runtime?.live_readiness?.ready
  const enabledCount = strategies.filter(s => s.enabled).length

  return <div className="space-y-6">
    <div className="flex items-center justify-between">
      <div>
        <h1 className="text-2xl font-bold text-white">Control Center</h1>
        <p className="text-sm text-gray-400 mt-1">交易模式控制与动态策略组配置。Workers 随交易模式自动启动或暂停，不再单独手动控制。</p>
      </div>
      <button onClick={load} className="px-3 py-2 rounded bg-gray-800 border border-gray-700 text-gray-200 hover:bg-gray-700">刷新</button>
    </div>

    {msg && <div className="bg-gray-900 border border-gray-700 rounded p-3 text-sm text-gray-200">{msg}</div>}

    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <StatCard title="User Mode" value={mode} color={mode === 'FORMAL_SIM_LIVE' ? 'text-red-400' : mode === 'SIM_TEST' ? 'text-green-400' : 'text-gray-300'} />
      <StatCard title="Workers" value={runtime?.workers_enabled ? 'AUTO ON' : 'AUTO OFF'} color={runtime?.workers_enabled ? 'text-green-400' : 'text-gray-300'} />
      <StatCard title="Enabled Strategies" value={`${enabledCount}/${strategies.length}`} />
      <StatCard title="PnL" value={fmtSol(summary?.total_pnl_sol)} color={Number(summary?.total_pnl_sol || 0) >= 0 ? 'text-green-400' : 'text-red-400'} />
    </div>

    <div className="bg-gray-900 border border-gray-700 rounded p-4">
      <h2 className="text-lg font-semibold text-white mb-3">Mode</h2>
      <div className="flex flex-wrap gap-3">
        <button disabled={loading} onClick={() => switchMode('SIM_TEST')} className="px-4 py-2 rounded bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white">启动模拟交易</button>
        <button disabled={loading || !liveReady} onClick={() => switchMode('FORMAL_SIM_LIVE')} className="px-4 py-2 rounded bg-red-700 hover:bg-red-600 disabled:opacity-50 text-white">启动实盘交易</button>
        <button disabled={loading} onClick={() => switchMode('IDLE')} className="px-4 py-2 rounded bg-gray-700 hover:bg-gray-600 disabled:opacity-50 text-white">暂停交易</button>
      </div>
      {!liveReady && <div className="text-xs text-yellow-400 mt-3">实盘未就绪：{(runtime?.live_readiness?.missing || []).join(', ') || '请检查配置'}</div>}
    </div>

    <div className="bg-gray-900 border border-gray-700 rounded p-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h2 className="text-lg font-semibold text-white">Strategy Groups</h2>
          <p className="text-xs text-gray-500 mt-1">三列核心参数为风控 x、倍率 y、入场时机 t。保存、新增、删除后由后端在下一轮 trench 轮询动态读取。</p>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-gray-400 border-b border-gray-700">
            <tr>
              <th className="text-left py-2 pr-2">启用</th>
              <th className="text-left py-2 pr-2">名称</th>
              <th className="text-left py-2 pr-2">风控 x</th>
              <th className="text-left py-2 pr-2">倍率 y</th>
              <th className="text-left py-2 pr-2">入场时机 t(s)</th>
              <th className="text-left py-2 pr-2">优先级</th>
              <th className="text-left py-2 pr-2">版本</th>
              <th className="text-right py-2 pl-2">操作</th>
            </tr>
          </thead>
          <tbody>
            {strategies.map(row => <tr key={row.id} className="border-b border-gray-800">
              <td className="py-2 pr-2"><input type="checkbox" checked={Boolean(row.enabled)} onChange={e => patchStrategy(row.id, { enabled: e.target.checked })} /></td>
              <td className="py-2 pr-2"><input className="w-44 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={row.name || ''} onChange={e => patchStrategy(row.id, { name: e.target.value })} /></td>
              <td className="py-2 pr-2"><input type="number" step="0.01" className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={row.x} onChange={e => patchStrategy(row.id, { x: Number(e.target.value) })} /></td>
              <td className="py-2 pr-2"><input type="number" step="0.01" className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={row.y} onChange={e => patchStrategy(row.id, { y: Number(e.target.value) })} /></td>
              <td className="py-2 pr-2"><input type="number" step="1" className="w-28 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={row.t_seconds} onChange={e => patchStrategy(row.id, { t_seconds: Number(e.target.value) })} /></td>
              <td className="py-2 pr-2"><input type="number" step="1" className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={row.priority ?? 100} onChange={e => patchStrategy(row.id, { priority: Number(e.target.value) })} /></td>
              <td className="py-2 pr-2 text-gray-400">v{row.config_version ?? '-'}</td>
              <td className="py-2 pl-2 text-right space-x-2 whitespace-nowrap">
                <button disabled={loading} onClick={() => saveStrategy(row)} className="px-3 py-1 rounded bg-cyan-700 hover:bg-cyan-600 disabled:opacity-50 text-white">保存</button>
                <button disabled={loading} onClick={() => deleteStrategy(row.id)} className="px-3 py-1 rounded bg-gray-800 hover:bg-red-800 disabled:opacity-50 text-gray-200">删除</button>
              </td>
            </tr>)}

            <tr className="border-b border-gray-800 bg-gray-950/40">
              <td className="py-2 pr-2"><input type="checkbox" checked={Boolean(draft.enabled)} onChange={e => setDraft(prev => ({ ...prev, enabled: e.target.checked }))} /></td>
              <td className="py-2 pr-2"><input className="w-44 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" placeholder="可留空自动命名" value={draft.name || ''} onChange={e => setDraft(prev => ({ ...prev, name: e.target.value }))} /></td>
              <td className="py-2 pr-2"><input type="number" step="0.01" className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={draft.x} onChange={e => setDraft(prev => ({ ...prev, x: Number(e.target.value) }))} /></td>
              <td className="py-2 pr-2"><input type="number" step="0.01" className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={draft.y} onChange={e => setDraft(prev => ({ ...prev, y: Number(e.target.value) }))} /></td>
              <td className="py-2 pr-2"><input type="number" step="1" className="w-28 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={draft.t_seconds} onChange={e => setDraft(prev => ({ ...prev, t_seconds: Number(e.target.value) }))} /></td>
              <td className="py-2 pr-2"><input type="number" step="1" className="w-24 bg-gray-950 border border-gray-700 rounded px-2 py-1 text-gray-100" value={draft.priority ?? 100} onChange={e => setDraft(prev => ({ ...prev, priority: Number(e.target.value) }))} /></td>
              <td className="py-2 pr-2 text-gray-500">new</td>
              <td className="py-2 pl-2 text-right"><button disabled={loading} onClick={addStrategy} className="px-3 py-1 rounded bg-green-700 hover:bg-green-600 disabled:opacity-50 text-white">新增</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
}
