const viteEnv = (import.meta as unknown as { env?: { VITE_API_BASE?: string } }).env;
export const API_BASE = (viteEnv?.VITE_API_BASE ?? '').replace(/\/$/, '');

async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
    ...options,
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const message = data?.error || data?.detail || response.statusText;
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  return data as T;
}

export type RuntimeMode = 'IDLE' | 'SIM_TEST' | 'FORMAL_SIM_LIVE';

export interface RuntimeStatus {
  ok?: boolean;
  user_mode: RuntimeMode;
  workers_enabled: boolean;
  live_entries_enabled: boolean;
  provider_mode: string;
  pause_new_entries: boolean;
  session_started_at?: string;
  live_open_count: number;
  has_live_positions: boolean;
  can_live_trade: boolean;
  live_readiness?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface PositionSummary {
  total_open?: number;
  live_open?: number;
  sim_open?: number;
  live_open_count?: number;
  sim_open_count?: number;
  total_pnl_usd?: number;
  live_pnl_usd?: number;
  sim_pnl_usd?: number;
  live_pnl_sol?: number;
  sim_pnl_sol?: number;
  [key: string]: unknown;
}

export interface StrategyGroup {
  id: number;
  name: string;
  enabled: number | boolean;
  is_live: number | boolean;
  priority?: number;
  config_version?: number;
  x: number;
  y: number;
  t_seconds: number;
  raw_config_json?: string;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface StrategyPayload {
  name?: string;
  enabled: boolean;
  is_live: boolean;
  x: number;
  y: number;
  t_seconds: number;
}

export interface TradingParamSpec {
  key: string;
  label: string;
  description: string;
  value_type: 'int' | 'float';
  default: number;
  min_value?: number | null;
}

export interface TradingParamsResponse {
  specs: TradingParamSpec[];
  values: Record<string, number>;
}

export interface PortfolioRow {
  id: number;
  status: string;
  ratio?: number;
  remaining?: number;
  remaining_value_usd?: number;
  pnl_pct?: number;
  mint_short?: string;
  token_mint?: string;
  account_type?: 'LIVE' | 'SIM';
  strategy_id?: number;
  strategy_name?: string;
  updated_at?: string;
  [key: string]: unknown;
}

interface StrategiesResponseWire {
  ok?: boolean;
  strategies?: StrategyGroup[];
  items?: StrategyGroup[];
}

interface StrategyResponseWire {
  ok?: boolean;
  strategy?: StrategyGroup;
  id?: number;
  status?: string;
}

function normalizeStrategies(data: StrategiesResponseWire): { strategies: StrategyGroup[] } {
  return { strategies: data.strategies ?? data.items ?? [] };
}

function normalizeTradingParams(data: TradingParamsResponse & { ok?: boolean }): TradingParamsResponse {
  return { specs: data.specs ?? [], values: data.values ?? {} };
}

export const api = {
  getRuntimeStatus: () => apiFetch<RuntimeStatus>('/api/runtime/status'),
  switchRuntimeMode: (user_mode: RuntimeMode) => apiFetch<{ ok: boolean; user_mode: RuntimeMode }>('/api/runtime/mode', {
    method: 'POST',
    body: JSON.stringify({ user_mode }),
  }),
  getPositionsSummary: () => apiFetch<PositionSummary>('/api/runtime/positions/summary'),
  getStrategies: async () => normalizeStrategies(await apiFetch<StrategiesResponseWire>('/api/runtime/strategies')),
  createStrategy: async (payload: StrategyPayload) => apiFetch<StrategyResponseWire>('/api/runtime/strategies', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  updateStrategy: async (id: number, payload: StrategyPayload) => apiFetch<StrategyResponseWire>(`/api/runtime/strategies/${id}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  }),
  deleteStrategy: (id: number) => apiFetch<{ ok: boolean }>(`/api/runtime/strategies/${id}`, { method: 'DELETE' }),
  getTradingParams: async () => normalizeTradingParams(await apiFetch<TradingParamsResponse & { ok?: boolean }>('/api/runtime/trading-params')),
  updateTradingParams: (values: Record<string, number>) => apiFetch<{ ok: boolean; values: Record<string, number> }>('/api/runtime/trading-params', {
    method: 'PUT',
    body: JSON.stringify({ values }),
  }),
  getPortfolio: (account: 'LIVE' | 'SIM') => apiFetch<PortfolioRow[]>(`/api/runtime/portfolio/table?account_type=${account}`),
  sellAllLive: () => apiFetch<{ ok: boolean; sold_count: number; user_mode: RuntimeMode }>('/api/runtime/emergency/sell-all-live', { method: 'POST' }),
  stopLive: () => apiFetch<{ ok: boolean; user_mode: RuntimeMode }>('/api/runtime/emergency/stop-live', { method: 'POST' }),
  resumeLive: () => apiFetch<{ ok: boolean; user_mode: RuntimeMode }>('/api/runtime/emergency/resume-live', { method: 'POST' }),
  backupDb: () => apiFetch<{ ok: boolean; export_path: string }>('/api/runtime/emergency/backup-db', { method: 'POST' }),
  exportLosing: () => apiFetch<{ ok: boolean; export_path: string; losing_count: number }>('/api/runtime/emergency/export-losing', { method: 'POST' }),
  exportLogs: () => apiFetch<{ ok: boolean; export_path: string; error_count: number }>('/api/runtime/emergency/export-logs', { method: 'POST' }),
};
