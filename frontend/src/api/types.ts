export type Profile = "aggressive" | "balanced" | "conservative";

export interface RuntimeStatus {
  app_env: string;
  dry_run: boolean;
  simulation_enabled: boolean;
  live_trading_enabled: boolean;
  trading_provider: string;
  wallet: string | null;
  collector: Record<string, unknown>;
  prediction_worker: Record<string, unknown>;
  scheduler: Record<string, unknown>;
  paper_monitor: Record<string, unknown>;
  model_health: Record<string, unknown>;
  model_health_worker: Record<string, unknown>;
  training_worker: Record<string, unknown>;
  reconciliation: Record<string, unknown> | null;
  reconciliation_worker: Record<string, unknown>;
  liquidation: Record<string, unknown> | null;
  liquidation_worker: Record<string, unknown>;
}

export interface RiskStatus {
  new_entries_paused: boolean;
  pause_reason: string | null;
  max_open_positions: number;
  open_live_positions: number;
  consecutive_live_losses: number;
  consecutive_loss_limit: number;
  daily_live_loss_usd: number;
  max_daily_loss_fraction: number;
  wallet_sol_reserve: number;
}

export interface ModelFeatureItem {
  name: string;
  default_enabled: boolean;
  champion_enabled: boolean;
  available_rows: number;
  total_mature_rows: number;
  coverage: number;
}

export interface ModelFeatureCatalog {
  default_features: string[];
  available_features: string[];
  total_mature_rows: number;
  items: ModelFeatureItem[];
}

export interface TrainingRun {
  id: string;
  trigger: string;
  status: string;
  requested_at: string;
  started_at: string | null;
  completed_at: string | null;
  scheduled_for: string | null;
  retry_count: number;
  candidate_model_id: string | null;
  champion_before_id: string | null;
  promoted: boolean;
  request: { feature_names?: string[] };
  summary: Record<string, unknown>;
  error_message: string | null;
}

export interface ModelVersion {
  id: string;
  version: string;
  algorithm: string;
  status: string;
  early_stage: boolean;
  trained_at: string;
  thresholds: Partial<Record<Profile, number>>;
  metrics: Record<string, number | string | boolean | null>;
  feature_names: string[];
  rejection_reason?: string | null;
}

export interface DatasetStats {
  total: number;
  mature: number;
  pending: number;
  positives: number;
  positive_rate: number | null;
  launchpads: Array<{ launchpad: string; count: number }>;
}

export interface DashboardData {
  as_of: string;
  model: ModelVersion | null;
  dataset: DatasetStats;
  pnl: {
    today: Record<string, number>;
    seven_days: Record<string, number>;
  };
  open_positions: Array<{ account_kind: string; count: number; invested_usd: number }>;
  simulation: SimulationStatus;
  risk: RiskStatus;
  runtime: RuntimeStatus;
  equity_curve: Array<{ date: string; account_kind: string; daily_pnl: number }>;
  signal_activity: Array<{ date: string; profile: string; predictions: number; selected: number }>;
}

export interface Signal {
  id: number;
  probability: number;
  profile: string;
  threshold: number;
  selected: number;
  predicted_at: string;
  address: string;
  name: string | null;
  symbol: string | null;
  launchpad: string | null;
  entry_time: number;
  tag: number | null;
  model_version: string;
}

export interface SimulationAccount {
  session_id: string;
  account: string;
  cash_usd: number;
  sol_fee_reserve: number;
  initial_cash_usd: number;
  initial_sol_fee_reserve: number;
  source: string;
  updated_at: string;
  positions: number;
  open_positions: number;
  closed_positions: number;
  realized_pnl_usd: number;
}

export interface SimulationStatus {
  session: {
    id: string;
    started_at: string;
    ended_at?: string | null;
    status?: "active" | "closed";
    created_reason?: string;
    initial_cash_usd: number;
    initial_sol_fee_reserve: number;
  };
  accounts: Record<string, SimulationAccount>;
}

export interface SimulationHistoryItem {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: "active" | "closed";
  initial_cash_usd: number;
  initial_sol_fee_reserve: number;
  created_reason: string;
  realized_pnl_usd: number;
  accounts: Record<string, {
    account_kind: string;
    positions: number;
    open_positions: number;
    closed_positions: number;
    realized_pnl_usd: number;
  }>;
}

export interface Position {
  id: string;
  token_address: string;
  account_kind: string;
  profile: Profile;
  status: string;
  entry_time: string;
  expires_at: string;
  invested_usd: number;
  entry_price: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  net_pnl_usd: number | null;
}

export interface AgentProposal {
  id: string;
  proposal_type: string;
  payload: Record<string, unknown>;
  status: "pending_approval" | "approved" | "rejected" | "executed" | "failed";
  created_at: string;
  decided_at: string | null;
  decision_note: string | null;
  executed_at: string | null;
  result: Record<string, unknown> | null;
  error_message: string | null;
}

export interface AgentContext {
  access_level: string;
  allowed_proposal_types: string[];
  explicitly_forbidden: string[];
  runtime: RuntimeStatus;
  risk: RiskStatus;
  champion_model: ModelVersion | null;
  open_positions: Position[];
  recent_signals: Signal[];
  audit_summary: Array<{ category: string; action: string; severity: string; created_at: string }>;
}

export interface PreparedAction {
  challenge: string;
  expires_at: number;
  summary: Record<string, string | number | boolean | null>;
  can_confirm: boolean;
  blocker: string | null;
}

