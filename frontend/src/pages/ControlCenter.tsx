import { useEffect, useMemo, useState } from 'react';
import { api, RuntimeStatus, StrategyGroup, TradingParamSpec } from '../api/client';

const emptyStrategy = { name: '', x: 10, y: 20, min_created: 180, max_created: 300, enabled: true, is_live: false };

function asBool(value: number | boolean | undefined) {
  return value === true || value === 1;
}

function fmtUsd(value: unknown) {
  const n = typeof value === 'number' ? value : Number(value || 0);
  return `$${n.toFixed(2)}`;
}

export default function ControlCenter() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [summary, setSummary] = useState<Record<string, unknown>>({});
  const [strategies, setStrategies] = useState<StrategyGroup[]>([]);
  const [form, setForm] = useState(emptyStrategy);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [paramSpecs, setParamSpecs] = useState<TradingParamSpec[]>([]);
  const [params, setParams] = useState<Record<string, number>>({});
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(false);

  const load = async () => {
    const [runtime, pnl, strategyRes, paramRes] = await Promise.all([
      api.getRuntimeStatus(),
      api.getPositionsSummary(),
      api.getStrategies(),
      api.getTradingParams(),
    ]);
    setStatus(runtime);
    setSummary(pnl);
    setStrategies(strategyRes.strategies);
    setParamSpecs(paramRes.specs);
    setParams(paramRes.values);
  };

  useEffect(() => {
    load().catch((e) => setMessage(e.message));
    const timer = window.setInterval(() => load().catch(() => undefined), 5000);
    return () => window.clearInterval(timer);
  }, []);

  const simRunning = status?.user_mode === 'SIM_TEST';
  const liveRunning = status?.user_mode === 'FORMAL_SIM_LIVE';
  const liveStrategyCount = useMemo(() => strategies.filter((s) => asBool(s.is_live)).length, [strategies]);

  const switchMode = async (mode: 'SIM_TEST' | 'FORMAL_SIM_LIVE' | 'IDLE') => {
    setLoading(true);
    setMessage('');
    try {
      await api.switchRuntimeMode(mode);
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const onSimClick = () => switchMode(simRunning ? 'IDLE' : 'SIM_TEST');
  const onLiveClick = () => switchMode(liveRunning ? 'SIM_TEST' : 'FORMAL_SIM_LIVE');

  const submitStrategy = async () => {
    setLoading(true);
    setMessage('');
    try {
      const payload = {
        ...form,
        name: form.name || `x=${form.x}, y=${form.y}, min=${form.min_created}s`,
        x: Number(form.x),
        y: Number(form.y),
        min_created: Number(form.min_created),
        max_created: Number(form.max_created),
      };
      if (editingId) await api.updateStrategy(editingId, payload);
      else await api.createStrategy(payload);
      setForm(emptyStrategy);
      setEditingId(null);
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const editStrategy = (s: StrategyGroup) => {
    setEditingId(s.id);
    setForm({
      name: s.name,
      x: Number(s.x),
      y: Number(s.y),
      min_created: Number(s.min_created),
      max_created: Number(s.max_created),
      enabled: asBool(s.enabled),
      is_live: asBool(s.is_live),
    });
  };

  const deleteStrategy = async (s: StrategyGroup) => {
    if (!window.confirm(`确定删除策略组「${s.name}」(ID=${s.id})？`)) return;
    setLoading(true);
    setMessage('');
    try {
      await api.deleteStrategy(s.id);
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const saveParams = async () => {
    setLoading(true);
    setMessage('');
    try {
      await api.updateTradingParams(params);
      setMessage('交易参数已保存，正在运行的轮询间隔会在下一轮生效。');
      await load();
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="page-stack">
      <div className="grid two">
        <div className="card">
          <h2>运行状态</h2>
          <p className="hint">系统只能处于一种交易状态：模拟交易只运行模拟盘策略；实盘交易同时运行模拟盘与唯一实盘策略。</p>
          <div className="metric-row">
            <span>当前状态</span>
            <strong>{status?.user_mode ?? '加载中'}</strong>
          </div>
          <div className="metric-row">
            <span>实盘PnL</span>
            <strong>{fmtUsd(summary.live_pnl_usd ?? summary.total_pnl_usd ?? 0)}</strong>
          </div>
          <p className="hint">PnL仅统计实盘策略已实现收益；模拟交易时该项固定为 $0.00。</p>
          <div className="button-row">
            <button className={simRunning ? 'danger' : 'primary'} disabled={loading} onClick={onSimClick}>
              {simRunning ? '暂停模拟交易' : '启动模拟交易'}
            </button>
            <button className={liveRunning ? 'danger' : 'primary'} disabled={loading} onClick={onLiveClick}>
              {liveRunning ? '暂停实盘交易' : '启动实盘交易'}
            </button>
          </div>
          {message && <p className="message">{message}</p>}
        </div>

        <div className="card">
          <h2>交易规则说明</h2>
          <p>模拟盘策略数量不限制，用于参数对照和收益比较。</p>
          <p>实盘策略最多只能保留一条，用于真实买入卖出。</p>
          <p>多个策略同时命中同一池子时，不再按优先级裁决，而是各自建仓，方便对照收益。</p>
          <p>当前实盘策略数：<strong>{liveStrategyCount}</strong></p>
        </div>
      </div>

      <div className="card">
        <h2>Strategy Groups</h2>
        <p className="hint">已删除优先级；新增“交易属性”。实盘属性最多一条，模拟盘不限制。</p>
        <div className="form-grid strategy-form">
          <label>策略名<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="留空自动生成" /></label>
          <label>X<input type="number" value={form.x} onChange={(e) => setForm({ ...form, x: Number(e.target.value) })} /></label>
          <label>Y<input type="number" value={form.y} onChange={(e) => setForm({ ...form, y: Number(e.target.value) })} /></label>
          <label>min_created(秒)<input type="number" value={form.min_created} onChange={(e) => setForm({ ...form, min_created: Number(e.target.value) })} /></label>
          <label>max_created(秒)<input type="number" value={form.max_created} onChange={(e) => setForm({ ...form, max_created: Number(e.target.value) })} /></label>
          <label>交易属性
            <select value={form.is_live ? 'LIVE' : 'SIM'} onChange={(e) => setForm({ ...form, is_live: e.target.value === 'LIVE' })}>
              <option value="SIM">模拟盘</option>
              <option value="LIVE">实盘</option>
            </select>
          </label>
          <label>启用
            <select value={form.enabled ? '1' : '0'} onChange={(e) => setForm({ ...form, enabled: e.target.value === '1' })}>
              <option value="1">启用</option>
              <option value="0">停用</option>
            </select>
          </label>
          <button className="primary" onClick={submitStrategy} disabled={loading}>{editingId ? '保存策略' : '新增策略'}</button>
          {editingId && <button onClick={() => { setEditingId(null); setForm(emptyStrategy); }}>取消编辑</button>}
        </div>

        <table>
          <thead>
            <tr>
              <th>ID</th><th>名称</th><th>交易属性</th><th>启用</th><th>X</th><th>Y</th><th>min</th><th>max</th><th>版本</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {strategies.map((s) => (
              <tr key={s.id}>
                <td>{s.id}</td>
                <td>{s.name}</td>
                <td><span className={asBool(s.is_live) ? 'tag live' : 'tag sim'}>{asBool(s.is_live) ? '实盘' : '模拟盘'}</span></td>
                <td>{asBool(s.enabled) ? '是' : '否'}</td>
                <td>{s.x}</td><td>{s.y}</td><td>{s.min_created}</td><td>{s.max_created}</td><td>{s.config_version ?? '-'}</td>
                <td><button onClick={() => editStrategy(s)}>编辑</button> <button className="danger" onClick={() => deleteStrategy(s)}>删除</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>交易参数</h2>
        <p className="hint">以下原 .env 交易参数已改为系统内动态配置；保存后会写入 runtime_settings，并同步到后端运行时。</p>
        <div className="form-grid params-grid">
          {paramSpecs.map((spec) => (
            <label key={spec.key}>
              <span>{spec.label}</span>
              <small>{spec.description}</small>
              <input
                type="number"
                step={spec.value_type === 'int' ? 1 : 'any'}
                value={params[spec.key] ?? spec.default}
                onChange={(e) => setParams({ ...params, [spec.key]: spec.value_type === 'int' ? Number.parseInt(e.target.value || '0', 10) : Number(e.target.value) })}
              />
            </label>
          ))}
        </div>
        <div className="button-row"><button className="primary" onClick={saveParams} disabled={loading}>保存交易参数</button></div>
      </div>
    </section>
  );
}
