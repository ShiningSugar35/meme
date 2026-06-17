"""P3: Test entry_data_gate completeness checks."""
import pytest
from app.trading.entry_data_gate import (
    ENTRY_HARD_REQUIRED_FIELDS, POSITIVE_REQUIRED,
    check_entry_data_completeness, EntryDataCompletenessReport,
)


def _snap(**overrides) -> dict:
    """Build a minimal complete snapshot for testing."""
    base = {
        "price_usd": 0.00005, "liquidity_usd": 10000.0, "market_cap": 50000.0,
        "holder_count": 100, "top_10_holder_rate": 0.12, "fresh_wallet_rate": 0.05,
        "max_rug_ratio": 0.01, "max_entrapment_ratio": 0.01,
        "max_insider_ratio": 0.01,
        "max_bundler_rate": 0.01,
        "suspected_insider_hold_rate": 0.01, "is_wash_trading": False,
        "rat_trader_amount_rate": 0.01, "sell_tax": 0.0, "burn_status": "burn",
        "sniper_count": 0, "creator_balance_rate": 0.03, "swaps_1h": 12,
        "volume_1h": 500.0, "price_change_percent1h": 10.0,
        "socials": ["https://x.com/token"],
    }
    base.update(overrides)
    return base


class TestEntryDataGate:
    def test_all_fields_present_passes(self):
        snap = _snap()
        report = check_entry_data_completeness(snap)
        assert report.passed
        assert not report.blocked
        assert len(report.missing_fields) == 0
        assert len(report.abnormal_fields) == 0

    def test_missing_required_field_fails(self):
        for field in ENTRY_HARD_REQUIRED_FIELDS:
            snap = _snap()
            del snap[field]
            report = check_entry_data_completeness(snap)
            assert field in report.missing_fields, f"{field} should be missing"
            assert report.blocked

    def test_price_usd_zero_is_abnormal(self):
        snap = _snap(price_usd=0)
        report = check_entry_data_completeness(snap)
        assert "price_usd" in report.abnormal_fields
        assert report.blocked

    def test_liquidity_usd_zero_is_abnormal(self):
        snap = _snap(liquidity_usd=0)
        report = check_entry_data_completeness(snap)
        assert "liquidity_usd" in report.abnormal_fields

    def test_holder_count_zero_is_abnormal(self):
        snap = _snap(holder_count=0)
        report = check_entry_data_completeness(snap)
        assert "holder_count" in report.abnormal_fields

    def test_market_cap_zero_is_abnormal(self):
        snap = _snap(market_cap=0)
        report = check_entry_data_completeness(snap)
        assert "market_cap" in report.abnormal_fields

    def test_swaps_1h_zero_is_abnormal(self):
        snap = _snap(swaps_1h=0)
        report = check_entry_data_completeness(snap)
        assert "swaps_1h" in report.abnormal_fields

    def test_volume_1h_zero_is_abnormal(self):
        snap = _snap(volume_1h=0)
        report = check_entry_data_completeness(snap)
        assert "volume_1h" in report.abnormal_fields

    def test_none_value_blocks(self):
        snap = _snap(price_usd=None)
        report = check_entry_data_completeness(snap)
        assert "price_usd" in report.missing_fields

    def test_empty_socials_blocks(self):
        snap = _snap(socials=[])
        report = check_entry_data_completeness(snap)
        assert "socials" in report.missing_fields
        assert report.blocked

    def test_creator_balance_rate_none_blocks(self):
        snap = _snap(creator_balance_rate=None)
        report = check_entry_data_completeness(snap)
        assert "creator_balance_rate" in report.missing_fields

    def test_is_wash_trading_none_blocks(self):
        snap = _snap(is_wash_trading=None)
        report = check_entry_data_completeness(snap)
        assert "is_wash_trading" in report.missing_fields

    def test_sell_tax_none_blocks(self):
        snap = _snap(sell_tax=None)
        report = check_entry_data_completeness(snap)
        assert "sell_tax" in report.missing_fields

    def test_burn_status_none_blocks(self):
        snap = _snap(burn_status=None)
        report = check_entry_data_completeness(snap)
        assert "burn_status" in report.missing_fields

    def test_top_10_holder_rate_none_blocks(self):
        snap = _snap(top_10_holder_rate=None)
        report = check_entry_data_completeness(snap)
        assert "top_10_holder_rate" in report.missing_fields

    def test_price_change_percent1h_none_blocks(self):
        snap = _snap(price_change_percent1h=None)
        report = check_entry_data_completeness(snap)
        assert "price_change_percent1h" in report.missing_fields

    def test_max_rug_ratio_none_blocks(self):
        snap = _snap(max_rug_ratio=None)
        report = check_entry_data_completeness(snap)
        assert "max_rug_ratio" in report.missing_fields

    def test_max_entrapment_ratio_none_blocks(self):
        snap = _snap(max_entrapment_ratio=None)
        report = check_entry_data_completeness(snap)
        assert "max_entrapment_ratio" in report.missing_fields

    def test_max_bundler_rate_none_blocks(self):
        snap = _snap(max_bundler_rate=None)
        report = check_entry_data_completeness(snap)
        assert "max_bundler_rate" in report.missing_fields

    def test_suspected_insider_hold_rate_none_blocks(self):
        snap = _snap(suspected_insider_hold_rate=None)
        report = check_entry_data_completeness(snap)
        assert "suspected_insider_hold_rate" in report.missing_fields

    def test_rat_trader_amount_rate_none_blocks(self):
        snap = _snap(rat_trader_amount_rate=None)
        report = check_entry_data_completeness(snap)
        assert "rat_trader_amount_rate" in report.missing_fields

    def test_sniper_count_none_blocks(self):
        snap = _snap(sniper_count=None)
        report = check_entry_data_completeness(snap)
        assert "sniper_count" in report.missing_fields

    def test_socials_none_blocks(self):
        snap = _snap(socials=None)
        report = check_entry_data_completeness(snap)
        assert "socials" in report.missing_fields

    def test_max_insider_ratio_missing_blocks(self):
        snap = _snap()
        del snap["max_insider_ratio"]
        report = check_entry_data_completeness(snap)
        assert "max_insider_ratio" in report.missing_fields
        assert report.blocked

    def test_max_insider_ratio_zero_is_valid(self):
        """max_insider_ratio=0 is a valid value, NOT missing."""
        snap = _snap(max_insider_ratio=0)
        report = check_entry_data_completeness(snap)
        assert "max_insider_ratio" not in report.missing_fields
        # 0 is not in POSITIVE_REQUIRED, so not abnormal either
        assert "max_insider_ratio" not in report.abnormal_fields

    def test_max_insider_ratio_required_in_set(self):
        assert "max_insider_ratio" in ENTRY_HARD_REQUIRED_FIELDS

    def test_positive_required_count(self):
        assert len(POSITIVE_REQUIRED) == 6
        for f in ("price_usd", "liquidity_usd", "holder_count", "market_cap", "swaps_1h", "volume_1h"):
            assert f in POSITIVE_REQUIRED

    def test_socials_from_link_string_passes(self):
        """A snapshot with socials=[link_string] should not be blocked on socials."""
        snap = _snap(socials=["https://x.com/token"])
        report = check_entry_data_completeness(snap)
        assert "socials" not in report.missing_fields
        assert not report.blocked
