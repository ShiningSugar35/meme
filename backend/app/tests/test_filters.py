import asyncio
import math
from datetime import datetime, timedelta, timezone
from ..strategy.filters import (
    run_entry_local_risk_filter, evaluate_price_activity_rules, evaluate_smart_degen,
    run_holding_risk_filter, evaluate_top1_holder, run_risk_filter,
    _normalize_pct,
    _parse_creation_ts, _compute_age_minutes,
)
from ..strategy.thresholds import compute_thresholds, entry_size_usd, StrategyThresholds
from ..providers.gmgn_real import GMGNProvider


def make_snapshot(**kwargs):
    base = {
        "type": "new_creation",
        "liquidity_usd": 5000,
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
        "holder_count": 80,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Threshold tests (x-based)
# ---------------------------------------------------------------------------

def test_thresholds_x_02():
    t = compute_thresholds(0.2)
    assert math.isclose(t.common_risk, 0.15, rel_tol=1e-9)
    assert math.isclose(t.min_liquidity, 4500.0, rel_tol=1e-9)
    assert math.isclose(t.max_top_holder_rate, 0.275, rel_tol=1e-9)
    assert math.isclose(t.min_holder_count, 24.0, rel_tol=1e-9)
    assert math.isclose(t.min_marketcap, 4950.0, rel_tol=1e-9)
    assert math.isclose(t.min_volume_24h, 1200.0, rel_tol=1e-9)
    assert math.isclose(t.volume_per_swap_1h_min, 27.0, rel_tol=1e-9)
    assert math.isclose(t.price_change_1h_min_pct, -5.0, rel_tol=1e-9)
    assert math.isclose(t.price_change_1h_max_pct, 50.0, rel_tol=1e-9)
    assert math.isclose(t.sell_tax_max, 0.02, rel_tol=1e-9)
    assert math.isclose(t.sniper_count_max, 15.0, rel_tol=1e-9)          # 持仓风控 75*0.2
    assert math.isclose(t.entry_sniper_count_max, 10.0, rel_tol=1e-9)     # 买入条件 50*0.2
    assert math.isclose(t.top1_addr_type0_max, 0.051, rel_tol=1e-9)
    assert math.isclose(t.top1_addr_type0_min, 0.018, rel_tol=1e-9)
    assert math.isclose(t.min_liquidity_holder_ratio, 40.0, rel_tol=1e-9)
    assert math.isclose(t.swaps_1h_min, 11.0, rel_tol=1e-9)
    assert math.isclose(t.price_range_24h_percentile_min, 0.05, rel_tol=1e-9)
    assert math.isclose(t.price_range_24h_percentile_max, 0.35, rel_tol=1e-9)


def test_thresholds_x_01():
    t = compute_thresholds(0.1)
    assert math.isclose(t.common_risk, 0.10, rel_tol=1e-9)
    assert math.isclose(t.min_volume_24h, 1400.0, rel_tol=1e-9)
    assert math.isclose(t.price_change_1h_min_pct, -10.0, rel_tol=1e-9)
    assert math.isclose(t.price_change_1h_max_pct, 55.0, rel_tol=1e-9)


def test_thresholds_x_03():
    t = compute_thresholds(0.3)
    assert math.isclose(t.common_risk, 0.20, rel_tol=1e-9)
    assert math.isclose(t.min_volume_24h, 1000.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Entry local risk filter tests
# ---------------------------------------------------------------------------

def test_entry_risk_all_pass():
    s = make_snapshot()
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert res.passed is True


def test_entry_risk_burn_fails():
    s = make_snapshot(burn_status="not_burn")
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "burn_status" and not d.passed for d in res.details)


def test_entry_risk_wash_trading_fails():
    s = make_snapshot(is_wash_trading=1)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "is_wash_trading" and not d.passed for d in res.details)


def test_entry_risk_sniper_fails():
    s = make_snapshot(sniper_count=99)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "sniper_count" and not d.passed for d in res.details)


# ---------------------------------------------------------------------------
# Top1 holder test
# ---------------------------------------------------------------------------

