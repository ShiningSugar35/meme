"""
Provider management routes.

This version keeps the existing provider-mode/test endpoints, but fixes
/api/providers/health so the frontend receives one row per provider instead of
wrapper fields such as provider_mode/providers/overall_health.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..config import ProviderMode, settings

router = APIRouter(prefix="/api/providers", tags=["providers"])


class ModeRequest(BaseModel):
    mode: str


def _json_response(content: Any, status_code: int = 200) -> JSONResponse:
    """Return JSON after converting datetime/Enum/SecretStr/etc. to JSON-safe values."""
    return JSONResponse(content=jsonable_encoder(content), status_code=status_code)


def _mode_value() -> str:
    mode = settings.get_provider_mode()
    return mode.value if isinstance(mode, ProviderMode) else str(mode)


def _entry(ok: bool, summary: str, *, latency_ms: int | None = None, error: str | None = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "ok": bool(ok),
        "summary": summary,
    }
    if latency_ms is not None:
        item["latency_ms"] = latency_ms
    if error:
        item["error"] = error
    return item


def _provider_health_rows() -> Dict[str, Dict[str, Any]]:
    """
    Build a frontend-friendly provider health map.

    The Operations page iterates Object.entries(providerHealth) and expects each
    value to contain an `ok` boolean. Returning wrapper keys causes the UI to
    render provider_mode/providers/overall_health as failed pseudo-providers.
    """
    mode = settings.get_provider_mode()
    mode_text = _mode_value()

    gmgn_keys = settings.get_gmgn_api_keys()
    gmgn_client_ids = settings.get_gmgn_client_ids() if hasattr(settings, "get_gmgn_client_ids") else []
    gmgn_credentials = settings.get_gmgn_credentials() if hasattr(settings, "get_gmgn_credentials") else []
    gmgn_base = (settings.GMGN_API_BASE_URL or "").strip()
    gmgn_ok = bool(gmgn_base)
    gmgn_summary = f"mode={mode_text}; base={gmgn_base or 'missing'}; keys={len(gmgn_keys)}; client_ids={len(gmgn_client_ids)}"
    gmgn_error = None
    if mode == ProviderMode.LIVE and not (gmgn_keys or gmgn_client_ids or gmgn_credentials):
        gmgn_ok = False
        gmgn_error = "GMGN_API_KEY_N or GMGN_CLIENT_ID_N/GMGN_PUBLIC_KEY_N is required in LIVE mode"
    elif not gmgn_base:
        gmgn_error = "GMGN_API_BASE_URL is missing"

    jupiter_base = (settings.JUPITER_API_BASE_URL or "").strip()
    jupiter_ok = bool(jupiter_base)
    jupiter_error = None if jupiter_ok else "JUPITER_API_BASE_URL is missing"

    rpc_url = (settings.SOLANA_RPC_URL or "").strip()
    rpc_ok = bool(rpc_url) or mode == ProviderMode.MOCK
    rpc_error = None if rpc_ok else "SOLANA_RPC_URL is missing"

    if settings.JITO_ENABLED:
        jito_ok = bool(settings.JITO_BLOCK_ENGINE_URL)
        jito_summary = f"enabled; block_engine={settings.JITO_BLOCK_ENGINE_URL or 'missing'}"
        jito_error = None if jito_ok else "JITO_BLOCK_ENGINE_URL is missing while JITO_ENABLED=true"
    else:
        # Jito is optional. Treat disabled as non-failing so it does not turn the
        # dashboard red when the user intentionally runs without Jito bundles.
        jito_ok = True
        jito_summary = "disabled / optional"
        jito_error = None

    return {
        "GMGN": _entry(gmgn_ok, gmgn_summary, error=gmgn_error),
        "Jupiter": _entry(jupiter_ok, f"base={jupiter_base or 'missing'}", error=jupiter_error),
        "RPC": _entry(rpc_ok, f"mode={mode_text}; rpc={'configured' if rpc_url else 'missing'}", error=rpc_error),
        "Jito": _entry(jito_ok, jito_summary, error=jito_error),
    }


@router.get("/health")
async def health():
    """Return a flat provider health map that the Operations UI can render directly."""
    return _json_response(_provider_health_rows())


@router.post("/mode")
async def set_mode(req: ModeRequest):
    """Switch provider mode at runtime. Supported: mock, online_readonly, live."""
    try:
        mode = ProviderMode(req.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid mode. Use: mock, online_readonly, live") from exc

    settings.set_provider_mode(mode)
    return _json_response({"ok": True, "mode": mode.value})


@router.post("/gmgn/test")
async def test_gmgn():
    """Lightweight GMGN config test. This does not place trades."""
    mode = settings.get_provider_mode()
    keys = settings.get_gmgn_api_keys()
    client_ids = settings.get_gmgn_client_ids() if hasattr(settings, "get_gmgn_client_ids") else []
    credentials = settings.get_gmgn_credentials() if hasattr(settings, "get_gmgn_credentials") else []
    base = (settings.GMGN_API_BASE_URL or "").strip()
    ok = bool(base) and (mode != ProviderMode.LIVE or bool(keys or client_ids or credentials))
    return _json_response(
        {
            "provider": "GMGN",
            "ok": ok,
            "error_code": None if ok else "GMGN_CONFIG_INCOMPLETE",
            "mode": _mode_value(),
            "base_url": base,
            "api_key_count": len(keys),
            "client_id_count": len(client_ids),
            "credential_count": len(credentials),
            "message": "GMGN configuration looks usable" if ok else "GMGN configuration is incomplete",
        },
        status_code=200 if ok else 400,
    )


@router.post("/jupiter/test")
async def test_jupiter():
    """Lightweight Jupiter config test. This does not place swaps."""
    base = (settings.JUPITER_API_BASE_URL or "").strip()
    ok = bool(base)
    return _json_response(
        {
            "ok": ok,
            "base_url": base,
            "message": "Jupiter configuration looks usable" if ok else "Jupiter base URL is missing",
        },
        status_code=200 if ok else 400,
    )


@router.post("/jupiter/quote-test")
async def quote_test_jupiter(request: Request, payload: Dict[str, Any]):
    """Backward-compatible safe quote test endpoint; always uses MOCK mode."""
    from ..providers.jupiter_real import JupiterProvider

    repo = request.app.state.repo
    provider = JupiterProvider(repo, mode=ProviderMode.MOCK)
    quote = await provider.quote_exact_in(
        payload.get("input_mint") or "SOL",
        payload.get("output_mint") or "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        int(payload.get("amount_lamports") or payload.get("amount") or 0),
        int(payload.get("slippage_bps") or 1500),
    )
    return _json_response({
        "provider": "Jupiter",
        "ok": not bool(quote.get("error")),
        "latency_ms": 1,
        "error_code": quote.get("error"),
        "summary": {"priceImpactPct": quote.get("priceImpactPct")},
    })


@router.post("/jito/tip-test")
async def tip_test_jito(request: Request):
    """Backward-compatible safe Jito tip test endpoint; always uses MOCK mode."""
    from ..providers.jito_real import JitoProvider

    repo = request.app.state.repo
    provider = JitoProvider(repo, mode=ProviderMode.MOCK)
    tip = await provider.get_tip_floor()
    return _json_response({
        "provider": "Jito",
        "ok": True,
        "summary": {
            "mode": tip.get("mode"),
            "has_50th": bool(tip.get("landed_tips_50th_percentile")),
        },
    })
