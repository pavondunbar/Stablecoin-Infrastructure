"""
tests/test_metrics_and_shared.py
──────────────────────────────────
Tests for the shared metrics module, Kafka event schemas,
and database session utilities.

Covers:
  ✓ Counter increments correctly
  ✓ Counter Prometheus text format renders valid labels
  ✓ Histogram buckets accumulate correctly
  ✓ Histogram render includes _sum and _count
  ✓ All Pydantic event schemas serialise to/from JSON
  ✓ Event IDs are unique across instances
  ✓ Decimal amounts serialise to strings (not floats)
  ✓ BaseEvent timestamps are UTC-aware
"""

import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

sys.path.insert(0, "/home/claude/stablecoin-infra")
sys.path.insert(0, "/home/claude/stablecoin-infra/shared")

from shared.metrics import Counter, Histogram, render_all
from shared.events import (
    BaseEvent,
    TokenIssuanceCompleted, TokenRedemptionCompleted, TokenBalanceUpdated,
    RTGSSettlementSubmitted, RTGSSettlementCompleted, RTGSSettlementFailed,
    ConditionalPaymentCreated, ConditionalPaymentCompleted,
    EscrowCreated, EscrowReleased, EscrowExpired,
    FXRateUpdated, FXSettlementInitiated, FXSettlementCompleted,
    ComplianceEvent, AuditTrailEntry,
)


# ─── Counter Tests ────────────────────────────────────────────────────────────

class TestCounter:

    def test_initial_value_zero(self):
        c = Counter("test_counter_a", "help", ["label"])
        # No data points yet — render should have header only
        rendered = c.render()
        assert "# HELP test_counter_a help" in rendered
        assert "# TYPE test_counter_a counter" in rendered

    def test_inc_increments(self):
        c = Counter("test_counter_b", "help", ["svc"])
        c.inc(("rtgs",))
        c.inc(("rtgs",))
        c.inc(("fx",))
        assert c._data[("rtgs",)] == 2.0
        assert c._data[("fx",)]   == 1.0

    def test_inc_custom_amount(self):
        c = Counter("test_counter_c", "help", [])
        c.inc(amount=5.5)
        assert c._data[()] == 5.5

    def test_render_contains_label_values(self):
        c = Counter("test_counter_d", "help", ["service", "topic"])
        c.inc(("rtgs", "settlement.completed"))
        rendered = c.render()
        assert 'service="rtgs"' in rendered
        assert 'topic="settlement.completed"' in rendered

    def test_render_contains_value(self):
        c = Counter("test_counter_e", "help", ["k"])
        c.inc(("v",), amount=42.0)
        rendered = c.render()
        assert "42.0" in rendered

    def test_multiple_labels_render_correctly(self):
        c = Counter("test_counter_f", "help", ["a", "b", "c"])
        c.inc(("x", "y", "z"))
        rendered = c.render()
        assert 'a="x"' in rendered
        assert 'b="y"' in rendered
        assert 'c="z"' in rendered


# ─── Histogram Tests ─────────────────────────────────────────────────────────

class TestHistogram:

    def test_observe_counts_in_correct_bucket(self):
        h = Histogram("test_hist_a", "help", ["svc"])
        h.observe(30.0, ("api",))    # should land in ≤50ms bucket
        counts = h._counts[("api",)]
        # Buckets: 1, 5, 10, 25, 50, 100 ...
        bucket_50_idx = list(Histogram.BUCKETS).index(50)
        assert counts[bucket_50_idx] == 1

    def test_observe_accumulates_sum(self):
        h = Histogram("test_hist_b", "help", ["svc"])
        h.observe(10.0, ("x",))
        h.observe(20.0, ("x",))
        h.observe(30.0, ("x",))
        assert h._sums[("x",)] == 60.0

    def test_render_includes_sum_and_count(self):
        h = Histogram("test_hist_c", "help", ["s"])
        h.observe(100.0, ("svc",))
        rendered = h.render()
        assert "test_hist_c_sum" in rendered
        assert "test_hist_c_count" in rendered

    def test_render_includes_inf_bucket(self):
        h = Histogram("test_hist_d", "help", [])
        h.observe(99999.0, ())
        rendered = h.render()
        assert '+Inf' in rendered

    def test_no_observations_no_buckets(self):
        h = Histogram("test_hist_e", "help", ["s"])
        rendered = h.render()
        # No observations → no bucket lines (only HELP+TYPE)
        lines = [l for l in rendered.split("\n") if l and not l.startswith("#")]
        assert len(lines) == 0

    def test_render_all_is_string(self):
        output = render_all()
        assert isinstance(output, str)
        assert len(output) > 0


# ─── Event Schema Tests ───────────────────────────────────────────────────────

