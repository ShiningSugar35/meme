from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from ..config import get_settings
from ..database import get_database
from ..repositories.models import ModelRepository
from ..risk.service import RiskService
from ..services.agent_service import AgentService
from ..services.csv_importer import CsvImporter
from ..services.dashboard import DashboardService
from ..services.paper_trading import PaperTradingService
from ..services.runtime import RuntimeService
from ..services.prediction import PredictionService
from ..services.training import TrainingService
from .schemas import (
    AgentDecisionRequest,
    AgentProposalRequest,
    ConfirmActionRequest,
    ImportRequest,
    TrainingRequest,
)


router = APIRouter(prefix="/api")


@router.get("/dashboard")
def dashboard() -> dict:
    return DashboardService(get_database()).overview()


@router.get("/models")
def models(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    database = get_database()
    repository = ModelRepository(database)
    training = TrainingService(database)
    return {
        "champion": repository.champion(),
        "items": repository.list(limit=limit),
        "feature_catalog": training.feature_catalog(),
        "training_runs": training.list_runs(limit=min(limit, 20)),
    }


@router.post("/models/train", status_code=202)
def train_model(request: TrainingRequest) -> dict:
    service = TrainingService(get_database())
    try:
        run_id = service.create_run("manual", feature_names=request.features)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Execution is intentionally delegated to the durable TrainingWorker. The
    # HTTP request only creates a persisted queue item, so a browser disconnect
    # or process restart cannot orphan a manual training request.
    return {"run_id": run_id, "status": "queued", "reason": request.reason}


@router.get("/models/training-runs")
def training_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict:
    return {"items": TrainingService(get_database()).list_runs(limit=limit)}


@router.post("/models/{model_id}/rollback")
def rollback_model(model_id: str) -> dict:
    try:
        return {"champion": TrainingService(get_database()).rollback_model(model_id)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/signals")
def signals(limit: int = Query(default=100, ge=1, le=500)) -> dict:
    return {"items": DashboardService(get_database()).list_signals(limit=limit)}


@router.post("/signals/process")
def process_signals(limit: int = Query(default=100, ge=1, le=1_000)) -> dict:
    """Run one idempotent scoring/settlement cycle for operations and tests."""
    return asdict(PredictionService(get_database()).run_cycle(limit=limit))


@router.get("/portfolio")
def portfolio(
    include_closed: bool = True,
    include_previous_simulation_sessions: bool = False,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    return {
        "items": DashboardService(get_database()).list_positions(
            include_closed=include_closed,
            include_previous_simulation_sessions=include_previous_simulation_sessions,
            limit=limit,
        )
    }


@router.get("/simulation")
def simulation_status() -> dict:
    return PaperTradingService(get_database()).simulation_status()


@router.post("/simulation/reset")
def reset_simulation() -> dict:
    try:
        return PaperTradingService(get_database()).reset_simulation()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/simulation/history")
def simulation_history(limit: int = Query(default=20, ge=1, le=200)) -> dict:
    return {"items": PaperTradingService(get_database()).simulation_history(limit=limit)}


@router.get("/runtime")
def runtime_status() -> dict:
    database = get_database()
    return {"runtime": RuntimeService(database).status(), "risk": RiskService(database).status()}


@router.get("/runtime/collector-events")
def collector_events(limit: int = Query(default=200, ge=1, le=250)) -> dict:
    return {"items": RuntimeService(get_database()).collector_events(limit=limit)}


@router.post("/runtime/live/prepare")
def prepare_live() -> dict:
    return asdict(RuntimeService(get_database()).prepare_live_start())


@router.post("/runtime/live/confirm")
def confirm_live(request: ConfirmActionRequest) -> dict:
    try:
        return RuntimeService(get_database()).confirm_live_start(request.challenge)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runtime/live/stop")
def stop_live() -> dict:
    return RuntimeService(get_database()).stop_live()


@router.post("/risk/resume")
def resume_risk() -> dict:
    service = RiskService(get_database())
    service.resume_new_entries()
    return service.status()


@router.post("/portfolio/liquidate/prepare")
def prepare_liquidation() -> dict:
    return asdict(RuntimeService(get_database()).prepare_liquidation())


@router.post("/portfolio/liquidate/confirm")
def confirm_liquidation(request: ConfirmActionRequest) -> dict:
    try:
        return RuntimeService(get_database()).confirm_liquidation(request.challenge)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/agent/context")
def agent_context() -> dict:
    return AgentService(get_database()).get_context()


@router.post("/agent/proposals", status_code=202)
def create_agent_proposal(request: AgentProposalRequest) -> dict:
    try:
        return AgentService(get_database()).create_proposal(request.proposal_type, request.payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/agent/proposals")
def agent_proposals(limit: int = Query(default=50, ge=1, le=100)) -> dict:
    return {"items": AgentService(get_database()).list_proposals(limit=limit)}


@router.post("/agent/proposals/{proposal_id}/approve")
def approve_agent_proposal(proposal_id: str, request: AgentDecisionRequest) -> dict:
    try:
        return AgentService(get_database()).approve_proposal(proposal_id, note=request.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/agent/proposals/{proposal_id}/reject")
def reject_agent_proposal(proposal_id: str, request: AgentDecisionRequest) -> dict:
    try:
        return AgentService(get_database()).reject_proposal(proposal_id, note=request.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/data/import")
def import_data(request: ImportRequest) -> dict:
    settings = get_settings()
    path = settings.csv_import_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="meme data CSV not found")
    return asdict(CsvImporter(get_database()).import_file(Path(path), force=request.force))
