from __future__ import annotations

from ..config import Settings, get_settings
from ..database import Database
from ..trading.live import (
    ExecutionPolicy,
    ExecutionResult,
    ExecutionStep,
    GmgnCliProvider,
    LiveExecutionEngine,
    SubprocessJsonRunner,
    SwapIntent,
)
from ..trading.live.errors import LiveTradeError
from ..trading.live.models import FailureKind, TradeSide
from .order_journal import SqliteOrderJournal


def build_execution_policy(settings: Settings | None = None) -> ExecutionPolicy:
    settings = settings or get_settings()
    low = ExecutionStep(
        settings.trade_slippage_low,
        settings.trade_priority_fee_low_sol,
        settings.trade_tip_fee_low_sol,
    )
    medium = ExecutionStep(
        settings.trade_slippage_medium,
        settings.trade_priority_fee_medium_sol,
        settings.trade_tip_fee_medium_sol,
    )
    high = ExecutionStep(
        settings.trade_slippage_high,
        settings.trade_priority_fee_high_sol,
        settings.trade_tip_fee_high_sol,
    )
    # Buy: low tier three times, medium twice, high once. Sell gets an extra
    # highest-tier attempt before requiring manual intervention.
    return ExecutionPolicy(
        buy_steps=(low, low, low, medium, medium, high),
        sell_steps=(low, low, low, medium, medium, high, high),
        poll_interval_seconds=2.0,
        max_polls_per_attempt=30,
        read_retry_count=2,
        read_retry_backoff_seconds=1.0,
    )


def build_live_provider(
    settings: Settings | None = None,
    *,
    allow_live_execution: bool,
) -> GmgnCliProvider:
    settings = settings or get_settings()
    if settings.trading_provider != "gmgn_cli":
        raise LiveTradeError(
            "the selected live provider has no configured application adapter",
            kind=FailureKind.VALIDATION,
            code="PROVIDER_NOT_WIRED",
        )
    return GmgnCliProvider(
        SubprocessJsonRunner((settings.gmgn_cli_path,)),
        allow_live_execution=allow_live_execution,
        anti_mev=True,
    )


class LiveTradingService:
    """Application guard around the transport-neutral live execution engine."""

    def __init__(self, database: Database, settings: Settings | None = None) -> None:
        self.database = database
        self.settings = settings or get_settings()

    async def execute(
        self,
        intent: SwapIntent,
        *,
        allow_authorized_exit: bool = False,
    ) -> ExecutionResult:
        if self.settings.dry_run:
            raise LiveTradeError(
                "DRY_RUN is enabled; live execution is blocked",
                kind=FailureKind.VALIDATION,
                code="DRY_RUN",
            )

        live_enabled = bool(self.database.get_runtime_state("live_trading_enabled", False))
        if not live_enabled:
            liquidation = self.database.get_runtime_state("liquidation_job") or {}
            liquidation_authorized = (
                allow_authorized_exit
                and intent.side is TradeSide.SELL
                and liquidation.get("status") in {"queued", "running"}
                and str(intent.metadata.get("liquidation_job_id") or "")
                == str(liquidation.get("id") or "")
            )
            if not liquidation_authorized:
                raise LiveTradeError(
                    "live trading has not passed the two-click confirmation",
                    kind=FailureKind.VALIDATION,
                    code="LIVE_DISABLED",
                )

        provider = build_live_provider(self.settings, allow_live_execution=True)
        engine = LiveExecutionEngine(provider, SqliteOrderJournal(self.database))
        return await engine.execute(intent, build_execution_policy(self.settings))
