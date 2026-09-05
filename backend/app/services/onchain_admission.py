from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..collector.constants import FilterThresholds
from ..collector.filters import parse_timestamp, to_float
from .solana_rpc import SolanaRpcPool


@dataclass(frozen=True, slots=True)
class OnchainAdmissionDecision:
    accepted: bool
    reasons: tuple[str, ...]
    buy_swap_ratio_1h: float | None
    creator_launches_24h: int | None
    buy_swap_source: str | None
    creator_launch_source: str | None
    rpc_used: bool


def _find_created_payload(value: Any, depth: int = 0) -> Mapping[str, Any] | None:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        tokens = value.get("tokens")
        if isinstance(tokens, list) and ("inner_count" in value or "open_count" in value):
            return value
        for nested in value.values():
            found = _find_created_payload(nested, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_created_payload(nested, depth + 1)
            if found is not None:
                return found
    return None


def gmgn_creator_launches_24h(
    created_tokens: Mapping[str, Any],
    *,
    end_ts: int,
    reject_at: int,
) -> tuple[int | None, str | None, bool]:
    """Return (count, source, qualified_pass_without_exact_count)."""
    payload = _find_created_payload(created_tokens)
    if payload is None:
        return None, None, False
    inner = to_float(payload.get("inner_count"))
    opened = to_float(payload.get("open_count"))
    total = None
    if inner is not None and opened is not None and inner >= 0 and opened >= 0:
        total = int(inner + opened)
    tokens = payload.get("tokens")
    if isinstance(tokens, list) and total is not None and len(tokens) >= total:
        start_ts = int(end_ts) - 24 * 60 * 60
        timestamps: list[int] = []
        for item in tokens[:total]:
            if not isinstance(item, Mapping):
                timestamps = []
                break
            timestamp = parse_timestamp(item.get("create_timestamp"))
            if timestamp is None:
                timestamps = []
                break
            timestamps.append(timestamp)
        if len(timestamps) == total:
            count = sum(start_ts <= timestamp <= int(end_ts) for timestamp in timestamps)
            return count, "gmgn_created_tokens_exact", False
    if total is not None and total < reject_at:
        return None, "gmgn_created_tokens_total_upper_bound", True
    return None, None, False


def _account_keys(tx: Mapping[str, Any]) -> list[tuple[str, bool]]:
    transaction = tx.get("transaction")
    message = transaction.get("message") if isinstance(transaction, Mapping) else None
    raw_keys = message.get("accountKeys") if isinstance(message, Mapping) else None
    result: list[tuple[str, bool]] = []
    if not isinstance(raw_keys, list):
        return result
    for item in raw_keys:
        if isinstance(item, str):
            result.append((item, False))
        elif isinstance(item, Mapping):
            result.append((str(item.get("pubkey") or ""), bool(item.get("signer"))))
    return result


def _instructions(tx: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    transaction = tx.get("transaction")
    message = transaction.get("message") if isinstance(transaction, Mapping) else None
    direct = message.get("instructions") if isinstance(message, Mapping) else None
    if isinstance(direct, list):
        found.extend(item for item in direct if isinstance(item, Mapping))
    meta = tx.get("meta")
    inner = meta.get("innerInstructions") if isinstance(meta, Mapping) else None
    if isinstance(inner, list):
        for group in inner:
            instructions = group.get("instructions") if isinstance(group, Mapping) else None
            if isinstance(instructions, list):
                found.extend(item for item in instructions if isinstance(item, Mapping))
    return found


def is_creator_launch_transaction(tx: Mapping[str, Any], creator: str) -> bool:
    keys = _account_keys(tx)
    signers = {pubkey for pubkey, signer in keys if signer and pubkey}
    if not keys or creator not in signers:
        return False
    for instruction in _instructions(tx):
        parsed = instruction.get("parsed")
        if not isinstance(parsed, Mapping):
            continue
        instruction_type = str(parsed.get("type") or "").lower()
        if instruction_type in {"initializemint", "initializemint2"}:
            return True
    return False


def _token_amount(balance: Mapping[str, Any]) -> float | None:
    ui = balance.get("uiTokenAmount")
    if not isinstance(ui, Mapping):
        return None
    value = ui.get("uiAmountString")
    if value in (None, ""):
        raw = to_float(ui.get("amount"))
        decimals = to_float(ui.get("decimals"))
        if raw is None or decimals is None:
            return None
        return raw / (10 ** int(decimals))
    return to_float(value)


def classify_swap_transaction(tx: Mapping[str, Any], token_mint: str) -> int:
    """Return +1 buy, -1 sell, 0 unresolved/non-swap for the fee payer."""
    keys = _account_keys(tx)
    if not keys:
        return 0
    fee_payer = keys[0][0]
    signers = {pubkey for pubkey, signer in keys if signer and pubkey}
    signers.add(fee_payer)
    meta = tx.get("meta")
    if not isinstance(meta, Mapping):
        return 0
    pre = meta.get("preTokenBalances")
    post = meta.get("postTokenBalances")
    if not isinstance(pre, list) or not isinstance(post, list):
        return 0
    pre_by_index = {
        int(item.get("accountIndex")): item
        for item in pre
        if isinstance(item, Mapping) and str(item.get("mint") or "") == token_mint and item.get("accountIndex") is not None
    }
    post_by_index = {
        int(item.get("accountIndex")): item
        for item in post
        if isinstance(item, Mapping) and str(item.get("mint") or "") == token_mint and item.get("accountIndex") is not None
    }
    delta = 0.0
    observed = False
    for index in set(pre_by_index) | set(post_by_index):
        before = pre_by_index.get(index, {})
        after = post_by_index.get(index, {})
        owner = str(after.get("owner") or before.get("owner") or "")
        if owner not in signers:
            continue
        before_amount = _token_amount(before) if before else 0.0
        after_amount = _token_amount(after) if after else 0.0
        if before_amount is None or after_amount is None:
            continue
        delta += after_amount - before_amount
        observed = True
    if not observed or math.isclose(delta, 0.0, abs_tol=1e-18):
        return 0
    return 1 if delta > 0 else -1


class OnchainAdmissionService:
    def __init__(
        self,
        rpc: SolanaRpcPool | None = None,
        thresholds: FilterThresholds | None = None,
        *,
        rpc_max_pages: int = 100,
    ) -> None:
        self.rpc = rpc or SolanaRpcPool()
        self.t = thresholds or FilterThresholds()
        self.rpc_max_pages = max(1, int(rpc_max_pages))

    async def close(self) -> None:
        await self.rpc.close()

    async def _rpc_swap_ratio(
        self,
        *,
        token_mint: str,
        pool_address: str,
        entry_time: int,
        age_minutes: float,
    ) -> tuple[float | None, str | None, bool]:
        if not pool_address:
            return None, None, False
        lookback = min(60 * 60, max(1, int(float(age_minutes) * 60)))
        rows, provider, exhaustive = await self.rpc.transactions_for_address(
            pool_address,
            start_ts=int(entry_time) - lookback,
            end_ts=int(entry_time),
            max_pages=self.rpc_max_pages,
        )
        buys = sells = 0
        for tx in rows:
            direction = classify_swap_transaction(tx, token_mint)
            buys += int(direction > 0)
            sells += int(direction < 0)
        total = buys + sells
        if not exhaustive or total <= 0:
            return None, provider, exhaustive
        return buys / total, provider, True

    async def _rpc_creator_launches(
        self,
        *,
        creator: str,
        entry_time: int,
    ) -> tuple[int | None, str | None, bool]:
        if not creator:
            return None, None, False
        observed_launches = 0

        def stop_after_page(page: Sequence[Mapping[str, Any]]) -> bool:
            nonlocal observed_launches
            observed_launches += sum(is_creator_launch_transaction(tx, creator) for tx in page)
            return observed_launches >= self.t.max_creator_launches_24h

        rows, provider, exhaustive = await self.rpc.transactions_for_address(
            creator,
            start_ts=int(entry_time) - 24 * 60 * 60,
            end_ts=int(entry_time),
            max_pages=self.rpc_max_pages,
            stop_after_page=stop_after_page,
        )
        count = sum(is_creator_launch_transaction(tx, creator) for tx in rows)
        if count >= self.t.max_creator_launches_24h:
            return count, provider, True
        return (count if exhaustive else None), provider, exhaustive

    async def evaluate(
        self,
        *,
        token_mint: str,
        pool_address: str,
        creator: str,
        entry_time: int,
        age_minutes: float,
        gmgn_buys_1h: Any,
        gmgn_swaps_1h: Any,
        gmgn_created_tokens: Mapping[str, Any] | None = None,
        created_tokens_loader: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    ) -> OnchainAdmissionDecision:
        reasons: list[str] = []
        rpc_used = False
        buy_source: str | None = None
        launch_source: str | None = None
        buy_ratio: float | None = None
        launch_count: int | None = None

        buys = to_float(gmgn_buys_1h)
        swaps = to_float(gmgn_swaps_1h)
        if buys is not None and swaps is not None and 0 <= buys <= swaps and swaps > 0:
            buy_ratio = buys / swaps
            buy_source = "gmgn_buys_1h_swaps_1h"
        else:
            rpc_used = True
            try:
                buy_ratio, buy_source, _ = await self._rpc_swap_ratio(
                    token_mint=token_mint,
                    pool_address=pool_address,
                    entry_time=entry_time,
                    age_minutes=age_minutes,
                )
            except Exception:
                buy_ratio = None
        if buy_ratio is None:
            reasons.append("missing_or_invalid:buy_swap_ratio_1h")
        elif not buy_ratio < self.t.max_buy_swap_ratio_1h:
            reasons.append(f"buy_swap_ratio_1h<{self.t.max_buy_swap_ratio_1h:g}")
        if reasons:
            return OnchainAdmissionDecision(
                accepted=False,
                reasons=tuple(reasons),
                buy_swap_ratio_1h=buy_ratio,
                creator_launches_24h=None,
                buy_swap_source=buy_source,
                creator_launch_source=None,
                rpc_used=rpc_used,
            )

        created_tokens = gmgn_created_tokens
        if created_tokens is None and created_tokens_loader is not None:
            try:
                created_tokens = await created_tokens_loader()
            except Exception:
                created_tokens = {}
        launch_count, launch_source, qualified = gmgn_creator_launches_24h(
            created_tokens or {},
            end_ts=entry_time,
            reject_at=self.t.max_creator_launches_24h,
        )
        if launch_count is None and not qualified:
            rpc_used = True
            try:
                launch_count, launch_source, _ = await self._rpc_creator_launches(
                    creator=creator,
                    entry_time=entry_time,
                )
            except Exception:
                launch_count = None
        if not qualified:
            if launch_count is None:
                reasons.append("missing_or_invalid:creator_launches_24h")
            elif not launch_count < self.t.max_creator_launches_24h:
                reasons.append(f"creator_launches_24h<{self.t.max_creator_launches_24h}")

        return OnchainAdmissionDecision(
            accepted=not reasons,
            reasons=tuple(reasons),
            buy_swap_ratio_1h=buy_ratio,
            creator_launches_24h=launch_count,
            buy_swap_source=buy_source,
            creator_launch_source=launch_source,
            rpc_used=rpc_used,
        )
