from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import time
from typing import Any, Literal

import httpx

from ...collector.constants import USDC_MINT
from ...config import Settings, get_settings
from ...services.platform_configuration import read_provider_base_url, read_provider_credentials

RouteProbeState = Literal["quoted", "no_route", "unavailable", "disabled"]


@dataclass(frozen=True, slots=True)
class RouteProbeResult:
    state: RouteProbeState
    source: str
    out_amount_raw: int | None = None
    price_impact_pct: float | None = None
    route_count: int = 0
    router: str | None = None
    mode: str | None = None
    error_kind: str | None = None
    message: str | None = None
    latency_ms: int | None = None
    quoted_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JupiterReadOnlyQuoteProbe:
    """Read-only Swap V2 quote; omitting taker prevents transaction assembly."""

    SOURCE = "jupiter_swap_v2_order"

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._key_index = 0
        self._transport = transport

    async def quote_sell(
        self,
        *,
        token_address: str,
        amount_raw: int,
        slippage_bps: int,
    ) -> RouteProbeResult:
        # ``slippage_bps`` stays in the protocol for callers/tests, but Swap V2
        # is deliberately invoked without optional routing modifiers. Jupiter
        # then runs in ultra mode with all routers eligible and chooses slippage
        # automatically. Most importantly, no taker is ever supplied here.
        _ = slippage_bps
        if not self.settings.paper_read_only_quote_enabled:
            return RouteProbeResult("disabled", self.SOURCE)
        if amount_raw <= 0:
            return RouteProbeResult(
                "unavailable",
                self.SOURCE,
                error_kind="invalid_amount",
                message="token amount is not positive",
            )
        keys = (
            read_provider_credentials("jupiter") or self.settings.jupiter_api_keys
            if self.settings.app_env != "test"
            else self.settings.jupiter_api_keys
        )
        if not keys:
            return RouteProbeResult(
                "unavailable",
                self.SOURCE,
                error_kind="missing_api_key",
                message="no Jupiter API key is configured",
            )

        params = {
            "inputMint": token_address,
            "outputMint": USDC_MINT,
            "amount": str(int(amount_raw)),
        }
        last_error: RouteProbeResult | None = None
        request_started = time.monotonic()
        start_index = self._key_index % len(keys)
        self._key_index = (start_index + 1) % len(keys)

        def timing_fields() -> dict[str, Any]:
            return {
                "latency_ms": max(0, int(round((time.monotonic() - request_started) * 1000))),
                "quoted_at": datetime.now(timezone.utc).isoformat(),
            }

        async with httpx.AsyncClient(
            timeout=self.settings.paper_quote_timeout_seconds,
            transport=self._transport,
        ) as client:
            for offset in range(len(keys)):
                key = keys[(start_index + offset) % len(keys)]
                try:
                    response = await client.get(
                        (
                            read_provider_base_url("jupiter") or self.settings.paper_jupiter_quote_url
                            if self.settings.app_env != "test"
                            else self.settings.paper_jupiter_quote_url
                        ),
                        params=params,
                        headers={"x-api-key": key},
                    )
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = RouteProbeResult(
                        "unavailable",
                        self.SOURCE,
                        error_kind="network",
                        message=f"{type(exc).__name__}: {exc}"[:240],
                        **timing_fields(),
                    )
                    continue

                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                message = str(
                    payload.get("error")
                    or payload.get("message")
                    or payload.get("errorMessage")
                    or ""
                )
                lowered = message.lower()
                explicit_no_route = any(
                    marker in lowered
                    for marker in (
                        "could not find any route",
                        "no route",
                        "route not found",
                        "not tradable",
                    )
                )
                if explicit_no_route:
                    return RouteProbeResult(
                        "no_route",
                        self.SOURCE,
                        router=str(payload.get("router") or "") or None,
                        mode=str(payload.get("mode") or "") or None,
                        error_kind="no_route",
                        message=message[:240] or "Jupiter returned no route",
                        **timing_fields(),
                    )
                if response.status_code == 429:
                    last_error = RouteProbeResult(
                        "unavailable",
                        self.SOURCE,
                        error_kind="rate_limit",
                        message=message[:240] or "Jupiter rate limit",
                        **timing_fields(),
                    )
                    continue
                if response.status_code >= 400:
                    last_error = RouteProbeResult(
                        "unavailable",
                        self.SOURCE,
                        error_kind="api",
                        message=(message or f"HTTP {response.status_code}")[:240],
                        **timing_fields(),
                    )
                    continue

                try:
                    out_amount_raw = int(str(payload.get("outAmount")))
                except (TypeError, ValueError):
                    out_amount_raw = 0
                if out_amount_raw <= 0:
                    # A malformed/partial successful response is not sufficient
                    # evidence that the token is a rug. Only an explicit route
                    # failure may become NO_ROUTE.
                    last_error = RouteProbeResult(
                        "unavailable",
                        self.SOURCE,
                        router=str(payload.get("router") or "") or None,
                        mode=str(payload.get("mode") or "") or None,
                        error_kind="invalid_quote",
                        message=message[:240] or "Jupiter order returned no positive outAmount",
                        **timing_fields(),
                    )
                    continue

                route_plan = payload.get("routePlan")
                route_count = len(route_plan) if isinstance(route_plan, list) else 0
                try:
                    # Swap V2's canonical ``priceImpact`` is percentage points.
                    # Keep the legacy field semantics as a decimal ratio.
                    impact = float(payload.get("priceImpact")) / 100.0
                except (TypeError, ValueError):
                    try:
                        impact = float(payload.get("priceImpactPct"))
                    except (TypeError, ValueError):
                        impact = None
                return RouteProbeResult(
                    "quoted",
                    self.SOURCE,
                    out_amount_raw=out_amount_raw,
                    price_impact_pct=impact,
                    route_count=route_count,
                    router=str(payload.get("router") or "") or None,
                    mode=str(payload.get("mode") or "") or None,
                    **timing_fields(),
                )

        return last_error or RouteProbeResult(
            "unavailable",
            self.SOURCE,
            error_kind="unknown",
            message="Jupiter Swap V2 order quote did not return a result",
            **timing_fields(),
        )
