from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import asyncio
from contextlib import asynccontextmanager
from .config import settings
from .logging_config import logger
from .db.repositories import Repositories
from .services.provider_factory import create_providers
from .api.routes_mock import router as mock_router
from .api.routes_strategies import router as strategies_router
from .api.routes_tokens import router as tokens_router
from .api.routes_positions import router as positions_router
from .api.routes_trades import router as trades_router
from .api.routes_logs import router as logs_router
from .api.routes_risk import router as risk_router
from .api.routes_providers import router as providers_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting backend", env=settings.APP_ENV)
    repo = await Repositories.create()
    await repo.ensure_default_strategy_groups()
    app.state.repo = repo
    app.state.providers = create_providers(repo)
    app.state.pause_new_entries = False
    yield
    logger.info("Shutting down backend")
    repo = getattr(app.state, 'repo', None)
    if repo:
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


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "version": "1.0.0", "timestamp": "2026-05-08T00:00:00Z"})
