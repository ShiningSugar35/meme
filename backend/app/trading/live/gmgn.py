"""GMGN v1 atomic swap provider.

Authentication is delegated to ``SignedTradeTransport``.  This module never
loads a key, private key or wallet secret and never adds one to command-line
arguments.
"""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .errors import LiveTradeError
from .models import (
    ExecutionStep,
    FailureKind,
    OrderSnapshot,
    OrderStatus,
    Quote,
    Submission,
    SwapIntent,
)


@dataclass(frozen=True, slots=True)
class TradeTransportResponse:
    status_code: int
    data: Any
    headers: Mapping[str, str] = field(default_factory=dict)


class SignedTradeTransport(Protocol):
    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> TradeTransportResponse: ...


HeaderFactory = Callable[
    [str, str, Mapping[str, Any] | None, Mapping[str, Any] | None],
    Mapping[str, str] | Awaitable[Mapping[str, str]],
]


class HttpxSignedTradeTransport:
    """HTTP adapter whose injected header factory performs signed auth locally."""

    def __init__(
        self,
        *,
        base_url: str,
        header_factory: HeaderFactory,
        timeout_seconds: float = 10.0,
        client: Any | None = None,
    ) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - deployment wiring
            raise RuntimeError("httpx is required for HttpxSignedTradeTransport") from exc
        if not base_url.strip():
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.header_factory = header_factory
        self.timeout_seconds = timeout_seconds
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> TradeTransportResponse:
        headers = self.header_factory(method, path, params, json_body)
        if inspect.isawaitable(headers):
            headers = await headers
        try:
            response = await self._client.request(
                method,
                f"{self.base_url}/{path.lstrip('/')}",
                params=dict(params or {}),
                json=dict(json_body) if json_body is not None else None,
                headers=dict(headers),
                timeout=self.timeout_seconds,
            )
        except Exception as exc:
            raise LiveTradeError(
                "GMGN transport network request failed",
                kind=FailureKind.NETWORK,
            ) from exc
        try:
            data = response.json()
        except Exception:
            data = {"message": "GMGN returned a non-JSON response"}
        return TradeTransportResponse(response.status_code, data, dict(response.headers))

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


@dataclass(frozen=True, slots=True)
class GmgnEndpoints:
    quote: str = "/v1/trade/quote"
    swap: str = "/v1/trade/swap"
    query_order: str = "/v1/trade/query_order"


def _unwrap(data: Any) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    nested = data.get("data")
    return nested if isinstance(nested, Mapping) else data


def _message(data: Any) -> str:
    mapping = data if isinstance(data, Mapping) else {}
    for key in ("message", "msg", "error_status", "error"):
        value = mapping.get(key)
        if value not in (None, "") and not isinstance(value, Mapping):
            return str(value)[:300]
    return "GMGN trade request failed"


def _reset_at(response: TradeTransportResponse) -> int | None:
    candidates: list[Any] = []
    for key, value in response.headers.items():
        if key.lower() == "x-ratelimit-reset":
            candidates.append(value)
    if isinstance(response.data, Mapping):
        candidates.extend((response.data.get("reset_at"), response.data.get("resetAt")))
    for raw in candidates:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            continue
        if value < 10_000_000:
            return int(time.time()) + value
        return value // 1_000 if value > 10_000_000_000 else value
    match = re.search(r"reset_at[^0-9]*([0-9]{10,13})", str(response.data))
    if match:
        value = int(match.group(1))
        return value // 1_000 if value > 10_000_000_000 else value
    return None


def _failure_kind(code: str | None, message: str) -> FailureKind:
    text = f"{code or ''} {message}".lower()
    if any(mark in text for mark in ("balance", "insufficient fund", "not enough")):
        return FailureKind.BALANCE
    if any(mark in text for mark in ("no route", "route_not", "route not", "no liquidity")):
        return FailureKind.NO_ROUTE
    if any(mark in text for mark in (
        "bad_request", "auth_", "wallet_mismatch", "signature", "invalid parameter",
        "chain_not_supported", "validation",
    )):
        return FailureKind.VALIDATION
    if any(mark in text for mark in ("blockhash", "slippage", "transaction", "on-chain", "chain")):
        return FailureKind.CHAIN
    return FailureKind.API


def _status(value: Any) -> OrderStatus:
    normalized = str(value or "").strip().lower()
    aliases = {
        "pending": OrderStatus.PENDING,
        "processed": OrderStatus.PROCESSED,
        "confirmed": OrderStatus.CONFIRMED,
        "successful": OrderStatus.CONFIRMED,
        "success": OrderStatus.CONFIRMED,
        "failed": OrderStatus.FAILED,
        "failure": OrderStatus.FAILED,
        "expired": OrderStatus.EXPIRED,
    }
    return aliases.get(normalized, OrderStatus.UNKNOWN)


