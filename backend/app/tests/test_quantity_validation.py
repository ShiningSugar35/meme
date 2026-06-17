"""P1: Test SIM quantity validation logic."""
import math
from app.trading.executor import validate_and_select_sim_token_amount


class TestQuantityValidation:
    def test_gmgn_price_zero_blocks_buy(self):
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=0.0, quote=None, token_decimals=None,
        )
        assert amount == 0.0
        assert diag["buy_allowed"] is False
        assert diag["quantity_validation_status"] == "blocked_invalid_gmgn_price"

    def test_gmgn_price_negative_blocks_buy(self):
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=-0.01, quote=None, token_decimals=None,
        )
        assert amount == 0.0
        assert diag["buy_allowed"] is False

    def test_no_quote_uses_fallback(self):
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=0.00005, quote=None, token_decimals=None,
        )
        assert amount == 1_000_000.0
        assert diag["token_amount_source"] == "no_quote"
        assert diag["quantity_validation_status"] == "fallback_no_quote"

    def test_quote_extreme_ratio_rejected(self):
        """Jupiter returns absurd quantity: ratio ~5781 -> rejected."""
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=67.16, gmgn_price_usd=4.9035995e-06,
            quote={"outAmount": "2369007562198"}, token_decimals=9,
        )
        # quote_amount = 2369007562198 / 10^9 = 2369.007562198
        # implied_price = 67.16 / 2369.007562198 = 0.02835
        # ratio = 0.02835 / 4.9035995e-6 = 5781
        assert diag["token_amount_source"] == "jupiter_quote_rejected_ratio"
        assert diag["quantity_validation_status"] == "fallback_quote_ratio_rejected"
        assert diag["quote_vs_gmgn_price_ratio"] > 1.1
        # Should fall back to size_usd/gmgn_price
        expected_fallback = 67.16 / 4.9035995e-06
        assert math.isclose(amount, expected_fallback, rel_tol=1e-6)

    def test_quote_missing_decimals_falls_back(self):
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=0.00005,
            quote={"outAmount": "1000000"}, token_decimals=None,
        )
        assert diag["token_amount_source"] == "jupiter_quote_missing_decimals"
        assert diag["quantity_validation_status"] == "fallback_missing_decimals"
        expected = 50.0 / 0.00005
        assert amount == expected

    def test_quote_valid_ratio_accepted(self):
        """1M tokens at $0.00005 = $50. Implies outAmount=1M * 10^6."""
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=0.00005,
            quote={"outAmount": "1000000000000"}, token_decimals=6,
        )
        assert diag["token_amount_source"] == "jupiter_quote_validated"
        assert diag["quantity_validation_status"] == "quote_validated"
        assert math.isclose(amount, 1000000.0)

    def test_quote_zero_out_amount_falls_back(self):
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=0.00005,
            quote={"outAmount": "0"}, token_decimals=6,
        )
        assert diag["quantity_validation_status"] == "fallback_zero_out_amount"
        assert amount == 50.0 / 0.00005

    def test_quote_bad_out_amount_falls_back(self):
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=50.0, gmgn_price_usd=0.00005,
            quote={"outAmount": "not_a_number"}, token_decimals=6,
        )
        assert diag["quantity_validation_status"] == "fallback_zero_out_amount"
        assert amount == 50.0 / 0.00005

    def test_quote_ratio_in_range_lower_bound(self):
        """ratio = 0.9 should be accepted."""
        size_usd = 50.0
        gmgn_price = 0.00005  # $50K per 1B
        # We need quote_amount such that ratio = 0.9
        # ratio = (size/quote_amount) / gmgn_price = 0.9
        # size / (0.9 * gmgn_price) = quote_amount
        quote_amount = size_usd / (0.9 * gmgn_price)  # ~1111111
        out_amount = int(quote_amount * 10**6)
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=size_usd, gmgn_price_usd=gmgn_price,
            quote={"outAmount": str(out_amount)}, token_decimals=6,
        )
        assert diag["token_amount_source"] == "jupiter_quote_validated"

    def test_quote_ratio_in_range_upper_bound(self):
        """ratio = 1.0 should be accepted (well within bounds)."""
        size_usd = 50.0
        gmgn_price = 0.00005
        quote_amount = size_usd / gmgn_price  # 1,000,000
        out_amount = int(quote_amount * 10**6)  # 10^12
        amount, diag = validate_and_select_sim_token_amount(
            size_usd=size_usd, gmgn_price_usd=gmgn_price,
            quote={"outAmount": str(out_amount)}, token_decimals=6,
        )
        assert diag["token_amount_source"] == "jupiter_quote_validated"

    def test_quoteless_dict_not_confused(self):
        """Empty or error quote dicts fall back to no_quote path."""
        for bad in (None, {}, {"error": "FORBIDDEN"}):
            amount, diag = validate_and_select_sim_token_amount(
                size_usd=50.0, gmgn_price_usd=0.00005, quote=bad, token_decimals=6,
            )
            assert diag["token_amount_source"] in ("no_quote", "gmgn_spot_fallback")