class TestEventSchemas:

    def test_base_event_has_unique_ids(self):
        e1 = BaseEvent(service="test")
        e2 = BaseEvent(service="test")
        assert e1.event_id != e2.event_id

    def test_base_event_timestamp_is_utc(self):
        e = BaseEvent(service="test")
        assert e.event_time.tzinfo is not None

    def test_decimal_serialises_as_string(self):
        ev = TokenIssuanceCompleted(
            service="token-issuance",
            issuance_ref="ISS-001",
            account_id=str(uuid.uuid4()),
            currency="USD",
            amount=Decimal("1234567.89000001"),
            new_balance=Decimal("9876543.21000001"),
        )
        payload = json.loads(ev.model_dump_json())
        # Pydantic Config.json_encoders: Decimal → str
        assert isinstance(payload["amount"],      str)
        assert isinstance(payload["new_balance"], str)
        assert payload["amount"] == "1234567.89000001"

    def test_token_issuance_completed_roundtrip(self):
        ev = TokenIssuanceCompleted(
            service="token-issuance",
            issuance_ref="ISS-RT-001",
            account_id="acct-abc",
            currency="EUR",
            amount=Decimal("500000.00"),
            new_balance=Decimal("1500000.00"),
        )
        raw     = ev.model_dump_json()
        rebuilt = TokenIssuanceCompleted.model_validate_json(raw)
        assert rebuilt.issuance_ref == "ISS-RT-001"
        assert rebuilt.currency     == "EUR"

    def test_rtgs_settlement_completed_schema(self):
        ev = RTGSSettlementCompleted(
            service="rtgs",
            settlement_ref="RTGS-001",
            sending_account_id=str(uuid.uuid4()),
            receiving_account_id=str(uuid.uuid4()),
            currency="GBP",
            amount=Decimal("10_000_000"),
            transaction_id=str(uuid.uuid4()),
            settled_at=datetime.now(timezone.utc),
        )
        assert ev.currency == "GBP"
        assert ev.amount   == Decimal("10000000")

    def test_fx_settlement_initiated_schema(self):
        ev = FXSettlementInitiated(
            service="fx-settlement",
            settlement_ref="FXS-001",
            sending_account_id="acct-1",
            receiving_account_id="acct-2",
            sell_currency="USD",
            sell_amount=Decimal("5_000_000"),
            buy_currency="EUR",
            buy_amount=Decimal("4_590_000"),
            applied_rate=Decimal("0.918"),
            rails="blockchain",
        )
        payload = json.loads(ev.model_dump_json())
        assert payload["rails"]         == "blockchain"
        assert payload["sell_currency"] == "USD"

    def test_escrow_created_schema(self):
        ev = EscrowCreated(
            service="payment-engine",
            contract_ref="ESC-001",
            depositor_account_id="dep-acct",
            beneficiary_account_id="ben-acct",
            currency="USD",
            amount=Decimal("2_000_000"),
            conditions={"delivery_ref": "BL-001"},
            expires_at=datetime.now(timezone.utc),
        )
        assert ev.conditions["delivery_ref"] == "BL-001"

    def test_compliance_event_schema(self):
        ev = ComplianceEvent(
            service="compliance-monitor",
            entity_type="transaction",
            entity_id=str(uuid.uuid4()),
            event_type="aml_check",
            result="pass",
            score=Decimal("0.0"),
            details={"flags": []},
        )
        assert ev.result == "pass"
        payload = json.loads(ev.model_dump_json())
        assert payload["details"]["flags"] == []

    def test_audit_trail_entry_optional_fields(self):
        ev = AuditTrailEntry(
            service="api-gateway",
            actor_service="api-gateway",
            action="POST /v1/tokens/issue",
            entity_type="http_request",
            entity_id=str(uuid.uuid4()),
        )
        assert ev.before_state is None
        assert ev.after_state  is None
        assert ev.ip_address   is None

    @pytest.mark.parametrize("EventClass,kwargs", [
        (TokenBalanceUpdated, dict(
            service="token-issuance", account_id="acct", currency="USD",
            old_balance=Decimal("100"), new_balance=Decimal("200"),
            reserved=Decimal("0"), reason="issuance",
        )),
        (RTGSSettlementFailed, dict(
            service="rtgs", settlement_ref="RTGS-FAIL",
            reason="Insufficient balance", retry_count=3,
        )),
        (EscrowExpired, dict(
            service="payment-engine", contract_ref="ESC-EXP",
            refunded_account_id="acct", amount=Decimal("500_000"),
        )),
        (FXRateUpdated, dict(
            service="fx-settlement", base_currency="USD",
            quote_currency="EUR", mid_rate=Decimal("0.918"), source="internal",
        )),
    ])
    def test_event_serialises_without_error(self, EventClass, kwargs):
        ev  = EventClass(**kwargs)
        raw = ev.model_dump_json()
        assert len(raw) > 10
        rebuilt = EventClass.model_validate_json(raw)
        assert rebuilt.event_id == ev.event_id


# ─── Model Tests ─────────────────────────────────────────────────────────────

class TestOrmModels:

    def test_token_balance_available_property(self, db, alice):
        from shared.models import TokenBalance
        from sqlalchemy import select

        bal = db.execute(
            select(TokenBalance).where(
                TokenBalance.account_id == alice.id,
                TokenBalance.currency   == "USD",
            )
        ).scalar_one()

        assert bal.available == bal.balance - bal.reserved

    def test_token_balance_available_with_reserve(self, db):
        from tests.conftest import make_account, make_balance
        acct = make_account(db, "Reserve Test Bank")
        make_balance(db, str(acct.id), "USD",
                     balance=Decimal("1_000_000"),
                     reserved=Decimal("300_000"))

        from shared.models import TokenBalance
        from sqlalchemy import select
        bal = db.execute(
            select(TokenBalance).where(
                TokenBalance.account_id == acct.id,
                TokenBalance.currency   == "USD",
            )
        ).scalar_one()

        assert bal.balance   == Decimal("1_000_000")
        assert bal.reserved  == Decimal("300_000")
        assert bal.available == Decimal("700_000")