def test_top1_holder_fails():
    res = evaluate_top1_holder({"addr_type": 0, "top1_holder_rate": 0.06}, 0.2)
    assert res.passed is False


def test_top1_holder_passes():
    res = evaluate_top1_holder({"addr_type": 0, "top1_holder_rate": 0.04}, 0.2)
    assert res.passed is True


def test_top1_holder_missing():
    res = evaluate_top1_holder(None, 0.2)
    assert res.passed is False


# ---------------------------------------------------------------------------
# Price filter tests
# ---------------------------------------------------------------------------

def _pass_range_klines(current_price: float):
    return [{"open_time": datetime.now(timezone.utc).isoformat(),
             "open": current_price, "high": current_price * 3,
             "low": current_price * 0.5, "close": current_price}]


def test_swaps_1h_below_threshold_fails():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()}
    latest = {"price": 0.001, "price_usd": 0.001, "swaps_1h": 8, "price_1h": 0.0009,
              "volume_1h": 500}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=_pass_range_klines(0.001)))
    swaps_detail = next((d for d in res.details if d.get("rule") == "swaps_1h_min"), None)
    assert swaps_detail is not None
    # x=0.2 → threshold = 7+20*0.2 = 11, 8 < 11 should fail
    assert swaps_detail.get("passed") is False, "swaps_1h below threshold should fail"


def test_volume_per_swap_fails():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()}
    latest = {"price": 0.001, "price_usd": 0.001, "swaps_1h": 200,
              "price_1h": 0.0009, "volume_1h": 1000}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=_pass_range_klines(0.001)))
    vps_detail = next((d for d in res.details if d.get("rule") == "volume_per_swap_1h"), None)
    assert vps_detail is not None
    assert vps_detail.get("vps") == 5.0
    # threshold = 23 + 20*0.2 = 27, 5.0 < 27 should fail
    assert vps_detail.get("passed") is False


def test_volume_per_swap_zero_when_no_1h_swaps():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()}
    latest = {"price": 0.001, "price_usd": 0.001, "swaps_1h": 0,
              "price_1h": 0.0009, "volume_1h": 0}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=_pass_range_klines(0.001)))
    vps_detail = next((d for d in res.details if d.get("rule") == "volume_per_swap_1h"), None)
    assert vps_detail is not None
    assert vps_detail.get("vps") == 0.0
    assert vps_detail.get("value") == 0.0
    assert vps_detail.get("passed") is False


def test_price_change_threshold():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()}
    latest = {"price": 0.0011, "price_usd": 0.0011, "swaps_1h": 500, "price_1h": 0.001,
              "volume_1h": 14000}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=_pass_range_klines(0.0011)))
    pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
    assert pct_detail is not None
    # (0.0011 - 0.001) / 0.001 * 100 = 10%
    assert math.isclose(pct_detail.get("pct_change") or 0, 10.0, rel_tol=1e-9)
    # lower_threshold = 50 * (0.2 - 0.3) = -5
    assert math.isclose(pct_detail.get("lower_threshold") or 0, -5.0, rel_tol=1e-9)
    # upper_threshold = 60 - 50 * 0.2 = 50
    assert math.isclose(pct_detail.get("upper_threshold") or 0, 50.0, rel_tol=1e-9)
    assert pct_detail.get("passed") is True, "10% within (-10, 50) should pass"


def test_missing_price_fails():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, {}))
    assert res.passed is False


def test_swaps_fallback_from_token():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(),
             "swaps_1h": 500, "volume_1h": 14000}
    latest = {"price": 0.001, "price_usd": 0.001, "price_1h": 0.0009}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=_pass_range_klines(0.001)))
    swaps_detail = next((d for d in res.details if d.get("rule") == "swaps_1h_min"), None)
    assert swaps_detail.get("swaps_1h") == 500
    assert swaps_detail.get("source") == "token_snapshot"


def test_kline_fallback():
    token = {"pool_created_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()}
    klines = [{"open_time": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
               "open": 0.001, "high": 0.006, "low": 0.001, "close": 0.002}]
    latest = {"price": 0.002, "price_usd": 0.002, "swaps_1h": 500, "volume_1h": 14000}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=klines))
    pct_detail = next((d for d in res.details if d.get("rule") == "price_change_1h"), None)
    assert pct_detail.get("source") == "kline_since_open"


