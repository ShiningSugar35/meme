"""P0: Test holding risk top1 thresholds and evaluate_top1_holder."""
import math, pytest
from app.strategy.thresholds import compute_thresholds, compute_holding_thresholds
from app.strategy.filters import evaluate_top1_holder


class TestHoldingTop1Thresholds:
    def test_x02_holding_top1_min(self):
        """x=0.2, holding top1 min = 0.028 - 0.02*0.2 = 0.024"""
        h = compute_holding_thresholds(0.2)
        assert math.isclose(h["holding_top1_addr_type0_min"], 0.024, rel_tol=1e-9)

    def test_x02_holding_top1_max(self):
        """x=0.2, holding top1 max = 0.054 + 0.01*0.2 = 0.056"""
        h = compute_holding_thresholds(0.2)
        assert math.isclose(h["holding_top1_addr_type0_max"], 0.056, rel_tol=1e-9)

    def test_x03_holding_top1_min(self):
        """x=0.3, holding top1 min = 0.028 - 0.02*0.3 = 0.022"""
        h = compute_holding_thresholds(0.3)
        assert math.isclose(h["holding_top1_addr_type0_min"], 0.022, rel_tol=1e-9)

    def test_x03_holding_top1_max(self):
        """x=0.3, holding top1 max = 0.054 + 0.01*0.3 = 0.057"""
        h = compute_holding_thresholds(0.3)
        assert math.isclose(h["holding_top1_addr_type0_max"], 0.057, rel_tol=1e-9)

    def test_x005_holding_top1_max(self):
        """x=0.05, holding top1 max = 0.054 + 0.01*0.05 = 0.0545"""
        h = compute_holding_thresholds(0.05)
        assert math.isclose(h["holding_top1_addr_type0_max"], 0.0545, rel_tol=1e-9)

    def test_strategy_thresholds_unchanged(self):
        """Entry thresholds unchanged: top1_max for x=0.2 = 0.049+0.002 = 0.051."""
        t = compute_thresholds(0.2)
        assert math.isclose(t.top1_addr_type0_max, 0.051, rel_tol=1e-9)

    def test_compute_holding_thresholds_includes_top1(self):
        h = compute_holding_thresholds(0.2)
        assert "holding_top1_addr_type0_min" in h
        assert "holding_top1_addr_type0_max" in h

    def test_holding_and_entry_diverge(self):
        """Holding top1 range is wider than entry range (0.024-0.056 vs 0.029-0.051)."""
        h = compute_holding_thresholds(0.2)
        t = compute_thresholds(0.2)
        assert h["holding_top1_addr_type0_min"] < t.top1_addr_type0_min
        assert h["holding_top1_addr_type0_max"] > t.top1_addr_type0_max


class TestEvaluateTop1Holder:
    def test_top1_in_range_passes(self):
        """top1=3% at x=0.2 passes entry check (min=0.029, max=0.051)."""
        holder = {"addr_type": 0, "amount_percentage": 3.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True
        assert math.isclose(res.feature_vector["top1_holder_rate"], 0.03)

    def test_top1_below_min_fails(self):
        """top1=1% at x=0.2 fails entry check (min=0.029)."""
        holder = {"addr_type": 0, "amount_percentage": 1.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_above_max_fails(self):
        """top1=6% at x=0.2 fails entry check (max=0.051)."""
        holder = {"addr_type": 0, "amount_percentage": 6.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_at_entry_max_fails(self):
        """top1=5.2% at x=0.2 exceeds entry max=0.051 (5.2/100=0.052)."""
        holder = {"addr_type": 0, "amount_percentage": 5.2}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_005_passes_entry_max(self):
        """top1=5% at x=0.2 passes entry check (0.05 < 0.051)."""
        holder = {"addr_type": 0, "amount_percentage": 5.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True

    def test_top1_missing_fails(self):
        """Missing top1 should fail."""
        holder = {"addr_type": 0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_decimal_already(self):
        """amount_percentage=0.03 (already decimal) should work."""
        holder = {"addr_type": 0, "amount_percentage": 0.03}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is True
        assert math.isclose(res.feature_vector["top1_holder_rate"], 0.03)

    def test_top1_wrong_addr_type_skipped(self):
        """addr_type != 0 should be skipped."""
        holder = {"addr_type": 1, "amount_percentage": 3.0}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_fraction_normalization(self):
        """amount_percentage=0.02 (2%) at x=0.2: 0.02 < 0.029 => fails."""
        holder = {"addr_type": 0, "amount_percentage": 0.02}
        res = evaluate_top1_holder(holder, 0.2)
        assert res.passed is False

    def test_top1_xx005_entry_boundaries(self):
        """x=0.05: entry min=0.032, entry max=0.0495; holding min=0.027, holding max=0.0545."""
        h = compute_holding_thresholds(0.05)
        assert math.isclose(h["holding_top1_addr_type0_min"], 0.027, rel_tol=1e-9)
        assert math.isclose(h["holding_top1_addr_type0_max"], 0.0545, rel_tol=1e-9)
        t = compute_thresholds(0.05)
        assert math.isclose(t.top1_addr_type0_min, 0.032, rel_tol=1e-9)
        assert math.isclose(t.top1_addr_type0_max, 0.0495, rel_tol=1e-9)
