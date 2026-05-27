PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runtime_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL DEFAULT 'system'
);

CREATE TABLE IF NOT EXISTS system_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  level TEXT NOT NULL,
  category TEXT NOT NULL,
  message TEXT NOT NULL,
  context_json TEXT,
  account_type TEXT NOT NULL DEFAULT 'SIM',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_events_level
ON system_events(level);
CREATE INDEX IF NOT EXISTS idx_system_events_category
ON system_events(category);
CREATE INDEX IF NOT EXISTS idx_system_events_account
ON system_events(account_type, created_at);

CREATE TABLE IF NOT EXISTS trade_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  position_id INTEGER,
  token_mint TEXT NOT NULL,
  strategy_id INTEGER,
  is_live INTEGER NOT NULL,
  account_type TEXT NOT NULL DEFAULT 'SIM',

  side TEXT NOT NULL,
  event_type TEXT NOT NULL,
  status TEXT NOT NULL,

  requested_pct REAL,
  requested_sol_amount REAL,
  requested_token_amount REAL,
  executed_sol_amount REAL,
  executed_token_amount REAL,

  price_usd REAL,
  price_sol REAL,
  slippage_bps INTEGER,
  price_impact_pct REAL,

  quote_json TEXT,
  route_plan_json TEXT,
  jito_tip_lamports INTEGER,
  priority_fee_lamports INTEGER,
  tx_signature TEXT,
  bundle_id TEXT,

  error_code TEXT,
  error_message TEXT,
  provider TEXT,
  latency_ms INTEGER,

  idempotency_key TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_trade_idempotency
