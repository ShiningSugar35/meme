from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import httpx

from ...collector.constants import USDC_MINT
from ...config import Settings, get_settings

RouteProbeState = Literal["quoted", "no_route", "unavailable", "disabled"]


@dataclass(frozen=True, slots=True)
class RouteProbeResult:
    state: RouteProbeState
    source: str
    out_amount_raw: int | None = None
    price_impact_pct: float | None = None
    route_count: int = 0
    error_kind: str | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class JupiterReadOnlyQuoteProbe:
    """Read-only Solana sell-route verification; never builds or submits swaps."""

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
        if not self.settings.paper_read_only_quote_enabled:
            return RouteProbeResult("disabled", "jupiter_quote")
        if amount_raw <= 0:
            return RouteProbeResult(
                "unavailable",
                "jupiter_quote",
                error_kind="invalid_amount",
                message="token amount is not positive",
            )
        keys = self.settings.jupiter_api_keys
        if not keys:
            return RouteProbeResult(
                "unavailable",
                "jupiter_quote",
                error_kind="missing_api_key",
                message="no Jupiter API key is configured",
            )

        url = self.settings.jupiter_api_base_url.rstrip("/") + "/quote"
        params = {
            "inputMint": token_address,
            "outputMint": USDC_MINT,
            "amount": str(int(amount_raw)),
            "slippageBps": str(max(1, int(slippage_bps))),
            "restrictIntermediateTokens": "true",
        }
        last_error: RouteProbeResult | None = None
        async with httpx.AsyncClient(
            timeout=self.settings.paper_quote_timeout_seconds,
            transport=self._transport,
        ) as client:
            for offset in range(len(keys)):
                key = keys[(self._key_index + offset) % len(keys)]
                try:
                    response = await client.get(url, params=params, headers={"x-api-key": key})
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    last_error = RouteProbeResult(
                        "unavailable",
                        "jupiter_quote",
                        error_kind="network",
                        message=f"{type(exc).__name__}: {exc}"[:240],
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
                    self._key_index = (self._key_index + offset + 1) % len(keys)
                    return RouteProbeResult(
                        "no_route",
                        "jupiter_quote",
                        error_kind="no_route",
                        message=message[:240] or "Jupiter returned no route",
                    )
                if response.status_code == 429:
                    last_error = RouteProbeResult(
                        "unavailable",
                        "jupiter_quote",
                        error_kind="rate_limit",
                        message=message[:240] or "Jupiter rate limit",
                    )
                    continue
                if response.status_code >= 400:
                    last_error = RouteProbeResult(
                        "unavailable",
                        "jupiter_quote",
                        error_kind="api",
                        message=(message or f"HTTP {response.status_code}")[:240],
                    )
                    continue

                out_raw = payload.get("outAmount")
                route_plan = payload.get("routePlan")
                try:
                    out_amount_raw = int(str(out_raw))
                except (TypeError, ValueError):
                    out_amount_raw = 0
                if out_amount_raw <= 0 or not isinstance(route_plan, list) or not route_plan:
                    return RouteProbeResult(
                        "no_route",
                        "jupiter_quote",
                        error_kind="empty_route",
                        message="Jupiter returned no executable route plan",
                    )
                try:
                    impact = float(payload.get("priceImpactPct"))
                except (TypeError, ValueError):
                    impact = None
                self._key_index = (self._key_index + offset + 1) % len(keys)
                return RouteProbeResult(
                    "quoted",
                    "jupiter_quote",
                    out_amount_raw=out_amount_raw,
                    price_impact_pct=impact,
                    route_count=len(route_plan),
                )

        return last_error or RouteProbeResult(
            "unavailable",
            "jupiter_quote",
            error_kind="unknown",
            message="Jupiter quote probe did not return a result",
        )
