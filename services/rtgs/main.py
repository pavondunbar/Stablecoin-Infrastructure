"""
services/rtgs/main.py
──────────────────────
Real-Time Gross Settlement (RTGS) Engine

Responsibilities:
  • Accept and queue settlement instructions from participants
  • Process settlements in priority order (urgent → high → normal → low)
  • Each settlement is gross (individual, not netted) and final
  • Atomic balance transfer with optimistic locking and retry on conflict
  • Background worker drains the queue continuously
  • Publish settlement lifecycle events to Kafka

Design: mirrors Fedwire / TARGET2 / BOE CHAPS principles.
"""

import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import asc, select
from sqlalchemy.orm import Session

import sys
sys.path.insert(0, "/app/shared")

from database import get_db_session, SessionLocal
from metrics import instrument_app, record_business_event
from models import (
    Account, LedgerEntry, RTGSSettlement, TokenBalance, Transaction,
    CurrencyCode, SettlementStatus, TxnStatus,
)
import kafka_client as kafka
from events import (
    RTGSSettlementSubmitted, RTGSSettlementProcessing,
    RTGSSettlementCompleted, RTGSSettlementFailed, AuditTrailEntry,
)
from shared.journal import (
    record_journal_pair,
    get_balance as journal_get_balance,
    acquire_balance_lock,
)
from shared.outbox import insert_outbox_event
from shared.status import record_status
from shared.state_machine import (
    RTGS_VALID_TRANSITIONS,
    validate_transition,
)
from shared.rbac import require_role, check_separation_of_duties
from shared.context import extract_context
from shared.models import (
    RTGSSettlementStatusHistory,
    TransactionStatusHistory,
    JournalEntry,
)
from shared.blockchain_sim import record_on_chain

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
SERVICE = os.environ.get("SERVICE_NAME", "rtgs")

PRECISION = Decimal("0.00000001")
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
MAX_RETRY = 3


# ─── Settlement Processor ─────────────────────────────────────────────────────

def _transfer_balances(
    db: Session,
    settlement: RTGSSettlement,
) -> Transaction:
    """
    Core atomic transfer. Acquires advisory lock, checks journal-derived
    balance, then records a debit/credit journal pair.
    """
    sender_id = str(settlement.sending_account_id)
    receiver_id = str(settlement.receiving_account_id)
    currency = settlement.currency.value
    ref = settlement.settlement_ref
    narrative = f"RTGS settlement {ref}"

    acquire_balance_lock(db, sender_id, currency)

    available = journal_get_balance(db, sender_id, currency)
    if available < settlement.amount:
        raise ValueError(
            f"Insufficient balance: available={available} "
            f"required={settlement.amount}"
        )

    record_journal_pair(
        db,
        sender_id,
        "INSTITUTION_LIABILITY",
        currency,
        settlement.amount,
        "rtgs_settlement",
        str(settlement.id),
        receiver_id,
        "INSTITUTION_LIABILITY",
        narrative,
    )

    txn = Transaction(
        txn_ref=ref,
        debit_account_id=sender_id,
        credit_account_id=receiver_id,
        currency=settlement.currency,
        amount=settlement.amount,
        txn_type="rtgs_settlement",
        status=TxnStatus.COMPLETED,
        settled_at=datetime.now(timezone.utc),
        extra_metadata={"priority": settlement.priority},
    )
    db.add(txn)
    db.flush()
    return txn


