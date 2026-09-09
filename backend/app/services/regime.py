from __future__ import annotations

import asyncio
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..collector.constants import FEATURE_SCHEMA_VERSION
from ..collector.dexscreener_fallback import DexScreenerFallbackProvider
from ..collector.filters import to_float
from ..database import Database, utc_now_iso
from .crypto_market import CoinbasePublicMarketProvider, PublicMarketSnapshot
from .solana_rpc import SolanaNetworkSnapshot, SolanaRpcPool

REGIME_POLICY_VERSION = "market_regime_v2_crypto_shadow"


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, float(value)))


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    id: int
    observed_at: int
    score: float
    label: str
    confidence: float
    features: Mapping[str, Any]
    source_health: Mapping[str, Any]
    reasons: tuple[str, ...]


class MarketRegimeService:
    """PIT-safe global context; token age never truncates market lookbacks."""

    WEIGHTS = {
        "crypto": 0.15,
        "sol": 0.20,
        "network": 0.15,
        "breadth": 0.15,
        "attention": 0.15,
        "strategy": 0.15,
        "execution": 0.05,
    }

    def __init__(self, database: Database) -> None:
        self.database = database

    def _price_at(
        self,
        ts: int,
        *,
        max_lag_seconds: int = 240,
    ) -> tuple[float | None, int | None]:
        row = self.database.fetch_one(
            "SELECT price_usd,observed_at FROM asset_usd_prices "
            "WHERE asset='SOL' AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
            (ts,),
        )
        if not row:
            return None, None
        observed_at = int(row.get("observed_at") or 0)
        value = to_float(row.get("price_usd"))
        if value is None or value <= 0 or ts - observed_at > max_lag_seconds:
            return None, observed_at or None
        return value, observed_at

    def _sol(self, now: int) -> dict[str, Any]:
        current, current_observed_at = self._price_at(now, max_lag_seconds=180)
        result: dict[str, Any] = {
            "sol_return_5m": None,
            "sol_return_15m": None,
            "sol_return_60m": None,
            "sol_realized_vol_15m": None,
            "sol_primary_observed_at": current_observed_at,
            "sol_primary_fresh": 1.0 if current is not None else 0.0,
        }
        if current is None:
            return result

        for minutes in (5, 15, 60):
            past, _ = self._price_at(now - minutes * 60, max_lag_seconds=240)
            result[f"sol_return_{minutes}m"] = current / past - 1.0 if past else None

        rows = self.database.fetch_all(
            "SELECT observed_at,price_usd FROM asset_usd_prices "
            "WHERE asset='SOL' AND observed_at BETWEEN ? AND ? ORDER BY observed_at",
            (now - 900, now),
        )
        prices = [
            float(row["price_usd"])
            for row in rows
            if to_float(row.get("price_usd")) not in (None, 0)
        ]
        changes = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
        result["sol_realized_vol_15m"] = statistics.pstdev(changes) if len(changes) >= 2 else None
        return result

    def _breadth(self, now: int) -> dict[str, float | None]:
        rows = self.database.fetch_all(
            "SELECT discovered,accepted,new_creation_returned,trending_returned "
            "FROM collector_cycle_snapshots WHERE observed_at>=? "
            "ORDER BY observed_at DESC LIMIT 15",
            (now - 1800,),
        )
        if not rows:
            return {
                "meme_discovery_breadth": None,
                "meme_acceptance_rate": None,
                "meme_new_creation_breadth": None,
                "meme_trending_volume_breadth": None,
            }
        discovered = sum(int(row.get("discovered") or 0) for row in rows)
        accepted = sum(int(row.get("accepted") or 0) for row in rows)
        return {
            "meme_discovery_breadth": statistics.mean(
                float(row.get("discovered") or 0) for row in rows
            ),
            "meme_acceptance_rate": accepted / discovered if discovered else None,
            "meme_new_creation_breadth": statistics.mean(
                float(row.get("new_creation_returned") or 0) for row in rows
            ),
            "meme_trending_volume_breadth": statistics.mean(
                float(row.get("trending_returned") or 0) for row in rows
            ),
        }

    @staticmethod
    def _max_drawdown(ordered_returns: list[float]) -> float | None:
        if not ordered_returns:
            return None
        equity = peak = 1.0
        maximum = 0.0
        for value in ordered_returns:
            equity *= max(1e-9, 1.0 + value)
            peak = max(peak, equity)
            maximum = max(maximum, 1.0 - equity / peak)
        return maximum

    def _strategy(self, now: int) -> dict[str, float | None]:
        rows = self.database.fetch_all(
            """
            SELECT p.sample_id,p.exit_time,p.net_pnl_usd,p.invested_usd
            FROM positions p
            JOIN samples s ON s.id=p.sample_id
            WHERE p.account_kind='simulation' AND p.status='closed' AND p.exit_time>=?
              AND p.strategy_key IN ('model_1','model_2','model_3')
              AND s.feature_schema_version=?
            ORDER BY p.exit_time,p.sample_id
            """,
            (_iso(now - 21600), FEATURE_SCHEMA_VERSION),
        )
        observations: list[tuple[int, int, float]] = []
        for row in rows:
            invested = to_float(row.get("invested_usd"))
            pnl = to_float(row.get("net_pnl_usd"))
            if not invested or invested <= 0 or pnl is None:
                continue
            try:
                ended = datetime.fromisoformat(str(row.get("exit_time") or ""))
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
                ended_at = int(ended.timestamp())
            except ValueError:
                continue
            observations.append((ended_at, int(row.get("sample_id") or 0), pnl / invested))

        probability_rows = self.database.fetch_all(
            """
            SELECT s.entry_time,p.sample_id,p.probability
            FROM predictions p
            JOIN samples s ON s.id=p.sample_id
            WHERE s.entry_time>=? AND s.feature_schema_version=?
              AND p.strategy_key IN ('model_1','model_2','model_3')
            ORDER BY s.entry_time,p.sample_id
            """,
            (now - 21600, FEATURE_SCHEMA_VERSION),
        )

        result: dict[str, float | None] = {}
        for hours in (1, 3, 6):
            cutoff = now - hours * 3600
            grouped: dict[int, list[tuple[int, float]]] = {}
            for ended_at, sample_id, rate in observations:
                if ended_at >= cutoff:
                    grouped.setdefault(sample_id, []).append((ended_at, rate))
            clusters = sorted(
                (
                    (max(ts for ts, _ in group), statistics.mean(rate for _, rate in group))
                    for group in grouped.values()
                    if group
                ),
                key=lambda item: item[0],
            )
            returns = [rate for _, rate in clusters]
            wins = sum(item > 0 for item in returns)
            n = len(returns)
            result[f"strategy_cluster_count_{hours}h"] = float(n)
            result[f"strategy_bayes_win_rate_{hours}h"] = (wins + 1.0) / (n + 6.0)
            result[f"strategy_mean_return_{hours}h"] = (
                statistics.mean(returns) if returns else None
            )
            result[f"strategy_drawdown_{hours}h"] = self._max_drawdown(returns)

            by_sample: dict[int, list[float]] = {}
            for row in probability_rows:
                if int(row.get("entry_time") or 0) < cutoff:
                    continue
                probability = to_float(row.get("probability"))
                if probability is not None:
                    by_sample.setdefault(int(row.get("sample_id") or 0), []).append(probability)
            disagreements = [
                statistics.pstdev(values) for values in by_sample.values() if len(values) >= 2
            ]
            result[f"strategy_model_disagreement_{hours}h"] = (
                statistics.mean(disagreements) if disagreements else None
            )
        return result

    def _execution(self, now: int) -> dict[str, float | None]:
        rows = self.database.fetch_all(
            """
            SELECT t.status,t.slippage_cost_usd,t.requested_amount,
                   t.failure_category,t.failure_code,t.failure_message
            FROM trades t
            JOIN positions p ON p.id=t.position_id
            JOIN samples s ON s.id=p.sample_id
            WHERE t.account_kind='simulation' AND t.created_at>=?
              AND s.feature_schema_version=?
            ORDER BY t.created_at DESC LIMIT 500
            """,
            (_iso(now - 3600), FEATURE_SCHEMA_VERSION),
        )
        if not rows:
            return {
                "execution_success_rate_1h": None,
                "execution_slippage_fraction_1h": None,
                "execution_no_route_rate_1h": None,
            }
        success = sum(
            str(row.get("status") or "").lower() in {"processed", "confirmed"} for row in rows
        ) / len(rows)
        ratios: list[float] = []
        no_route = 0
        for row in rows:
            cost = to_float(row.get("slippage_cost_usd"))
            amount = to_float(row.get("requested_amount"))
            if cost is not None and amount and amount > 0:
                ratios.append(cost / amount)
            text = " ".join(
                str(row.get(key) or "").lower()
                for key in ("failure_category", "failure_code", "failure_message")
            )
            if "no_route" in text or "no route" in text or "route_not_found" in text:
                no_route += 1
        return {
            "execution_success_rate_1h": success,
            "execution_slippage_fraction_1h": statistics.mean(ratios) if ratios else None,
            "execution_no_route_rate_1h": no_route / len(rows),
        }

    def _robust_delta(self, key: str, current: float | None) -> float | None:
        if current is None or not math.isfinite(current):
            return None
        rows = self.database.fetch_all(
            "SELECT features_json FROM market_regime_snapshots ORDER BY observed_at DESC LIMIT 720"
        )
        history: list[float] = []
        for row in rows:
            try:
                value = to_float(json.loads(row.get("features_json") or "{}").get(key))
            except (TypeError, json.JSONDecodeError):
                value = None
            if value is not None and math.isfinite(value):
                history.append(value)
        if len(history) < 12:
            return None
        center = statistics.median(history)
        mad = statistics.median(abs(value - center) for value in history)
        scale = max(mad * 1.4826, abs(center) * 0.10, 1e-9)
        return math.tanh((current - center) / (2 * scale))

    def _score(
        self,
        features: Mapping[str, Any],
        *,
        network_ok: bool,
        gmgn_primary_ok: bool,
        crypto_ok: bool,
        sol_fallback_used: bool,
    ) -> tuple[float, float, list[str]]:
        family: dict[str, float] = {}

        crypto: list[float] = []
        for key, scale in (("btc_return_15m", 0.025), ("btc_return_60m", 0.05)):
            value = to_float(features.get(key))
            if value is not None:
                crypto.append(math.tanh(value / scale))
        btc_vol = self._robust_delta(
            "btc_realized_vol_15m", to_float(features.get("btc_realized_vol_15m"))
        )
        if btc_vol is not None:
            crypto.append(-0.5 * btc_vol)
        if crypto:
            family["crypto"] = statistics.mean(crypto)

        sol: list[float] = []
        for key, scale in (("sol_return_15m", 0.03), ("sol_return_60m", 0.06)):
            value = to_float(features.get(key))
            if value is not None:
                sol.append(math.tanh(value / scale))
        sol_vol = self._robust_delta(
            "sol_realized_vol_15m", to_float(features.get("sol_realized_vol_15m"))
        )
        if sol_vol is not None:
            sol.append(-0.4 * sol_vol)
        if sol:
            family["sol"] = statistics.mean(sol)

        network: list[float] = []
        tps = self._robust_delta(
            "solana_non_vote_tps", to_float(features.get("solana_non_vote_tps"))
        )
        fee = self._robust_delta(
            "solana_priority_fee_p90", to_float(features.get("solana_priority_fee_p90"))
        )
        if tps is not None:
            network.append(tps)
        if fee is not None:
            network.append(-0.5 * fee)
        if network:
            family["network"] = statistics.mean(network)

        breadth = [
            self._robust_delta(key, to_float(features.get(key)))
            for key in ("meme_discovery_breadth", "meme_acceptance_rate")
        ]
        breadth = [value for value in breadth if value is not None]
        if breadth:
            family["breadth"] = statistics.mean(breadth)

        attention = [
            to_float(features.get(key))
            for key in ("market_buy_count_imbalance_1m", "market_buy_volume_imbalance_1m")
        ]
        attention = [max(-1.0, min(1.0, value)) for value in attention if value is not None]
        for key in (
            "hot_search_visits_1m",
            "signal_smart_money_buy_15m",
            "signal_kol_buy_15m",
            "signal_large_buy_15m",
            "signal_multi_buy_15m",
            "signal_dex_ad_15m",
            "signal_dex_boost_15m",
            "signal_dex_trending_15m",
            "dexscreener_sol_ads_latest_count",
            "dexscreener_sol_boosts_latest_count",
            "dexscreener_sol_boost_amount_latest",
        ):
            value = self._robust_delta(key, to_float(features.get(key)))
            if value is not None:
                attention.append(value)
        if attention:
            family["attention"] = statistics.mean(attention)

        strategy: list[float] = []
        for hours in (1, 3, 6):
            win = to_float(features.get(f"strategy_bayes_win_rate_{hours}h"))
            mean = to_float(features.get(f"strategy_mean_return_{hours}h"))
            drawdown = to_float(features.get(f"strategy_drawdown_{hours}h"))
            disagreement = to_float(features.get(f"strategy_model_disagreement_{hours}h"))
            if win is not None:
                strategy.append(math.tanh((win - 1 / 6) / 0.08))
            if mean is not None:
                strategy.append(math.tanh(mean / 0.12))
            if drawdown is not None:
                strategy.append(-math.tanh(drawdown / 0.20))
            if disagreement is not None:
                strategy.append(-0.5 * math.tanh(disagreement / 0.12))
        if strategy:
            family["strategy"] = statistics.mean(strategy)

        execution: list[float] = []
        success = to_float(features.get("execution_success_rate_1h"))
        slip = to_float(features.get("execution_slippage_fraction_1h"))
        no_route = to_float(features.get("execution_no_route_rate_1h"))
        if success is not None:
            execution.append(math.tanh((success - 0.9) / 0.08))
        if slip is not None:
            execution.append(-math.tanh(slip / 0.05))
        if no_route is not None:
            execution.append(-math.tanh(no_route / 0.10))
        if execution:
            family["execution"] = statistics.mean(execution)

        weight = sum(self.WEIGHTS[name] for name in family)
        composite = (
            sum(self.WEIGHTS[name] * value for name, value in family.items()) / weight if weight else 0.0
        )
        score = _clip(0.5 + 0.35 * composite)
        confidence = _clip(weight / sum(self.WEIGHTS.values()))
        if not network_ok:
            confidence *= 0.85
        if not gmgn_primary_ok:
            confidence *= 0.85
        if not crypto_ok:
            confidence *= 0.90
        if sol_fallback_used:
            confidence *= 0.95
        confidence = _clip(confidence)
        reasons = [
            f"{name}:{'hot' if value > .15 else 'cold' if value < -.15 else 'neutral'}:{value:+.2f}"
            for name, value in sorted(family.items(), key=lambda item: abs(item[1]), reverse=True)[:4]
        ]
        return score, confidence, reasons

    def capture(
        self,
        network: SolanaNetworkSnapshot,
        gmgn: Mapping[str, Any] | None = None,
        crypto: PublicMarketSnapshot | None = None,
    ) -> RegimeSnapshot:
        now = int(network.observed_at or time.time())
        feed = dict(gmgn or {})
        features: dict[str, Any] = {
            **self._sol(now),
            **network.as_features(),
            **self._breadth(now),
            **self._strategy(now),
            **self._execution(now),
        }
        crypto_features = dict(crypto.features) if crypto is not None else {}
        features.update(crypto_features)

        sol_fallback_used = False
        if crypto is not None and crypto.sol_available:
            for suffix in ("return_5m", "return_15m", "return_60m", "realized_vol_15m"):
                canonical = f"sol_{suffix}"
                fallback = f"coinbase_sol_{suffix}"
                if features.get(canonical) is None and crypto_features.get(fallback) is not None:
                    features[canonical] = crypto_features[fallback]
                    sol_fallback_used = True

        features.update(
            {
                key: value
                for key, value in feed.items()
                if key not in {"observed_at", "available", "primary_available", "errors", "source"}
            }
        )
        feed_observed_at = int(to_float(feed.get("observed_at")) or 0)
        gmgn_primary_ok = bool(feed.get("primary_available", feed.get("available"))) and (
            feed_observed_at <= 0 or now - feed_observed_at <= 300
        )
        crypto_ok = bool(crypto and crypto.btc_available and now - crypto.observed_at <= 600)
        score, confidence, reasons = self._score(
            features,
            network_ok=network.healthy,
            gmgn_primary_ok=gmgn_primary_ok,
            crypto_ok=crypto_ok,
            sol_fallback_used=sol_fallback_used,
        )
        label = "cold" if score < 0.40 else "hot" if score > 0.60 else "neutral"
        health = {
            "rpc_provider": network.provider,
            "rpc_healthy": network.healthy,
            "public_rpc_emergency": network.emergency_public_rpc,
            "rpc_errors": list(network.errors),
            "gmgn_available": gmgn_primary_ok,
            "attention_source": str(feed.get("source") or "gmgn"),
            "attention_available": bool(feed.get("available")),
            "attention_errors": list(feed.get("errors") or []),
            "coinbase_btc_available": crypto_ok,
            "coinbase_sol_available": bool(crypto and crypto.sol_available),
            "coinbase_errors": list(crypto.errors) if crypto else ["snapshot_missing"],
            "sol_market_source": (
                "coinbase_public_fallback" if sol_fallback_used else "gmgn_cached_sol_price"
            ),
            "sol_primary_fresh": bool(features.get("sol_primary_fresh")),
        }
        with self.database.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO market_regime_snapshots(
                    observed_at,regime_score,regime_label,confidence,features_json,
                    source_health_json,reasons_json,policy_version,recorded_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(observed_at) DO UPDATE SET
                    regime_score=excluded.regime_score,
                    regime_label=excluded.regime_label,
                    confidence=excluded.confidence,
                    features_json=excluded.features_json,
                    source_health_json=excluded.source_health_json,
                    reasons_json=excluded.reasons_json,
                    policy_version=excluded.policy_version,
                    recorded_at=excluded.recorded_at
                """,
                (
                    now,
                    score,
                    label,
                    confidence,
                    json.dumps(features, separators=(",", ":")),
                    json.dumps(health, separators=(",", ":")),
                    json.dumps(reasons, separators=(",", ":")),
                    REGIME_POLICY_VERSION,
                    utc_now_iso(),
                ),
            )
            row = connection.execute(
                "SELECT id FROM market_regime_snapshots WHERE observed_at=?", (now,)
            ).fetchone()
        return RegimeSnapshot(
            int(row["id"]), now, score, label, confidence, features, health, tuple(reasons)
        )

    def latest(self) -> RegimeSnapshot | None:
        row = self.database.fetch_one(
            "SELECT * FROM market_regime_snapshots ORDER BY observed_at DESC LIMIT 1"
        )
        if not row:
            return None
        return RegimeSnapshot(
            int(row["id"]),
            int(row["observed_at"]),
            float(row["regime_score"]),
            str(row["regime_label"]),
            float(row["confidence"]),
            json.loads(row["features_json"]),
            json.loads(row["source_health_json"]),
            tuple(json.loads(row["reasons_json"])),
        )

    def public_status(self) -> dict[str, Any]:
        latest = self.latest()
        return {
            "latest": asdict(latest) if latest else None,
            "worker": self.database.get_runtime_state("market_regime_worker_status", {}),
        }


class MarketRegimeWorker:
    def __init__(self, database: Database, poll_seconds: int = 60) -> None:
        self.database = database
        self.poll_seconds = max(15, int(poll_seconds))
        self.service = MarketRegimeService(database)
        self.rpc = SolanaRpcPool()
        self.dexscreener_fallback = DexScreenerFallbackProvider()
        self.crypto_market = CoinbasePublicMarketProvider()
        self._stop = asyncio.Event()

    async def run_once(self) -> RegimeSnapshot:
        network = await self.rpc.snapshot()
        feed = self.database.get_runtime_state("gmgn_market_regime_feed", {})
        feed = feed if isinstance(feed, Mapping) else {}
        now = int(network.observed_at or time.time())
        feed_observed_at = int(to_float(feed.get("observed_at")) or 0)
        gmgn_available = bool(feed.get("available")) and (
            feed_observed_at <= 0 or now - feed_observed_at <= 300
        )
        if gmgn_available:
            feed = {**feed, "primary_available": True, "source": "gmgn"}
        fallback_used = False
        dex_signal_missing = all(
            feed.get(key) is None
            for key in ("signal_dex_ad_15m", "signal_dex_boost_15m", "signal_dex_trending_15m")
        )
        # Fallback is family-specific: Trending may still be healthy while the
        # GMGN signal endpoint is unavailable. In that case keep GMGN as the
        # primary source and supplement only the missing DEX-attention family.
        if not gmgn_available or dex_signal_missing:
            fallback = await self.dexscreener_fallback.snapshot()
            if bool(fallback.get("available")):
                combined_errors = list(feed.get("errors") or []) + list(fallback.get("errors") or [])
                feed = {
                    **feed,
                    **fallback,
                    "available": True,
                    "primary_available": gmgn_available,
                    "source": "gmgn+dexscreener_fallback" if gmgn_available else "dexscreener_public_fallback",
                    "errors": combined_errors,
                }
                fallback_used = True
            else:
                feed = {**feed, "primary_available": gmgn_available}

        crypto = await self.crypto_market.snapshot(observed_at=now, include_sol=True)
        snapshot = self.service.capture(network, feed, crypto)
        self.database.set_runtime_state(
            "market_regime_worker_status",
            {
                "state": "running",
                "last_run_at": utc_now_iso(),
                "regime_score": snapshot.score,
                "regime_label": snapshot.label,
                "confidence": snapshot.confidence,
                "rpc_provider": network.provider,
                "gmgn_available": gmgn_available,
                "dexscreener_fallback_used": fallback_used,
                "coinbase_btc_available": crypto.btc_available,
                "coinbase_sol_available": crypto.sol_available,
            },
        )
        return snapshot

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.run_once()
            except Exception as exc:
                self.database.set_runtime_state(
                    "market_regime_worker_status",
                    {
                        "state": "degraded",
                        "last_run_at": utc_now_iso(),
                        "last_error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    },
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=max(1.0, self.poll_seconds - (time.monotonic() - started)),
                )
            except TimeoutError:
                pass
        await self.rpc.close()
        await self.dexscreener_fallback.close()
        await self.crypto_market.close()

    def stop(self) -> None:
        self._stop.set()
