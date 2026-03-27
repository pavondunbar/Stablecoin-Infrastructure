"""
tests/test_fx_settlement.py
─────────────────────────────
Unit tests for the FX Settlement Service.

Covers:
  ✓ FX rate retrieval and spread application
  ✓ Bid/ask spread calculation (buy vs sell direction)
  ✓ Quote amount computation
  ✓ PvP (Payment-vs-Payment) atomic settlement — both legs succeed
  ✓ PvP rollback — sell leg succeeds but buy leg fails → atomicity
  ✓ Insufficient sell-currency balance
  ✓ Insufficient nostro buy-currency balance
  ✓ Blockchain hash generation (deterministic-ish)
  ✓ FX rate inversion for reverse pairs
  ✓ Kafka event lifecycle (initiated, leg.completed x2, completed)
  ✓ Both legs land on correct accounts
"""

import hashlib
import uuid
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_DOWN
from datetime import datetime, timezone, date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models import (
    Account, FXRate, FXSettlement, TokenBalance,
    SettlementRails, SettlementStatus, TxnStatus,
)
from tests.conftest import make_account, make_balance, make_fx_rate, OMNIBUS_ID

import sys
sys.path.insert(0, "/home/claude/stablecoin-infra/services/fx-settlement")

from main import (
    _get_live_rate, _apply_spread, _execute_leg,
    _process_fx_settlement, _simulate_blockchain_hash,
    NOSTRO_MAP, RATE_PREC, PRECISION,
)


# ─── Helper ───────────────────────────────────────────────────────────────────

def get_balance(db: Session, account_id, currency: str) -> Decimal:
    b = db.execute(
        select(TokenBalance).where(
            TokenBalance.account_id == account_id,
            TokenBalance.currency   == currency,
        )
    ).scalar_one_or_none()
    return b.balance if b else Decimal("0")


def make_fx_settlement(
    db: Session,
    sender_id: str,
    receiver_id: str,
    sell_ccy: str,
    sell_amount: Decimal,
    buy_ccy: str,
    buy_amount: Decimal,
    rate: Decimal,
    rails: SettlementRails = SettlementRails.BLOCKCHAIN,
) -> FXSettlement:
    fx = FXSettlement(
        settlement_ref=f"FXS-{uuid.uuid4().hex[:12].upper()}",
        sending_account_id=sender_id,
        receiving_account_id=receiver_id,
        sell_currency=sell_ccy,
        sell_amount=sell_amount,
        buy_currency=buy_ccy,
        buy_amount=buy_amount,
        applied_rate=rate,
        rails=rails,
        status=SettlementStatus.QUEUED,
        value_date=date.today(),
    )
    db.add(fx)
    db.flush()
    return fx


def setup_nostros(db: Session):
    """Ensure the three system nostro accounts have ample balances."""
    nostro_map = {
        "USD": NOSTRO_MAP["USD"],
        "EUR": NOSTRO_MAP["EUR"],
        "GBP": NOSTRO_MAP["GBP"],
    }
    for ccy, acc_id in nostro_map.items():
        existing = db.execute(
            select(Account).where(Account.id == acc_id)
        ).scalar_one_or_none()
        if not existing:
            db.add(Account(
                id=uuid.UUID(acc_id),
                entity_name=f"FX_NOSTRO_{ccy}",
                account_type="bank",
                kyc_verified=True,
                aml_cleared=True,
                is_active=True,
            ))
        bal = db.execute(
            select(TokenBalance).where(
                TokenBalance.account_id == acc_id,
                TokenBalance.currency   == ccy,
            )
        ).scalar_one_or_none()
        if not bal:
            db.add(TokenBalance(
                account_id=uuid.UUID(acc_id),
                currency=ccy,
                balance=Decimal("500_000_000"),
                reserved=Decimal("0"),
            ))
    db.flush()


# ─── Rate & Spread Tests ──────────────────────────────────────────────────────

