"""
tests/test_token_issuance.py
─────────────────────────────
Unit tests for the Token Issuance & Redemption Engine.

Covers:
  ✓ Happy-path token issuance
  ✓ Happy-path redemption
  ✓ Double-entry ledger balance invariant
  ✓ Idempotency (duplicate requests return same result)
  ✓ Insufficient reserve balance
  ✓ KYC/AML gate enforcement
  ✓ Inactive account rejection
  ✓ Available balance = balance - reserved
  ✓ Kafka event assertions
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import (
    Account, LedgerEntry, TokenBalance, TokenIssuance, Transaction, TxnStatus,
)
from tests.conftest import OMNIBUS_ID, make_account, make_balance


# ── Import the business logic under test (not FastAPI, just functions) ────────
import sys
sys.path.insert(0, "/home/claude/stablecoin-infra/services/token-issuance")

from main import _issue_tokens, _redeem_tokens


# ─── Helper ───────────────────────────────────────────────────────────────────

def get_balance(db: Session, account_id, currency: str) -> Decimal:
    b = db.execute(
        select(TokenBalance).where(
            TokenBalance.account_id == account_id,
            TokenBalance.currency   == currency,
        )
    ).scalar_one_or_none()
    return b.balance if b else Decimal("0")


# ─── Issuance Tests ───────────────────────────────────────────────────────────

class TestTokenIssuance:

    def test_basic_issuance_credits_account(self, db, alice, omnibus, mock_kafka):
        before = get_balance(db, alice.id, "USD")
        amount = Decimal("1_000_000")

        issuance = _issue_tokens(
            db,
            account_id=str(alice.id),
            currency="USD",
            amount=amount,
            backing_ref="FIAT-DEP-001",
            custodian="JPMorgan",
            idempotency_key=None,
        )

        assert issuance.status == TxnStatus.COMPLETED
        assert issuance.amount == amount
        after = get_balance(db, alice.id, "USD")
        assert after == before + amount

    def test_issuance_debits_omnibus(self, db, alice, omnibus, mock_kafka):
        omnibus_before = get_balance(db, OMNIBUS_ID, "USD")
        amount = Decimal("500_000")

        _issue_tokens(db, str(alice.id), "USD", amount, "DEP-002", None, None)

        omnibus_after = get_balance(db, OMNIBUS_ID, "USD")
        assert omnibus_after == omnibus_before - amount

    def test_double_entry_ledger_balance(self, db, alice, omnibus, mock_kafka):
        """Sum of all debit entries == sum of all credit entries for the issuance."""
        amount = Decimal("750_000")
        ref = f"ISS-{uuid.uuid4().hex[:8]}"

        _issue_tokens(db, str(alice.id), "USD", amount, "DEP-003", None, ref)

        entries = db.execute(
            select(LedgerEntry).where(LedgerEntry.txn_ref == ref)
        ).scalars().all()

        debits  = sum(e.amount for e in entries if e.entry_type == "debit")
        credits = sum(e.amount for e in entries if e.entry_type == "credit")
        assert debits  == amount
        assert credits == amount
        assert len(entries) == 2   # exactly one debit + one credit

    def test_issuance_publishes_kafka_events(self, db, alice, omnibus, mock_kafka):
        _issue_tokens(db, str(alice.id), "USD", Decimal("100_000"), "DEP-004", None, None)

        completed_events = mock_kafka.events_for("token.issuance.completed")
        balance_events   = mock_kafka.events_for("token.balance.updated")

        assert len(completed_events) == 1
        assert len(balance_events)   == 1
        ev = completed_events[0]
        assert ev.account_id == str(alice.id)
        assert ev.currency   == "USD"
        assert ev.amount     == Decimal("100_000")

    def test_idempotency_returns_same_result(self, db, alice, omnibus, mock_kafka):
        key = "ISS-IDEM-001"
        r1  = _issue_tokens(db, str(alice.id), "USD", Decimal("200_000"), "DEP-005", None, key)
        r2  = _issue_tokens(db, str(alice.id), "USD", Decimal("200_000"), "DEP-005", None, key)

        assert r1.issuance_ref == r2.issuance_ref

        # Balance should only increase by 200_000 once
        issuances = db.execute(
            select(TokenIssuance).where(TokenIssuance.issuance_ref == key)
        ).scalars().all()
        assert len(issuances) == 1

    def test_kyc_aml_gate_blocks_issuance(self, db, unverified_account, omnibus, mock_kafka):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _issue_tokens(
                db, str(unverified_account.id), "USD",
                Decimal("1_000"), "DEP-099", None, None,
            )
        assert exc_info.value.status_code == 403
        assert "KYC" in exc_info.value.detail or "AML" in exc_info.value.detail

    def test_inactive_account_blocked(self, db, omnibus, mock_kafka):
        inactive = make_account(db, "Frozen Corp", active=False)
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _issue_tokens(db, str(inactive.id), "USD", Decimal("1_000"), "DEP-100", None, None)
        assert exc_info.value.status_code in (403, 404)

    def test_insufficient_reserve_raises(self, db, omnibus, mock_kafka):
        """Attempting to issue more than the reserve holds should fail."""
        # Drain the omnibus EUR balance first
        bal = db.execute(
            select(TokenBalance).where(
                TokenBalance.account_id == OMNIBUS_ID,
                TokenBalance.currency   == "EUR",
            )
        ).scalar_one()
        bal.balance = Decimal("500")
        db.flush()

        recipient = make_account(db, "Rich Bank")
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _issue_tokens(db, str(recipient.id), "EUR", Decimal("1_000"), "DEP-101", None, None)
        assert exc_info.value.status_code == 422
        assert "reserve" in exc_info.value.detail.lower()

    def test_account_not_found(self, db, omnibus, mock_kafka):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            _issue_tokens(db, str(uuid.uuid4()), "USD", Decimal("1_000"), "DEP-102", None, None)
        assert exc_info.value.status_code == 404


# ─── Redemption Tests ─────────────────────────────────────────────────────────

class TestTokenRedemption:

    def test_basic_redemption_debits_account(self, db, alice, omnibus, mock_kafka):
        before = get_balance(db, alice.id, "USD")
        amount = Decimal("2_000_000")

        redemption = _redeem_tokens(
            db,
            account_id=str(alice.id),
            currency="USD",
            amount=amount,
            settlement_ref="SETTLE-001",
            idempotency_key=None,
        )

        assert redemption.status == TxnStatus.COMPLETED
        after = get_balance(db, alice.id, "USD")
        assert after == before - amount

    def test_redemption_credits_omnibus(self, db, alice, omnibus, mock_kafka):
        omnibus_before = get_balance(db, OMNIBUS_ID, "USD")
        amount = Decimal("1_000_000")

        _redeem_tokens(db, str(alice.id), "USD", amount, None, None)

        omnibus_after = get_balance(db, OMNIBUS_ID, "USD")
        assert omnibus_after == omnibus_before + amount

    def test_redemption_publishes_kafka_events(self, db, alice, omnibus, mock_kafka):
        _redeem_tokens(db, str(alice.id), "USD", Decimal("500_000"), None, None)

        events = mock_kafka.events_for("token.redemption.completed")
        assert len(events) == 1
        assert events[0].account_id == str(alice.id)
        assert events[0].amount     == Decimal("500_000")

    def test_redemption_insufficient_balance(self, db, alice, omnibus, mock_kafka):
        from fastapi import HTTPException
        # Alice has $50M, try to redeem $100M
        with pytest.raises(HTTPException) as exc_info:
            _redeem_tokens(db, str(alice.id), "USD", Decimal("100_000_000"), None, None)
        assert exc_info.value.status_code == 422
        assert "available" in exc_info.value.detail.lower()

    def test_redemption_respects_reserved_balance(self, db, omnibus, mock_kafka):
        """Reserved funds must not be spendable via redemption."""
        acct = make_account(db, "Reserved Corp")
        # Give 1M total but 900K reserved → only 100K available
        make_balance(db, str(acct.id), "USD",
                     balance=Decimal("1_000_000"),
                     reserved=Decimal("900_000"))

        from fastapi import HTTPException
        with pytest.raises(HTTPException):
            # Attempt to redeem 200K (more than available 100K)
            _redeem_tokens(db, str(acct.id), "USD", Decimal("200_000"), None, None)

    def test_redemption_idempotency(self, db, alice, omnibus, mock_kafka):
        key    = "RED-IDEM-001"
        amount = Decimal("100_000")
        r1 = _redeem_tokens(db, str(alice.id), "USD", amount, None, key)
        r2 = _redeem_tokens(db, str(alice.id), "USD", amount, None, key)
        assert r1.issuance_ref == r2.issuance_ref

    def test_double_entry_on_redemption(self, db, alice, omnibus, mock_kafka):
        ref    = f"RED-{uuid.uuid4().hex[:8]}"
        amount = Decimal("300_000")
        _redeem_tokens(db, str(alice.id), "USD", amount, None, ref)

        entries = db.execute(
            select(LedgerEntry).where(LedgerEntry.txn_ref == ref)
        ).scalars().all()
        debits  = sum(e.amount for e in entries if e.entry_type == "debit")
        credits = sum(e.amount for e in entries if e.entry_type == "credit")
        assert debits  == amount
        assert credits == amount

    def test_precision_handling(self, db, alice, omnibus, mock_kafka):
        """8-decimal-place amounts should round correctly."""
        amount = Decimal("999999.999999999")   # beyond 8dp
        issuance = _issue_tokens(db, str(alice.id), "USD", amount, "DEP-PREC", None, None)
        # Should be normalised to 8dp
        assert issuance.amount == amount.quantize(Decimal("0.00000001"))