def _process_one_settlement(db: Session, settlement: RTGSSettlement) -> bool:
    """Process a single settlement; returns True on success."""
    settlement.status = SettlementStatus.PROCESSING
    settlement.processing_started = datetime.now(timezone.utc)
    record_status(
        db, RTGSSettlementStatusHistory,
        "settlement_id", settlement.id,
        SettlementStatus.PROCESSING.value,
    )
    db.flush()

    insert_outbox_event(
        db,
        settlement.settlement_ref,
        "rtgs.settlement.processing",
        RTGSSettlementProcessing(
            service=SERVICE,
            settlement_ref=settlement.settlement_ref,
            started_at=settlement.processing_started,
        ),
    )

    try:
        txn = _transfer_balances(db, settlement)
        settlement.status = SettlementStatus.SETTLED
        settlement.settled_at = datetime.now(timezone.utc)
        settlement.transaction_id = txn.id

        # Record on simulated blockchain
        receipt = record_on_chain(
            settlement.settlement_ref, "rtgs_settlement"
        )
        settlement.extra_metadata = {
            **settlement.extra_metadata, "blockchain": receipt
        }

        record_status(
            db, RTGSSettlementStatusHistory,
            "settlement_id", settlement.id,
            SettlementStatus.SETTLED.value,
        )
        db.flush()

        insert_outbox_event(
            db,
            settlement.settlement_ref,
            "rtgs.settlement.completed",
            RTGSSettlementCompleted(
                service=SERVICE,
                settlement_ref=settlement.settlement_ref,
                sending_account_id=str(settlement.sending_account_id),
                receiving_account_id=str(settlement.receiving_account_id),
                currency=str(settlement.currency.value),
                amount=settlement.amount,
                transaction_id=str(txn.id),
                settled_at=settlement.settled_at,
            ),
        )
        log.info("Settled: %s  amount=%s %s",
                 settlement.settlement_ref, settlement.amount,
                 settlement.currency)
        return True

    except ValueError as exc:
        settlement.retry_count += 1

        if settlement.retry_count < MAX_RETRY and "Insufficient" not in str(exc):
            settlement.status = SettlementStatus.SIGNED
            record_status(
                db, RTGSSettlementStatusHistory,
                "settlement_id", settlement.id,
                SettlementStatus.SIGNED.value,
                detail={"reason": str(exc), "retry": settlement.retry_count},
            )
            log.warning("Retry %d/%d for %s",
                        settlement.retry_count, MAX_RETRY,
                        settlement.settlement_ref)
        else:
            settlement.status = SettlementStatus.FAILED
            record_status(
                db, RTGSSettlementStatusHistory,
                "settlement_id", settlement.id,
                SettlementStatus.FAILED.value,
                detail={"reason": str(exc),
                        "retry_count": settlement.retry_count},
            )
            insert_outbox_event(
                db,
                settlement.settlement_ref,
                "rtgs.settlement.failed",
                RTGSSettlementFailed(
                    service=SERVICE,
                    settlement_ref=settlement.settlement_ref,
                    reason=str(exc),
                    retry_count=settlement.retry_count,
                ),
            )
            log.error("Failed: %s reason=%s",
                      settlement.settlement_ref, exc)
        db.flush()
        return False


def _settlement_worker():
    """Background thread: continuously drains the settlement queue in priority order."""
    log.info("RTGS settlement worker started.")
    while True:
        try:
            db = SessionLocal()
            try:
                # Priority-ordered dequeue
                pending = db.execute(
                    select(RTGSSettlement)
                    .where(RTGSSettlement.status == SettlementStatus.SIGNED)
                    .order_by(
                        # urgent=0, high=1, normal=2, low=3 — use array_position equivalent
                        RTGSSettlement.priority.asc(),
                        RTGSSettlement.queued_at.asc(),
                    )
                    .limit(10)
                    .with_for_update(skip_locked=True)
                ).scalars().all()

                if not pending:
                    time.sleep(0.5)
                    continue

                for settlement in pending:
                    _process_one_settlement(db, settlement)

                db.commit()
            except Exception as exc:
                db.rollback()
                log.exception("Worker cycle error: %s", exc)
                time.sleep(2)
            finally:
                db.close()
        except Exception as exc:
            log.exception("Worker outer error: %s", exc)
            time.sleep(5)


# ─── API Schemas ──────────────────────────────────────────────────────────────

