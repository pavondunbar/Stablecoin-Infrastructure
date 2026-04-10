"""
services/fx-settlement/main.py
───────────────────────────────
Cross-Border FX Settlement via Blockchain Rails

Responsibilities:
  • Maintain a live FX rate book (sourced internally / from oracle feeds)
  • Generate FX quotes with locked rates and configurable spread
  • Execute atomic PvP (Payment-vs-Payment) FX swaps:
      Leg A: debit seller's sell-currency balance
      Leg B: credit seller's buy-currency balance
      Mirror legs on the FX nostro accounts
  • Supports multiple settlement rails: blockchain, SWIFT, Fedwire, TARGET2
  • Generate simulated blockchain tx hash for on-chain rails
  • Kafka consumer: subscribe to fx.rate.updated for live rate ingestion
  • Background worker: processes queued FX settlements (PvP)

PvP (Payment-vs-Payment) is the FX equivalent of DvP in securities — both
legs settle simultaneously, eliminating Herstatt risk.
"""

import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, ROUND_DOWN
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

import sys
sys.path.insert(0, "/app/shared")

from database import get_db_session, SessionLocal
from metrics import instrument_app, record_business_event
from models import (
    Account, FXRate, FXSettlement, LedgerEntry, TokenBalance, Transaction,
    CurrencyCode, SettlementRails, SettlementStatus, TxnStatus,
)
import kafka_client as kafka
from events import (
    FXRateUpdated, FXSettlementInitiated, FXSettlementLegCompleted,
    FXSettlementCompleted, FXSettlementFailed,
)
from shared.journal import (
    record_journal_pair,
    get_balance as journal_get_balance,
    acquire_balance_lock,
)
from shared.outbox import insert_outbox_event
from shared.status import record_status
from shared.models import (
    FXSettlementStatusHistory,
    TransactionStatusHistory,
    JournalEntry,
)
from shared.blockchain_sim import record_on_chain

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
SERVICE     = os.environ.get("SERVICE_NAME", "fx-settlement")
PRECISION   = Decimal("0.00000001")
RATE_PREC   = Decimal("0.00000001")
SIGNING_GATEWAY_URL = os.environ.get(
    "SIGNING_GATEWAY_URL", "http://signing-gateway:8006"
)

# Nostro account mapping — in production these are real correspondent bank accounts
NOSTRO_MAP = {
    "USD": "00000000-0000-0000-0000-000000000002",
    "EUR": "00000000-0000-0000-0000-000000000003",
    "GBP": "00000000-0000-0000-0000-000000000004",
}


# ─── FX Rate Management ───────────────────────────────────────────────────────

def _get_live_rate(db: Session, base: str, quote: str) -> Optional[FXRate]:
    """Fetch the most recent active rate for a currency pair."""
    return db.execute(
        select(FXRate)
        .where(
            FXRate.base_currency  == base,
            FXRate.quote_currency == quote,
            FXRate.is_active      == True,
        )
        .order_by(desc(FXRate.valid_from))
        .limit(1)
    ).scalar_one_or_none()


def _apply_spread(rate: FXRate, direction: str, custom_bps: Optional[Decimal] = None) -> Decimal:
    """
    Apply bid/ask spread to the mid rate.
    direction='buy': customer buys quote currency → apply ask
    direction='sell': customer sells quote currency → apply bid
    """
    spread_bps = custom_bps or rate.spread_bps or Decimal("10")
    spread_factor = spread_bps / Decimal("10000")
    half_spread   = rate.mid_rate * spread_factor / 2

    if direction == "buy":
        return (rate.mid_rate + half_spread).quantize(RATE_PREC)
    else:
        return (rate.mid_rate - half_spread).quantize(RATE_PREC)


