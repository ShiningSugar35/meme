import asyncio
import math
from datetime import datetime, timedelta, timezone
from ..strategy.filters import run_initial_filter, run_price_filter, _parse_creation_ts, _compute_age_minutes, evaluate_price_activity_rules, evaluate_smart_degen
from ..strategy.thresholds import compute_thresholds


def make_snapshot(**kwargs):
    base = {
        "type": "new_creation",
        "liquidity_usd": 20000,
        "top_10_holder_rate": 0.18,
        "top1_holder_rate": 0.04,
        "renounced_mint": 1,
        "renounced_freeze_account": 1,
        "max_rug_ratio": -0.1,
        "max_insider_ratio": -0.1,
        "max_entrapment_ratio": -0.1,
        "is_wash_trading": 0,
        "rat_trader_amount_rate": -0.1,
        "suspected_insider_hold_rate": 0.05,
        "max_bundler_rate": -0.1,
        "fresh_wallet_rate": 0.1,
        "sell_tax": 0.01,
        "has_social": 1,
        "creator_token_status": "creator_close",
        "dev_team_hold_rate": 0.0,
        "dev_token_burn_ratio": 1.0,
        "sniper_count": 1,
        "burn_status": "burn",
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(seconds=150)).isoformat(),
        "platform": "Pump.fun",
    }
    base.update(kwargs)
    return base


def test_filters_all_pass():
    snapshot = make_snapshot(liquidity_usd=30000)
    strategy_group = {"x": 0.15, "min_created": 120}
    res = asyncio.run(run_initial_filter(snapshot, strategy_group, datetime.now(timezone.utc)))
    assert res.passed is True


def test_missing_field_fails():
    s = make_snapshot()
    s.pop("liquidity_usd", None)
    strategy_group = {"x": 0.15, "min_created": 120}
    res = asyncio.run(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.name == "min_liquidity_usd" and not d.passed for d in res.details)


def test_has_social_required_for_small_x():
    s = make_snapshot(liquidity_usd=35000)
    s["has_social"] = 0
    strategy_group = {"x": 0.1, "min_created": 120}
    res = asyncio.run(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.name == "has_at_least_one_social" and not d.passed for d in res.details)


def test_x_thresholds_and_boundaries():
    x = 0.2
    s = make_snapshot(liquidity_usd=25000)
    thresh_liq = 6500 - 5000 * x
    s["liquidity_usd"] = thresh_liq - 1
    strategy_group = {"x": x, "min_created": 120}
    res = asyncio.run(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.name == "min_liquidity_usd" and not d.passed for d in res.details)


def test_top10_top1_boundaries():
    x = 0.2
    s = make_snapshot(liquidity_usd=25000)
    s["top_10_holder_rate"] = 0.14
    strategy_group = {"x": x, "min_created": 120}
    res = asyncio.run(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.name == "top_10_holder_rate_range" and not d.passed for d in res.details)

    # top1_holder is now only checked in holder API stage (Stage 3), not in local Stage 0


def test_pool_created_at_window_edges():
    now = datetime.now(timezone.utc)
    s = make_snapshot(liquidity_usd=30000)
    res = asyncio.run(run_initial_filter(s, {"x": 0.15, "min_created": 120}, now))
    assert res.passed is True


def test_platform_whitelist_and_creator_dev_rules():
    s = make_snapshot()
    s["platform"] = "unknown_platform"
    strategy_group = {"x": 0.15, "min_created": 120}
    res = asyncio.run(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.name == "platform" and not d.passed for d in res.details)


def test_core_field_missing_fails():
    s = make_snapshot()
    s.pop("renounced_mint", None)
    strategy_group = {"x": 0.15, "min_created": 120}
    res = asyncio.run(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.name == "renounced_mint" and not d.passed for d in res.details)


# --- New tests for price filter (Part 3) ---

def test_price_filter_swaps_divisor_age_30m():
    """Token age 30min, swaps_1h=120, swaps_5m=30, y=2.25: divisor should be ~6, not 12."""
    from ..strategy.filters import PriceFilterResult
    import math
    token = {
        "type": "new_creation",
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
    }
    latest = {
        "price": 0.001,
        "price_usd": 0.001,
        "swaps_5m": 30,
        "swaps_1h": 120,
        "price_1h": 0.0009,
    }
    sg = {"x": 0.2}
    res = asyncio.run(run_price_filter(token, sg, latest, []))
    swaps_detail = next((d for d in res.details if d.get("rule") == "swaps_5m_scaled"), None)
    assert swaps_detail is not None
    assert math.isclose(swaps_detail.get("divisor"), 6.0, rel_tol=0.01), f"Expected divisor~6, got {swaps_detail.get('divisor')}"
    assert swaps_detail.get("passed") is True, "swaps_5m=30 > threshold=20 should pass (new rule)"