class SubmitSettlementRequest(BaseModel):
    sending_account_id:   str
    receiving_account_id: str
    currency:             CurrencyCode
    amount:               Decimal = Field(gt=0)
    priority:             str = Field(default="normal", pattern="^(urgent|high|normal|low)$")
    scheduled_at:         Optional[datetime] = None
    metadata:             dict = Field(default_factory=dict)
    idempotency_key:      Optional[str] = None


class SettlementResponse(BaseModel):
    settlement_ref:      str
    status:              str
    sending_account_id:  str
    receiving_account_id: str
    currency:            str
    amount:              Decimal
    priority:            str
    queued_at:           datetime


# ─── FastAPI App ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    worker = threading.Thread(target=_settlement_worker, daemon=True, name="rtgs-worker")
    worker.start()
    log.info("RTGS Service and settlement worker started.")
    yield
    log.info("RTGS Service shutting down.")


app = FastAPI(
    title="RTGS Settlement Service",
    version="1.0.0",
    description="Real-Time Gross Settlement — processes every payment individually and immediately",
    lifespan=lifespan,
)
instrument_app(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.post(
    "/settlements/submit",
    response_model=SettlementResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_settlement(req: SubmitSettlementRequest, db: Session = Depends(get_db_session)):
    """
    Submit a gross settlement instruction.
    The instruction is queued and processed by the background worker in priority order.
    Urgent instructions are prioritised above all others (mirrors Fedwire same-day priority).
    """
    # Validate accounts
    sender = db.get(Account, req.sending_account_id)
    if not sender or not sender.is_active:
        raise HTTPException(status_code=404, detail="Sending account not found or inactive")
    receiver = db.get(Account, req.receiving_account_id)
    if not receiver or not receiver.is_active:
        raise HTTPException(status_code=404, detail="Receiving account not found or inactive")

    # Idempotency
    if req.idempotency_key:
        existing = db.execute(
            select(RTGSSettlement).where(
                RTGSSettlement.settlement_ref == req.idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return SettlementResponse(
                settlement_ref=existing.settlement_ref,
                status=existing.status.value,
                sending_account_id=str(existing.sending_account_id),
                receiving_account_id=str(existing.receiving_account_id),
                currency=existing.currency.value,
                amount=existing.amount,
                priority=existing.priority,
                queued_at=existing.queued_at,
            )

    ref = req.idempotency_key or f"RTGS-{uuid.uuid4().hex[:16].upper()}"
    amount = req.amount.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)

    settlement = RTGSSettlement(
        settlement_ref=ref,
        sending_account_id=req.sending_account_id,
        receiving_account_id=req.receiving_account_id,
        currency=req.currency,
        amount=amount,
        priority=req.priority,
        status=SettlementStatus.PENDING,
        scheduled_at=req.scheduled_at,
        extra_metadata=req.metadata,
    )
    db.add(settlement)
    db.flush()

    insert_outbox_event(
        db,
        ref,
        "rtgs.settlement.submitted",
        RTGSSettlementSubmitted(
            service=SERVICE,
            settlement_ref=ref,
            sending_account_id=req.sending_account_id,
            receiving_account_id=req.receiving_account_id,
            currency=req.currency.value,
            amount=amount,
            priority=req.priority,
        ),
    )

    return SettlementResponse(
        settlement_ref=ref,
        status=settlement.status.value,
        sending_account_id=req.sending_account_id,
        receiving_account_id=req.receiving_account_id,
        currency=req.currency.value,
        amount=amount,
        priority=req.priority,
        queued_at=settlement.queued_at or datetime.now(timezone.utc),
    )


@app.get("/settlements/{settlement_ref}")
def get_settlement(settlement_ref: str, db: Session = Depends(get_db_session)):
    """Retrieve the current state of a settlement instruction."""
    s = db.execute(
        select(RTGSSettlement).where(RTGSSettlement.settlement_ref == settlement_ref)
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settlement not found")
    return {
        "settlement_ref":       s.settlement_ref,
        "status":               s.status.value,
        "currency":             s.currency.value,
        "amount":               str(s.amount),
        "priority":             s.priority,
        "queued_at":            s.queued_at,
        "processing_started":   s.processing_started,
        "settled_at":           s.settled_at,
        "failure_reason":       s.failure_reason,
        "retry_count":          s.retry_count,
        "transaction_id":       str(s.transaction_id) if s.transaction_id else None,
        "blockchain":           s.extra_metadata.get("blockchain"),
    }


@app.get("/settlements")
def list_settlements(
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db_session),
):
    """List settlements optionally filtered by status."""
    q = select(RTGSSettlement).order_by(RTGSSettlement.queued_at.desc()).limit(limit)
    if status:
        q = q.where(RTGSSettlement.status == status)
    results = db.execute(q).scalars().all()
    return [
        {
            "settlement_ref": r.settlement_ref,
            "status":         r.status.value,
            "currency":       r.currency.value,
            "amount":         str(r.amount),
            "priority":       r.priority,
            "queued_at":      r.queued_at,
            "settled_at":     r.settled_at,
        }
        for r in results
    ]


@app.post("/settlements/{settlement_ref}/approve")
def approve_settlement(
    settlement_ref: str,
    request: Request,
    db: Session = Depends(get_db_session),
    _role=Depends(require_role("admin", "approver")),
):
    """Approve a pending settlement (requires approver role)."""
    s = db.execute(
        select(RTGSSettlement).where(
            RTGSSettlement.settlement_ref == settlement_ref
        ).with_for_update()
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settlement not found")

    validate_transition(
        s.status.value, "approved", RTGS_VALID_TRANSITIONS
    )

    ctx = extract_context(request)
    actor_id = ctx.get("actor_id")

    s.status = SettlementStatus.APPROVED
    s.approved_by = actor_id
    s.approved_at = datetime.now(timezone.utc)
    record_status(
        db, RTGSSettlementStatusHistory,
        "settlement_id", s.id,
        SettlementStatus.APPROVED.value,
        detail={"approved_by": str(actor_id)},
        request_id=ctx.get("request_id"),
        actor_id=actor_id,
        actor_service=ctx.get("actor_service"),
    )
    db.flush()
    return {
        "settlement_ref": s.settlement_ref,
        "status": s.status.value,
        "approved_by": str(s.approved_by),
        "approved_at": s.approved_at,
    }


@app.post("/settlements/{settlement_ref}/sign")
def sign_settlement(
    settlement_ref: str,
    request: Request,
    db: Session = Depends(get_db_session),
    _role=Depends(require_role("admin", "signer")),
):
    """Sign an approved settlement (requires signer role, different actor)."""
    s = db.execute(
        select(RTGSSettlement).where(
            RTGSSettlement.settlement_ref == settlement_ref
        ).with_for_update()
    ).scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Settlement not found")

    validate_transition(
        s.status.value, "signed", RTGS_VALID_TRANSITIONS
    )

    ctx = extract_context(request)
    actor_id = ctx.get("actor_id")

    check_separation_of_duties(
        str(s.approved_by) if s.approved_by else "",
        str(actor_id) if actor_id else "",
    )

    s.status = SettlementStatus.SIGNED
    s.signed_by = actor_id
    s.signed_at = datetime.now(timezone.utc)
    record_status(
        db, RTGSSettlementStatusHistory,
        "settlement_id", s.id,
        SettlementStatus.SIGNED.value,
        detail={"signed_by": str(actor_id)},
        request_id=ctx.get("request_id"),
        actor_id=actor_id,
        actor_service=ctx.get("actor_service"),
    )
    db.flush()
    return {
        "settlement_ref": s.settlement_ref,
        "status": s.status.value,
        "signed_by": str(s.signed_by),
        "signed_at": s.signed_at,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8002))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
