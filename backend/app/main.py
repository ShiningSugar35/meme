from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.routes import router
from .config import get_settings
from .database import get_database, utc_now_iso
from .collector.rate_limit import AsyncRateLimiter
from .scheduler.service import TrainingScheduler
from .services.collector_worker import CollectorWorker
from .services.liquidation import LiquidationWorker
from .services.model_health import ModelHealthWorker
from .services.position_monitor import PositionMonitorWorker
from .services.platform_configuration import PlatformConfigurationService
from .services.prediction import PredictionWorker
from .services.reconciliation import ReconciliationWorker
from .services.regime import MarketRegimeWorker
from .services.system_awake import SystemAwakeService
from .services.training_worker import TrainingWorker


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    database = get_database()
    database.initialize()
    platform_configuration = PlatformConfigurationService(database)
    gmgn_limiter = AsyncRateLimiter(platform_configuration.runtime_values()["gmgn_global_rps"])
    # Legacy CSV import is an explicit maintenance action only. Startup must not
    # repopulate pre-current-generation samples after a feature-contract reset.

    tasks: list[asyncio.Task] = []
    collector: CollectorWorker | None = None
    scheduler: TrainingScheduler | None = None
    prediction_worker: PredictionWorker | None = None
    reconciliation_worker: ReconciliationWorker | None = None
    liquidation_worker: LiquidationWorker | None = None
    model_health_worker: ModelHealthWorker | None = None
    position_monitor_worker: PositionMonitorWorker | None = None
    regime_worker: MarketRegimeWorker | None = None
    training_worker: TrainingWorker | None = None
    system_awake_service: SystemAwakeService | None = None

    if settings.app_env != "test":
        # Reconcile durable non-terminal orders before any worker can create new
        # signals or orders. Any unresolved outcome keeps new entries fail-closed.
        reconciliation_worker = ReconciliationWorker(database, settings)
        try:
            await reconciliation_worker.run_once()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:500]
            database.set_runtime_state("new_entries_paused", True)
            database.set_runtime_state(
                "new_entries_pause_reason", "startup_reconciliation_failed"
            )
            database.set_runtime_state(
                "reconciliation_worker_status",
                {
                    "state": "degraded",
                    "last_error": message,
                    "last_run_at": utc_now_iso(),
                },
            )
            database.audit(
                category="reconciliation",
                action="startup_failed",
                severity="error",
                details={"error": message},
            )

    if settings.background_workers_enabled and settings.app_env != "test":
        if settings.collector_enabled and settings.prevent_sleep_while_collecting:
            system_awake_service = SystemAwakeService(database, enabled=True)
            tasks.append(
                asyncio.create_task(
                    system_awake_service.run_forever(),
                    name="system-awake-request",
                )
            )

        training_worker = TrainingWorker(database, settings)
        # Recover interrupted runs before any producer (scheduler/model health/API)
        # can enqueue more work; subsequent execution is serialized by this worker.
        recovery = training_worker.recover_interrupted_runs()
        tasks.append(
            asyncio.create_task(
                training_worker.run_forever(recovery=recovery),
                name="training-worker",
            )
        )

        scheduler = TrainingScheduler(database, settings)
        # Establish the 16:00/17:00 model-entry gate synchronously before the
        # prediction worker can reconcile any fresh buy signal after startup.
        scheduler.refresh_entry_gate()
        tasks.append(asyncio.create_task(scheduler.run_forever(), name="model-training-scheduler"))

        assert reconciliation_worker is not None
        tasks.append(
            asyncio.create_task(
                reconciliation_worker.run_forever(), name="order-reconciliation-worker"
            )
        )
        liquidation_worker = LiquidationWorker(database, settings)
        tasks.append(
            asyncio.create_task(
                liquidation_worker.run_forever(), name="liquidation-worker"
            )
        )

        model_health_worker = ModelHealthWorker(database, settings)
        tasks.append(asyncio.create_task(model_health_worker.run_forever(), name="model-health-worker"))

        if settings.position_monitor_enabled:
            position_monitor_worker = PositionMonitorWorker(database, settings, gmgn_limiter=gmgn_limiter)
            tasks.append(
                asyncio.create_task(
                    position_monitor_worker.run_forever(), name="position-monitor-worker"
                )
            )

        regime_worker = MarketRegimeWorker(database, settings.regime_poll_seconds)
        tasks.append(asyncio.create_task(regime_worker.run_forever(), name="market-regime-worker"))

        prediction_worker = PredictionWorker(database, settings)
        tasks.append(asyncio.create_task(prediction_worker.run_forever(), name="prediction-worker"))
        if settings.collector_enabled:
            collector = CollectorWorker(database, settings, monitor_only=False, gmgn_limiter=gmgn_limiter)
            tasks.append(
                asyncio.create_task(
                    collector.run_forever(),
                    name="gmgn-collector",
                )
            )
        else:
            # Never leave a stale "running" snapshot from a previous process when
            # Collector is disabled at startup. This is especially important after
            # hot reloads or temporary maintenance toggles because runtime_state is
            # durable across process restarts.
            database.set_runtime_state(
                "collector_status",
                {
                    "state": "disabled",
                    "mode": "collector",
                    "cycle_state": "idle",
                    "reason": "collector_disabled_by_configuration",
                    "updated_at": utc_now_iso(),
                },
            )

    try:
        yield
    finally:
        if collector:
            collector.stop()
        if scheduler:
            scheduler.stop()
        if prediction_worker:
            prediction_worker.stop()
        if reconciliation_worker:
            reconciliation_worker.stop()
        if liquidation_worker:
            liquidation_worker.stop()
        if model_health_worker:
            model_health_worker.stop()
        if position_monitor_worker:
            position_monitor_worker.stop()
        if regime_worker:
            regime_worker.stop()
        if training_worker:
            training_worker.stop()
        if system_awake_service:
            system_awake_service.stop()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Solana Meme Quant Trading System",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.app_env}

    app.include_router(router)
    return app


app = create_app()
