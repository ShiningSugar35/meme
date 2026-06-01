from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .logging_config import logger
from .db.repositories import Repositories
from .services.provider_factory import create_providers
from .services.worker_manager import WorkerManager
from .services.price_aggregator import PriceAggregator
from .providers.gmgn_subscriber import create_gmgn_subscriber
from .services.event_bus import event_bus
from .runners.discovery_runner import DiscoveryRunner
from .runners.price_monitor_runner import PriceMonitorRunner
from .runners.position_risk_runner import PositionRiskRunner
from .runners.kill_switch_runner import KillSwitchRunner
from .runners.active_position_price_runner import ActivePositionPriceRunner
from .trading.executor import TradingPipeline
from .api.routes_mock import router as mock_router
from .api.routes_strategies import router as strategies_router
from .api.routes_tokens import router as tokens_router
from .api.routes_positions import router as positions_router
from .api.routes_trades import router as trades_router
from .api.routes_logs import router as logs_router
from .api.routes_risk import router as risk_router
from .api.routes_providers import router as providers_router
from .api.routes_runtime import router as runtime_router, ensure_runtime_defaults


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting backend", env=settings.APP_ENV)

    try:
        repo = await Repositories.create()
    except Exception as e:
        logger.exception("Failed to create DB repo; backend aborting")
        raise

    try:
        await ensure_runtime_defaults(repo)
    except Exception as e:
        logger.error(f"ensure_runtime_defaults failed (non-fatal): {e}")

    app.state.repo = repo
    app.state.session_started_at = _iso_now()

    try:
        providers = create_providers(repo)
    except Exception as e:
        logger.error(f"create_providers failed: {e}")
        providers = None
    app.state.providers = providers
    app.state.pause_new_entries = True

    subscriber = create_gmgn_subscriber()
    try:
        aggregator = PriceAggregator(repo, providers.gmgn if providers else None, providers.jupiter if providers else None, subscriber)
    except Exception as e:
        logger.error(f"PriceAggregator init failed: {e}")
        aggregator = None
    app.state.price_aggregator = aggregator

    try:
        trading_pipeline = TradingPipeline(repo, providers.gmgn if providers else None, providers.jupiter if providers else None, providers.jito if providers else None, providers.rpc if providers else None)
    except Exception as e:
        logger.error(f"TradingPipeline init failed: {e}")
        trading_pipeline = None
    app.state.trading_pipeline = trading_pipeline

    worker_mgr = WorkerManager(repo, event_bus=event_bus)
    app.state.worker_manager = worker_mgr

    try:
        strategy_groups = await repo.list_strategy_groups()
    except Exception:
        strategy_groups = []
    discovery = DiscoveryRunner(repo, providers.gmgn if providers else None, strategy_groups, providers.jupiter if providers else None, providers.jito if providers else None, providers.rpc if providers else None)
    price = PriceMonitorRunner(repo, aggregator) if aggregator else None
    risk = PositionRiskRunner(repo, providers.gmgn if providers else None, trading_pipeline=trading_pipeline)
    kill = KillSwitchRunner(repo)
    active_price_runner = ActivePositionPriceRunner(repo, providers.gmgn if providers else None, trading_pipeline=trading_pipeline)

    worker_mgr.register_worker('discovery', discovery.run_once, int(settings.POLL_INTERVAL_SECONDS))
    if price:
        worker_mgr.register_worker('price_monitor', price.run_once, int(settings.ACTIVE_POSITION_PRICE_POLL_SECONDS))
    worker_mgr.register_worker('position_risk', risk.run_once, 1)
    worker_mgr.register_worker('kill_switch', kill.run_once, 30)
    worker_mgr.register_worker('active_position_price', active_price_runner.run_once, int(settings.ACTIVE_POSITION_PRICE_POLL_SECONDS))

    try:
        await repo.set_runtime_setting('user_mode', 'IDLE', 'system')
        await repo.set_runtime_setting('workers_enabled', 'false', 'system')
        await repo.set_runtime_setting('live_entries_enabled', 'false', 'system')
        await repo.set_runtime_setting('session_started_at', app.state.session_started_at, 'system')
        await repo.append_system_event('INFO', 'RUNTIME', 'Backend session started', app.state.session_started_at, account_type='SIM')
    except Exception as e:
        logger.error(f"Init runtime settings/event failed (non-fatal): {e}")

    try:
        yield
    finally:
        logger.info("Shutting down backend")
        await worker_mgr.stop_all()
        try:
            await repo.close()
        except Exception:
            logger.exception("Error closing repo on shutdown")


app = FastAPI(title="Solana Meme Trading Bot", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mock_router)
app.include_router(strategies_router)
app.include_router(tokens_router)
app.include_router(positions_router)
app.include_router(trades_router)
app.include_router(logs_router)
app.include_router(risk_router)
app.include_router(providers_router)
app.include_router(runtime_router)


@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "version": "1.1.1-runtime-api-merged",
        "timestamp": _iso_now(),
    })