def test_parse_creation_ts_seconds():
    token = {"creation_timestamp": 1710000000}
    ts, source, missing = _parse_creation_ts(token)
    assert ts is not None
    assert ts == 1710000000.0
    assert source == "creation_timestamp_s"
    assert missing is False


def test_parse_creation_ts_milliseconds():
    token = {"creation_timestamp": 1710000000000}
    ts, source, missing = _parse_creation_ts(token)
    assert ts is not None
    assert ts == 1710000000.0
    assert "ms" in source


def test_parse_creation_ts_pool_created_at_iso():
    dt = datetime.now(timezone.utc) - timedelta(minutes=45)
    token = {"pool_created_at": dt.isoformat()}
    ts, source, missing = _parse_creation_ts(token)
    assert ts is not None
    assert "iso" in source
    assert missing is False


def test_price_filter_price_change_age_45m():
    """Token age 45min -> should use kline if available, else computed_from_price_1h."""
    token = {
        "type": "new_creation",
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(),
    }
    latest = {
        "price": 0.0015,
        "price_usd": 0.0015,
        "swaps_5m": 100,
        "swaps_1h": 200,
        "price_1h": 0.001,
    }
    sg = {"x": 0.2}
    res = asyncio.run(run_price_filter(token, sg, latest, []))
    pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
    assert pct_detail is not None
    # Without klines, age<60 falls back to computed_from_price_1h
    assert pct_detail.get("source") == "computed_from_price_1h"
    assert pct_detail.get("age_mode") == "young_no_kline_fallback"
    # (0.0015 - 0.001) / 0.001 * 100 = 50%
    assert pct_detail.get("pct_change") == 50.0
    # threshold = 100 * (0.3 - 0.2) = 10.0
    assert math.isclose(pct_detail.get("threshold") or 0, 10.0, rel_tol=1e-9)
    # unit should be percent_points
    assert pct_detail.get("price_change_unit") == "percent_points"
    assert pct_detail.get("passed") is True, "50% > 25% should pass"


def test_price_change_unit_in_feature_vector():
    """Verify price_change_unit is present in feature_vector."""
    token = {
        "type": "new_creation",
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(),
    }
    latest = {
        "price": 0.001,
        "price_usd": 0.001,
        "swaps_5m": 100,
        "swaps_1h": 200,
        "price_1h": 0.00099,
    }
    sg = {"x": 0.2}
    res = asyncio.run(run_price_filter(token, sg, latest, []))
    assert res.feature_vector.get("price_change_unit") == "percent_points"
    assert res.feature_vector.get("price_change_source") == "computed_from_price_1h"


def test_platform_field_launchpad_platform():
    """GMGN raw item with only launchpad_platform should be recognized as platform."""
    from ..providers.gmgn_real import GMGNProvider
    raw = {"token_mint": "TEST111", "launchpad_platform": "Pump.fun", "type": "new_creation",
           "liquidity_usd": 30000, "renounced_mint": 1, "renounced_freeze_account": 1,
           "burn_status": "burn"}
    normalized = GMGNProvider._normalize_token_data(raw)
    assert normalized.get("platform") == "Pump.fun"
    assert normalized.get("launchpad") == "Pump.fun"


def test_platform_normalized_passes_risk_filter():
    """Token normalized from launchpad_platform should pass platform whitelist check."""
    raw = {"token_mint": "TEST222", "launchpad_platform": "Pump.fun", "type": "new_creation",
           "top_10_holder_rate": 0.18, "top1_holder_rate": 0.04, "liquidity_usd": 30000,
           "renounced_mint": 1, "renounced_freeze_account": 1,
           "max_rug_ratio": -0.1, "max_entrapment_ratio": -0.1,
           "is_wash_trading": 0, "rat_trader_amount_rate": -0.1,
           "suspected_insider_hold_rate": 0.05, "max_bundler_rate": -0.1,
           "fresh_wallet_rate": 0.1, "sell_tax": 0.01, "has_social": 1,
           "burn_status": "burn", "sniper_count": 1}
    from ..providers.gmgn_real import GMGNProvider
    snapshot = GMGNProvider._normalize_token_data(raw)
    res = asyncio.run(run_initial_filter(snapshot, {"x": 0.15}, datetime.now(timezone.utc)))
    assert res.passed is True, f"Should pass platform whitelist, got: {[d.name for d in res.details if not d.passed]}"


def test_price_filter_fallback_to_price_1h():
    token = {
        "type": "new_creation",
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat(),
    }
    latest = {
        "price": 0.0015,
        "price_usd": 0.0015,
        "swaps_5m": 100,
        "swaps_1h": 500,
        "price_1h": 0.001,
    }
    sg = {"x": 0.2}
    res = asyncio.run(run_price_filter(token, sg, latest, []))
    pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
    assert pct_detail is not None
    assert pct_detail.get("source") == "computed_from_price_1h"
    # (0.0015 - 0.001) / 0.001 * 100 = 50%
    assert pct_detail.get("pct_change") == 50.0


