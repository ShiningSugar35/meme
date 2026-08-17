from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Response

from ..config import get_settings
from ..database import get_database
from ..repositories.models import ModelRepository
from ..risk.service import RiskService
from ..services.agent_service import AgentService
from ..services.csv_importer import CsvImporter
from ..services.dashboard import DashboardService
from ..services.adaptive_policy import AdaptivePolicyService
from ..services.paper_trading import PaperTradingService
from ..services.regime import MarketRegimeService
from ..services.platform_configuration import PlatformConfigurationService
from ..services.runtime import RuntimeService
from ..services.sample_export import SampleExportService
from ..services.prediction import PredictionService
from ..services.training import TrainingService
from .schemas import (
    AgentDecisionRequest,
    AgentProposalRequest,
    ConfirmActionRequest,
    FeatureSelectionRequest,
    ImportRequest,
    PlatformRuntimeConfigRequest,
    ProviderCredentialRequest,
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
        "active_models": repository.active_models(),
        "items": repository.list(limit=limit),
        "feature_catalog": training.feature_catalog(),
        "training_runs": training.list_runs(limit=min(limit, 20)),
    }


@router.put("/models/feature-selection")
def save_model_feature_selection(request: FeatureSelectionRequest) -> dict:
    service = TrainingService(get_database())
    try:
        selected = service.save_feature_selection(request.features)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"selected_features": list(selected), "count": len(selected)}


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


@router.get("/samples")
def samples(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=10, le=200),
) -> dict:
    return DashboardService(get_database()).sample_ledger(page=page, page_size=page_size)


@router.get("/samples/export.csv")
def export_samples() -> Response:
    text, count = SampleExportService(get_database()).render_csv()
    return Response(
        content="\ufeff" + text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="meme-data.csv"; filename*=UTF-8\'\'meme%E6%95%B0%E6%8D%AE.csv',
            "X-Sample-Count": str(count),
        },
    )


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


@router.get("/portfolio/view")
def portfolio_view(
    mode: str = Query(default="simulation", pattern="^(simulation|live)$"),
    strategy: str = Query(default="model_1", pattern="^(model_1|model_2|model_3|rules_only)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=10, le=100),
    start_at: str | None = None,
    end_at: str | None = None,
) -> dict:
    try:
        return DashboardService(get_database()).portfolio_view(
            mode=mode,
            strategy=strategy,
            page=page,
            page_size=page_size,
            start_at=start_at,
            end_at=end_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@router.get("/simulation/audit")
def simulation_audit(limit_sessions: int = Query(default=50, ge=1, le=200)) -> dict:
    return {"items": PaperTradingService(get_database()).simulation_audit(limit_sessions=limit_sessions)}


@router.get("/runtime")
def runtime_status() -> dict:
    database = get_database()
    return {"runtime": RuntimeService(database).status(), "risk": RiskService(database).status()}


@router.get("/regime")
def market_regime_status() -> dict:
    return MarketRegimeService(get_database()).public_status()


@router.get("/adaptive-policy")
def adaptive_policy_status() -> dict:
    return AdaptivePolicyService(get_database()).status()


@router.get("/configuration")
def platform_configuration() -> dict:
    return PlatformConfigurationService(get_database()).configuration()


@router.put("/configuration/runtime")
def save_platform_runtime_configuration(request: PlatformRuntimeConfigRequest) -> dict:
    try:
        return PlatformConfigurationService(get_database()).save_runtime(
            position_monitor_poll_seconds=request.position_monitor_poll_seconds,
            gmgn_global_rps=request.gmgn_global_rps,
            regime_poll_seconds=request.regime_poll_seconds,
            adaptive_action_interval_minutes=request.adaptive_action_interval_minutes,
            adaptive_min_confidence=request.adaptive_min_confidence,
            adaptive_exploration_rate=request.adaptive_exploration_rate,
            gmgn_base_url=request.gmgn_base_url,
            jupiter_quote_url=request.jupiter_quote_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/configuration/providers/{provider}/credentials")
def add_platform_provider_credential(provider: str, request: ProviderCredentialRequest) -> dict:
    try:
        return PlatformConfigurationService(get_database()).add_credential(
            provider,
            request.credential.get_secret_value(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/configuration/providers/{provider}/credentials/{slot}")
def delete_platform_provider_credential(provider: str, slot: int) -> dict:
    try:
        return PlatformConfigurationService(get_database()).delete_credential(provider, slot)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
def prepare_liquidation(
    mode: str = Query(default="all", pattern="^(all|simulation|live)$"),
) -> dict:
    try:
        return asdict(RuntimeService(get_database()).prepare_liquidation(mode))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/portfolio/liquidate/confirm")
def confirm_liquidation(
    request: ConfirmActionRequest,
    mode: str = Query(default="all", pattern="^(all|simulation|live)$"),
) -> dict:
    try:
        return RuntimeService(get_database()).confirm_liquidation(request.challenge, mode)
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
