from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from dotenv import dotenv_values

from ..config import PROJECT_ROOT


@dataclass(frozen=True, slots=True)
class RpcEndpoint:
    provider: str
    slot: int
    url: str = field(repr=False)
    production_grade: bool = True

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.slot}"


@dataclass(frozen=True, slots=True)
class SolanaNetworkSnapshot:
    observed_at: int
    provider: str | None
    non_vote_tps: float | None
    slot_rate: float | None
    priority_fee_p50: float | None
    priority_fee_p90: float | None
    healthy: bool
    emergency_public_rpc: bool = False
    latency_ms: float | None = None
    errors: tuple[str, ...] = ()

    def as_features(self) -> dict[str, float | None]:
        return {
            "solana_non_vote_tps": self.non_vote_tps,
            "solana_slot_rate": self.slot_rate,
            "solana_priority_fee_p50": self.priority_fee_p50,
            "solana_priority_fee_p90": self.priority_fee_p90,
        }


def _read_env(path: Path | None = None) -> dict[str, str]:
    values = dotenv_values(path or PROJECT_ROOT / ".env")
    return {str(key): str(value) for key, value in values.items() if value not in (None, "")}


def _split_urls(raw: str) -> list[str]:
    value = raw.strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    normalized = value.replace(";", ",").replace("\n", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _is_ankr_url(url: str) -> bool:
    host = (urlparse(str(url)).hostname or "").lower()
    return host == "ankr.com" or host.endswith(".ankr.com")


def _is_alchemy_url(url: str) -> bool:
    host = (urlparse(str(url)).hostname or "").lower()
    return host == "alchemy.com" or host.endswith(".alchemy.com")


def configured_rpc_endpoints(path: Path | None = None) -> tuple[RpcEndpoint, ...]:
    """Build a secret-safe provider order: Alchemy -> public emergency.

    Four independent Alchemy accounts are treated as independent auth/failure
    domains.  We do not assume their same-IP rate limiting is independent; the
    bounded live probe records that separately.
    """
    env = _read_env(path)
    alchemy_urls = _split_urls(env.get("SOLANA_RPC_HTTP_URLS", ""))
    if not alchemy_urls:
        alchemy_urls = _split_urls(env.get("SOLANA_RPC_URL", ""))
    alchemy_urls = [url for url in alchemy_urls if _is_alchemy_url(url) and not _is_ankr_url(url)]
    alchemy_keys = [
        value for key, value in sorted(env.items())
        if key.startswith("ALCHEMY_API_KEY_") and value.strip()
    ]
    if len(alchemy_urls) < len(alchemy_keys):
        existing = set(alchemy_urls)
        for key in alchemy_keys:
            candidate = f"https://solana-mainnet.g.alchemy.com/v2/{key}"
            if candidate not in existing:
                alchemy_urls.append(candidate)
                existing.add(candidate)
    endpoints: list[RpcEndpoint] = [
        RpcEndpoint("alchemy", index + 1, url)
        for index, url in enumerate(alchemy_urls[: max(4, len(alchemy_urls))])
        if url.startswith(("http://", "https://"))
    ]
    # Public RPC is deliberately last and marked non-production.  A snapshot
    # sourced from it lowers Regime confidence rather than silently becoming a
    # normal production feed.
    endpoints.append(RpcEndpoint("solana_public", 1, "https://api.mainnet-beta.solana.com", False))
    # De-duplicate by exact endpoint while preserving failover order.
    unique: list[RpcEndpoint] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        if endpoint.url in seen:
            continue
        seen.add(endpoint.url)
        unique.append(endpoint)
    return tuple(unique)


class SolanaRpcPool:
    def __init__(
        self,
        endpoints: Sequence[RpcEndpoint] | None = None,
        *,
        timeout_seconds: float = 8.0,
        circuit_seconds: float = 30.0,
        client: Any | None = None,
    ) -> None:
        self.endpoints = tuple(endpoints or configured_rpc_endpoints())
        self.timeout_seconds = float(timeout_seconds)
        self.circuit_seconds = float(circuit_seconds)
        self._client = client
        self._owns_client = client is None
        self._cooldown_until: dict[str, float] = {}
        self._next_alchemy = 0

    async def _http_client(self) -> Any:
        if self._client is None:
            import httpx
            # Solana RPC must not inherit workstation HTTP(S)/ALL_PROXY settings.
            # The local proxy route can make Alchemy mainnet RPC time out even
            # while direct TCP/HTTPS is healthy; keep this provider pool direct.
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False)
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ordered(self) -> tuple[RpcEndpoint, ...]:
        alchemy = [item for item in self.endpoints if item.provider == "alchemy"]
        rest = [item for item in self.endpoints if item.provider != "alchemy"]
        if alchemy:
            start = self._next_alchemy % len(alchemy)
            self._next_alchemy += 1
            alchemy = alchemy[start:] + alchemy[:start]
        return tuple([*alchemy, *rest])

    @staticmethod
    def _safe_error(endpoint: RpcEndpoint, exc: BaseException) -> str:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            return f"{endpoint.label}:http_{int(status_code)}"
        text = str(exc)
        if text.startswith(("rate_limited:", "rpc_error:")):
            return text[:120]
        # Provider URLs may embed API tokens. Never persist an
        # arbitrary exception message from an HTTP client.
        return f"{endpoint.label}:{type(exc).__name__}"

    async def _rpc(self, endpoint: RpcEndpoint, method: str, params: list[Any]) -> Any:
        client = await self._http_client()
        started = time.perf_counter()
        response = await client.post(
            endpoint.url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        if response.status_code == 429:
            raise RuntimeError(f"rate_limited:{endpoint.label}")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping) or payload.get("error"):
            code = payload.get("error", {}).get("code") if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping) else "unknown"
            raise RuntimeError(f"rpc_error:{endpoint.label}:{code}")
        return payload.get("result"), latency_ms

    @staticmethod
    def _percentile(values: Sequence[float], q: float) -> float | None:
        clean = sorted(float(value) for value in values if math.isfinite(float(value)))
        if not clean:
            return None
        if len(clean) == 1:
            return clean[0]
        position = (len(clean) - 1) * q
        low = int(math.floor(position))
        high = int(math.ceil(position))
        if low == high:
            return clean[low]
        weight = position - low
        return clean[low] * (1 - weight) + clean[high] * weight

    async def snapshot(self, *, now_ts: int | None = None) -> SolanaNetworkSnapshot:
        observed_at = int(now_ts or time.time())
        errors: list[str] = []
        for endpoint in self._ordered():
            if self._cooldown_until.get(endpoint.label, 0.0) > time.monotonic():
                continue
            try:
                performance, latency_a = await self._rpc(endpoint, "getRecentPerformanceSamples", [1])
                fees, latency_b = await self._rpc(endpoint, "getRecentPrioritizationFees", [])
                sample = performance[0] if isinstance(performance, list) and performance else {}
                period = float(sample.get("samplePeriodSecs") or 0) if isinstance(sample, Mapping) else 0.0
                non_vote = float(sample.get("numNonVoteTransactions") or 0) if isinstance(sample, Mapping) else 0.0
                slots = float(sample.get("numSlots") or 0) if isinstance(sample, Mapping) else 0.0
                fee_values = [
                    float(item.get("prioritizationFee"))
                    for item in fees or []
                    if isinstance(item, Mapping) and item.get("prioritizationFee") is not None
                ]
                return SolanaNetworkSnapshot(
                    observed_at=observed_at,
                    provider=endpoint.label,
                    non_vote_tps=(non_vote / period) if period > 0 else None,
                    slot_rate=(slots / period) if period > 0 else None,
                    priority_fee_p50=self._percentile(fee_values, 0.50),
                    priority_fee_p90=self._percentile(fee_values, 0.90),
                    healthy=endpoint.production_grade,
                    emergency_public_rpc=not endpoint.production_grade,
                    latency_ms=latency_a + latency_b,
                    errors=tuple(errors),
                )
            except Exception as exc:
                self._cooldown_until[endpoint.label] = time.monotonic() + self.circuit_seconds
                errors.append(self._safe_error(endpoint, exc))
        return SolanaNetworkSnapshot(
            observed_at=observed_at,
            provider=None,
            non_vote_tps=None,
            slot_rate=None,
            priority_fee_p50=None,
            priority_fee_p90=None,
            healthy=False,
            errors=tuple(errors),
        )

    async def transactions_for_address(
        self,
        address: str,
        *,
        start_ts: int,
        end_ts: int,
        max_pages: int = 10,
        page_limit: int = 100,
        stop_after_page: Callable[[Sequence[Mapping[str, Any]]], bool] | None = None,
    ) -> tuple[list[Mapping[str, Any]], str, bool]:
        """Read a bounded historical window through Alchemy archival RPC only."""
        if not address or int(end_ts) < int(start_ts):
            raise ValueError("invalid address/time window")
        pages = max(1, int(max_pages))
        limit = max(1, min(100, int(page_limit)))
        errors: list[str] = []
        for endpoint in self._ordered():
            if endpoint.provider != "alchemy":
                continue
            if self._cooldown_until.get(endpoint.label, 0.0) > time.monotonic():
                continue
            rows: list[Mapping[str, Any]] = []
            pagination_token: str | None = None
            try:
                for _page in range(pages):
                    config: dict[str, Any] = {
                        "transactionDetails": "full",
                        "sortOrder": "desc",
                        "limit": limit,
                        "commitment": "finalized",
                        "encoding": "jsonParsed",
                        "filters": {
                            "status": "succeeded",
                            "blockTime": {"gte": int(start_ts), "lte": int(end_ts)},
                        },
                    }
                    if pagination_token:
                        config["paginationToken"] = pagination_token
                    result, _latency = await self._rpc(
                        endpoint, "getTransactionsForAddress", [address, config]
                    )
                    if not isinstance(result, Mapping):
                        raise RuntimeError(f"rpc_error:{endpoint.label}:malformed_result")
                    data = result.get("data")
                    if not isinstance(data, list):
                        raise RuntimeError(f"rpc_error:{endpoint.label}:malformed_data")
                    page_rows = [item for item in data if isinstance(item, Mapping)]
                    rows.extend(page_rows)
                    if stop_after_page is not None and stop_after_page(page_rows):
                        return rows, endpoint.label, False
                    next_token = result.get("paginationToken")
                    pagination_token = str(next_token) if next_token else None
                    if not pagination_token:
                        return rows, endpoint.label, True
                return rows, endpoint.label, False
            except Exception as exc:
                self._cooldown_until[endpoint.label] = time.monotonic() + self.circuit_seconds
                errors.append(self._safe_error(endpoint, exc))
        raise RuntimeError("alchemy_history_unavailable:" + ",".join(errors[:4]))