def test_price_filter_missing_price_fails():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()}
    latest = {}
    sg = {"x": 0.2}
    res = asyncio.run(run_price_filter(token, sg, latest, []))
    assert res.passed is False
    p_detail = next((d for d in res.details if d.get("rule") == "latest_price_present"), None)
    assert p_detail is not None
    assert p_detail.get("passed") is False


def test_compute_age_minutes():
    now_ts = datetime.now(timezone.utc).timestamp()
    creation_ts = now_ts - 30 * 60  # 30 min ago
    age = _compute_age_minutes(creation_ts)
    assert age is not None
    assert 29 <= age <= 31, f"Expected age ~30 min, got {age}"


def test_price_filter_swaps_from_token_fallback():
    token = {
        "type": "new_creation",
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(),
        "swaps_5m": 100,
        "swaps_1h": 500,
    }
    latest = {"price": 0.001, "price_usd": 0.001, "price_1h": 0.0009}
    sg = {"x": 0.2}
    res = asyncio.run(run_price_filter(token, sg, latest, []))
    swaps_detail = next((d for d in res.details if d.get("rule") == "swaps_5m_scaled"), None)
    assert swaps_detail is not None
    assert swaps_detail.get("swaps_5m") == 100
    assert swaps_detail.get("swaps_1h") == 500
    assert swaps_detail.get("source") == "token_snapshot"


# --- New tests for evaluate_price_activity_rules (Stage 1/2) ---

def test_stage_price_activity_rules_basic():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()}
    latest = {"price": 0.002, "price_usd": 0.002, "swaps_5m": 100, "swaps_1h": 500, "price_1h": 0.0015}
    sg = {"x": 0.2}
    res = asyncio.run(evaluate_price_activity_rules(token, sg, latest))
    assert isinstance(res.passed, bool)
    pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
    assert pct_detail is not None
    assert pct_detail.get("price_change_unit") == "percent_points"


def test_stage_price_activity_with_kline_fallback():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()}
    klines = [{"open_time": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
               "open": 0.001, "high": 0.002, "low": 0.001, "close": 0.002}]
    latest = {"price": 0.002, "price_usd": 0.002, "swaps_5m": 100, "swaps_1h": 500}
    sg = {"x": 0.2}
    res = asyncio.run(evaluate_price_activity_rules(token, sg, latest, klines=klines))
    pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
    assert pct_detail is not None
    assert pct_detail.get("source") == "kline_since_open"
    assert pct_detail.get("age_mode") == "young_kline_priority"
    # open=0.001, current=0.002 => (0.002-0.001)/0.001*100 = 100%
    assert pct_detail.get("pct_change") == 100.0


# --- New tests for evaluate_smart_degen (Stage 4) ---

def test_stage_smart_degen_ceil_count():
    sg = {"x": 0.2}
    holders = [
        {"amount_percentage": 0.03, "usd_value": 500},
        {"amount_percentage": 0.02, "usd_value": 300},
    ]
    # ceil(3 - 10*0.2) = ceil(1) = 1
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    detail = res.details[0]
    assert detail["rule"] == "smart_degen"
    assert detail["required_count"] == 1
    assert detail["degen_count"] == 2
    # max_pct=0.03 > 0.015, max_usd=500 > 200; min_pct=0.02 > 0.010
    assert detail["passed"] is True


def test_stage_smart_degen_normalizes_large_percents():
    sg = {"x": 0.05}
    holders = [{"amount_percentage": 1.6, "usd_value": 300}]
    # ceil(3 - 0.5) = ceil(2.5) = 3, only 1 holder < 3 -> fails count check
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    detail = res.details[0]
    assert detail["passed"] is False
    assert detail["degen_count"] == 1
    assert detail["required_count"] == 3


def test_stage_smart_degen_insufficient_holders():
    sg = {"x": 0.15}
    holders = [{"amount_percentage": 0.05, "usd_value": 1000}]
    # ceil(3-1.5) = ceil(1.5) = 2, only 1 holder -> fail
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    assert res.passed is False


def test_thresholds_x_02():
    import math
    t = compute_thresholds(0.2)
    assert math.isclose(t.max_risk_ratio, 0.15, rel_tol=1e-9)
    assert math.isclose(t.min_liquidity_usd, 5250.0, rel_tol=1e-9)
    assert math.isclose(t.min_holder_count, 29.0, rel_tol=1e-9)
    assert math.isclose(t.min_marketcap, 2900.0, rel_tol=1e-9)
    assert math.isclose(t.min_volume_24h, 1200.0, rel_tol=1e-9)
    assert math.isclose(t.price_change_1h_min_pct, 10.0, rel_tol=1e-9)
    assert math.isclose(t.sell_tax_max, 0.02, rel_tol=1e-9)
    assert math.isclose(t.sniper_count_max, 10.0, rel_tol=1e-9)
    assert math.isclose(t.top1_addr_type0_max, 0.051, rel_tol=1e-9)