# ---------------------------------------------------------------------------
# Smart degen tests
# ---------------------------------------------------------------------------

def test_smart_degen_passes():
    sg = {"x": 0.2}
    holders = [{"amount_percentage": 0.03, "usd_value": 500}, {"amount_percentage": 0.02, "usd_value": 300}]
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    detail = res.details[0]
    assert detail["passed"] is True


def test_smart_degen_large_pct_normalized():
    sg = {"x": 0.05}
    holders = [{"amount_percentage": 1.6, "usd_value": 300}]
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    detail = res.details[0]
    # min_smart_degen_count = max(0, 1.5-10*0.05) = 1.0, floor(1.0)+1 = 2
    assert detail["required_count"] == 2
    assert detail["passed"] is False


def test_smart_degen_too_few_holders():
    sg = {"x": 0}
    holders = [{"amount_percentage": 0.05, "usd_value": 1000}]
    # min_smart_degen_count = max(0, 1.5-0) = 1.5, floor(1.5)+1 = 2, only 1 holder -> fail
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    assert res.passed is False


def test_smart_degen_passes_with_new_thresholds():
    sg = {"x": 0.2}
    holders = [{"amount_percentage": 0.02, "usd_value": 160}, {"amount_percentage": 0.015, "usd_value": 60}]
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    detail = res.details[0]
    # max_usd=160 > 40, max_pct_norm=0.02 > 0.004 -> max_ok=True
    # min_usd=60 > 20, min_pct_norm=0.015 > 0.002 -> min_ok=True
    assert detail["passed"] is True


# ---------------------------------------------------------------------------
# Smart degen not-required tests (x > 0.15)
# ---------------------------------------------------------------------------

def test_evaluate_smart_degen_not_required_x_02_passes_with_empty_holders():
    sg = {"id": 1, "x": 0.2}
    res = asyncio.run(evaluate_smart_degen(sg, []))
    assert res.passed is True
    assert res.feature_vector["smart_degen_required"] is False
    assert res.details[0]["rule"] == "smart_degen_not_required"


def test_evaluate_smart_degen_required_x_015_fails_with_empty_holders():
    sg = {"id": 1, "x": 0.15}
    res = asyncio.run(evaluate_smart_degen(sg, []))
    assert res.passed is False


def test_evaluate_smart_degen_required_x_01_passes_with_valid_holder():
    sg = {"id": 1, "x": 0.1}
    holders = [{"amount_percentage": 0.005, "usd_value": 50}]
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    assert res.passed is True


# ---------------------------------------------------------------------------
# Creation time parsing tests
# ---------------------------------------------------------------------------

def test_parse_creation_ts_seconds():
    ts, source, missing = _parse_creation_ts({"creation_timestamp": 1710000000})
    assert math.isclose(ts, 1710000000.0, rel_tol=1e-9)


def test_parse_creation_ts_pool_iso():
    dt = datetime.now(timezone.utc) - timedelta(minutes=45)
    ts, source, missing = _parse_creation_ts({"pool_created_at": dt.isoformat()})
    assert ts is not None


def test_compute_age_minutes():
    now_ts = datetime.now(timezone.utc).timestamp()
    age = _compute_age_minutes(now_ts - 30 * 60)
    assert 29 <= age <= 31


# ---------------------------------------------------------------------------
# Holding risk filter tests
# ---------------------------------------------------------------------------

def test_holding_risk_fails_rug():
    s = make_snapshot(max_rug_ratio=0.5)
    s.pop("top_10_holder_rate", None)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "rug_ratio" and not d.passed for d in res.details)


def test_holding_risk_passes():
    s = make_snapshot(top_10_holder_rate=0.2, holder_count=30)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert res.passed is True


# ---------------------------------------------------------------------------
# Entry sizing tests ($150)
# ---------------------------------------------------------------------------

