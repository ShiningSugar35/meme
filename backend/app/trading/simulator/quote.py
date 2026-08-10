from __future__ import annotations

from dataclasses import dataclass
import math
import random

from .types import (
    ExecutionQuote,
    FailureCategory,
    QuoteRequest,
    Side,
)


@dataclass(frozen=True)
class QuoteModelConfig:
    base_slippage_bps: float = 20.0
    impact_coefficient_bps: float = 250.0
    slippage_noise_bps: float = 8.0
    # GMGN's published product fee is 1% per transaction. Model training still
    # follows the user's fee-free label utility; execution simulation does not.
    platform_fee_rate: float = 0.01
    network_fee_sol: float = 0.0002
    min_latency_ms: int = 150
    max_latency_ms: int = 900
    failure_probability: float = 0.0


class SimulatedQuoteProvider:
    """Seeded local quote/fill model; it never performs network activity."""

    def __init__(
        self,
        config: QuoteModelConfig | None = None,
        *,
        seed: int = 42,
    ) -> None:
        self.config = config or QuoteModelConfig()
        self._random = random.Random(seed)

    def quote(self, request: QuoteRequest) -> ExecutionQuote:
        if request.amount_usd <= 0 or request.reference_price <= 0:
            return ExecutionQuote(
                success=False,
                fill_price=None,
                gross_usd=0.0,
                fee_usd=0.0,
                network_fee_sol=0.0,
                slippage_bps=0.0,
                latency_ms=0,
                failure_category=FailureCategory.API,
                message="amount and reference price must be positive",
            )
        if request.liquidity_usd <= 0:
            return ExecutionQuote(
                success=False,
                fill_price=None,
                gross_usd=0.0,
                fee_usd=0.0,
                network_fee_sol=0.0,
                slippage_bps=0.0,
                latency_ms=0,
                failure_category=FailureCategory.NO_ROUTE,
                message="no executable route without positive liquidity",
            )

        latency = self._random.randint(
            self.config.min_latency_ms, self.config.max_latency_ms
        )
        if self._random.random() < self.config.failure_probability:
            failure = self._random.choice(
                [
                    FailureCategory.NETWORK,
                    FailureCategory.API,
                    FailureCategory.RATE_LIMIT,
                    FailureCategory.NO_ROUTE,
                    FailureCategory.CHAIN_REJECTED,
                ]
            )
            charged = (
                self.config.network_fee_sol
                if failure == FailureCategory.CHAIN_REJECTED
                else 0.0
            )
            return ExecutionQuote(
                success=False,
                fill_price=None,
                gross_usd=0.0,
                fee_usd=0.0,
                network_fee_sol=charged,
                slippage_bps=0.0,
                latency_ms=latency,
                failure_category=failure,
                message="simulated execution failure",
            )

        participation = request.amount_usd / request.liquidity_usd
        impact = self.config.impact_coefficient_bps * math.sqrt(max(participation, 0.0))
        noise = self._random.uniform(
            -self.config.slippage_noise_bps, self.config.slippage_noise_bps
        )
        slippage_bps = max(0.0, self.config.base_slippage_bps + impact + noise)
        slippage_fraction = slippage_bps / 10_000.0
        if request.side == Side.BUY:
            fill_price = request.reference_price * (1.0 + slippage_fraction)
            gross_usd = request.amount_usd
        else:
            fill_price = request.reference_price * (1.0 - slippage_fraction)
            gross_usd = request.amount_usd * (1.0 - slippage_fraction)

        return ExecutionQuote(
            success=True,
            fill_price=fill_price,
            gross_usd=gross_usd,
            fee_usd=request.amount_usd * self.config.platform_fee_rate,
            network_fee_sol=self.config.network_fee_sol,
            slippage_bps=slippage_bps,
            latency_ms=latency,
        )
