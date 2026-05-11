from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from .config import settings
from .logging_config import logger
from .db.repositories import Repositories
from .services.provider_factory import create_providers
from .services.worker_manager import WorkerManager
from .services.price_aggregator import PriceAggregator
from .providers.gmgn_subscriber import create_gmgn_subscriber
from .services.event_bus import event_bus
from .runners.discovery_runner import DiscoveryRunner
from .runners.second_filter_runner import SecondFilterRunner
from .runners.price_monitor_runner import PriceMonitorRunner
from .runners.position_risk_runner import PositionRiskRunner
from .runners.kill_switch_runner import KillSwitchRunner
from .api.routes_mock import router as mock_router
from .api.routes_strategies import router as strategies_router
from .api.routes_tokens import router as tokens_router
from .api.routes_positions import router as positions_router
from .api.routes_trades import router as trades_router
from .api.routes_logs import router as logs_router
from .api.routes_risk import router as risk_router
from .api.routes_providers import router as providers_router
from .api.routes_runtime import router as runtime_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting backend", env=settings.APP_ENV)
    repo = await Repositories.create()
    await repo.ensure_default_strategy_groups()
    app.state.repo = repo
    app.state.providers = create_providers(repo)
    app.state.pause_new_entries = False

    subscriber = create_gmgn_subscriber()
    aggregator = PriceAggregator(repo, app.state.providers.gmgn, app.state.providers.jupiter, subscriber)
    app.state.price_aggregator = aggregator

    worker_mgr = WorkerManager(repo, event_bus=event_bus)
    app.state.worker_manager = worker_mgr

    strategy_groups = await repo.list_strategy_groups()

    discovery = DiscoveryRunner(repo, app.state.providers.gmgn, strategy_groups)
    second = SecondFilterRunner(repo, app.state.providers.gmgn, app.state.providers.jupiter,
                                app.state.providers.jito, app.state.providers.rpc, strategy_groups)
    price = PriceMonitorRunner(repo, aggregator)
    risk = PositionRiskRunner(repo, app.state.providers.gmgn)
    kill = KillSwitchRunner(repo)

    worker_mgr.register_worker('discovery', discovery.run_once, 60)
    worker_mgr.register_worker('second_filter', second.run_once, 30)
    worker_mgr.register_worker('price_monitor', price.run_once, 5)
    worker_mgr.register_worker('position_risk', risk.run_once, 10)
    worker_mgr.register_worker('kill_switch', kill.run_once, 30)

    await repo.set_runtime_setting('user_mode', 'IDLE', 'system')
    await repo.set_runtime_setting('workers_enabled', 'false', 'system')
    # Workers start only when user clicks 模拟交易 or 实盘交易 button

    yield

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
    from datetime import datetime, timezone
    return JSONResponse({
        "status": "ok", "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
