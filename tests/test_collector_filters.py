from __future__ import annotations

from backend.app.collector.filters import SafetyFilter, normalize_token


def valid_token() -> dict[str, object]:
    return {
        "address": "Token111111111111111111111111111111111111",
        "type": "new_creation",
        "launchpad": "Pump.fun",
        "symbol": "MEME",
        "quote_symbol": "SOL",
        "price": 0.00001,
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


def test_live_sniper_wallets_alias_maps_to_sniper_count() -> None:
    raw = valid_token()
    raw.pop("sniper_count")
    raw["sniper_wallets"] = 5
    normalized = normalize_token(raw, "new_creation")
    assert normalized["sniper_count"] == 5
    assert SafetyFilter().evaluate_required_facts(normalized).accepted


def test_top_10_holder_rate_uses_new_strict_range() -> None:
    safety = SafetyFilter()
    for accepted in (0.140001, 0.2, 0.249999):
        token = valid_token()
        token["top_10_holder_rate"] = accepted
        assert safety.evaluate(token).accepted
        assert safety.evaluate_discovery_prefilter(token).accepted

    for rejected in (0.139999, 0.14, 0.25, 0.250001):
        token = valid_token()
        token["top_10_holder_rate"] = rejected
        decision = safety.evaluate(token)
        assert not decision.accepted
        assert "top_10_holder_rate" in decision.reasons
        prefilter = safety.evaluate_discovery_prefilter(token)
        assert not prefilter.accepted
        assert "top_10_holder_rate" in prefilter.reasons


def test_volume_per_swap_must_be_strictly_above_31() -> None:
    safety = SafetyFilter()

    boundary = valid_token()
    boundary["swaps_1h"] = 20
    boundary["volume_1h"] = 620
    decision = safety.evaluate(boundary)
    assert not decision.accepted
    assert "volume_1h/swaps_1h" in decision.reasons

    accepted = valid_token()
    accepted["swaps_1h"] = 20
    accepted["volume_1h"] = 620.01
    assert safety.evaluate(accepted).accepted


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


def test_only_the_eight_frozen_business_launchpads_are_allowed() -> None:
    safety = SafetyFilter()
    for allowed in ("Pump.fun", "Moonshot", "bags", "heaven"):
        token = valid_token()
        token["launchpad"] = allowed
        assert safety.evaluate(token).accepted, allowed

    for blocked in ("unknown-pad", "memoo", "token_mill", "xstocks"):
        token = valid_token()
        token["launchpad"] = blocked
        assert "launchpad" in safety.evaluate(token).reasons


def test_discovery_prefilter_defers_missing_facts_but_rejects_known_failures() -> None:
    safety = SafetyFilter()
    sparse = {
        "launchpad": "Pump.fun",
        "symbol": "MEME",
        "liquidity": None,
        "holder_count": None,
        "marketcap": None,
        "top_10_holder_rate": None,
    }
    assert safety.evaluate_discovery_prefilter(sparse).accepted

    known_bad = dict(sparse, liquidity=4_800, holder_count=100, marketcap=10_000)
    decision = safety.evaluate_discovery_prefilter(known_bad)
    assert not decision.accepted
    assert "liquidity>4800" in decision.reasons
    assert "marketcap>10000" in decision.reasons


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


def test_age_must_be_strictly_between_five_and_240_minutes() -> None:
    safety = SafetyFilter()

    lower = valid_token()
    lower["age"] = 5.0
    decision = safety.evaluate(lower)
    assert not decision.accepted
    assert "age>5" in decision.reasons

    just_over_lower = valid_token()
    just_over_lower["age"] = 5.0001
    assert safety.evaluate(just_over_lower).accepted

    accepted = valid_token()
    accepted["age"] = 239.9999
    assert safety.evaluate(accepted).accepted

    upper = valid_token()
    upper["age"] = 240.0
    decision = safety.evaluate(upper)
    assert not decision.accepted
    assert "age<240" in decision.reasons


def test_missing_or_malformed_safety_facts_never_count_as_safe() -> None:
    safety = SafetyFilter()
    for field, bad_value in (
        ("rug_ratio", None),
        ("insider_ratio", "nan"),
        ("bundler_rate", -0.01),
        ("top_10_holder_rate", float("inf")),
        ("fresh_wallet_rate", -0.1),
        ("sell_tax", -0.01),
        ("buy_tax", "null"),
        ("sniper_count", -1),
        ("renounced_mint", None),
        ("renounced_freeze_account", "null"),
        ("is_wash_trading", "null"),
    ):
        token = valid_token()
        token[field] = bad_value
        readiness = safety.evaluate_required_facts(token)
        assert not readiness.accepted, field
        assert f"missing_or_invalid:{field}" in readiness.reasons
        assert not safety.evaluate(token).accepted, field


def test_required_filter_facts_are_complete_for_a_valid_token() -> None:
    assert SafetyFilter().evaluate_required_facts(valid_token()).accepted


def test_malformed_or_missing_holder_type_does_not_default_to_type_zero() -> None:
    safety = SafetyFilter()
    assert safety.top1_addr_type0_rate([{"rate": 0.04}]) is None
    assert safety.top1_addr_type0_rate([{"addr_type": "bad", "rate": 0.04}]) is None
    assert not safety.evaluate_top_holders([{"rate": 0.04}]).accepted

