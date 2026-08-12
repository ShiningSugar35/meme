from __future__ import annotations

from backend.app.collector.filters import SafetyFilter


def valid_token() -> dict[str, object]:
    return {
        "type": "new_creation",
        "launchpad": "Pump.fun",
        "symbol": "MEME",
        "quote_symbol": "SOL",
        "rug_ratio": 0.1,
        "insider_ratio": 0.1,
        "bundler_rate": 0.1,
        "liquidity": 10_000,
        "top_10_holder_rate": 0.2,
        "fresh_wallet_rate": 0.1,
        "burn_status": "burn",
        "renounced_mint": 1,
        "renounced_freeze_account": 1,
        "is_wash_trading": False,
        "rat_trader_amount_rate": 0.1,
        "holder_count": 100,
        "marketcap": 20_000,
        "sell_tax": 0.01,
        "buy_tax": 0.01,
        "sniper_count": 5,
        "age": 10,
        "swaps_1h": 20,
        "volume_1h": 10_000,
        "volume": 10_000,
        "smart_degen_count": 1,
        "renowned_count": 0,
    }


def test_valid_token_passes_all_local_filters() -> None:
    assert SafetyFilter().evaluate(valid_token()).accepted


def test_strict_boundaries_match_readme() -> None:
    token = valid_token()
    token["fresh_wallet_rate"] = 0.2
    token["liquidity"] = 4_800
    token["swaps_1h"] = 19
    decision = SafetyFilter().evaluate(token)
    assert not decision.accepted
    assert "fresh_wallet_rate<0.2" in decision.reasons
    assert "liquidity>4800" in decision.reasons
    assert "swaps_1h>19" in decision.reasons


def test_completed_pool_receives_no_filter_relaxation() -> None:
    token = valid_token()
    token.update(type="completed", renounced_mint=None, renounced_freeze_account=None)
    decision = SafetyFilter().evaluate(token)
    assert not decision.accepted
    assert "renounced_mint" in decision.reasons
    assert "renounced_freeze_account" in decision.reasons


def test_only_the_eight_frozen_launchpads_are_allowed() -> None:
    for blocked in ("unknown-pad", "memoo", "token_mill"):
        token = valid_token()
        token["launchpad"] = blocked
        decision = SafetyFilter().evaluate(token)
        assert "launchpad" in decision.reasons


def test_quote_asset_is_limited_to_sol_usdc_usdt() -> None:
    token = valid_token()
    token["quote_symbol"] = "WETH"
    decision = SafetyFilter().evaluate(token)
    assert "quote_asset_not_allowed" in decision.reasons


def test_major_assets_cannot_be_target_tokens() -> None:
    token = valid_token()
    token["symbol"] = "USDC"
    decision = SafetyFilter().evaluate(token)
    assert "target_asset_excluded" in decision.reasons


def test_top1_addr_type_zero_uses_strict_range() -> None:
    safety = SafetyFilter()
    assert safety.evaluate_top_holders([
        {"addr_type": 1, "rate": 0.5},
        {"addr_type": 0, "rate": 0.04},
    ]).accepted
    assert not safety.evaluate_top_holders([{"addr_type": 0, "rate": 0.028}]).accepted
    assert not safety.evaluate_top_holders([{"addr_type": 0, "rate": 0.056}]).accepted

