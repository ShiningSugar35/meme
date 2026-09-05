from __future__ import annotations

import pytest

from backend.app.services.onchain_admission import (
    OnchainAdmissionService,
    classify_swap_transaction,
    gmgn_creator_launches_24h,
    is_creator_launch_transaction,
)
from backend.app.services.solana_rpc import RpcEndpoint, SolanaRpcPool


TOKEN = "token-mint"
POOL = "pool-address"
CREATOR = "creator-wallet"
ENTRY = 1_800_000_000


class PagingRpc(SolanaRpcPool):
    def __init__(self) -> None:
        super().__init__((RpcEndpoint("alchemy", 1, "https://solana-mainnet.g.alchemy.com/v2/test"),), client=object())
        self.page_calls = 0

    async def _rpc(self, endpoint, method, params):
        self.page_calls += 1
        if self.page_calls == 1:
            return {"data": [{"blockTime": ENTRY}], "paginationToken": "next"}, 1.0
        return {"data": [{"blockTime": ENTRY - 1}]}, 1.0


@pytest.mark.asyncio
async def test_rpc_history_stop_after_page_prevents_extra_pagination() -> None:
    rpc = PagingRpc()
    rows, provider, exhaustive = await rpc.transactions_for_address(
        CREATOR,
        start_ts=ENTRY - 86400,
        end_ts=ENTRY,
        max_pages=10,
        stop_after_page=lambda page: True,
    )
    assert rpc.page_calls == 1
    assert len(rows) == 1
    assert provider == "alchemy:1"
    assert exhaustive is False


def token_balance_tx(*, before: float, after: float, owner: str = "buyer") -> dict[str, object]:
    return {
        "transaction": {
            "message": {
                "accountKeys": [
                    {"pubkey": owner, "signer": True},
                    {"pubkey": "token-account", "signer": False},
                ],
                "instructions": [],
            }
        },
        "meta": {
            "preTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": TOKEN,
                    "owner": owner,
                    "uiTokenAmount": {"uiAmountString": str(before)},
                }
            ],
            "postTokenBalances": [
                {
                    "accountIndex": 1,
                    "mint": TOKEN,
                    "owner": owner,
                    "uiTokenAmount": {"uiAmountString": str(after)},
                }
            ],
        },
    }


def launch_tx(creator: str = CREATOR) -> dict[str, object]:
    return {
        "transaction": {
            "message": {
                "accountKeys": [{"pubkey": creator, "signer": True}],
                "instructions": [{"parsed": {"type": "initializeMint2", "info": {}}}],
            }
        },
        "meta": {"innerInstructions": []},
    }


def test_swap_classifier_uses_fee_payer_token_balance_direction() -> None:
    assert classify_swap_transaction(token_balance_tx(before=0, after=10), TOKEN) == 1
    assert classify_swap_transaction(token_balance_tx(before=10, after=2), TOKEN) == -1
    assert classify_swap_transaction(token_balance_tx(before=10, after=10), TOKEN) == 0


def test_creator_launch_requires_creator_as_fee_payer_and_mint_initialize() -> None:
    assert is_creator_launch_transaction(launch_tx(), CREATOR)
    assert not is_creator_launch_transaction(launch_tx("other"), CREATOR)
    non_launch = launch_tx()
    non_launch["transaction"]["message"]["instructions"] = [{"parsed": {"type": "transfer"}}]
    assert not is_creator_launch_transaction(non_launch, CREATOR)


def test_creator_launch_accepts_sponsored_transaction_when_creator_is_top_level_signer() -> None:
    tx = launch_tx("sponsor-wallet")
    tx["transaction"]["message"]["accountKeys"].append(
        {"pubkey": CREATOR, "signer": True}
    )
    assert is_creator_launch_transaction(tx, CREATOR)


def test_gmgn_creator_history_can_prove_pass_by_all_time_upper_bound() -> None:
    count, source, qualified = gmgn_creator_launches_24h(
        {"data": {"inner_count": 5, "open_count": 4, "tokens": []}},
        end_ts=ENTRY,
        reject_at=20,
    )
    assert count is None
    assert source == "gmgn_created_tokens_total_upper_bound"
    assert qualified is True


