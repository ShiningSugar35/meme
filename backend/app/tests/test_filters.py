import asyncio
from datetime import datetime, timedelta, timezone
from ..strategy.filters import run_initial_filter


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
        "pool_created_at": (datetime.now(timezone.utc) - timedelta(seconds=150)).isoformat(),
        "platform": "Pump.fun",
    }
    base.update(kwargs)
    return base


def test_filters_all_pass():
    snapshot = make_snapshot()
    strategy_group = {"x": 0.15, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(snapshot, strategy_group, datetime.now(timezone.utc)))
    assert res.passed is True


def test_missing_field_fails():
    s = make_snapshot()
    s.pop("liquidity_usd", None)
    strategy_group = {"x": 0.15, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "liquidity_usd" and not d.passed for d in res.details)


def test_has_social_required_for_small_x():
    s = make_snapshot()
    s["has_social"] = 0
    strategy_group = {"x": 0.2, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    # x=0.2 -> has_social required
    assert any(d.rule_name == "has_social" and not d.passed for d in res.details)


def test_x_thresholds_and_boundaries():
    x = 0.2
    s = make_snapshot()
    # liquidity threshold
    thresh_liq = 13000 - 20000 * x
    s["liquidity_usd"] = thresh_liq - 1
    strategy_group = {"x": x, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "liquidity_usd" and not d.passed for d in res.details)


def test_top10_top1_boundaries():
    x = 0.2
    s = make_snapshot()
    # top10 lower boundary
    s["top_10_holder_rate"] = 0.175 - 0.15 * x
    strategy_group = {"x": x, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "top_10_holder_rate" and not d.passed for d in res.details)

    # top1 upper boundary
    s2 = make_snapshot()
    s2["top1_holder_rate"] = 0.044 + 0.04 * x
    res2 = asyncio.get_event_loop().run_until_complete(run_initial_filter(s2, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "top1_holder_rate" and not d.passed for d in res2.details)


def test_pool_created_at_window_edges():
    now = datetime.now(timezone.utc)
    s = make_snapshot()
    t = 150
    # left edge: now - t seconds
    s["pool_created_at"] = (now - timedelta(seconds=t)).isoformat()
    strategy_group = {"x": 0.15, "t_seconds": t}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, now))
    assert any(d.rule_name == "time_window" and d.passed for d in res.details)

    # right edge: now - (t+60)
    s2 = make_snapshot()
    s2["pool_created_at"] = (now - timedelta(seconds=t + 60)).isoformat()
    res2 = asyncio.get_event_loop().run_until_complete(run_initial_filter(s2, strategy_group, now))
    assert any(d.rule_name == "time_window" and d.passed for d in res2.details)


def test_platform_whitelist_and_creator_dev_rules():
    s = make_snapshot()
    s["platform"] = "unknown_platform"
    strategy_group = {"x": 0.15, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "platform" and not d.passed for d in res.details)

    # creator_token_status check: if not creator_close, dev_team_hold_rate must be below threshold
    s2 = make_snapshot()
    s2["creator_token_status"] = "open"
    s2["dev_team_hold_rate"] = 0.5
    strategy_group = {"x": 0.15, "t_seconds": 120}
    res2 = asyncio.get_event_loop().run_until_complete(run_initial_filter(s2, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "creator_or_dev_hold" and not d.passed for d in res2.details)


def test_core_field_missing_fails():
    s = make_snapshot()
    s.pop("renounced_mint", None)
    strategy_group = {"x": 0.15, "t_seconds": 120}
    res = asyncio.get_event_loop().run_until_complete(run_initial_filter(s, strategy_group, datetime.now(timezone.utc)))
    assert any(d.rule_name == "renounced_mint" and not d.passed for d in res.details)
