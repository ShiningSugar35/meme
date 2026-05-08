"""
Provider-related API routes

Includes endpoints for:
- POST /api/providers/dry-run-check: Check DRY_RUN status of all providers
- POST /api/providers/online-readonly-check: Test online_readonly mode connections
- GET /api/providers/status: Get provider status (GET version)
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from typing import Dict, Any, List
from ..config import settings, ProviderMode
from ..logging_config import logger

router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.post("/dry-run-check")
async def dry_run_check() -> JSONResponse:
    """
    Check DRY_RUN status of the system and all providers.
    
    Returns:
        - provider_mode: Current provider mode (mock/online_readonly/live)
        - system_dry_run: Global DRY_RUN setting from config (legacy)
        - live_trading_enabled: Global LIVE_TRADING_ENABLED setting
        - providers: List of providers and their DRY_RUN status
        - warnings: List of any configuration warnings
        
    This endpoint is safe to call in any mode (read-only check).
    """
    try:
        result = {
            "provider_mode": settings.get_provider_mode().value,
            "system_dry_run": settings.DRY_RUN,
            "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
            "providers": [],
            "warnings": []
        }
        
        # Check system-level configuration
        if settings.LIVE_TRADING_ENABLED and settings.DRY_RUN:
            result["warnings"].append(
                "Configuration conflict: LIVE_TRADING_ENABLED=true but DRY_RUN=true. "
                "DRY_RUN takes precedence - no live trading will occur."
            )
        
        if settings.LIVE_TRADING_ENABLED and not settings.DRY_RUN:
            result["warnings"].append(
                "WARNING: Live trading is ENABLED. Real transactions may be broadcast if executed."
            )
        
        # Get provider mode info
        mode = settings.get_provider_mode()
        
        # Provider-level checks (based on what's in app.state.providers)
        # Note: This is a simplified check. In real implementation, we would
        # inspect the actual provider instances.
        provider_list = [
            {"name": "GMGN", "type": "market_data", "mode": mode.value},
            {"name": "Jupiter", "type": "swap", "mode": mode.value},
            {"name": "Jito", "type": "execution", "mode": mode.value, "critical": True},
            {"name": "RPC", "type": "rpc", "mode": mode.value},
        ]
        
        # Add special warning for Jito in live mode
        for p in provider_list:
            if p["name"] == "Jito" and p["mode"] == "live":
                p["warning"] = "Jito send() will broadcast REAL transactions when mode=live"
        
        result["providers"] = provider_list
        
        # Overall status
        if mode == ProviderMode.MOCK:
            result["overall_status"] = "MOCK - All providers using mock data"
        elif mode == ProviderMode.ONLINE_READONLY:
            result["overall_status"] = "ONLINE_READONLY - Providers may call real APIs (read-only)"
        elif mode == ProviderMode.LIVE:
            result["overall_status"] = "LIVE - Providers may broadcast real transactions"
        
        return JSONResponse(result)
        
    except Exception as e:
        logger.exception("Error in dry-run-check endpoint")
        return JSONResponse(
            {"error": str(e), "overall_status": "ERROR"},
            status_code=500
        )


@router.post("/online-readonly-check")
async def online_readonly_check() -> JSONResponse:
    """
    Test online_readonly mode connections.
    
    Only available when PROVIDER_MODE=online_readonly or LIVE_TRADING_ENABLED=true.
    
    Executes:
    - GMGN: sample trenches/latest/kline (if API key available)
    - Jupiter: sample quote schema check
    - Jito: tip/status read-only check (if configured)
    - RPC: getLatestBlockhash
    
    Any failure returns structured result, don't let entire request 500.
    Don't allow send, don't sign, don't broadcast.
    """
    try:
        mode = settings.get_provider_mode()
        
        # Only allow in online_readonly or live mode
        if mode not in [ProviderMode.ONLINE_READONLY, ProviderMode.LIVE]:
            return JSONResponse({
                "status": "skipped",
                "message": f"online-readonly-check only available in online_readonly/live mode. Current: {mode.value}",
                "checks": []
            })
        
        result = {
            "status": "running",
            "mode": mode.value,
            "checks": []
        }
        
        # Check GMGN
        gmgn_check = {"provider": "GMGN", "status": "unknown", "latency_ms": 0, "error": None}
        try:
            from ..providers.gmgn_real import GMGNProvider
            from ..db.repositories import Repositories
            import time
            
            # Create temporary provider for testing
            repo = await Repositories.create()
            gmgn = GMGNProvider(repo, mode=mode)
            
            # Test fetch_latest_price
            start = time.time()
            price = await gmgn.fetch_latest_price("PASS1")  # Use mock token for testing
            gmgn_check["latency_ms"] = int((time.time() - start) * 1000)
            gmgn_check["status"] = "ok"
            gmgn_check["response"] = {"has_price": bool(price.get('price'))}
            
            await repo.close()
        except Exception as e:
            gmgn_check["status"] = "error"
            gmgn_check["error"] = str(e)
        result["checks"].append(gmgn_check)
        
        # Check Jupiter
        jupiter_check = {"provider": "Jupiter", "status": "unknown", "latency_ms": 0, "error": None}
        try:
            from ..providers.jupiter_real import JupiterProvider
            from ..db.repositories import Repositories
            import time
            
            repo = await Repositories.create()
            jupiter = JupiterProvider(repo, mode=mode)
            
            # Test quote (read-only, no broadcast)
            start = time.time()
            quote = await jupiter.quote_exact_in("SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 1000000, 1500)
            jupiter_check["latency_ms"] = int((time.time() - start) * 1000)
            
            if quote.get('error') == 'HIGH_PRICE_IMPACT':
                jupiter_check["status"] = "ok_with_warning"
                jupiter_check["warning"] = "Price impact too high (expected for test token)"
            else:
                jupiter_check["status"] = "ok"
                jupiter_check["response"] = {
                    "has_quote": bool(quote.get('outAmount')),
                    "price_impact": quote.get('priceImpactPct')
                }
            
            await repo.close()
        except Exception as e:
            jupiter_check["status"] = "error"
            jupiter_check["error"] = str(e)
        result["checks"].append(jupiter_check)
        
        # Check Jito (if configured)
        jito_check = {"provider": "Jito", "status": "skipped", "latency_ms": 0, "error": None}
        if settings.JITO_ENABLED:
            try:
                from ..providers.jito_real import JitoProvider
                from ..db.repositories import Repositories
                import time
                
                repo = await Repositories.create()
                jito = JitoProvider(repo, mode=mode)
                
                # Test get_tip_floor (read-only)
                start = time.time()
                tip = await jito.get_tip_floor()
                jito_check["latency_ms"] = int((time.time() - start) * 1000)
                jito_check["status"] = "ok"
                jito_check["response"] = {"has_tip_data": bool(tip.get('landed_tips_50th_percentile'))}
                
                await repo.close()
            except Exception as e:
                jito_check["status"] = "error"
                jito_check["error"] = str(e)
        result["checks"].append(jito_check)
        
        # Check RPC
        rpc_check = {"provider": "RPC", "status": "unknown", "latency_ms": 0, "error": None}
        try:
            from ..providers.rpc_real import RpcProvider
            from ..db.repositories import Repositories
            import time
            
            repo = await Repositories.create()
            rpc = RpcProvider(repo, mode=mode)
            
            # Test get_balance (read-only)
            start = time.time()
            balance = await rpc.get_balance("11111111111111111111111111111111")  # System program ID
            rpc_check["latency_ms"] = int((time.time() - start) * 1000)
            rpc_check["status"] = "ok"
            rpc_check["response"] = {"has_balance": True}
            
            await repo.close()
        except Exception as e:
            rpc_check["status"] = "error"
            rpc_check["error"] = str(e)
        result["checks"].append(rpc_check)
        
        # Overall status
        all_ok = all(c.get("status") in ["ok", "ok_with_warning", "skipped"] for c in result["checks"])
        result["status"] = "completed" if all_ok else "completed_with_errors"
        
        return JSONResponse(result)
        
    except Exception as e:
        logger.exception("Error in online-readonly-check endpoint")
        return JSONResponse(
            {"status": "error", "error": str(e)},
            status_code=500
        )


@router.get("/status")
async def provider_status() -> JSONResponse:
    """
    Get provider status (GET version for easy browser check)
    
    Returns the same as POST /dry-run-check but as GET request.
    """
    # Reuse the POST endpoint logic
    return await dry_run_check()
