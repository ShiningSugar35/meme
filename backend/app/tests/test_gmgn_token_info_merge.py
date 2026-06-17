"""P7/P-1: Test that _normalize_token_data correctly extracts fields and computes missing ones."""
import math
from app.providers.gmgn_real import GMGNProvider


def _norm(raw: dict) -> dict:
    """Normalize a dict that has already been flattened (post sub-object merge)."""
    return GMGNProvider._normalize_token_data(raw)


class TestTokenInfoMerge:
    """Verify normalized output from already-flattened merged dict."""

    def test_price_maps_to_price_usd(self):
        """The 'price' field in the merged dict maps to price_usd."""
        out = _norm({"price": "0.00005689"})
        assert math.isclose(out["price_usd"], 0.00005689, rel_tol=1e-6)

    def test_swaps_1h_direct(self):
        out = _norm({"swaps_1h": 6, "volume_1h": "247.15"})
        assert out["swaps_1h"] == 6

    def test_stat_fields_flattened(self):
        """After stat dict is flattened into merged dict, fields are at top level."""
        out = _norm({
            "fresh_wallet_rate": 0.04, "creator_hold_rate": 0.03,
            "dev_team_hold_rate": 0.02, "top_rat_trader_percentage": 0.01,
            "top_bundler_trader_percentage": 0.02,
            "top_entrapment_trader_percentage": 0.03,
            "top70_sniper_hold_rate": 0.01,
            "holder_count": 150, "price": "0.00005",
        })
        assert math.isclose(out["fresh_wallet_rate"], 0.04)
        assert math.isclose(out["creator_balance_rate"], 0.03)
        assert math.isclose(out["dev_team_hold_rate"], 0.02)
        assert math.isclose(out["rat_trader_amount_rate"], 0.01)
        assert math.isclose(out["max_bundler_rate"], 0.02)
        assert math.isclose(out["max_entrapment_ratio"], 0.03)
        assert math.isclose(out["sniper_count"], 0.01)
        assert out["holder_count"] == 150

    def test_dev_fields_flattened(self):
        out = _norm({
            "creator_token_balance": 700000, "creator_token_status": "creator_hold",
            "total_supply": 1000000000, "price": "0.00005",
        })
        assert out["creator_token_balance"] == 700000
        assert out["creator_token_status"] == "creator_hold"

    def test_price_change_computed(self):
        out = _norm({"price": "0.00010", "price_1h": "0.00008"})
        assert math.isclose(out["price_change_percent1h"], 25.0, rel_tol=1e-6)

    def test_market_cap_computed(self):
        out = _norm({"price": "0.05", "total_supply": 1000000000})
        assert math.isclose(out["market_cap"], 50000000.0, rel_tol=1e-6)

    def test_volume_1h_computed(self):
        out = _norm({"buy_volume_1h": "100", "sell_volume_1h": "50", "price": "0.00005"})
        assert math.isclose(out["volume_1h"], 150.0, rel_tol=1e-6)

    def test_creator_balance_rate_computed(self):
        out = _norm({"creator_token_balance": 50000000, "total_supply": 1000000000,
                      "price": "0.00005"})
        assert math.isclose(out["creator_balance_rate"], 0.05, rel_tol=1e-6)

    def test_socials_from_link_dict(self):
        out = _norm({"link": {"website": "https://example.com", "twitter": "https://x.com/token"}})
        assert len(out.get("socials", [])) >= 2

    def test_top_level_scalars(self):
        out = _norm({
            "symbol": "TEST", "name": "TestToken", "holder_count": 42,
            "total_supply": 1000000, "liquidity": 5000.0,
            "launchpad": "Pump.fun", "created_timestamp": 1781682000,
        })
        assert out["symbol"] == "TEST"
        assert out["name"] == "TestToken"
        assert out["holder_count"] == 42
        assert out["total_supply"] == 1000000
        assert math.isclose(out["liquidity_usd"], 5000.0)
        assert out["launchpad"] == "Pump.fun"
        assert out["pool_created_at"] == 1781682000

    def test_token_mint_not_polluted_by_pool_address(self):
        out = _norm({
            "address": "TOKEN_MINT",
            "pool_address": "POOL_ADDR",
            "price": "0.00005",
        })
        assert out["token_mint"] == "TOKEN_MINT"
        assert out["pool_address"] == "POOL_ADDR"

    def test_volume_usd_from_volume_24h(self):
        out = _norm({"volume_24h": "12345.67"})
        assert math.isclose(out["volume_usd"], 12345.67, rel_tol=1e-6)

    def test_swaps_1h_alias_trade_1h(self):
        out = _norm({"trade_1h": 30, "price": "0.00005"})
        assert out["swaps_1h"] == 30

    def test_swaps_1h_alias_swaps1h(self):
        out = _norm({"swaps1h": 20, "price": "0.00005"})
        assert out["swaps_1h"] == 20

    def test_bundler_alias(self):
        out = _norm({"top_bundler_trader_percentage": 0.05, "price": "0.00005"})
        assert math.isclose(out["max_bundler_rate"], 0.05)

    def test_sniper_count_alias(self):
        out = _norm({"top70_sniper_hold_rate": 0.02, "price": "0.00005"})
        assert math.isclose(out["sniper_count"], 0.02)

    def test_direct_price_change_wins(self):
        out = _norm({"price": "0.00010", "price_1h": "0.00008",
                      "price_change_percent1h": 50.0})
        assert math.isclose(out["price_change_percent1h"], 50.0)

    def test_market_cap_direct_wins(self):
        out = _norm({"price": "0.05", "total_supply": 1000000000,
                      "market_cap": 9999.0})
        assert math.isclose(out["market_cap"], 9999.0)


