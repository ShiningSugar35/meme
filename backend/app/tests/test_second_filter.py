import asyncio
from ..strategy.second_filter import run_second_filter


def test_second_filter_high_eq_low():
    token = {"token_mint": "AAA"}
    strategy_group = {"y": 2.25}
    latest_price = {"price": 1.0}
    klines = [{"close": 1.0}, {"close": 1.0}]
    buy_sell_1m = {"buy_volume": 10, "sell_volume": 5}
    res = asyncio.get_event_loop().run_until_complete(run_second_filter(token, strategy_group, latest_price, klines, buy_sell_1m))
    assert res.passed is False


def test_second_filter_basic_pass():
    token = {"token_mint": "BBB"}
    strategy_group = {"y": 2.25}
    latest_price = {"price": 2.0}
    klines = [{"close": 1.0}, {"close": 3.0}]
    buy_sell_1m = {"buy_volume": 100, "sell_volume": 10}
    res = asyncio.get_event_loop().run_until_complete(run_second_filter(token, strategy_group, latest_price, klines, buy_sell_1m))
    assert res.passed is True


def test_second_filter_various_y_thresholds():
    for y in (2.25, 2.5, 2.75):
        token = {"token_mint": "CCC"}
        strategy_group = {"y": y}
        latest_price = {"price": 2.0}
        klines = [{"close": 1.0}, {"close": 3.0}]
        buy_sell_1m = {"buy_volume": 100, "sell_volume": 10}
        res = asyncio.get_event_loop().run_until_complete(run_second_filter(token, strategy_group, latest_price, klines, buy_sell_1m))
        assert res.passed is True


def test_second_filter_buy_volume_failure():
    token = {"token_mint": "DDD"}
    strategy_group = {"y": 2.25}
    latest_price = {"price": 2.0}
    klines = [{"close": 1.0}, {"close": 3.0}]
    buy_sell_1m = {"buy_volume": 1, "sell_volume": 100}
    res = asyncio.get_event_loop().run_until_complete(run_second_filter(token, strategy_group, latest_price, klines, buy_sell_1m))
    assert any(d["rule"] == "buy_vs_sell_1m" and not d["passed"] for d in res.details)


def test_second_filter_price_ratio_failures():
    token = {"token_mint": "EEE"}
    strategy_group = {"y": 2.25}
    # price too low
    latest_price = {"price": 0.1}
    klines = [{"close": 1.0}, {"close": 3.0}]
    buy_sell_1m = {"buy_volume": 100, "sell_volume": 10}
    res = asyncio.get_event_loop().run_until_complete(run_second_filter(token, strategy_group, latest_price, klines, buy_sell_1m))
    assert any(d["rule"] == "price_gt_high_over_y" and not d["passed"] for d in res.details)

    # price too high
    latest_price2 = {"price": 100.0}
    res2 = asyncio.get_event_loop().run_until_complete(run_second_filter(token, strategy_group, latest_price2, klines, buy_sell_1m))
    assert any(d["rule"] == "price_lt_low_times_y" and not d["passed"] for d in res2.details)