def _sign_settlement(
    settlement_ref: str, settlement_data: dict
) -> str:
    """Call signing gateway for MPC-signed hash."""
    import httpx
    try:
        resp = httpx.post(
            f"{SIGNING_GATEWAY_URL}/sign",
            json={
                "transaction_id": settlement_ref,
                "payload": settlement_data,
            },
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json().get("signature", "")
        return ""
    except Exception:
        return ""


# ─── PvP Settlement Core ──────────────────────────────────────────────────────

def _execute_leg(
    db: Session,
    debit_id: str,
    credit_id: str,
    currency: str,
    amount: Decimal,
    narrative: str,
    leg_type: str = "sell",
) -> Transaction:
    """Execute a single FX settlement leg via journal entries.

    Args:
        leg_type: 'sell' or 'buy' — determines COA codes.
          sell: debit sender INSTITUTION_LIABILITY,
                credit nostro FX_NOSTRO_{currency}
          buy:  debit nostro FX_NOSTRO_{currency},
                credit receiver INSTITUTION_LIABILITY
    """
    if leg_type == "sell":
        debit_coa = "INSTITUTION_LIABILITY"
        credit_coa = f"FX_NOSTRO_{currency}"
    else:
        debit_coa = f"FX_NOSTRO_{currency}"
        credit_coa = "INSTITUTION_LIABILITY"

    acquire_balance_lock(db, debit_id, currency)

    available = journal_get_balance(
        db, debit_id, currency, coa_code=debit_coa,
    )
    if available < amount:
        raise ValueError(
            f"Insufficient {currency} balance for {debit_id}: "
            f"available={available}"
        )

    ref = f"FX-{uuid.uuid4().hex[:16].upper()}"

    txn = Transaction(
        txn_ref=ref,
        debit_account_id=debit_id,
        credit_account_id=credit_id,
        currency=currency,
        amount=amount,
        txn_type="fx_leg",
        status=TxnStatus.COMPLETED,
        settled_at=datetime.now(timezone.utc),
    )
    db.add(txn)
    db.flush()

    record_journal_pair(
        db,
        account_id=debit_id,
        coa_code=debit_coa,
        currency=currency,
        amount=amount,
        entry_type="fx_leg",
        reference_id=str(txn.id),
        counter_account_id=credit_id,
        counter_coa_code=credit_coa,
        narrative=narrative,
    )

    record_status(
        db,
        TransactionStatusHistory,
        entity_id_field="transaction_id",
        entity_id=txn.id,
        status="completed",
    )

    return txn


def _process_fx_settlement(db: Session, fx: FXSettlement) -> bool:
    """Execute a PvP FX settlement atomically.

    Sell leg: sender pays sell_currency to FX nostro
    Buy  leg: FX nostro pays buy_currency to receiver
    Both legs execute in the same DB transaction.
    """
    fx.status = SettlementStatus.PROCESSING
    record_status(
        db, FXSettlementStatusHistory,
        entity_id_field="settlement_id",
        entity_id=fx.id, status="processing",
    )
    db.flush()

    try:
        settle_ref = fx.settlement_ref

        # -- Leg A: sender -> nostro (sell currency) -----------------------
        sell_nostro = NOSTRO_MAP.get(
            fx.sell_currency.value, NOSTRO_MAP["USD"]
        )
        sell_txn = _execute_leg(
            db,
            debit_id=str(fx.sending_account_id),
            credit_id=sell_nostro,
            currency=fx.sell_currency.value,
            amount=fx.sell_amount,
            narrative=(
                f"FX sell leg {settle_ref}: "
                f"sell {fx.sell_currency.value}"
            ),
            leg_type="sell",
        )
        fx.sell_txn_id = sell_txn.id
        db.flush()
        insert_outbox_event(
            db, settle_ref,
            "fx.settlement.leg.completed",
            FXSettlementLegCompleted(
                service=SERVICE,
                settlement_ref=settle_ref,
                leg="sell",
                transaction_id=str(sell_txn.id),
                amount=fx.sell_amount,
                currency=fx.sell_currency.value,
            ),
        )

        # -- Leg B: nostro -> receiver (buy currency) ----------------------
        buy_nostro = NOSTRO_MAP.get(
            fx.buy_currency.value, NOSTRO_MAP["USD"]
        )
        buy_txn = _execute_leg(
            db,
            debit_id=buy_nostro,
            credit_id=str(fx.receiving_account_id),
            currency=fx.buy_currency.value,
            amount=fx.buy_amount,
            narrative=(
                f"FX buy leg {settle_ref}: "
                f"buy {fx.buy_currency.value}"
            ),
            leg_type="buy",
        )
        fx.buy_txn_id = buy_txn.id
        db.flush()
        insert_outbox_event(
            db, settle_ref,
            "fx.settlement.leg.completed",
            FXSettlementLegCompleted(
                service=SERVICE,
                settlement_ref=settle_ref,
                leg="buy",
                transaction_id=str(buy_txn.id),
                amount=fx.buy_amount,
                currency=fx.buy_currency.value,
            ),
        )

        # -- Finalise settlement -------------------------------------------
        now = datetime.now(timezone.utc)
        fx.status = SettlementStatus.SETTLED
        fx.settled_at = now

        # Record on simulated blockchain
        receipt = record_on_chain(
            settle_ref, "fx_settlement"
        )
        fx.blockchain_tx_hash = receipt["tx_hash"]
        fx.extra_metadata = {
            **fx.extra_metadata, "blockchain": receipt
        }

        # MPC signing for blockchain rails
        if fx.rails == SettlementRails.BLOCKCHAIN:
            mpc_sig = _sign_settlement(
                settle_ref,
                {
                    "sell_currency": fx.sell_currency.value,
                    "sell_amount": str(fx.sell_amount),
                    "buy_currency": fx.buy_currency.value,
                    "buy_amount": str(fx.buy_amount),
                },
            )
            if mpc_sig:
                fx.extra_metadata["blockchain"][
                    "mpc_signature"
                ] = mpc_sig

        record_status(
            db, FXSettlementStatusHistory,
            entity_id_field="settlement_id",
            entity_id=fx.id, status="settled",
        )
        db.flush()

        insert_outbox_event(
            db, settle_ref,
            "fx.settlement.completed",
            FXSettlementCompleted(
                service=SERVICE,
                settlement_ref=settle_ref,
                sell_txn_id=str(sell_txn.id),
                buy_txn_id=str(buy_txn.id),
                blockchain_hash=fx.blockchain_tx_hash,
                settled_at=now,
            ),
        )
        log.info(
            "FX settled: %s  %s %s -> %s %s  rate=%s  rails=%s  hash=%s",
            settle_ref, fx.sell_amount, fx.sell_currency.value,
            fx.buy_amount, fx.buy_currency.value, fx.applied_rate,
            fx.rails.value, fx.blockchain_tx_hash or "n/a",
        )
        return True

    except ValueError as exc:
        fx.status = SettlementStatus.FAILED
        record_status(
            db, FXSettlementStatusHistory,
            entity_id_field="settlement_id",
            entity_id=fx.id, status="failed",
            detail={"reason": str(exc)},
        )
        db.flush()
        insert_outbox_event(
            db, fx.settlement_ref,
            "fx.settlement.failed",
            FXSettlementFailed(
                service=SERVICE,
                settlement_ref=fx.settlement_ref,
                reason=str(exc),
            ),
        )
        log.error(
            "FX settlement failed: %s reason=%s",
            fx.settlement_ref, exc,
        )
        return False


def _fx_settlement_worker():
    """Background thread: processes queued FX settlements in order."""
    log.info("FX settlement worker started.")
    while True:
        try:
            db = SessionLocal()
            try:
                pending = db.execute(
                    select(FXSettlement)
                    .where(FXSettlement.status == SettlementStatus.QUEUED)
                    .order_by(FXSettlement.created_at.asc())
                    .limit(5)
                    .with_for_update(skip_locked=True)
                ).scalars().all()

                if not pending:
                    time.sleep(0.5)
                    continue

                for fx in pending:
                    _process_fx_settlement(db, fx)
                db.commit()
            except Exception as exc:
                db.rollback()
                log.exception("FX worker error: %s", exc)
                time.sleep(2)
            finally:
                db.close()
        except Exception as exc:
            log.exception("FX worker outer error: %s", exc)
            time.sleep(5)


# ─── Kafka Rate Consumer ──────────────────────────────────────────────────────

def _rate_update_handler(topic: str, payload: dict):
    """Ingest real-time FX rate updates from Kafka and persist to DB."""
    try:
        db = SessionLocal()
        try:
            # Deactivate old rate for this pair
            old_rates = db.execute(
                select(FXRate).where(
                    FXRate.base_currency  == payload["base_currency"],
                    FXRate.quote_currency == payload["quote_currency"],
                    FXRate.is_active      == True,
                )
            ).scalars().all()
            for r in old_rates:
                r.is_active = False

            mid = Decimal(str(payload["mid_rate"]))
            new_rate = FXRate(
                base_currency=payload["base_currency"],
                quote_currency=payload["quote_currency"],
                mid_rate=mid,
                bid_rate=Decimal(str(payload["bid_rate"])) if payload.get("bid_rate") else None,
                ask_rate=Decimal(str(payload["ask_rate"])) if payload.get("ask_rate") else None,
                source=payload.get("source", "kafka"),
                is_active=True,
            )
            db.add(new_rate)
            db.commit()
            log.info("FX rate updated: %s/%s = %s", payload["base_currency"], payload["quote_currency"], mid)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        log.exception("Rate update handler error: %s", exc)


def _start_rate_consumer():
    consumer = kafka.build_consumer(
        group_id="fx-settlement-rate-consumer",
        topics=["fx.rate.updated"],
    )
    kafka.consume_loop(consumer, _rate_update_handler)


# ─── API Schemas ──────────────────────────────────────────────────────────────

class FXQuoteRequest(BaseModel):
    sell_currency: CurrencyCode
    sell_amount:   Decimal = Field(gt=0)
    buy_currency:  CurrencyCode


class FXQuoteResponse(BaseModel):
    sell_currency: str
    sell_amount:   Decimal
    buy_currency:  str
    buy_amount:    Decimal
    applied_rate:  Decimal
    mid_rate:      Decimal
    spread_bps:    Optional[Decimal]
    quote_valid_until: datetime


class InitiateFXSettlementRequest(BaseModel):
    sending_account_id:   str
    receiving_account_id: str
    sell_currency:        CurrencyCode
    sell_amount:          Decimal = Field(gt=0)
    buy_currency:         CurrencyCode
    rails:                SettlementRails = SettlementRails.BLOCKCHAIN
    value_date:           Optional[date] = None
    metadata:             dict = Field(default_factory=dict)
    idempotency_key:      Optional[str] = None


# ─── FastAPI App ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker   = threading.Thread(target=_fx_settlement_worker, daemon=True, name="fx-worker")
    consumer = threading.Thread(target=_start_rate_consumer,  daemon=True, name="fx-rate-consumer")
    worker.start()
    consumer.start()
    log.info("FX Settlement Service started.")
    yield
    log.info("FX Settlement Service shutting down.")


app = FastAPI(
    title="FX Settlement Service",
    version="1.0.0",
    description="Cross-border FX settlement with PvP atomicity via blockchain rails",
    lifespan=lifespan,
)
instrument_app(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/fx/rates")
def get_fx_rates(db: Session = Depends(get_db_session)):
    """Return all active FX rates."""
    rates = db.execute(
        select(FXRate).where(FXRate.is_active == True).order_by(FXRate.valid_from.desc())
    ).scalars().all()
    return [
        {
            "base":       r.base_currency.value,
            "quote":      r.quote_currency.value,
            "mid_rate":   str(r.mid_rate),
            "bid_rate":   str(r.bid_rate) if r.bid_rate else None,
            "ask_rate":   str(r.ask_rate) if r.ask_rate else None,
            "spread_bps": str(r.spread_bps) if r.spread_bps else None,
            "source":     r.source,
            "updated_at": r.valid_from,
        }
        for r in rates
    ]


@app.post("/fx/quote", response_model=FXQuoteResponse)
def get_fx_quote(req: FXQuoteRequest, db: Session = Depends(get_db_session)):
    """
    Generate an indicative FX quote.  Rate is locked for 30 seconds.
    The buy amount = sell_amount × applied_rate (ask side of spread).
    """
    if req.sell_currency == req.buy_currency:
        raise HTTPException(status_code=400, detail="Sell and buy currencies must differ")

    rate = _get_live_rate(db, req.sell_currency.value, req.buy_currency.value)
    if not rate:
        # Try inverse pair
        inv_rate = _get_live_rate(db, req.buy_currency.value, req.sell_currency.value)
        if not inv_rate:
            raise HTTPException(
                status_code=404,
                detail=f"No FX rate available for {req.sell_currency}/{req.buy_currency}"
            )
        # Invert: mid = 1 / inv_mid
        applied = (Decimal("1") / _apply_spread(inv_rate, "sell")).quantize(RATE_PREC)
        mid     = (Decimal("1") / inv_rate.mid_rate).quantize(RATE_PREC)
        spread  = inv_rate.spread_bps
    else:
        applied = _apply_spread(rate, "buy")
        mid     = rate.mid_rate
        spread  = rate.spread_bps

    sell_amount = req.sell_amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)
    buy_amount  = (sell_amount * applied).quantize(PRECISION, rounding=ROUND_DOWN)

    return FXQuoteResponse(
        sell_currency=req.sell_currency.value,
        sell_amount=sell_amount,
        buy_currency=req.buy_currency.value,
        buy_amount=buy_amount,
        applied_rate=applied,
        mid_rate=mid,
        spread_bps=spread,
        quote_valid_until=datetime.now(timezone.utc) + timedelta(seconds=30),
    )


@app.post("/fx/settle", status_code=status.HTTP_202_ACCEPTED)
def initiate_fx_settlement(
    req: InitiateFXSettlementRequest,
    db: Session = Depends(get_db_session),
):
    """
    Initiate a cross-border FX settlement.
    A PvP atomic swap is queued and executed by the background worker.
    Both sell and buy legs are committed in the same DB transaction.
    """
    if req.sell_currency == req.buy_currency:
        raise HTTPException(status_code=400, detail="Currency pair must differ")

    # Idempotency guard
    if req.idempotency_key:
        existing = db.execute(
            select(FXSettlement).where(
                FXSettlement.settlement_ref == req.idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return {
                "settlement_ref":      existing.settlement_ref,
                "status":              existing.status.value,
                "sell_currency":       existing.sell_currency.value,
                "sell_amount":         str(existing.sell_amount),
                "buy_currency":        existing.buy_currency.value,
                "buy_amount":          str(existing.buy_amount),
                "applied_rate":        str(existing.applied_rate),
                "rails":               existing.rails.value,
                "value_date":          str(existing.value_date),
            }

    sender   = db.get(Account, req.sending_account_id)
    receiver = db.get(Account, req.receiving_account_id)
    if not sender or not sender.is_active:
        raise HTTPException(status_code=404, detail="Sending account not found")
    if not receiver or not receiver.is_active:
        raise HTTPException(status_code=404, detail="Receiving account not found")

    # Rate lookup
    rate = _get_live_rate(db, req.sell_currency.value, req.buy_currency.value)
    if not rate:
        inv_rate = _get_live_rate(db, req.buy_currency.value, req.sell_currency.value)
        if not inv_rate:
            raise HTTPException(status_code=404, detail="No FX rate available")
        applied_rate = (Decimal("1") / _apply_spread(inv_rate, "sell")).quantize(RATE_PREC)
        fx_rate_id   = inv_rate.id
    else:
        applied_rate = _apply_spread(rate, "buy")
        fx_rate_id   = rate.id

    sell_amount = req.sell_amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)
    buy_amount  = (sell_amount * applied_rate).quantize(PRECISION, rounding=ROUND_DOWN)
    ref         = req.idempotency_key or f"FXS-{uuid.uuid4().hex[:16].upper()}"

    fx = FXSettlement(
        settlement_ref=ref,
        sending_account_id=req.sending_account_id,
        receiving_account_id=req.receiving_account_id,
        sell_currency=req.sell_currency,
        sell_amount=sell_amount,
        buy_currency=req.buy_currency,
        buy_amount=buy_amount,
        applied_rate=applied_rate,
        fx_rate_id=fx_rate_id,
        rails=req.rails,
        status=SettlementStatus.QUEUED,
        value_date=req.value_date or date.today(),
        extra_metadata=req.metadata,
    )
    db.add(fx)
    db.flush()

    record_status(
        db, FXSettlementStatusHistory,
        entity_id_field="settlement_id",
        entity_id=fx.id, status="queued",
    )
    insert_outbox_event(
        db, ref,
        "fx.settlement.initiated",
        FXSettlementInitiated(
            service=SERVICE,
            settlement_ref=ref,
            sending_account_id=req.sending_account_id,
            receiving_account_id=req.receiving_account_id,
            sell_currency=req.sell_currency.value,
            sell_amount=sell_amount,
            buy_currency=req.buy_currency.value,
            buy_amount=buy_amount,
            applied_rate=applied_rate,
            rails=req.rails.value,
        ),
    )

    return {
        "settlement_ref":      ref,
        "status":              fx.status.value,
        "sell_currency":       req.sell_currency.value,
        "sell_amount":         str(sell_amount),
        "buy_currency":        req.buy_currency.value,
        "buy_amount":          str(buy_amount),
        "applied_rate":        str(applied_rate),
        "rails":               req.rails.value,
        "value_date":          str(fx.value_date),
        "estimated_settlement": "< 5 seconds (blockchain)" if req.rails == SettlementRails.BLOCKCHAIN else "T+1",
    }


@app.get("/fx/settlements/{settlement_ref}")
def get_fx_settlement(settlement_ref: str, db: Session = Depends(get_db_session)):
    fx = db.execute(
        select(FXSettlement).where(FXSettlement.settlement_ref == settlement_ref)
    ).scalar_one_or_none()
    if not fx:
        raise HTTPException(status_code=404, detail="FX settlement not found")
    return {
        "settlement_ref":      fx.settlement_ref,
        "status":              fx.status.value,
        "sell_currency":       fx.sell_currency.value,
        "sell_amount":         str(fx.sell_amount),
        "buy_currency":        fx.buy_currency.value,
        "buy_amount":          str(fx.buy_amount),
        "applied_rate":        str(fx.applied_rate),
        "rails":               fx.rails.value,
        "blockchain_tx_hash":  fx.blockchain_tx_hash,
        "sell_txn_id":         str(fx.sell_txn_id) if fx.sell_txn_id else None,
        "buy_txn_id":          str(fx.buy_txn_id)  if fx.buy_txn_id  else None,
        "value_date":          str(fx.value_date),
        "created_at":          fx.created_at,
        "settled_at":          fx.settled_at,
        "blockchain":          fx.extra_metadata.get("blockchain"),
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8004))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