def test_gmgn_creator_history_exact_count_uses_create_timestamp() -> None:
    tokens = [
        {"create_timestamp": ENTRY - 60},
        {"create_timestamp": ENTRY - 3600},
        {"create_timestamp": ENTRY - 90000},
    ]
    count, source, qualified = gmgn_creator_launches_24h(
        {"data": {"inner_count": 2, "open_count": 1, "tokens": tokens}},
        end_ts=ENTRY,
        reject_at=2,
    )
    assert count == 2
    assert source == "gmgn_created_tokens_exact"
    assert qualified is False


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def transactions_for_address(
        self, address: str, *, start_ts: int, end_ts: int, max_pages: int, stop_after_page=None
    ):
        self.calls.append((address, start_ts, end_ts))
        if address == POOL:
            rows = [token_balance_tx(before=0, after=10) for _ in range(18)]
            rows.extend(token_balance_tx(before=10, after=0) for _ in range(2))
            return rows, "alchemy:1", True
        if address == CREATOR:
            rows = [launch_tx() for _ in range(3)]
            stopped = bool(stop_after_page and stop_after_page(rows))
            return rows, "alchemy:1", not stopped
        raise AssertionError(address)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_onchain_admission_falls_back_to_rpc_and_shortens_swap_window_for_young_token() -> None:
    rpc = FakeRpc()
    service = OnchainAdmissionService(rpc=rpc)
    decision = await service.evaluate(
        token_mint=TOKEN,
        pool_address=POOL,
        creator=CREATOR,
        entry_time=ENTRY,
        age_minutes=30,
        gmgn_buys_1h=None,
        gmgn_swaps_1h=None,
        gmgn_created_tokens={},
    )
    assert decision.accepted
    assert decision.buy_swap_ratio_1h == pytest.approx(0.9)
    assert decision.creator_launches_24h == 3
    assert decision.rpc_used
    assert rpc.calls[0] == (POOL, ENTRY - 30 * 60, ENTRY)
    assert rpc.calls[1] == (CREATOR, ENTRY - 24 * 60 * 60, ENTRY)


class NoRpcAllowed:
    async def transactions_for_address(self, *args, **kwargs):
        raise AssertionError("RPC should not be used when GMGN proves both facts")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_onchain_admission_uses_gmgn_before_rpc() -> None:
    service = OnchainAdmissionService(rpc=NoRpcAllowed())
    decision = await service.evaluate(
        token_mint=TOKEN,
        pool_address=POOL,
        creator=CREATOR,
        entry_time=ENTRY,
        age_minutes=90,
        gmgn_buys_1h=90,
        gmgn_swaps_1h=100,
        gmgn_created_tokens={"data": {"inner_count": 4, "open_count": 3, "tokens": []}},
    )
    assert decision.accepted
    assert decision.buy_swap_source == "gmgn_buys_1h_swaps_1h"
    assert decision.creator_launch_source == "gmgn_created_tokens_total_upper_bound"
    assert decision.rpc_used is False


@pytest.mark.asyncio
async def test_buy_ratio_boundary_is_strictly_below_95_percent() -> None:
    service = OnchainAdmissionService(rpc=NoRpcAllowed())
    creator_loader_called = False

    async def creator_loader():
        nonlocal creator_loader_called
        creator_loader_called = True
        raise AssertionError("creator history must not load after buy-ratio rejection")

    decision = await service.evaluate(
        token_mint=TOKEN,
        pool_address=POOL,
        creator=CREATOR,
        entry_time=ENTRY,
        age_minutes=90,
        gmgn_buys_1h=95,
        gmgn_swaps_1h=100,
        created_tokens_loader=creator_loader,
    )
    assert not decision.accepted
    assert "buy_swap_ratio_1h<0.95" in decision.reasons
    assert creator_loader_called is False
