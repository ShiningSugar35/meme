import type {
  AgentContext,
  AgentProposal,
  DashboardData,
  ModelFeatureCatalog,
  ModelVersion,
  Position,
  TrainingRun,
  PreparedAction,
  RiskStatus,
  RuntimeStatus,
  Signal,
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
  models: () => request<{ champion: ModelVersion | null; items: ModelVersion[]; feature_catalog: ModelFeatureCatalog; training_runs: TrainingRun[] }>("/api/models"),
  signals: () => request<{ items: Signal[] }>("/api/signals"),
  portfolio: () => request<{ items: Position[] }>("/api/portfolio"),
  simulation: () => request<SimulationStatus>("/api/simulation"),
  simulationHistory: () => request<{ items: SimulationHistoryItem[] }>("/api/simulation/history"),
  resetSimulation: () => request<SimulationStatus>("/api/simulation/reset", { method: "POST" }),
  agentContext: () => request<AgentContext>("/api/agent/context"),
  agentProposals: () => request<{ items: AgentProposal[] }>("/api/agent/proposals"),
  approveAgentProposal: (proposalId: string, note?: string) => request<AgentProposal>(`/api/agent/proposals/${encodeURIComponent(proposalId)}/approve`, { method: "POST", body: JSON.stringify({ note }) }),
  rejectAgentProposal: (proposalId: string, note?: string) => request<AgentProposal>(`/api/agent/proposals/${encodeURIComponent(proposalId)}/reject`, { method: "POST", body: JSON.stringify({ note }) }),
  runtime: () => request<{ runtime: RuntimeStatus; risk: RiskStatus }>("/api/runtime"),
  trainModel: (features?: string[]) => request<{ run_id: string; status: string }>("/api/models/train", { method: "POST", body: JSON.stringify({ reason: "manual", features }) }),
  rollbackModel: (modelId: string) => request<{ champion: ModelVersion }>(`/api/models/${encodeURIComponent(modelId)}/rollback`, { method: "POST" }),
  prepareLive: () => request<PreparedAction>("/api/runtime/live/prepare", { method: "POST" }),
  confirmLive: (challenge: string) =>
    request<RuntimeStatus>("/api/runtime/live/confirm", {
      method: "POST",
      body: JSON.stringify({ challenge })
    }),
  stopLive: () => request<RuntimeStatus>("/api/runtime/live/stop", { method: "POST" }),
  prepareLiquidation: () => request<PreparedAction>("/api/portfolio/liquidate/prepare", { method: "POST" }),
  confirmLiquidation: (challenge: string) =>
    request<{ status: string; execution: string }>("/api/portfolio/liquidate/confirm", {
      method: "POST",
      body: JSON.stringify({ challenge })
    }),
  resumeRisk: () => request<RiskStatus>("/api/risk/resume", { method: "POST" })
};