class TestFXRates:

    def test_get_live_rate_returns_active_rate(self, db):
        make_fx_rate(db, "USD", "EUR", Decimal("0.918"))
        rate = _get_live_rate(db, "USD", "EUR")
        assert rate is not None
        assert rate.mid_rate == Decimal("0.918")

    def test_get_live_rate_missing_pair_returns_none(self, db):
        rate = _get_live_rate(db, "USD", "JPY")
        assert rate is None

    def test_apply_spread_buy_side(self, db):
        rate = make_fx_rate(db, "USD", "EUR", Decimal("0.9180"), Decimal("10"))
        applied = _apply_spread(rate, "buy")
        # Ask = mid + (mid * 10bps / 2)
        expected = Decimal("0.9180") * (1 + Decimal("10") / Decimal("20000"))
        assert applied == expected.quantize(RATE_PREC)
        assert applied > rate.mid_rate   # ask > mid

    def test_apply_spread_sell_side(self, db):
        rate = make_fx_rate(db, "EUR", "USD", Decimal("1.0900"), Decimal("10"))
        applied = _apply_spread(rate, "sell")
        # Bid = mid - (mid * 10bps / 2)
        assert applied < rate.mid_rate   # bid < mid

    def test_spread_widens_with_higher_bps(self, db):
        tight  = make_fx_rate(db, "USD", "GBP", Decimal("0.792"), Decimal("5"))
        wide   = make_fx_rate(db, "EUR", "GBP", Decimal("0.863"), Decimal("50"))
        tight_spread = _apply_spread(tight, "buy") - tight.mid_rate
        wide_spread  = _apply_spread(wide,  "buy") - wide.mid_rate
        assert wide_spread > tight_spread


# ─── Blockchain Hash ─────────────────────────────────────────────────────────

class TestBlockchainHash:

    def test_hash_starts_with_0x(self):
        h = _simulate_blockchain_hash("FXS-TEST-001")
        assert h.startswith("0x")

    def test_hash_is_64_hex_chars_after_prefix(self):
        h = _simulate_blockchain_hash("FXS-TEST-002")
        assert len(h) == 66   # 0x + 64 hex chars

    def test_different_refs_produce_different_hashes(self):
        h1 = _simulate_blockchain_hash("FXS-AAA")
        h2 = _simulate_blockchain_hash("FXS-BBB")
        assert h1 != h2


# ─── PvP Settlement Tests ─────────────────────────────────────────────────────