class GMGNAtomicProvider:
    """Adapter for quote -> atomic swap -> query_order."""

    name = "gmgn"

    def __init__(
        self,
        transport: SignedTradeTransport,
        *,
        endpoints: GmgnEndpoints | None = None,
        anti_mev: bool = True,
    ) -> None:
        self.transport = transport
        self.endpoints = endpoints or GmgnEndpoints()
        self.anti_mev = anti_mev

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        try:
            response = await self.transport.request(
                method,
                path,
                params=params,
                json_body=json_body,
            )
        except LiveTradeError:
            raise
        except Exception as exc:
            raise LiveTradeError(
                "GMGN transport failed",
                kind=FailureKind.NETWORK,
            ) from exc
        mapping = response.data if isinstance(response.data, Mapping) else {}
        code = str(mapping.get("code")) if mapping.get("code") not in (None, "", 0, "0") else None
        message = _message(mapping)
        if response.status_code == 429 or code == "429" or "rate_limit" in message.lower():
            raise LiveTradeError(
                "GMGN rate limit is active; wait until reset before another request",
                kind=FailureKind.RATE_LIMIT,
                code=code or "429",
                reset_at=_reset_at(response),
            )
        if response.status_code >= 400 or (code and code.lower() != "success"):
            kind = _failure_kind(code, message)
            raise LiveTradeError(
                f"GMGN trade API rejected request: {message}",
                kind=kind,
                code=code,
            )
        return _unwrap(mapping)

    async def quote(self, intent: SwapIntent, step: ExecutionStep) -> Quote:
        data = await self._request(
            "GET",
            self.endpoints.quote,
            params={
                "chain": intent.chain.lower(),
                "from": intent.wallet_address,
                "input_token": intent.input_token,
                "output_token": intent.output_token,
                "amount": intent.input_amount_raw,
                "slippage": step.slippage,
            },
        )
        output = str(data.get("output_amount") or "")
        minimum = str(data.get("min_output_amount") or "")
        if not output or not minimum:
            raise LiveTradeError(
                "GMGN quote omitted output or minimum output amount",
                kind=FailureKind.NO_ROUTE,
                code="INCOMPLETE_QUOTE",
            )
        return Quote(
            input_token=str(data.get("input_token") or intent.input_token),
            output_token=str(data.get("output_token") or intent.output_token),
            input_amount_raw=str(data.get("input_amount") or intent.input_amount_raw),
            output_amount_raw=output,
            min_output_amount_raw=minimum,
            slippage=float(data.get("slippage") if data.get("slippage") is not None else step.slippage),
            raw=data,
        )

    async def submit_swap(
        self,
        intent: SwapIntent,
        quote: Quote,
        step: ExecutionStep,
        *,
        submission_client_order_id: str,
    ) -> Submission:
        data = await self._request(
            "POST",
            self.endpoints.swap,
            json_body={
                "chain": intent.chain.lower(),
                "from": intent.wallet_address,
                "input_token": intent.input_token,
                "output_token": intent.output_token,
                "amount": intent.input_amount_raw,
                "min_output": quote.min_output_amount_raw,
                "slippage": step.slippage,
                "priority_fee": step.priority_fee_sol,
                "tip_fee": step.tip_fee_sol,
                "anti_mev": self.anti_mev,
                "client_order_id": submission_client_order_id,
            },
        )
        order_id = str(data.get("order_id") or "")
        if not order_id:
            raise LiveTradeError(
                "GMGN swap response omitted order_id",
                kind=FailureKind.API,
                code="MISSING_ORDER_ID",
            )
        submission_status = _status(data.get("status"))
        if submission_status is OrderStatus.UNKNOWN:
            submission_status = OrderStatus.PENDING
        return Submission(
            order_id=order_id,
            status=submission_status,
            tx_hash=str(data.get("hash") or "") or None,
            raw=data,
        )

    async def get_order(self, order_id: str) -> OrderSnapshot:
        data = await self._request(
            "GET",
            self.endpoints.query_order,
            params={"chain": "sol", "order_id": order_id},
        )
        status = _status(data.get("status"))
        code = str(data.get("error_code") or "") or None
        message = str(data.get("error_status") or data.get("message") or "") or None
        if status is OrderStatus.UNKNOWN:
            status = OrderStatus.PENDING
        kind: FailureKind | None = None
        if status is OrderStatus.EXPIRED:
            kind = FailureKind.EXPIRED
        elif status is OrderStatus.FAILED:
            kind = _failure_kind(code, message or "")
            # A terminal on-chain order failure is a chain failure unless its
            # response explicitly identifies validation, balance or no-route.
            if kind is FailureKind.API:
                kind = FailureKind.CHAIN
        report = data.get("report") if isinstance(data.get("report"), Mapping) else {}
        return OrderSnapshot(
            order_id=str(data.get("order_id") or order_id),
            status=status,
            tx_hash=str(data.get("hash") or "") or None,
            failure_kind=kind,
            error_code=code,
            error_message=message,
            report=report,
        )
