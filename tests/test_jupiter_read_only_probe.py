from __future__ import annotations

import httpx
import pytest

from backend.app.config import Settings
from backend.app.trading.simulator.jupiter_probe import JupiterReadOnlyQuoteProbe


def settings(tmp_path, **overrides) -> Settings:
    values = dict(
        _env_file=None,
        app_env="test",
        sqlite_path=str(tmp_path / "unused.db"),
        jupiter_api_base_url="https://api.jup.ag/swap/v1",
        jupiter_api_key_1="secret-key",
        paper_read_only_quote_enabled=True,
    )
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_read_only_quote_returns_executable_route_without_submit(tmp_path) -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("x-api-key")
        return httpx.Response(
            200,
            json={
                "outAmount": "698078629",
                "priceImpactPct": "0.02029",
                "routePlan": [{"swapInfo": {"label": "Pump.fun Amm"}}],
            },
        )

    probe = JupiterReadOnlyQuoteProbe(settings(tmp_path), transport=httpx.MockTransport(handler))
    result = await probe.quote_sell(
        token_address="TokenMint",
        amount_raw=50_000_000,
        slippage_bps=2500,
    )

    assert result.state == "quoted"
    assert result.out_amount_raw == 698078629
    assert result.route_count == 1
    assert result.price_impact_pct == pytest.approx(0.02029)
    assert seen["method"] == "GET"
    assert seen["api_key"] == "secret-key"
    assert "secret-key" not in seen["url"]


@pytest.mark.asyncio
async def test_explicit_no_route_is_distinct_from_api_failure(tmp_path) -> None:
    no_route = JupiterReadOnlyQuoteProbe(
        settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "Could not find any route"})
        ),
    )
    no_route_result = await no_route.quote_sell(
        token_address="TokenMint", amount_raw=1_000_000, slippage_bps=2500
    )
    assert no_route_result.state == "no_route"

    unavailable = JupiterReadOnlyQuoteProbe(
        settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, json={"error": "rate limit"})
        ),
    )
    unavailable_result = await unavailable.quote_sell(
        token_address="TokenMint", amount_raw=1_000_000, slippage_bps=2500
    )
    assert unavailable_result.state == "unavailable"
    assert unavailable_result.error_kind == "rate_limit"


@pytest.mark.asyncio
async def test_missing_key_disables_network_probe_without_claiming_no_route(tmp_path) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    probe = JupiterReadOnlyQuoteProbe(
        settings(tmp_path, jupiter_api_key_1=None),
        transport=httpx.MockTransport(handler),
    )
    result = await probe.quote_sell(
        token_address="TokenMint", amount_raw=1_000_000, slippage_bps=2500
    )
    assert result.state == "unavailable"
    assert result.error_kind == "missing_api_key"
    assert not called
