import type {
  AgentContext,
  AgentProposal,
  CollectorEvent,
  DashboardData,
  ModelFeatureCatalog,
  ModelVersion,
  PortfolioView,
  Position,
  StrategyKey,
  TrainingRun,
  PreparedAction,
  RiskStatus,
  RuntimeStatus,
  Signal,
  SimulationAuditItem,
  SimulationHistoryItem,
  SimulationStatus
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) }
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail ?? `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  dashboard: () => request<DashboardData>("/api/dashboard"),
  models: () => request<{ champion: ModelVersion | null; active_models: ModelVersion[]; items: ModelVersion[]; feature_catalog: ModelFeatureCatalog; training_runs: TrainingRun[] }>("/api/models"),
  signals: () => request<{ items: Signal[] }>("/api/signals"),
  exportSamples: async () => {
    const response = await fetch("/api/samples/export.csv");
    if (!response.ok) throw new Error(`样本集导出失败: ${response.status}`);
    return response.blob();
  },
  portfolio: () => request<{ items: Position[] }>("/api/portfolio"),
  portfolioView: (options: { mode: "simulation" | "live"; strategy: StrategyKey; page: number; pageSize: number; startAt?: string; endAt?: string }) => {
    const params = new URLSearchParams({
      mode: options.mode,
      strategy: options.strategy,
      page: String(options.page),
      page_size: String(options.pageSize)
    });
    if (options.startAt) params.set("start_at", options.startAt);
    if (options.endAt) params.set("end_at", options.endAt);
    return request<PortfolioView>(`/api/portfolio/view?${params.toString()}`);
  },
  simulation: () => request<SimulationStatus>("/api/simulation"),
  simulationHistory: () => request<{ items: SimulationHistoryItem[] }>("/api/simulation/history"),
  simulationAudit: () => request<{ items: SimulationAuditItem[] }>("/api/simulation/audit"),
  resetSimulation: () => request<SimulationStatus>("/api/simulation/reset", { method: "POST" }),
  agentContext: () => request<AgentContext>("/api/agent/context"),
  agentProposals: () => request<{ items: AgentProposal[] }>("/api/agent/proposals"),
  approveAgentProposal: (proposalId: string, note?: string) => request<AgentProposal>(`/api/agent/proposals/${encodeURIComponent(proposalId)}/approve`, { method: "POST", body: JSON.stringify({ note }) }),
  rejectAgentProposal: (proposalId: string, note?: string) => request<AgentProposal>(`/api/agent/proposals/${encodeURIComponent(proposalId)}/reject`, { method: "POST", body: JSON.stringify({ note }) }),
  runtime: () => request<{ runtime: RuntimeStatus; risk: RiskStatus }>("/api/runtime"),
  collectorEvents: (limit = 200) => request<{ items: CollectorEvent[] }>(`/api/runtime/collector-events?limit=${limit}`),
  trainModel: (features?: string[]) => request<{ run_id: string; status: string }>("/api/models/train", { method: "POST", body: JSON.stringify({ reason: "manual", features }) }),
  rollbackModel: (modelId: string) => request<{ champion: ModelVersion }>(`/api/models/${encodeURIComponent(modelId)}/rollback`, { method: "POST" }),
  prepareLive: () => request<PreparedAction>("/api/runtime/live/prepare", { method: "POST" }),
  confirmLive: (challenge: string) =>
    request<RuntimeStatus>("/api/runtime/live/confirm", {
      method: "POST",
      body: JSON.stringify({ challenge })
    }),
  stopLive: () => request<RuntimeStatus>("/api/runtime/live/stop", { method: "POST" }),
  prepareLiquidation: (mode: "simulation" | "live" | "all" = "all") => request<PreparedAction>(`/api/portfolio/liquidate/prepare?mode=${mode}`, { method: "POST" }),
  confirmLiquidation: (challenge: string, mode: "simulation" | "live" | "all" = "all") =>
    request<{ id: string; status: string; execution: string; scope: string }>(`/api/portfolio/liquidate/confirm?mode=${mode}`, {
      method: "POST",
      body: JSON.stringify({ challenge })
    }),
  resumeRisk: () => request<RiskStatus>("/api/risk/resume", { method: "POST" })
};