def test_entry_size_liquidity_5250():
    size = entry_size_usd(5250, 0.2)
    assert math.isclose(size, min(5250 * 0.01, 50), rel_tol=1e-9)


def test_entry_size_liquidity_10000():
    size = entry_size_usd(10000, 0.2)
    # 10000 * 0.01 = 100, capped by 50
    assert math.isclose(size, 50.0, rel_tol=1e-9)


def test_entry_size_liquidity_20000():
    size = entry_size_usd(20000, 0.2)
    # 20000 * 0.01 = 200, capped by 50
    assert math.isclose(size, 50.0, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Platform normalization test
# ---------------------------------------------------------------------------

def test_platform_field_launchpad_platform():
    raw = {"token_mint": "TEST111", "launchpad_platform": "Pump.fun", "type": "new_creation",
           "liquidity_usd": 30000, "renounced_mint": 1, "renounced_freeze_account": 1, "burn_status": "burn"}
    normalized = GMGNProvider._normalize_token_data(raw)
    assert normalized.get("platform") == "Pump.fun"
    assert normalized.get("launchpad") == "Pump.fun"


# ---------------------------------------------------------------------------
# Enhanced entry local risk tests
# ---------------------------------------------------------------------------

def test_entry_risk_renounced_mint_0_fails():
    s = make_snapshot(renounced_mint=0)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "renounced_mint" and not d.passed for d in res.details)


def test_entry_risk_renounced_mint_missing_fails():
    s = make_snapshot()
    s.pop("renounced_mint", None)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "renounced_mint" and not d.passed and d.missing for d in res.details)


def test_entry_risk_renounced_freeze_0_fails():
    s = make_snapshot(renounced_freeze_account=0)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "renounced_freeze_account" and not d.passed for d in res.details)


def test_entry_risk_sell_tax_missing_fails():
    s = make_snapshot()
    s.pop("sell_tax", None)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "sell_tax" and not d.passed and d.missing for d in res.details)


def test_entry_risk_burn_not_burn_fails():
    s = make_snapshot(burn_status="not_burn")
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "burn_status" and not d.passed for d in res.details)


def test_entry_risk_x_014_no_social_fails():
    s = make_snapshot(x=0.14)
    s.pop("has_social", None)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.14}))
    assert any("social" in d.name and not d.passed for d in res.details)


# ---------------------------------------------------------------------------
# Enhanced holding risk tests
# ---------------------------------------------------------------------------

def test_holding_risk_rug_fails():
    s = make_snapshot(max_rug_ratio=0.5)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "rug_ratio" and not d.passed for d in res.details)


def test_holding_risk_entrapment_fails():
    s = make_snapshot(max_entrapment_ratio=0.5)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "entrapment_ratio" and not d.passed for d in res.details)


def test_holding_risk_insider_fails():
    s = make_snapshot(suspected_insider_hold_rate=0.5)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "suspected_insider_hold_rate" and not d.passed for d in res.details)


def test_holding_risk_rat_trader_fails():
    s = make_snapshot(rat_trader_amount_rate=0.3)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "rat_trader_amount_rate" and not d.passed for d in res.details)


def test_holding_risk_bundler_fails():
    s = make_snapshot(max_bundler_rate=0.5)
    s.pop("top_10_holder_rate", None)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "bundler_trader_amount_rate" and not d.passed for d in res.details)


def test_holding_risk_top_holder_out_of_range_fails():
    s = make_snapshot(top_10_holder_rate=0.5)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any("top_10_holder_rate" in d.name and not d.passed for d in res.details)


def test_holding_risk_holder_count_too_low_fails():
    s = make_snapshot(top_10_holder_rate=0.2, holder_count=1)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "holder_count" and not d.passed for d in res.details)


def test_holding_risk_holder_count_too_high_fails():
    s = make_snapshot(top_10_holder_rate=0.2, holder_count=1000)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "holder_count" and not d.passed for d in res.details)


def test_holding_risk_holder_count_in_range_passes():
    s = make_snapshot(top_10_holder_rate=0.2, holder_count=200)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    d = next((d for d in res.details if d.name == "holder_count"), None)
    assert d is not None
    assert d.passed is True


