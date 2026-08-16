from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime settings loaded from the existing project-level .env.

    Secret values are intentionally not represented in logs or API responses.
    Trading adapters read credentials only at the point where they are needed.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    frontend_origin: str = "http://localhost:5173"
    sqlite_path: str = "data/meme_quant.db"
    dry_run: bool = True
    simulation_enabled: bool = True
    trading_provider: Literal["gmgn_cli", "gmgn_http", "disabled"] = "gmgn_cli"
    gmgn_cli_path: str = "gmgn-cli"
    wallet_public_key: str | None = None
    # Read-only route verification for paper exits. Jupiter quote requests never
    # sign or submit a transaction; API keys are secret-wrapped and never exposed.
    paper_jupiter_quote_url: str = "https://api.jup.ag/swap/v2/order"
    jupiter_api_key_1: SecretStr | None = None
    jupiter_api_key_2: SecretStr | None = None
    jupiter_api_key_3: SecretStr | None = None
    paper_read_only_quote_enabled: bool = True
    paper_quote_timeout_seconds: float = Field(default=8.0, gt=0, le=30)

    live_confirmation_ttl_seconds: int = Field(default=60, ge=15, le=300)
    wallet_sol_reserve: float = Field(default=0.1, ge=0)
    max_open_positions: int = Field(default=10, ge=1, le=100)
    max_daily_loss_fraction: float = Field(default=0.20, gt=0, le=1)
    consecutive_loss_limit: int = Field(default=5, ge=1, le=100)

    training_timezone: str = "Asia/Shanghai"
    training_weekday: int = Field(default=6, ge=0, le=6)
    # Automatic training is staged at 17:00 Beijing time. Model strategies stop
    # opening new positions one hour earlier so the old generation can drain.
    training_hour: int = Field(default=17, ge=0, le=23)
    training_minute: int = Field(default=0, ge=0, le=59)
    training_lookback_days: int = Field(default=120, ge=30)
    training_holdout_days: int = Field(default=30, ge=7)
    training_max_retries: int = Field(default=2, ge=0, le=10)
    training_worker_poll_seconds: int = Field(default=5, ge=1, le=300)
    min_precision: float = Field(default=0.35, ge=0, le=1)
    promotion_min_pnl_lift: float = Field(default=0.05, ge=0)

    # Versioned conservative execution ladder. Values are operator-overridable.
    trade_slippage_low: float = Field(default=0.10, gt=0, le=0.25)
    trade_slippage_medium: float = Field(default=0.15, gt=0, le=0.25)
    trade_slippage_high: float = Field(default=0.25, gt=0, le=0.25)
    trade_priority_fee_low_sol: float = Field(default=0.002, ge=0)
    trade_priority_fee_medium_sol: float = Field(default=0.003, ge=0)
    trade_priority_fee_high_sol: float = Field(default=0.005, ge=0)
    trade_tip_fee_low_sol: float = Field(default=0.0001, ge=0)
    trade_tip_fee_medium_sol: float = Field(default=0.0005, ge=0)
    trade_tip_fee_high_sol: float = Field(default=0.001, ge=0)
    trade_total_fee_cap_sol: float = Field(default=0.006, gt=0, le=0.02)

    collector_poll_seconds: int = Field(default=120, ge=15)
    collector_enabled: bool = True
    # Older collector deployments used 200. The current GMGN endpoint accepts
    # at most 80 per request; the worker clamps the effective request while
    # retaining backward-compatible .env loading.
    gmgn_trenches_limit: int = Field(default=80, ge=1, le=10_000)
    signal_poll_seconds: int = Field(default=5, ge=1, le=300)
    signal_max_age_seconds: int = Field(default=300, ge=30, le=3_600)
    paper_market_monitor_enabled: bool = True
    position_monitor_enabled: bool = True
    position_monitor_poll_seconds: float = Field(default=3.0, ge=1.0, le=60.0)
    regime_poll_seconds: int = Field(default=60, ge=15, le=3_600)
    adaptive_policy_enabled: bool = True
    adaptive_action_interval_minutes: int = Field(default=15, ge=5, le=60)
    adaptive_min_confidence: float = Field(default=0.55, ge=0, le=1)
    adaptive_exploration_rate: float = Field(default=0.0, ge=0, le=0.05)
    adaptive_min_regime_snapshots: int = Field(default=12, ge=1, le=10_000)
    reconciliation_poll_seconds: int = Field(default=30, ge=5, le=3_600)
    liquidation_poll_seconds: int = Field(default=5, ge=1, le=300)
    background_workers_enabled: bool = True
    model_monitor_window_days: int = Field(default=7, ge=1)
    model_degraded_ratio: float = Field(default=0.70, gt=0, le=1)
    model_monitor_poll_seconds: int = Field(default=3_600, ge=60, le=86_400)
    model_monitor_min_predictions: int = Field(default=20, ge=5)
    model_monitor_min_selected: int = Field(default=3, ge=1)
    model_monitor_cooldown_hours: int = Field(default=24, ge=1, le=168)

    @field_validator("sqlite_path")
    @classmethod
    def sqlite_path_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SQLITE_PATH must not be empty")
        return value

    @model_validator(mode="after")
    def execution_ladder_is_monotone_and_capped(self) -> "Settings":
        slippage = (self.trade_slippage_low, self.trade_slippage_medium, self.trade_slippage_high)
        priority = (self.trade_priority_fee_low_sol, self.trade_priority_fee_medium_sol, self.trade_priority_fee_high_sol)
        tips = (self.trade_tip_fee_low_sol, self.trade_tip_fee_medium_sol, self.trade_tip_fee_high_sol)
        if tuple(sorted(slippage)) != slippage or tuple(sorted(priority)) != priority or tuple(sorted(tips)) != tips:
            raise ValueError("trade slippage, priority fee and tip ladders must be monotone")
        if any(p + t > self.trade_total_fee_cap_sol + 1e-12 for p, t in zip(priority, tips, strict=True)):
            raise ValueError("a priority+tip tier exceeds TRADE_TOTAL_FEE_CAP_SOL")
        return self

    @property
    def jupiter_api_keys(self) -> tuple[str, ...]:
        values = (self.jupiter_api_key_1, self.jupiter_api_key_2, self.jupiter_api_key_3)
        return tuple(
            value.get_secret_value()
            for value in values
            if value is not None and value.get_secret_value()
        )

    @property
    def database_path(self) -> Path:
        path = Path(self.sqlite_path)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def model_directory(self) -> Path:
        return PROJECT_ROOT / "ml_models"

    @property
    def csv_import_path(self) -> Path:
        return PROJECT_ROOT / "meme数据.csv"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.model_directory.mkdir(parents=True, exist_ok=True)
    return settings
