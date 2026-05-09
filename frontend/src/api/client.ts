const BASE = '/api'

async function fetchJSON(url: string, init?: RequestInit) {
  const r = await fetch(url, init)
  return r.json()
}

export const api = {
  // Health
  health: () => fetchJSON('/health'),

  // Config / Strategies
  getStrategies: () => fetchJSON(`${BASE}/config/strategies`),
  createStrategy: (body: object) => fetchJSON(`${BASE}/config/strategies`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  updateStrategy: (id: number, body: object) => fetchJSON(`${BASE}/config/strategies/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  applyConfig: () => fetchJSON(`${BASE}/config/apply`, { method: 'POST' }),
  pauseNewEntries: () => fetchJSON(`${BASE}/config/pause-new-entries`, { method: 'POST' }),
  resumeNewEntries: () => fetchJSON(`${BASE}/config/resume-new-entries`, { method: 'POST' }),

  // Tokens
  getTokens: () => fetchJSON(`${BASE}/tokens`),
  getToken: (mint: string) => fetchJSON(`${BASE}/tokens/${mint}`),
  getTokenSnapshots: (mint: string) => fetchJSON(`${BASE}/tokens/${mint}/snapshots`),
  getTokenDecisions: (mint: string) => fetchJSON(`${BASE}/tokens/${mint}/decisions`),

  // Positions
  getPositions: () => fetchJSON(`${BASE}/positions?status=all`),
  getPosition: (id: number) => fetchJSON(`${BASE}/positions/${id}`),
  manualClose: (id: number) => fetchJSON(`${BASE}/positions/${id}/manual-close`, { method: 'POST' }),

  // Trades
  getTrades: () => fetchJSON(`${BASE}/trades`),
  getProviderRequests: () => fetchJSON(`${BASE}/trades/provider-requests`),

  // Logs
  getRecentLogs: () => fetchJSON(`${BASE}/logs/recent`),

  // Risk
  getKillSwitch: () => fetchJSON(`${BASE}/risk/kill-switch`),
  resetKillSwitch: () => fetchJSON(`${BASE}/risk/kill-switch/reset`, { method: 'POST' }),

  // Providers
  getProviderHealth: () => fetchJSON(`${BASE}/providers/health`),
  mockRunOnce: () => fetchJSON(`${BASE}/mock/run-once`, { method: 'POST' }),
}
