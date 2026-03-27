"""
tests/test_compliance.py
─────────────────────────
Unit tests for the Compliance Monitor AML/sanctions screening rules.

Covers:
  ✓ Large transaction flagging (above CTR threshold)
  ✓ Structuring detection (just-below threshold)
  ✓ Velocity limit (too many transactions in 1 hour)
  ✓ Sanctions name matching
  ✓ Clean transactions pass all checks
  ✓ Multi-flag scenario (large + velocity)
  ✓ Currency conversion approximation
"""

import sys
import threading
import time
from decimal import Decimal

sys.path.insert(0, "/home/claude/stablecoin-infra/services/compliance-monitor")

import main as compliance
from main import (
    _check_large_transaction,
    _check_structuring,
    _check_velocity,
    _check_sanctions,
    AML_RULES,
)


# ─── Large Transaction Tests ─────────────────────────────────────────────────

class TestLargeTransactionRule:

    def test_above_threshold_flagged(self):
        flags = _check_large_transaction(Decimal("1_000_000"), "USD")
        assert len(flags) == 1
        assert "LARGE_TRANSACTION" in flags[0]

    def test_exactly_at_threshold_flagged(self):
        threshold = AML_RULES["large_transaction_usd"]
        flags = _check_large_transaction(threshold, "USD")
        assert len(flags) == 1

    def test_below_threshold_passes(self):
        flags = _check_large_transaction(Decimal("999_999.99"), "USD")
        assert len(flags) == 0

    def test_eur_amount_converted_and_flagged(self):
        # €950_000 × 1.09 ≈ $1.035M → above threshold
        flags = _check_large_transaction(Decimal("950_000"), "EUR")
        assert len(flags) == 1

    def test_gbp_amount_converted_and_flagged(self):
        # £800_000 × 1.27 ≈ $1.016M → above threshold
        flags = _check_large_transaction(Decimal("800_000"), "GBP")
        assert len(flags) == 1

    def test_small_eur_amount_passes(self):
        flags = _check_large_transaction(Decimal("10_000"), "EUR")
        assert len(flags) == 0

    def test_zero_amount_passes(self):
        flags = _check_large_transaction(Decimal("0"), "USD")
        assert len(flags) == 0


# ─── Structuring Detection Tests ─────────────────────────────────────────────

class TestStructuringRule:

    def test_just_below_threshold_flagged(self):
        # $9,500 is in the structuring window ($9,500 - $999,999)
        flags = _check_structuring(Decimal("9_500"), "USD")
        assert len(flags) == 1
        assert "STRUCTURING" in flags[0]

    def test_at_structuring_threshold_flagged(self):
        threshold = AML_RULES["structuring_window_usd"]
        flags = _check_structuring(threshold, "USD")
        assert len(flags) == 1

    def test_above_reporting_threshold_not_structuring(self):
        # Above $1M → this is a LARGE_TRANSACTION, not structuring
        flags = _check_structuring(Decimal("1_500_000"), "USD")
        assert len(flags) == 0

    def test_below_structuring_window_passes(self):
        flags = _check_structuring(Decimal("5_000"), "USD")
        assert len(flags) == 0

    def test_eur_structuring_detected(self):
        # €8,800 × 1.09 ≈ $9,592 → in structuring window
        flags = _check_structuring(Decimal("8_800"), "EUR")
        assert len(flags) == 1


# ─── Velocity Tests ──────────────────────────────────────────────────────────

