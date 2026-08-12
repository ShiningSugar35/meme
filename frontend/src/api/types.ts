export type StrategyKey = "model_1" | "model_2" | "model_3" | "rules_only";

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

export interface CollectorEvent {
  id: string;
  created_at: string;
  level: "debug" | "info" | "success" | "warning" | "error";
  action: string;
  message: string;
  cycle_id: string | null;
  details: Record<string, unknown>;
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
  active_model_slots: number[];
  available_rows: number;
  total_mature_rows: number;
  coverage: number;
}

export interface ModelFeatureCatalog {
  default_features: string[];
  available_features: string[];
  total_mature_rows: number;
  active_model_count: number;
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

export interface EvaluationMetricPayload {
  threshold?: number;
  precision?: number;
  recall?: number;
  trade_count?: number;
  true_positives?: number;
  false_positives?: number;
  positive_count?: number;
  profit_units?: number;
  fixed_profit_usd?: number;
  economic_capture?: number;
  [key: string]: unknown;
}

export interface GeneralizationPayload {
  average_precision_mean?: number;
  average_precision_std?: number;
  average_precision_skill_mean?: number;
  stability_score?: number;
  decay_score?: number;
  score?: number;
}

export interface ModelMetrics {
  rank?: number;
  precision?: number | null;
  recall?: number | null;
  trade_count?: number;
  fixed_profit_usd?: number | null;
  profit_units?: number | null;
  economic_score?: number | null;
  generalization_score?: number | null;
  composite_score?: number | null;
  final_recent_window?: EvaluationMetricPayload;
  development?: EvaluationMetricPayload;
  generalization?: GeneralizationPayload;
  rule_baseline_final?: EvaluationMetricPayload;
  [key: string]: unknown;
}

export interface ModelVersion {
  id: string;
  version: string;
  algorithm: string;
  status: string;
  early_stage: boolean;
  trained_at: string;
  thresholds: { decision?: number };
  metrics: ModelMetrics;
  feature_names: string[];
  rejection_reason?: string | null;
  active_slot?: number;
  active_threshold?: number;
  active_composite_score?: number;
  model_label?: string;
}

export interface StrategyInfo {
  strategy_key: StrategyKey;
  label: string;
  model_id: string | null;
  algorithm: string | null;
  rank: number | null;
  threshold: number | null;
  composite_score: number | null;
  feature_count: number;
}

export interface DatasetStats {
  total: number;
  mature: number;
  pending: number;
  positives: number;
  positive_rate: number | null;
  launchpads: Array<{ launchpad: string; count: number }>;
}

export interface StrategyPerformance {
  strategy_key: StrategyKey;
  positions: number;
  open_positions: number;
  closed_positions: number;
  realized_pnl_usd: number;
}

export interface DashboardData {
  as_of: string;
  model: ModelVersion | null;
  active_models: ModelVersion[];
  strategies: StrategyInfo[];
  strategy_performance: StrategyPerformance[];
  live_realized_pnl_usd: number;
  dataset: DatasetStats;
  pnl: { today: Record<string, number>; seven_days: Record<string, number> };
  open_positions: Array<{ strategy_key: string; count: number; invested_usd: number }>;
  simulation: SimulationStatus;
  risk: RiskStatus;
  runtime: RuntimeStatus;
  equity_curve: Array<{ date: string; strategy_key: string; daily_pnl: number }>;
  signal_activity: Array<{ date: string; strategy_key: string; predictions: number; selected: number }>;
}

export interface Signal {
  id: number;
  probability: number;
  strategy_key: StrategyKey;
  threshold: number;
  selected: number;
  predicted_at: string;
  address: string;
  name: string | null;
  symbol: string | null;
  launchpad: string | null;
  entry_time: number;
  tag: number | null;
  model_id: string;
  model_version: string;
  model_label: string;
  algorithm: string;
  active_slot: number | null;
}

export interface SimulationAccount {
  session_id: string;
  strategy_key: StrategyKey;
  cash_usd: number;
  initial_cash_usd: number;
  accounting_currency: "USD";
  network_fee_accounting: "fee_time_sol_usd";
  source: string;
  updated_at: string;
  positions: number;
  open_positions: number;
  closed_positions: number;
  invested_usd: number;
  realized_pnl_usd: number;
  platform_fee_usd: number;
  network_fee_usd: number;
  network_fee_sol: number;
  slippage_cost_usd: number;
  total_fees_usd: number;
  total_execution_cost_usd: number;
  model_id: string | null;
  statistics_started_at: string;
  trade_count: number;
  precision: number | null;
  recall: number | null;
}

export interface SimulationStatus {
  session: {
    id: string;
    started_at: string;
    ended_at?: string | null;
    status?: "active" | "closed";
    created_reason?: string;
    initial_cash_usd: number;
  };
  accounts: Record<StrategyKey, SimulationAccount>;
}

export interface SimulationHistoryItem {
  id: string;
  started_at: string;
  ended_at: string | null;
  status: "active" | "closed";
  initial_cash_usd: number;
  created_reason: string;
  realized_pnl_usd: number;
  accounts: Record<StrategyKey, {
    strategy_key: StrategyKey;
    positions: number;
    open_positions: number;
    closed_positions: number;
    realized_pnl_usd: number;
  }>;
}

export interface SimulationAuditItem {
  session_id: string;
  strategy_key: StrategyKey;
  model_id: string | null;
  algorithm: string | null;
  model_label: string;
  status: "active" | "closed";
  first_entry_time: string | null;
  last_exit_time: string | null;
  positions: number;
  open_positions: number;
  closed_positions: number;
  realized_pnl_usd: number;
}

export interface Position {
  id: string;
  token_address: string;
  launchpad?: string | null;
  strategy_key?: StrategyKey | null;
  status: string;
  entry_time: string;
  expires_at: string;
  exit_time?: string | null;
  invested_usd: number;
  entry_price: number | null;
  exit_price: number | null;
  exit_reason: string | null;
  net_pnl_usd: number | null;
  current_price?: number | null;
  current_liquidity_usd?: number | null;
  current_market_cap_usd?: number | null;
  market_snapshot_at?: string | null;
  sell_failed?: boolean;
  sell_failure_reason?: string | null;
}

export interface PortfolioView {
  mode: "simulation" | "live";
  strategy: StrategyKey;
  strategy_info: StrategyInfo;
  strategies: StrategyInfo[];
  live_trading_enabled: boolean;
  simulation_enabled: boolean;
  provider: string;
  model_alias: string | null;
  session: SimulationStatus["session"] | null;
  accounts: Partial<Record<StrategyKey, Partial<SimulationAccount>>>;
  account: Partial<SimulationAccount> & {
    positions?: number;
    open_positions?: number;
    closed_positions?: number;
    realized_pnl_usd?: number;
    cash_usd?: number | null;
    source?: string;
  };
  current: Position[];
  history: {
    items: Position[];
    page: number;
    page_size: number;
    total: number;
    total_pages: number;
  };
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