async def bounded_rate_probe(
    endpoints: Sequence[RpcEndpoint] | None = None,
    *,
    requests_per_alchemy_account: int = 75,
) -> dict[str, Any]:
    """Small same-IP diagnostic; deliberately not a rate-limit stress test.

    getSlot currently costs 20 CU, so 75 requests consume 1,500 CU per account
    and four independent accounts issue a 6,000-CU same-IP burst. Alchemy's Free
    pricing page currently shows 500 CUPS in its overview and 1,000 CUPS in a
    lower throughput table, while also documenting elastic headroom. Therefore
    a clean concurrent pass is useful evidence against a small hard IP-wide
    bucket, but it cannot prove universal rate-limit independence.
    """
    selected = [item for item in (endpoints or configured_rpc_endpoints()) if item.provider == "alchemy"]
    try:
        import httpx
    except ImportError:
        return {"status": "unavailable", "reason": "httpx_missing"}

    async with httpx.AsyncClient(timeout=8.0) as client:
        async def one(endpoint: RpcEndpoint) -> dict[str, Any]:
            latencies: list[float] = []
            status_counts: dict[str, int] = {}
            async def call(index: int) -> None:
                started = time.perf_counter()
                try:
                    response = await client.post(
                        endpoint.url,
                        json={"jsonrpc": "2.0", "id": index, "method": "getSlot", "params": []},
                    )
                    key = str(response.status_code)
                    if response.status_code == 200:
                        payload = response.json()
                        if isinstance(payload, Mapping) and payload.get("error"):
                            key = f"rpc_{payload['error'].get('code', 'error')}"
                    status_counts[key] = status_counts.get(key, 0) + 1
                except Exception as exc:
                    key = type(exc).__name__
                    status_counts[key] = status_counts.get(key, 0) + 1
                finally:
                    latencies.append((time.perf_counter() - started) * 1000.0)

            # This is intentionally a small bounded burst, not an attempt to
            # exhaust a provider's token bucket.
            await asyncio.gather(*(call(index) for index in range(requests_per_alchemy_account)))
            return {
                "provider": endpoint.label,
                "requests": requests_per_alchemy_account,
                "status_counts": status_counts,
                "latency_median_ms": median(latencies) if latencies else None,
                "latency_max_ms": max(latencies) if latencies else None,
            }

        per_account = await asyncio.gather(*(one(endpoint) for endpoint in selected))
    any_429 = any(item["status_counts"].get("429", 0) for item in per_account)
    return {
        "status": "bounded_probe_complete",
        "same_ip_shared_rate_limit_conclusion": (
            "no_strict_shared_500_cups_bucket_observed"
            if not any_429
            else "possible_shared_or_account_limit"
        ),
        "accounts_tested": len(selected),
        "requests_per_account": requests_per_alchemy_account,
        "total_requests": len(selected) * requests_per_alchemy_account,
        "any_http_429": bool(any_429),
        "per_account": per_account,
        "note": "No endpoint URL or credential is included. Absence of 429 in a bounded probe is not proof of independent rate limits.",
    }
