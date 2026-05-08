"""

Provider-related API routes

Includes endpoints for:
- GET /api/providers/health: Provider health check
- POST /api/providers/gmgn/test: Test GMGN connection
- POST /api/providers/jupiter/quote-test: Test Jupiter quote
- POST /api/providers/rpc/balance-test: Test RPC balance
- POST /api/providers/jito/tip-test: Test Jito tip floor
- POST /api/providers/dry-run-check: Check DRY_RUN status of all providers
- POST /api/providers/online-readonly-check: Test online_readonly mode connections
- GET /api/providers/status: Get provider status (GET version)
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from ..config import settings, ProviderMode
from ..logging_config import logger
router = APIRouter(prefix="/api/providers", tags=["providers"])


# Request models
class QuoteTestRequest(BaseModel):
    input_mint: str = "So11111111111111111111111111111111111111112"  # SOL
    output_mint: str = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    amount_lamports: int = 1000000  # 0.001 SOL
    slippage_bps: int = 1500


class BalanceTestRequest(BaseModel):
    wallet: str = "11111111111111111111111111111111"  # System program


class TrenchesTestRequest(BaseModel):
    """Test request for GMGN trenches (trending tokens)"""
    pass


class TokenSnapshotTestRequest(BaseModel):
    """Test request for GMGN token snapshot/price data"""
    token_mint: str = "So11111111111111111111111111111111111111112"  # SOL (default)


class KlineTestRequest(BaseModel):
    """Test request for GMGN kline (candlestick) data"""
    token_mint: str = "So11111111111111111111111111111111111111112"  # SOL (default)
    interval: str = "1m"  # Candlestick interval
    limit: int = 10


class LatestPriceTestRequest(BaseModel):
    """Test request for GMGN latest price"""
    token_mint: str = "So11111111111111111111111111111111111111112"  # SOL (default)


# Helper to check if real mode has required config
def _check_real_mode_config() -> Optional[Dict[str, Any]]:
    """Return error dict if real mode lacks required config, else None."""
    if settings.get_provider_mode() == ProviderMode.MOCK:
        return None  # Mock mode is always ok
    
    missing = []
    if settings.get_provider_mode() == ProviderMode.LIVE:
        if not settings.get_gmgn_api_key():
            missing.append('GMGN_API_KEY_1')
        if not settings.get_jupiter_api_key():
            missing.append('JUPITER_API_KEY_1')
        if not settings.JITO_ENABLED:
            missing.append('JITO_ENABLED')
        if not settings.WALLET_PRIVATE_KEY_BASE58:
            missing.append('WALLET_PRIVATE_KEY_BASE58')
    
    if missing:
        return {
            'ok': False,
            'error_code': 'MISSING_CONFIG',
            'error': f"Real mode missing config: {missing}. Set MOCK mode or add missing keys.",
            'missing': missing
        }
    return None


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


@router.get("/health")
async def provider_health() -> JSONResponse:
    """
    Provider health check.
    Returns status of each provider without exposing keys.
    """
    try:
        mode = settings.get_provider_mode()
        result = {
            "provider_mode": mode.value,
            "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
            "providers": []
        }
        
        # GMGN health
        gmgn_health = {"provider": "GMGN", "ok": False, "latency_ms": 0, "error_code": None, "summary": None}
        try:
            from ..providers.gmgn_real import GMGNProvider
            from ..db.repositories import Repositories
            import time
            repo = await Repositories.create()
            gmgn = GMGNProvider(repo, mode=mode)
            start = time.time()
            # Test with mock token in mock mode, skip in real if no key
            if mode == ProviderMode.MOCK:
                price = await gmgn.fetch_latest_price("PASS1")
                gmgn_health["ok"] = bool(price)
                gmgn_health["summary"] = {"has_price": bool(price)}
            else:
                config_check = _check_real_mode_config()
                if config_check:
                    gmgn_health["error_code"] = config_check["error_code"]
                    gmgn_health["summary"] = config_check["error"]
                else:
                    # TODO: Real health check with API key
                    gmgn_health["ok"] = True
                    gmgn_health["summary"] = {"mode": "real", "status": "configured"}
            gmgn_health["latency_ms"] = int((time.time() - start) * 1000)
            await repo.close()
        except Exception as e:
            gmgn_health["error_code"] = "GMGN_ERROR"
            gmgn_health["summary"] = str(e)[:200]  # Truncate to avoid leaking info
        result["providers"].append(gmgn_health)
        
        # Jupiter health
        jupiter_health = {"provider": "Jupiter", "ok": False, "latency_ms": 0, "error_code": None, "summary": None}
        try:
            from ..providers.jupiter_real import JupiterProvider
            from ..db.repositories import Repositories
            import time
            repo = await Repositories.create()
            jupiter = JupiterProvider(repo, mode=mode)
            start = time.time()
            if mode == ProviderMode.MOCK:
                quote = await jupiter.quote_exact_in("SOL", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", 1000000, 1500)
                jupiter_health["ok"] = "error" not in quote
                jupiter_health["summary"] = {"has_quote": "outAmount" in quote}
            else:
                config_check = _check_real_mode_config()
                if config_check:
                    jupiter_health["error_code"] = config_check["error_code"]
                    jupiter_health["summary"] = config_check["error"]
                else:
                    jupiter_health["ok"] = True
                    jupiter_health["summary"] = {"mode": "real", "status": "configured"}
            jupiter_health["latency_ms"] = int((time.time() - start) * 1000)
            await repo.close()
        except Exception as e:
            jupiter_health["error_code"] = "JUPITER_ERROR"
            jupiter_health["summary"] = str(e)[:200]
        result["providers"].append(jupiter_health)
        
        # Jito health
        jito_health = {"provider": "Jito", "ok": False, "latency_ms": 0, "error_code": None, "summary": None}
        if settings.JITO_ENABLED:
            try:
                from ..providers.jito_real import JitoProvider
                from ..db.repositories import Repositories
                import time
                repo = await Repositories.create()
                jito = JitoProvider(repo, mode=mode)
                start = time.time()
                if mode == ProviderMode.MOCK:
                    tip = await jito.get_tip_floor()
                    jito_health["ok"] = bool(tip.get("landed_tips_50th_percentile"))
                    jito_health["summary"] = {"has_tip": True}
                else:
                    config_check = _check_real_mode_config()
                    if config_check:
                        jito_health["error_code"] = config_check["error_code"]
                        jito_health["summary"] = config_check["error"]
                    else:
                        jito_health["ok"] = True
                        jito_health["summary"] = {"mode": "real", "status": "configured"}
                jito_health["latency_ms"] = int((time.time() - start) * 1000)
                await repo.close()
            except Exception as e:
                jito_health["error_code"] = "JITO_ERROR"
                jito_health["summary"] = str(e)[:200]
        else:
            jito_health["summary"] = "Jito disabled"
            jito_health["ok"] = True
        result["providers"].append(jito_health)
        
        # RPC health
        rpc_health = {"provider": "RPC", "ok": False, "latency_ms": 0, "error_code": None, "summary": None}
        try:
            from ..providers.rpc_real import RpcRealProvider
            from ..db.repositories import Repositories
            import time
            repo = await Repositories.create()
            rpc = RpcRealProvider(repo, mode=mode)
            start = time.time()
            if mode == ProviderMode.MOCK:
                balance = await rpc.get_balance("11111111111111111111111111111111")
                rpc_health["ok"] = "sol_balance" in balance
                rpc_health["summary"] = {"has_balance": True}
            else:
                config_check = _check_real_mode_config()
                if config_check:
                    rpc_health["error_code"] = config_check["error_code"]
                    rpc_health["summary"] = config_check["error"]
                else:
                    rpc_health["ok"] = True
                    rpc_health["summary"] = {"mode": "real", "status": "configured"}
            rpc_health["latency_ms"] = int((time.time() - start) * 1000)
            await repo.close()
        except Exception as e:
            rpc_health["error_code"] = "RPC_ERROR"
            rpc_health["summary"] = str(e)[:200]
        result["providers"].append(rpc_health)
        
        # Overall health
        all_ok = all(p["ok"] for p in result["providers"])
        result["overall_health"] = "healthy" if all_ok else "degraded"
        return JSONResponse(result)
    except Exception as e:
        logger.exception("Error in provider health endpoint")
        return JSONResponse({"error": str(e), "overall_health": "error"}, status_code=500)


@router.post("/gmgn/test")
async def test_gmgn() -> JSONResponse:
    """Test GMGN connection. No keys exposed."""
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.gmgn_real import GMGNProvider
        from ..db.repositories import Repositories
        import time
        
        repo = await Repositories.create()
        gmgn = GMGNProvider(repo, mode=mode)
        start = time.time()
        
        # Test trenches
        trenches = await gmgn.fetch_trenches({})
        # Test price
        price = await gmgn.fetch_latest_price("PASS1" if mode == ProviderMode.MOCK else "So11111111111111111111111111111111111112")
        
        latency_ms = int((time.time() - start) * 1000)
        await repo.close()
        
        return JSONResponse({
            "provider": "GMGN",
            "ok": True,
            "latency_ms": latency_ms,
            "error_code": None,
            "summary": {
                "trenches_count": len(trenches),
                "has_price": bool(price),
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "GMGN",
            "ok": False,
            "latency_ms": 0,
            "error_code": "GMGN_TEST_ERROR",
            "summary": str(e)[:200]
        }, status_code=500)


@router.post("/jupiter/quote-test")
async def test_jupiter_quote(request: QuoteTestRequest) -> JSONResponse:
    """Test Jupiter quote. No keys exposed."""
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.jupiter_real import JupiterProvider
        from ..db.repositories import Repositories
        import time
        
        repo = await Repositories.create()
        jupiter = JupiterProvider(repo, mode=mode)
        start = time.time()
        
        quote = await jupiter.quote_exact_in(
            request.input_mint, request.output_mint, 
            request.amount_lamports, request.slippage_bps
        )
        
        latency_ms = int((time.time() - start) * 1000)
        await repo.close()
        
        if quote.get("error") == "HIGH_PRICE_IMPACT":
            return JSONResponse({
                "provider": "Jupiter",
                "ok": True,
                "latency_ms": latency_ms,
                "error_code": "HIGH_PRICE_IMPACT",
                "summary": {"impact": quote.get("priceImpactPct"), "note": "Quote valid but high impact"}
            })
        
        return JSONResponse({
            "provider": "Jupiter",
            "ok": True,
            "latency_ms": latency_ms,
            "error_code": None,
            "summary": {
                "has_out_amount": "outAmount" in quote,
                "price_impact": quote.get("priceImpactPct"),
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "Jupiter",
            "ok": False,
            "latency_ms": 0,
            "error_code": "JUPITER_TEST_ERROR",
            "summary": str(e)[:200]
        }, status_code=500)


@router.post("/rpc/balance-test")
async def test_rpc_balance(request: BalanceTestRequest) -> JSONResponse:
    """Test RPC balance. No keys exposed."""
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.rpc_real import RpcRealProvider
        from ..db.repositories import Repositories
        import time
        
        repo = await Repositories.create()
        rpc = RpcRealProvider(repo, mode=mode)
        start = time.time()
        
        balance = await rpc.get_balance(request.wallet)
        
        latency_ms = int((time.time() - start) * 1000)
        await repo.close()
        
        return JSONResponse({
            "provider": "RPC",
            "ok": True,
            "latency_ms": latency_ms,
            "error_code": None,
            "summary": {
                "has_balance": "sol_balance" in balance,
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "RPC",
            "ok": False,
            "latency_ms": 0,
            "error_code": "RPC_TEST_ERROR",
            "summary": str(e)[:200]
        }, status_code=500)


@router.post("/jito/tip-test")
async def test_jito_tip() -> JSONResponse:
    """Test Jito tip floor. No keys exposed."""
    try:
        mode = settings.get_provider_mode()
        if not settings.JITO_ENABLED:
            return JSONResponse({
                "provider": "Jito",
                "ok": False,
                "latency_ms": 0,
                "error_code": "JITO_DISABLED",
                "summary": "Jito is disabled in config"
            })
        
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.jito_real import JitoProvider
        from ..db.repositories import Repositories
        import time
        
        repo = await Repositories.create()
        jito = JitoProvider(repo, mode=mode)
        start = time.time()
        
        tip = await jito.get_tip_floor()
        
        latency_ms = int((time.time() - start) * 1000)
        await repo.close()
        
        return JSONResponse({
            "provider": "Jito",
            "ok": True,
            "latency_ms": latency_ms,
            "error_code": None,
            "summary": {
                "has_50th": "landed_tips_50th_percentile" in tip,
                "has_75th": "landed_tips_75th_percentile" in tip,
                "has_95th": "landed_tips_95th_percentile" in tip,
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "Jito",
            "ok": False,
            "latency_ms": 0,
            "error_code": "JITO_TEST_ERROR",
            "summary": str(e)[:200]
        }, status_code=500)


@router.post("/gmgn/trenches-test")
async def test_gmgn_trenches(request: TrenchesTestRequest = None) -> JSONResponse:
    """
    Test GMGN trenches endpoint (fetch trending tokens).
    
    Records provider request with masked response summary.
    Works in all modes (mock/online_readonly/live).
    """
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.gmgn_real import GMGNProvider
        from ..db.repositories import Repositories
        import time
        import json
        
        repo = await Repositories.create()
        gmgn = GMGNProvider(repo, mode=mode)
        
        start = time.time()
        trenches = await gmgn.fetch_trenches({})
        latency_ms = int((time.time() - start) * 1000)
        
        # Create masked response summary
        response_summary = {
            'count': len(trenches) if trenches else 0,
        }
        
        # Log the request
        await repo.append_provider_request(
            provider='GMGN',
            endpoint='/gmgn/trenches-test',
            method='POST',
            status_code=200,
            latency_ms=latency_ms,
            ok=True,
            error_code=None,
            error_summary=None,
            request_summary_json=json.dumps({}),
            response_summary_json=json.dumps(response_summary)
        )
        
        await repo.close()
        
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/trenches-test",
            "ok": True,
            "latency_ms": latency_ms,
            "status_code": 200,
            "error_code": None,
            "summary": {
                "trenches_count": len(trenches) if trenches else 0,
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/trenches-test",
            "ok": False,
            "latency_ms": 0,
            "status_code": 500,
            "error_code": "GMGN_TRENCHES_TEST_ERROR",
            "error": str(e)[:200]
        }, status_code=500)


@router.post("/gmgn/token-snapshot-test")
async def test_gmgn_token_snapshot(request: TokenSnapshotTestRequest) -> JSONResponse:
    """
    Test GMGN token snapshot endpoint (fetch token price/metrics snapshot).
    
    Records provider request with masked response summary.
    Works in all modes (mock/online_readonly/live).
    """
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.gmgn_real import GMGNProvider
        from ..db.repositories import Repositories
        import time
        import json
        
        repo = await Repositories.create()
        gmgn = GMGNProvider(repo, mode=mode)
        
        start = time.time()
        snapshot = await gmgn.fetch_token_snapshot(request.token_mint)
        latency_ms = int((time.time() - start) * 1000)
        
        # Create masked response summary
        response_summary = {
            'has_data': bool(snapshot),
            'has_price': 'price' in snapshot if snapshot else False,
            'has_liquidity': 'liquidity_usd' in snapshot if snapshot else False
        }
        
        # Log the request
        await repo.append_provider_request(
            provider='GMGN',
            endpoint='/gmgn/token-snapshot-test',
            method='POST',
            status_code=200,
            latency_ms=latency_ms,
            ok=bool(snapshot),
            error_code=None,
            error_summary=None,
            request_summary_json=json.dumps({'token_mint': request.token_mint}),
            response_summary_json=json.dumps(response_summary)
        )
        
        await repo.close()
        
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/token-snapshot-test",
            "ok": bool(snapshot),
            "latency_ms": latency_ms,
            "status_code": 200,
            "error_code": None,
            "summary": {
                "token_mint": request.token_mint,
                "has_snapshot": bool(snapshot),
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/token-snapshot-test",
            "ok": False,
            "latency_ms": 0,
            "status_code": 500,
            "error_code": "GMGN_TOKEN_SNAPSHOT_TEST_ERROR",
            "error": str(e)[:200]
        }, status_code=500)


@router.post("/gmgn/kline-test")
async def test_gmgn_kline(request: KlineTestRequest) -> JSONResponse:
    """
    Test GMGN kline (candlestick) endpoint.
    
    Records provider request with masked response summary.
    Works in all modes (mock/online_readonly/live).
    """
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.gmgn_real import GMGNProvider
        from ..db.repositories import Repositories
        import time
        import json
        
        repo = await Repositories.create()
        gmgn = GMGNProvider(repo, mode=mode)
        
        start = time.time()
        klines = await gmgn.fetch_kline(request.token_mint, request.interval, request.limit)
        latency_ms = int((time.time() - start) * 1000)
        
        # Create masked response summary
        response_summary = {
            'kline_count': len(klines) if klines else 0,
            'interval': request.interval,
            'requested_limit': request.limit
        }
        
        # Log the request
        await repo.append_provider_request(
            provider='GMGN',
            endpoint='/gmgn/kline-test',
            method='POST',
            status_code=200,
            latency_ms=latency_ms,
            ok=True,
            error_code=None,
            error_summary=None,
            request_summary_json=json.dumps({
                'token_mint': request.token_mint,
                'interval': request.interval,
                'limit': request.limit
            }),
            response_summary_json=json.dumps(response_summary)
        )
        
        await repo.close()
        
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/kline-test",
            "ok": True,
            "latency_ms": latency_ms,
            "status_code": 200,
            "error_code": None,
            "summary": {
                "token_mint": request.token_mint,
                "interval": request.interval,
                "kline_count": len(klines) if klines else 0,
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/kline-test",
            "ok": False,
            "latency_ms": 0,
            "status_code": 500,
            "error_code": "GMGN_KLINE_TEST_ERROR",
            "error": str(e)[:200]
        }, status_code=500)


@router.post("/gmgn/latest-price-test")
async def test_gmgn_latest_price(request: LatestPriceTestRequest) -> JSONResponse:
    """
    Test GMGN latest price endpoint.
    
    Records provider request with masked response summary.
    Works in all modes (mock/online_readonly/live).
    """
    try:
        mode = settings.get_provider_mode()
        config_check = _check_real_mode_config()
        if config_check and mode != ProviderMode.MOCK:
            return JSONResponse(config_check)
        
        from ..providers.gmgn_real import GMGNProvider
        from ..db.repositories import Repositories
        import time
        import json
        
        repo = await Repositories.create()
        gmgn = GMGNProvider(repo, mode=mode)
        
        start = time.time()
        price_data = await gmgn.fetch_latest_price(request.token_mint)
        latency_ms = int((time.time() - start) * 1000)
        
        # Create masked response summary
        response_summary = {
            'has_price': 'price' in price_data if price_data else False,
            'has_price_sol': 'price_sol' in price_data if price_data else False,
            'has_liquidity': 'sol_side_liquidity' in price_data if price_data else False
        }
        
        # Log the request
        await repo.append_provider_request(
            provider='GMGN',
            endpoint='/gmgn/latest-price-test',
            method='POST',
            status_code=200,
            latency_ms=latency_ms,
            ok=bool(price_data),
            error_code=None,
            error_summary=None,
            request_summary_json=json.dumps({'token_mint': request.token_mint}),
            response_summary_json=json.dumps(response_summary)
        )
        
        await repo.close()
        
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/latest-price-test",
            "ok": bool(price_data),
            "latency_ms": latency_ms,
            "status_code": 200,
            "error_code": None,
            "summary": {
                "token_mint": request.token_mint,
                "has_price": bool(price_data),
                "mode": mode.value
            }
        })
    except Exception as e:
        return JSONResponse({
            "provider": "GMGN",
            "endpoint": "/gmgn/latest-price-test",
            "ok": False,
            "latency_ms": 0,
            "status_code": 500,
            "error_code": "GMGN_LATEST_PRICE_TEST_ERROR",
            "error": str(e)[:200]
        }, status_code=500)