def test_holding_risk_fresh_wallet_fails():
    s = make_snapshot(top_10_holder_rate=0.2, fresh_wallet_rate=0.5)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "fresh_wallet_rate" and not d.passed for d in res.details)


def test_holding_risk_creator_balance_fails():
    s = make_snapshot(top_10_holder_rate=0.2, dev_team_hold_rate=0.5)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "creator_balance_rate" and not d.passed for d in res.details)


def test_holding_risk_sniper_fails():
    s = make_snapshot(top_10_holder_rate=0.2, sniper_count=99)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "sniper_count" and not d.passed for d in res.details)


def test_holding_risk_wash_trading_fails():
    s = make_snapshot(top_10_holder_rate=0.2, is_wash_trading=1)
    res = asyncio.run(run_holding_risk_filter(s, {"x": 0.2}))
    assert any(d.name == "is_wash_trading" and not d.passed for d in res.details)


# ---------------------------------------------------------------------------
# Smart degen normalization tests
# ---------------------------------------------------------------------------

def test_normalize_pct_decimal():
    n, src = _normalize_pct(0.015)
    assert math.isclose(n, 0.015)
    assert src == "raw_decimal"


def test_normalize_pct_large():
    n, src = _normalize_pct(1.5)
    assert math.isclose(n, 0.015)
    assert src == "pct_divided_by_100"


def test_normalize_pct_none():
    n, src = _normalize_pct(None)
    assert n is None


def test_smart_degen_015_pct_format():
    sg = {"x": 0.1}  # x <= 0.15 才走完整评估路径
    holders = [{"amount_percentage": 0.03, "usd_value": 300}, {"amount_percentage": 0.02, "usd_value": 200}]
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    assert res.passed is True
    detail = res.details[0]
    h = detail["holdings"]
    assert h["max_holder_pct_norm_src"] == "raw_decimal"
    assert math.isclose(h["max_holder_pct_norm"], 0.03)


def test_smart_degen_15_pct_format():
    sg = {"x": 0.1}  # x <= 0.15 才走完整评估路径
    holders = [{"amount_percentage": 1.6, "usd_value": 300}, {"amount_percentage": 1.2, "usd_value": 200}]
    res = asyncio.run(evaluate_smart_degen(sg, holders))
    assert res.passed is True
    detail = res.details[0]
    h = detail["holdings"]
    assert h["max_holder_pct_norm_src"] == "pct_divided_by_100"
    assert math.isclose(h["max_holder_pct_norm"], 0.016)


# ---------------------------------------------------------------------------
# Trenches parameter tests
# ---------------------------------------------------------------------------

def test_trench_filters_x_02():
    t = StrategyThresholds.compute(0.2)
    filters = t.to_trench_filters()
    assert math.isclose(filters["min_liquidity"], 4500.0, rel_tol=1e-9)
    assert math.isclose(filters["max_rug_ratio"], 0.15, rel_tol=1e-9)
    assert math.isclose(filters["min_top_holder_rate"], 0.055, rel_tol=1e-9)
    assert math.isclose(filters["max_top_holder_rate"], 0.275, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Legacy alias test: run_risk_filter points to run_holding_risk_filter
# ---------------------------------------------------------------------------

def test_run_risk_filter_alias():
    assert run_risk_filter is run_holding_risk_filter


# ---------------------------------------------------------------------------
# Import smoke tests
# ---------------------------------------------------------------------------

def test_import_strategy_filters():
    from ..strategy import filters as _f
    assert hasattr(_f, "run_entry_local_risk_filter")
    assert hasattr(_f, "run_holding_risk_filter")
    assert hasattr(_f, "evaluate_smart_degen")
    assert hasattr(_f, "evaluate_top1_holder")


def test_import_discovery_runner():
    from ..runners import discovery_runner as _d
    assert hasattr(_d, "DiscoveryRunner")


def test_import_position_risk_runner():
    from ..runners import position_risk_runner as _p
    assert hasattr(_p, "PositionRiskRunner")


def test_requires_smart_degen_for_x_none_safe():
    """requires_smart_degen_for_x should handle None safely (default x=0.2)."""
    from ..strategy.thresholds import requires_smart_degen_for_x
    # x=0.2 > 0.15 → smart degen NOT required
    assert requires_smart_degen_for_x(0.2) is False
    assert requires_smart_degen_for_x(0.200001) is False
    # x <= 0.15 → smart degen IS required
    assert requires_smart_degen_for_x(0.15) is True
    assert requires_smart_degen_for_x(0.1) is True


# ============================================================================
# Price range 24h percentile tests (P0-1)
# ============================================================================

def test_price_range_percentile_passes_with_klines_x02():
    """x=0.2: percentile=0.25 in range (0, 0.35) => passed."""
    from ..strategy.filters import evaluate_price_activity_rules
    token = {"pool_created_at": "2025-01-01T00:00:00Z"}
    latest = {"price_usd": 1.5}
    klines = [
        {"high": 3.0, "low": 1.0, "open": 1.2, "close": 1.5},
        {"high": 2.8, "low": 1.1, "open": 1.3, "close": 1.4},
    ]
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=klines))
    pct_detail = next(d for d in res.details if d["rule"] == "price_range_24h_percentile")
    assert pct_detail["passed"] is True
    assert math.isclose(pct_detail["percentile"], 0.25, rel_tol=1e-9)
    assert pct_detail["source"] == "kline_24h"