ON trade_events(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_trade_events_account
ON trade_events(account_type, created_at);
CREATE INDEX IF NOT EXISTS idx_trade_events_token
ON trade_events(token_mint, created_at);
CREATE INDEX IF NOT EXISTS idx_trade_events_position
ON trade_events(position_id, created_at);

CREATE TABLE IF NOT EXISTS strategy_groups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  is_live INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 100,
  config_version INTEGER NOT NULL DEFAULT 1,

  x REAL NOT NULL,
  y REAL NOT NULL,

  buy_slippage_cap_bps INTEGER NOT NULL DEFAULT 1500,
  sell_slippage_cap_bps INTEGER NOT NULL DEFAULT 2000,
  emergency_slippage_cap_bps INTEGER NOT NULL DEFAULT 3500,
  price_impact_hard_cap_pct REAL NOT NULL DEFAULT 10,

  raw_config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
  token_mint TEXT PRIMARY KEY,
  chain TEXT NOT NULL DEFAULT 'solana',
  pool_address TEXT,
  launchpad TEXT,
  symbol TEXT,
  name TEXT,

  pool_created_at TEXT,
  first_seen_at TEXT NOT NULL,
  latest_state TEXT NOT NULL,

  latest_price_usd REAL,
  latest_price_sol REAL,
  latest_liquidity_usd REAL,
  latest_sol_side_liquidity REAL,
  latest_market_cap REAL,
  latest_type TEXT,

  latest_snapshot_id INTEGER,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokens_updated
ON tokens(updated_at);
CREATE INDEX IF NOT EXISTS idx_tokens_type
ON tokens(latest_type);

CREATE TABLE IF NOT EXISTS token_metric_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'GMGN',
  source_mode TEXT NOT NULL DEFAULT 'MOCK',
  observed_at TEXT NOT NULL,

  pool_address TEXT,
  platform TEXT,
  launchpad TEXT,
  type TEXT,
  liquidity_usd REAL,
  sol_side_liquidity REAL,
  volume_usd REAL,
  market_cap REAL,
  price_usd REAL,
  price_sol REAL,

  top_10_holder_rate REAL,
  top1_holder_rate REAL,
  renounced_mint INTEGER,
  renounced_freeze_account INTEGER,
  max_rug_ratio REAL,
  max_insider_ratio REAL,
  max_entrapment_ratio REAL,
  is_wash_trading INTEGER,
  rat_trader_amount_rate REAL,
  suspected_insider_hold_rate REAL,
  max_bundler_rate REAL,
  fresh_wallet_rate REAL,
  sell_tax REAL,
  has_social INTEGER,
  creator_token_status TEXT,
  dev_team_hold_rate REAL,
  dev_token_burn_ratio REAL,
  sniper_count INTEGER,
  burn_status TEXT,

  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_metric_snapshots_token_time
ON token_metric_snapshots(token_mint, observed_at);
CREATE INDEX IF NOT EXISTS idx_token_metric_snapshots_type_time
ON token_metric_snapshots(type, observed_at);

CREATE TABLE IF NOT EXISTS kline_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  interval TEXT NOT NULL,
  open_time TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  buy_volume REAL,
  sell_volume REAL,
  volume_usd REAL,
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kline_token_interval_time
ON kline_snapshots(token_mint, interval, open_time);

CREATE TABLE IF NOT EXISTS tick_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  source TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  price_usd REAL,
  price_sol REAL,
  liquidity_usd REAL,
  sol_side_liquidity REAL,
  market_cap REAL,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tick_token_time
ON tick_snapshots(token_mint, observed_at);

CREATE TABLE IF NOT EXISTS token_strategy_matches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  strategy_id INTEGER NOT NULL,
  strategy_config_version INTEGER NOT NULL,
  snapshot_id INTEGER,
  discovery_event_id INTEGER,
  stage TEXT NOT NULL,
  passed INTEGER NOT NULL,
  pass_fail_detail_json TEXT NOT NULL,
  feature_vector_json TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_matches_token_stage
ON token_strategy_matches(token_mint, stage, created_at);
CREATE INDEX IF NOT EXISTS idx_strategy_matches_discovery
ON token_strategy_matches(discovery_event_id, stage);

CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  pool_address TEXT,
  discovery_event_id INTEGER,

  is_live INTEGER NOT NULL,
  account_type TEXT NOT NULL DEFAULT 'SIM',
  live_strategy_id INTEGER,
  strategy_config_version INTEGER,
  locked_strategy_config_json TEXT,
  legacy_config_status TEXT,

  status TEXT NOT NULL,
  entry_price_usd REAL,
  entry_token_amount REAL,
  remaining_token_amount REAL,
  remaining_value_usd REAL,

  realized_pnl_pct REAL,
  pnl_pct REAL,

  max_runup_pct REAL DEFAULT 0,
  max_drawdown_pct REAL DEFAULT 0,

  next_check_at TEXT,
  last_checked_at TEXT,
  last_risk_check_at TEXT,
  next_risk_check_at TEXT,
  risk_check_interval_seconds INTEGER,

  executed_exit_rules_json TEXT NOT NULL DEFAULT '[]',

  opened_at TEXT NOT NULL,
  last_fill_at TEXT,
  last_fill_price_usd REAL,
  closed_at TEXT,

  open_trade_event_id INTEGER,
  close_reason TEXT,
  last_exit_reason TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status
ON positions(status, account_type);
CREATE INDEX IF NOT EXISTS idx_positions_account
ON positions(account_type, status);
CREATE INDEX IF NOT EXISTS idx_positions_token
ON positions(token_mint, account_type);
CREATE INDEX IF NOT EXISTS idx_positions_next_check
ON positions(next_check_at, status);
CREATE INDEX IF NOT EXISTS idx_positions_next_risk_check
ON positions(next_risk_check_at, status, account_type);
CREATE INDEX IF NOT EXISTS idx_positions_updated
ON positions(updated_at);

CREATE TABLE IF NOT EXISTS provider_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  method TEXT NOT NULL,
  status_code INTEGER,
  latency_ms INTEGER,
  ok INTEGER NOT NULL,
  error_code TEXT,
  error_summary TEXT,
  request_summary_json TEXT,
  response_summary_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bandit_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  position_id INTEGER,
  strategy_id INTEGER NOT NULL,
  is_live INTEGER NOT NULL,
  discovery_event_id INTEGER,

  action_json TEXT NOT NULL,
  feature_vector_json TEXT NOT NULL,
  reward_json TEXT,
  final_net_pnl_pct REAL,
  max_runup_pct REAL,
  max_drawdown_pct REAL,
  holding_seconds INTEGER,
  exit_reason TEXT,

  created_at TEXT NOT NULL,
  finalized_at TEXT
);

CREATE TABLE IF NOT EXISTS discovery_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_mint TEXT NOT NULL,
  pool_address TEXT NOT NULL DEFAULT '',
  strategy_id INTEGER,
  strategy_config_version INTEGER,

  first_seen_at TEXT NOT NULL,
  pool_created_at TEXT,

  status TEXT NOT NULL DEFAULT 'DISCOVERED',

  source_snapshot_id INTEGER,
  initial_snapshot_id INTEGER,
  recheck_snapshot_id INTEGER,

  initial_match_id INTEGER,
  recheck_match_id INTEGER,

  entry_position_id INTEGER,
  last_error TEXT,
  fail_reason_json TEXT,
  feature_vector_json TEXT,

  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovery_events_token
ON discovery_events(token_mint, status);
CREATE INDEX IF NOT EXISTS idx_discovery_events_strategy
ON discovery_events(strategy_id, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_discovery_events_token_strategy
ON discovery_events(token_mint, pool_address, strategy_id, status);

CREATE UNIQUE INDEX IF NOT EXISTS ux_discovery_snapshot_token_pool_strategy
ON discovery_events(source_snapshot_id, token_mint, pool_address, strategy_id)
WHERE source_snapshot_id IS NOT NULL AND strategy_id IS NOT NULL;