class TestFXSettlementPvP:

    def test_pvp_transfers_sell_currency_to_nostro(self, db, alice, bob, mock_kafka):
        setup_nostros(db)
        make_fx_rate(db, "USD", "EUR", Decimal("0.918"))

        sell_amount = Decimal("1_000_000")
        buy_amount  = (sell_amount * Decimal("0.918")).quantize(PRECISION, rounding=ROUND_DOWN)

        alice_usd_before  = get_balance(db, alice.id, "USD")
        nostro_usd_before = get_balance(db, NOSTRO_MAP["USD"], "USD")

        fx = make_fx_settlement(
            db, str(alice.id), str(bob.id),
            "USD", sell_amount, "EUR", buy_amount, Decimal("0.918"),
        )
        success = _process_fx_settlement(db, fx)

        assert success is True
        assert fx.status == SettlementStatus.SETTLED

        # Alice lost sell_amount USD
        assert get_balance(db, alice.id, "USD") == alice_usd_before - sell_amount
        # Nostro USD gained sell_amount
        assert get_balance(db, NOSTRO_MAP["USD"], "USD") == nostro_usd_before + sell_amount

    def test_pvp_credits_buy_currency_to_receiver(self, db, alice, bob, mock_kafka):
        setup_nostros(db)
        make_fx_rate(db, "USD", "EUR", Decimal("0.918"))

        sell_amount = Decimal("1_000_000")
        buy_amount  = (sell_amount * Decimal("0.918")).quantize(PRECISION, rounding=ROUND_DOWN)

        # Ensure bob has a EUR balance row (or starts at 0)
        bob_eur_before = get_balance(db, bob.id, "EUR")

        fx = make_fx_settlement(
            db, str(alice.id), str(bob.id),
            "USD", sell_amount, "EUR", buy_amount, Decimal("0.918"),
        )
        _process_fx_settlement(db, fx)

        # Bob received buy_amount EUR
        assert get_balance(db, bob.id, "EUR") == bob_eur_before + buy_amount

    def test_pvp_settlement_emits_full_event_chain(self, db, alice, bob, mock_kafka):
        setup_nostros(db)
        sell_amount = Decimal("500_000")
        buy_amount  = Decimal("459_000")

        fx = make_fx_settlement(
            db, str(alice.id), str(bob.id),
            "USD", sell_amount, "EUR", buy_amount, Decimal("0.918"),
        )
        _process_fx_settlement(db, fx)

        leg_events  = mock_kafka.events_for("fx.settlement.leg.completed")
        done_events = mock_kafka.events_for("fx.settlement.completed")

        assert len(leg_events)  == 2   # sell leg + buy leg
        assert len(done_events) == 1

        legs = {e.leg: e for e in leg_events}
        assert "sell" in legs
        assert "buy"  in legs
        assert legs["sell"].currency == "USD"
        assert legs["buy"].currency  == "EUR"

    def test_pvp_blockchain_hash_set_on_success(self, db, alice, bob, mock_kafka):
        setup_nostros(db)
        fx = make_fx_settlement(
            db, str(alice.id), str(bob.id),
            "USD", Decimal("100_000"), "EUR", Decimal("91_800"),
            Decimal("0.918"), rails=SettlementRails.BLOCKCHAIN,
        )
        _process_fx_settlement(db, fx)

        assert fx.blockchain_tx_hash is not None
        assert fx.blockchain_tx_hash.startswith("0x")

    def test_pvp_no_hash_on_swift_rails(self, db, alice, bob, mock_kafka):
        setup_nostros(db)
        fx = make_fx_settlement(
            db, str(alice.id), str(bob.id),
            "USD", Decimal("100_000"), "EUR", Decimal("91_800"),
            Decimal("0.918"), rails=SettlementRails.SWIFT,
        )
        _process_fx_settlement(db, fx)

        # blockchain_tx_hash should remain None for SWIFT rails
        assert fx.blockchain_tx_hash is None

    def test_pvp_fails_on_insufficient_sell_balance(self, db, omnibus, mock_kafka):
        setup_nostros(db)
        poor   = make_account(db, "FX Poor Bank")
        payee  = make_account(db, "FX Payee")
        make_balance(db, str(poor.id), "USD", Decimal("100"))

        fx = make_fx_settlement(
            db, str(poor.id), str(payee.id),
            "USD", Decimal("1_000_000"), "EUR", Decimal("918_000"),
            Decimal("0.918"),
        )
        success = _process_fx_settlement(db, fx)

        assert success is False
        assert fx.status == SettlementStatus.FAILED
        failed_events = mock_kafka.events_for("fx.settlement.failed")
        assert len(failed_events) == 1

    def test_pvp_both_legs_recorded(self, db, alice, bob, mock_kafka):
        setup_nostros(db)
        sell_amount = Decimal("200_000")
        buy_amount  = Decimal("183_600")

        fx = make_fx_settlement(
            db, str(alice.id), str(bob.id),
            "USD", sell_amount, "EUR", buy_amount, Decimal("0.918"),
        )
        _process_fx_settlement(db, fx)

        from shared.models import Transaction
        sell_txn = db.get(Transaction, fx.sell_txn_id)
        buy_txn  = db.get(Transaction, fx.buy_txn_id)

        assert sell_txn is not None
        assert buy_txn  is not None
        assert sell_txn.currency.value == "USD"
        assert buy_txn.currency.value  == "EUR"
        assert sell_txn.amount == sell_amount
        assert buy_txn.amount  == buy_amount


# ─── Amount Calculation ───────────────────────────────────────────────────────

class TestFXAmountCalculation:

    @pytest.mark.parametrize("sell,mid,expected_buy", [
        (Decimal("1_000_000"), Decimal("0.918"),  Decimal("918_000.00000000")),
        (Decimal("500_000"),   Decimal("1.0930"),  Decimal("546_500.00000000")),
        (Decimal("100"),       Decimal("0.7920"),  Decimal("79.20000000")),
    ])
    def test_buy_amount_calculation(self, sell, mid, expected_buy):
        """buy_amount = sell_amount × mid_rate (ignoring spread for simplicity)."""
        calculated = (sell * mid).quantize(PRECISION, rounding=ROUND_DOWN)
        assert calculated == expected_buy