def test_price_range_percentile_fails_when_too_high_x02():
    """x=0.2: percentile=0.5 > 0.35 => failed."""
    from ..strategy.filters import evaluate_price_activity_rules
    token = {"pool_created_at": "2025-01-01T00:00:00Z"}
    latest = {"price_usd": 2.0}
    klines = [
        {"high": 3.0, "low": 1.0, "open": 1.2, "close": 2.0},
    ]
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=klines))
    pct_detail = next(d for d in res.details if d["rule"] == "price_range_24h_percentile")
    assert pct_detail["passed"] is False
    assert math.isclose(pct_detail["percentile"], 0.5, rel_tol=1e-9)


def test_price_range_percentile_missing_without_klines():
    """Without klines, percentile is missing => failed."""
    from ..strategy.filters import evaluate_price_activity_rules
    token = {"pool_created_at": "2025-01-01T00:00:00Z"}
    latest = {"price_usd": 1.5}
    res = asyncio.run(evaluate_price_activity_rules(token, {"x": 0.2}, latest, klines=None))
    pct_detail = next(d for d in res.details if d["rule"] == "price_range_24h_percentile")
    assert pct_detail["passed"] is False
    assert pct_detail["data_unavailable"] is True
    assert pct_detail["source"] == "missing"


# ============================================================================
# Entry sizing spec tests (P0-3)
# ============================================================================

def test_sim_entry_size_matches_spec():
    from ..strategy.sizing import compute_entry_size_usd
    # min(1% liquidity, $50)
    assert asyncio.run(compute_entry_size_usd(5000)) == 50.0
    assert asyncio.run(compute_entry_size_usd(20000)) == 50.0


def test_live_entry_size_caps_wallet_balance():
    from ..strategy.sizing import compute_entry_size_usd
    # min(1% liquidity, $100, wallet_balance)
    size = asyncio.run(compute_entry_size_usd(20000, is_live=True, wallet_balance_usd=30))
    assert size == 30.0


# ============================================================================
# Merged-context regression tests (P0 — _usd_to_sol_amount with merged ctx)
# ============================================================================

def test_derive_sol_usd_price_from_price_fields():
    from ..trading.executor import _derive_sol_usd_price
    ctx = {"price_usd": 0.5, "price_sol": 0.0005}
    sol_usd = _derive_sol_usd_price(ctx, ctx)
    assert sol_usd == 1000.0


def test_derive_sol_usd_price_from_liquidity_fallback():
    from ..trading.executor import _derive_sol_usd_price
    ctx = {"liquidity_usd": 5000, "sol_side_liquidity": 5.0}
    sol_usd = _derive_sol_usd_price(ctx, ctx)
    assert sol_usd == 1000.0


