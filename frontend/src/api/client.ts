const viteEnv = (import.meta as unknown as { env?: { VITE_API_BASE?: string } }).env;
export const API_BASE = (viteEnv?.VITE_API_BASE ?? '').replace(/\/$/, '');

async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) },
    ...options,
  });
  const text = await response.text();
  if (!response.ok) {
    let message = text;
    try {
      const data = JSON.parse(text);
      message = data?.error || data?.detail || response.statusText || `HTTP ${response.status}`;
    } catch {}
    throw new Error(typeof message === 'string' ? message : JSON.stringify(message));
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as unknown as T;
  }
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
  token_mint: string;
  mint_short?: string;
  strategy_name?: string;
  strategy_id?: number;
  status: string;
  remaining?: number;
  remaining_value_usd?: number;
  pnl_pct?: number;
  ratio?: number | string;
  updated_at?: string;
  [key: string]: unknown;
}

export interface TrenchHistoryItem {
  count: number;
  passed: number;
  raw_count?: number;
  unique_count?: number;
  duplicate_count_estimate?: number;
  platform_fetch?: Record<string, unknown>;
  created_at?: string;
}

export interface RuleFailItem {
  rule: string;
  count?: number;
  label?: string;
  stage?: string;
  section?: string;
  checked_count?: number;
  actual_checked_count?: number;
  denominator_count?: number;
  failed_count?: number;
  fail_rate?: number;
  fail_rate_pct?: number;
  missing_count?: number;
  sample_values?: string[];
}

export interface EndpointHealthItem {
  endpoint: string;
  method: string;
  credential_slot?: string;
  calls: number;
  ok_calls: number;
  ok_rate: number;
  latest_status_code?: number;
  avg_latency_ms?: number;
  latest_error?: string | null;
  severity: 'ok' | 'warn' | 'critical';
}

export interface FieldHealthItem {
  section: string;
  field: string;
  label: string;
  source: string;
  checked_count: number;
  nonnull_count: number;
  missing_count: number;
  zero_count: number;
  missing_rate: number;
  zero_rate: number;
  sample_values: string[];
  sample_tokens: string[];
  severity: 'ok' | 'warn' | 'critical';
  note: string;
}

export interface PriceAgeHealth {
  under_60m_count: number;
  age_parse_missing_count: number;
  price_change_source_counts: Record<string, number>;
  swaps_source_counts: Record<string, number>;
  price_screen_reached_count?: number;
  risk_only_failed_count?: number;
  price_screen_not_reached_reason?: string;
  warnings: string[];
}

export interface PlatformHealthItem {
  platform: string;
  primary_slot?: number;
  used_slot?: number;
  used_role?: string;
  ok: boolean;
  raw_count: number;
  unique_count?: number;
  duplicate_count?: number;
  fallback_used?: boolean;
  error?: string | null;
  severity: 'ok' | 'warn' | 'critical';
  latency_ms?: number;
}

export interface PriceFaceHealth {
  latest_price_ok_rate: number | null;
  holder_endpoint_ok_rate: number | null;
  pass_fail_stats: Record<string, { total: number; passed: number; failed: number; missing: number; fail_rate: number; missing_rate: number; reasons: string[] }>;
  feature_vector_field_missing: Record<string, number>;
  warnings: string[];
}

export interface CredentialSummaryItem {
  slot: string;
  total_calls: number;
  failed_calls: number;
  ok_rate: number;
}

export interface DiscoveryFetchHealthItem {
  group_name: string;
  platforms: string[];
  slot?: number;
  role?: string;
  ok: boolean;
  raw_count: number;
  unique_count?: number;
  duplicate_count?: number;
  status_code?: number;
  error?: string | null;
  cooldown_until?: string | null;
  latency_ms?: number;
  severity: 'ok' | 'warn' | 'critical';
}

export interface CredentialHealthItem {
  slot: number;
  role: string;
  total_calls: number;
  total_weight: number;
  ok_calls: number;
  failed_calls: number;
  rate_limited_count: number;
  local_rate_limited_count?: number;
  cooldown_until?: number | null;
  cooldown_remaining_s?: number;
  ok_rate: number;
  endpoints: Record<string, number>;
  severity: 'ok' | 'warn' | 'critical';
}

export interface FeatureStageHealthItem {
  stage: string;
  label: string;
  endpoint: string;
  weight: number;
  candidates_in: number;
  checked_count: number;
  passed_count: number;
  failed_count: number;
  skipped_count: number;
  api_calls: number;
  ok_rate?: number | null;
  rate_limited_count: number;
  avg_latency_ms: number;
  severity: 'ok' | 'warn' | 'critical';
}

export interface DataSourceHealth {
  summary: Record<string, unknown>;
  endpoint_health: EndpointHealthItem[];
  credential_summary?: CredentialSummaryItem[];
  credential_health?: CredentialHealthItem[];
  discovery_fetch_health?: DiscoveryFetchHealthItem[];
  feature_stage_health?: FeatureStageHealthItem[];
  field_health: FieldHealthItem[];
  price_age_health?: PriceAgeHealth;
  price_face_health?: PriceFaceHealth;
  platform_health?: PlatformHealthItem[];
  system_event_warnings?: Record<string, unknown>[];
}

export interface FilterStats {
  trench_history: TrenchHistoryItem[];
  filter_fails: RuleFailItem[];
  data_source_health?: DataSourceHealth;
  error?: string;
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
  getFilterStats: () => apiFetch<FilterStats>('/api/runtime/filter-stats'),
};
