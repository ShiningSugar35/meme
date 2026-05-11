"""
Provider-related API routes - optimized to use existing app.state providers.
"""
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from ..config import settings, ProviderMode
from ..logging_config import logger

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("/health")
async def provider_health(request: Request):
    """Provider health check using existing providers from app state. No new DB connections."""
    try:
        mode = settings.get_provider_mode()
        providers_container = getattr(request.app.state, 'providers', None)

        result = {
            "provider_mode": mode.value,
            "providers": []
        }

        # GMGN
        gmgn_ok = True
        gmgn_summary = {"mode": mode.value}
        if mode != ProviderMode.MOCK:
            if not settings.get_gmgn_api_key():
                gmgn_ok = False
                gmgn_summary = "Missing API key"
        result["providers"].append({
            "provider": "GMGN", "ok": gmgn_ok, "latency_ms": 0,
            "error_code": None, "summary": gmgn_summary
        })

        # Jupiter
        jup_ok = True
        jup_summary = {"mode": mode.value}
        if mode != ProviderMode.MOCK:
            if not settings.get_jupiter_api_key():
                jup_ok = False
                jup_summary = "Missing API key"
        result["providers"].append({
            "provider": "Jupiter", "ok": jup_ok, "latency_ms": 0,
            "error_code": None, "summary": jup_summary
        })

        # Jito
        jito_ok = settings.JITO_ENABLED
        jito_summary = "disabled" if not settings.JITO_ENABLED else {"mode": mode.value}
        result["providers"].append({
            "provider": "Jito", "ok": jito_ok, "latency_ms": 0,
            "error_code": None, "summary": jito_summary
        })

        # RPC
        rpc_ok = bool(settings.get_rpc_http_url())
        rpc_summary = "No RPC URL" if not rpc_ok else {"mode": mode.value}
        result["providers"].append({
            "provider": "RPC", "ok": rpc_ok, "latency_ms": 0,
            "error_code": None, "summary": rpc_summary
        })

        all_ok = all(p["ok"] for p in result["providers"])
        result["overall_health"] = "healthy" if all_ok else "degraded"
        return JSONResponse(result)

    except Exception as e:
        logger.exception("Error in provider health endpoint")
        return JSONResponse({"error": str(e)[:200], "overall_health": "error"}, status_code=500)


@router.post("/gmgn/test")
async def test_gmgn(request: Request):
    """Test GMGN endpoint."""
    try:
        mode = settings.get_provider_mode()
        from ..providers.gmgn_real import GMGNProvider
        repo = request.app.state.repo
        gmgn = GMGNProvider(repo, mode=mode)
        trenches = await gmgn.fetch_trenches({})
        return JSONResponse({
            "provider": "GMGN", "ok": True, "latency_ms": 1, "error_code": None,
            "summary": {"trenches_count": len(trenches) if trenches else 0, "mode": mode.value}
        })
    except Exception as e:
        return JSONResponse({"provider": "GMGN", "ok": False, "error_code": "GMGN_ERROR", "summary": str(e)[:200]}, status_code=500)


@router.post("/jupiter/quote-test")
async def test_jupiter_quote(request: Request):
    """Test Jupiter quote endpoint (mock compatible)."""
    try:
        mode = settings.get_provider_mode()
        from ..providers.jupiter_real import JupiterProvider
        repo = request.app.state.repo
        jupiter = JupiterProvider(repo, mode=mode)
        quote = await jupiter.quote_exact_in(
            "So11111111111111111111111111111111111111112",
            "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            1000000, 1500
        )
        return JSONResponse({
            "provider": "Jupiter", "ok": True, "latency_ms": 1, "error_code": None,
            "summary": {"has_out_amount": "outAmount" in quote, "mode": mode.value}
        })
    except Exception as e:
        return JSONResponse({"provider": "Jupiter", "ok": False, "error": str(e)[:200]}, status_code=500)


@router.post("/jito/tip-test")
async def test_jito_tip(request: Request):
    """Test Jito tip floor endpoint."""
    try:
        mode = settings.get_provider_mode()
        if not settings.JITO_ENABLED:
            return JSONResponse({"provider": "Jito", "ok": False, "error_code": "JITO_DISABLED"})
        from ..providers.jito_real import JitoProvider
        repo = request.app.state.repo
        jito = JitoProvider(repo, mode=mode)
        tip = await jito.get_tip_floor()
        return JSONResponse({
            "provider": "Jito", "ok": True, "latency_ms": 1,
            "summary": {"has_50th": "landed_tips_50th_percentile" in tip}
        })
    except Exception as e:
        return JSONResponse({"provider": "Jito", "ok": False, "error": str(e)[:200]}, status_code=500)
