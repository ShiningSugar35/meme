const BASE = '/api'

async function fetchJSON(url: string, init?: RequestInit) {
  const r = await fetch(url, init)
  return r.json()
}

export const api = {
  health: () => fetchJSON('/health'),

  // Runtime
  getRuntimeStatus: () => fetchJSON(`${BASE}/runtime/status`),
  switchMode: (userMode: string) => fetchJSON(`${BASE}/runtime/mode`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ user_mode: userMode })
  }),
  startWorkers: () => fetchJSON(`${BASE}/runtime/workers/start`, { method: 'POST' }),
  stopWorkers: () => fetchJSON(`${BASE}/runtime/workers/stop`, { method: 'POST' }),
  getWorkersStatus: () => fetchJSON(`${BASE}/runtime/workers/status`),

  // Portfolio
  getPortfolioTable: (accountType: string) => fetchJSON(`${BASE}/runtime/portfolio/table?account_type=${accountType}`),
  getPositionsSummary: () => fetchJSON(`${BASE}/runtime/positions/summary`),

  // Config / Strategies
  getStrategies: () => fetchJSON(`${BASE}/config/strategies`),
  createStrategy: (body: object) => fetchJSON(`${BASE}/config/strategies`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  }),
  updateStrategy: (id: number, body: object) => fetchJSON(`${BASE}/config/strategies/${id}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
  }),
  applyConfig: () => fetchJSON(`${BASE}/config/apply`, { method: 'POST' }),

  // Tokens
  getTokens: () => fetchJSON(`${BASE}/tokens`),

  // Positions
  getPositions: (accountType?: string) => {
    const q = accountType ? `?account_type=${accountType}` : ''
    return fetchJSON(`${BASE}/positions${q}`)
  },
  manualClose: (id: number) => fetchJSON(`${BASE}/positions/${id}/manual-close`, { method: 'POST' }),

  // Trades
  getTrades: (accountType?: string) => {
    const q = accountType ? `?account_type=${accountType}` : ''
    return fetchJSON(`${BASE}/trades${q}`)
  },
  getProviderRequests: () => fetchJSON(`${BASE}/trades/provider-requests`),

  // Logs
  getRecentLogs: (level?: string, category?: string) => {
    const params = new URLSearchParams()
    if (level) params.set('level', level)
    if (category) params.set('category', category)
    const q = params.toString() ? `?${params.toString()}` : ''
    return fetchJSON(`${BASE}/logs/recent${q}`)
  },
  exportDiagnostic: () => fetchJSON(`${BASE}/logs/export-diagnostic`, { method: 'POST' }),

  // Risk
  getKillSwitch: () => fetchJSON(`${BASE}/risk/kill-switch`),

  // Providers
  getProviderHealth: () => fetchJSON(`${BASE}/providers/health`),

  // Emergency
  toggleKill: (enable: boolean) => fetchJSON(`${BASE}/runtime/emergency/kill-switch`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enable })
  }),
  stopLiveMode: () => fetchJSON(`${BASE}/runtime/emergency/stop-live`, { method: 'POST' }),
  resumeLiveMode: () => fetchJSON(`${BASE}/runtime/emergency/resume-live`, { method: 'POST' }),
  exportLosing: () => fetchJSON(`${BASE}/runtime/emergency/export-losing`, { method: 'POST' }),

  toggleKillSwitch: (enable: boolean) => fetchJSON(`${BASE}/runtime/emergency/kill-switch?enable=${enable}`, { method: 'POST' }),
  backupDb: () => fetchJSON(`${BASE}/runtime/emergency/backup-db`, { method: 'POST' }),
  repairLegacyDb: () => fetchJSON(`${BASE}/runtime/emergency/repair-legacy-db`, { method: 'POST' }),
}

// Runtime-editable strategy groups used by Control Center. These endpoints avoid
// relying on the older config router, and discovery/second-filter runners reload
// the DB every poll so changes take effect on the next trench cycle.
export type RuntimeStrategyPayload = {
  name?: string;
  x: number;
  y: number;
  t_seconds: number;
  enabled?: boolean;
  is_live?: boolean;
  priority?: number;
};

export const runtimeStrategyApi = {
  async list() {
    return fetchJSON(`${BASE}/runtime/strategies`);
  },
  async create(payload: RuntimeStrategyPayload) {
    return fetchJSON(`${BASE}/runtime/strategies`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },
  async update(id: number, payload: RuntimeStrategyPayload) {
    return fetchJSON(`${BASE}/runtime/strategies/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },
  async remove(id: number) {
    return fetchJSON(`${BASE}/runtime/strategies/${id}`, {
      method: 'DELETE',
    });
  },
};