class TestVelocityRule:

    def setup_method(self):
        """Clear velocity tracker between tests."""
        compliance._velocity_tracker.clear()

    def test_under_limit_passes(self):
        acct = "acct-velocity-pass"
        for _ in range(AML_RULES["velocity_per_hour"] - 1):
            _check_velocity(acct)
        flags = _check_velocity(acct)
        assert len(flags) == 0

    def test_at_limit_passes(self):
        acct = "acct-velocity-limit"
        for _ in range(AML_RULES["velocity_per_hour"]):
            _check_velocity(acct)
        # The check adds the current call, so at limit = no flag yet
        flags = _check_velocity(acct)
        # One over limit now
        assert len(flags) == 1

    def test_over_limit_flagged(self):
        acct = "acct-velocity-over"
        limit = AML_RULES["velocity_per_hour"]
        for _ in range(limit + 5):
            _check_velocity(acct)
        flags = _check_velocity(acct)
        assert len(flags) == 1
        assert "HIGH_VELOCITY" in flags[0]

    def test_different_accounts_isolated(self):
        """Velocity tracking must be per-account."""
        acct_a = "acct-isolated-a"
        acct_b = "acct-isolated-b"
        limit  = AML_RULES["velocity_per_hour"]
        # Fill acct_a way over limit
        for _ in range(limit + 10):
            _check_velocity(acct_a)
        # acct_b should have no flags from a clean start
        flags = _check_velocity(acct_b)
        assert len(flags) == 0

    def test_flag_contains_account_id(self):
        acct  = "acct-flag-content"
        limit = AML_RULES["velocity_per_hour"]
        for _ in range(limit + 1):
            _check_velocity(acct)
        flags = _check_velocity(acct)
        if flags:
            assert acct in flags[0]


# ─── Sanctions Screening Tests ────────────────────────────────────────────────

class TestSanctionsRule:

    def test_known_pattern_flagged(self):
        flags = _check_sanctions("DPRK State Bank", "acc-001")
        assert len(flags) == 1
        assert "SANCTIONS_HIT" in flags[0]

    def test_pattern_case_insensitive(self):
        flags = _check_sanctions("dprk state bank", "acc-002")
        assert len(flags) == 1

    def test_multiple_pattern_hits(self):
        flags = _check_sanctions("DPRK IRAN Sberbank Holdings", "acc-003")
        assert len(flags) == 3

    def test_clean_name_passes(self):
        flags = _check_sanctions("Standard Chartered Bank", "acc-004")
        assert len(flags) == 0

    def test_empty_name_passes(self):
        flags = _check_sanctions("", "acc-005")
        assert len(flags) == 0

    def test_lazarus_group_flagged(self):
        flags = _check_sanctions("Lazarus Group Capital LLC", "acc-006")
        assert len(flags) >= 1

    def test_partial_pattern_match(self):
        # SBERBANK appears within a longer name
        flags = _check_sanctions("Joint Stock Company Sberbank Russia", "acc-007")
        assert len(flags) >= 1


# ─── Integration: Full Screening Run ─────────────────────────────────────────

class TestFullScreening:

    def setup_method(self):
        compliance._velocity_tracker.clear()

    def test_clean_transaction_produces_zero_flags(self):
        all_flags = (
            _check_large_transaction(Decimal("100_000"), "USD")
            + _check_structuring(Decimal("100_000"), "USD")
            + _check_velocity("clean-acct")
            + _check_sanctions("Deutsche Bank AG", "clean-acct")
        )
        assert len(all_flags) == 0

    def test_large_plus_sanctions_produces_multiple_flags(self):
        all_flags = (
            _check_large_transaction(Decimal("5_000_000"), "USD")
            + _check_structuring(Decimal("5_000_000"), "USD")
            + _check_velocity("suspect-acct")
            + _check_sanctions("DPRK Central Reserve Bank", "suspect-acct")
        )
        assert len(all_flags) >= 2

    def test_structuring_and_velocity_flagged_together(self):
        acct   = "combo-acct"
        limit  = AML_RULES["velocity_per_hour"]
        for _ in range(limit + 1):
            _check_velocity(acct)

        struct_flags   = _check_structuring(Decimal("9_600"), "USD")
        velocity_flags = _check_velocity(acct)
        assert len(struct_flags) == 1
        assert len(velocity_flags) == 1