class TestFullMergeSimulation:
    """Simulate the full merge pipeline: flatten sub-objects then normalize."""

    def test_full_merge(self):
        # This is what the merged dict looks like after fetch_token_snapshot
        # flattens price, stat, dev, pool, and top-level scalars.
        merged = {
            "address": "TOKEN_MINT",
            "symbol": "TOKEN", "name": "TokenName",
            "holder_count": 100, "total_supply": 1000000000,
            "liquidity": 10000, "launchpad": "Pump.fun",
            "creation_timestamp": 1781682000,
            # From price sub-object (flattened)
            "price": "0.000049", "swaps_1h": 12,
            "volume_1h": "150", "buy_volume_1h": "100", "sell_volume_1h": "50",
            "price_1h": "0.000040",
            # From pool sub-object (flattened)
            "pool_address": "POOL_ADDR",
            "quote_reserve": "40",
            # From stat sub-object (flattened)
            "fresh_wallet_rate": 0.05, "creator_hold_rate": 0.03,
            "dev_team_hold_rate": 0.05, "top_rat_trader_percentage": 0,
            "top_bundler_trader_percentage": 0, "top_entrapment_trader_percentage": 0,
            "top70_sniper_hold_rate": 0,
            # From dev sub-object (flattened)
            "creator_token_balance": 700000, "creator_token_status": "creator_hold",
            # From security endpoint (flattened)
            "burn_status": "burn", "sell_tax": 0,
            "top_10_holder_rate": 0.12, "renounced_mint": True,
            "renounced_freeze_account": True, "dev_token_burn_ratio": 0,
            # Link
            "link": {"website": "https://example.com"},
        }

        out = GMGNProvider._normalize_token_data(merged)

        assert out["token_mint"] == "TOKEN_MINT"
        assert out["pool_address"] == "POOL_ADDR"
        assert math.isclose(out["price_usd"], 0.000049)
        assert math.isclose(out["liquidity_usd"], 10000.0)
        assert math.isclose(out["market_cap"], 49000.0)
        assert out["swaps_1h"] == 12
        assert math.isclose(out["volume_1h"], 150.0)
        assert math.isclose(out["price_change_percent1h"], 22.5)
        assert out["holder_count"] == 100
        assert math.isclose(out["fresh_wallet_rate"], 0.05)
        assert math.isclose(out["creator_balance_rate"], 0.03)
        assert out["burn_status"] == "burn"
        assert out["sell_tax"] == 0
        assert math.isclose(out["top_10_holder_rate"], 0.12)
        assert len(out.get("socials", [])) > 0