def test_derive_sol_usd_price_missing_fields():
    from ..trading.executor import _derive_sol_usd_price
    ctx = {}
    sol_usd = _derive_sol_usd_price(ctx, ctx)
    assert sol_usd is None


def test_usd_to_sol_amount_with_merged_context():
    """When both params get the same merged ctx, price_sol/price_usd are found."""
    from ..trading.executor import _usd_to_sol_amount
    ctx = {"price_usd": 0.5, "price_sol": 0.0005}
    size_sol = _usd_to_sol_amount(50.0, ctx, ctx)
    # 50 USD / (0.5/0.0005 = 1000) = 0.05 SOL
    assert size_sol == 0.05


def test_usd_to_sol_amount_fallback_uses_liquidity():
    """When price fields are missing, merged liquidity fields are used."""
    from ..trading.executor import _usd_to_sol_amount
    ctx = {"liquidity_usd": 5000, "sol_side_liquidity": 5.0}
    size_sol = _usd_to_sol_amount(50.0, ctx, ctx)
    # 50 USD / (5000/5.0 = 1000) = 0.05 SOL
    assert size_sol == 0.05


# ============================================================================
# Entry risk liquidity/holder_count regression test
# ============================================================================

def test_entry_risk_liquidity_holder_ratio_fails():
    from ..strategy.filters import run_entry_local_risk_filter
    s = make_snapshot(liquidity_usd=500, holder_count=80)
    res = asyncio.run(run_entry_local_risk_filter(s, {"x": 0.2}))
    # 500/80 = 6.25; for x=0.2, min_liquidity_holder_ratio=70-100*0.2=50
    # 6.25 <= 50 => fails
    assert any(d.name == "liquidity_holder_ratio" and not d.passed for d in res.details)


# ============================================================================
# _build_entry_market_context — snapshot columns priority test
# ============================================================================

def test_build_entry_market_context_snapshot_columns_before_raw_json():
    """Snapshot standard columns are read before raw_json.

    latest has price_usd; snapshot columns have liquidity_usd, price_sol & sol_side_liquidity.
    The merged ctx should contain all four without needing raw_json.
    """
    from unittest.mock import AsyncMock
    from ..trading.executor import TradingPipeline

    mock_repo = AsyncMock()
    mock_repo.get_token_metric_snapshot.return_value = {
        "liquidity_usd": 5000,
        "price_sol": 0.0005,
        "sol_side_liquidity": 5.0,
    }
    mock_repo.get_token.return_value = {}

    gmgn = AsyncMock()
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.repo = mock_repo
    pipeline.gmgn = gmgn

    latest = {"price_usd": 0.5}
    ctx = asyncio.run(pipeline._build_entry_market_context(
        "TESTMINT", latest, snapshot_id=1
    ))
    assert ctx["price_usd"] == 0.5
    assert ctx["liquidity_usd"] == 5000
    assert ctx["price_sol"] == 0.0005
    assert ctx["sol_side_liquidity"] == 5.0


def test_build_entry_market_context_fallback_chain():
    """When latest is empty, full chain is tried: columns → raw_json → tokens."""
    from unittest.mock import AsyncMock
    from ..trading.executor import TradingPipeline

    mock_repo = AsyncMock()
    mock_repo.get_token_metric_snapshot.return_value = {
        "liquidity_usd": None,
        "price_usd": None,
        "raw_json": '{"liquidity_usd": 3000}',
    }
    mock_repo.get_token.return_value = {"latest_price_usd": 0.3}

    gmgn = AsyncMock()
    pipeline = TradingPipeline.__new__(TradingPipeline)
    pipeline.repo = mock_repo
    pipeline.gmgn = gmgn

    ctx = asyncio.run(pipeline._build_entry_market_context(
        "TESTMINT2", {}, snapshot_id=2
    ))
    # liquidity_usd from raw_json (columns were None)
    assert ctx["liquidity_usd"] == 3000
    # price_usd from tokens table (latest was empty, columns None, raw_json no price_usd)
    assert ctx["price_usd"] == 0.3
