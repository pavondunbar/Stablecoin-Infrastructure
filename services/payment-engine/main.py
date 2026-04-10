"""
services/payment-engine/main.py
────────────────────────────────
Programmable Payment Logic Engine

Responsibilities:
  • Conditional payments: execute when an on-chain/off-chain condition is met
    Supported condition types:
      - time_lock         : release after a specified UTC timestamp
      - oracle_trigger    : release when an oracle posts a matching data value
      - multi_sig         : require N-of-M authorisation signatures
      - delivery_confirmation: release when a delivery ref is confirmed
      - kyc_verified      : release when counterparty KYC is cleared
  • Escrow contracts: lock funds, release to beneficiary or refund depositor
  • Background checker: periodically evaluates pending conditions
  • Kafka consumer: listens for oracle / delivery events to trigger payments

All fund movements go through atomic ledger operations (double-entry).
"""

import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import sys
sys.path.insert(0, "/app/shared")

from database import get_db_session, SessionLocal
from metrics import instrument_app, record_business_event
from models import (
    Account, ConditionalPayment, EscrowContract, LedgerEntry,
    TokenBalance, Transaction,
    CurrencyCode, ConditionType, EscrowStatus, TxnStatus,
)
import kafka_client as kafka
from events import (
    ConditionalPaymentCreated, ConditionalPaymentTriggered, ConditionalPaymentCompleted,
    EscrowCreated, EscrowReleased, EscrowExpired, AuditTrailEntry,
)
from shared.journal import (
    record_journal_pair,
    get_balance as journal_get_balance,
    get_available_balance,
    acquire_balance_lock,
)
from shared.outbox import insert_outbox_event
from shared.status import record_status
from shared.models import (
    EscrowHold, EscrowStatusHistory,
    ConditionalPaymentStatusHistory,
    TransactionStatusHistory, JournalEntry,
)
from shared.blockchain_sim import record_on_chain

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
SERVICE = os.environ.get("SERVICE_NAME", "payment-engine")
PRECISION = Decimal("0.00000001")


# ─── Condition Evaluators ─────────────────────────────────────────────────────

def evaluate_condition(condition_type: str, params: dict, trigger_data: dict = None) -> bool:
    """
    Returns True if the payment condition is satisfied.
    Each evaluator is deliberately simple; in production these would call
    oracle networks, signature services, or logistics APIs.
    """
    now = datetime.now(timezone.utc)

    if condition_type == ConditionType.TIME_LOCK.value:
        release_at = datetime.fromisoformat(params["release_at"])
        return now >= release_at

    elif condition_type == ConditionType.ORACLE_TRIGGER.value:
        # Trigger data must contain oracle_key and oracle_value matching params
        if not trigger_data:
            return False
        return (
            trigger_data.get("oracle_key") == params.get("oracle_key")
            and trigger_data.get("oracle_value") == params.get("expected_value")
        )

    elif condition_type == ConditionType.MULTI_SIG.value:
        # Trigger data must contain a list of authorised signatures
        if not trigger_data:
            return False
        required = set(params.get("required_signers", []))
        received = set(trigger_data.get("signatures", []))
        threshold = params.get("threshold", len(required))
        return len(required & received) >= threshold

    elif condition_type == ConditionType.DELIVERY_CONFIRMATION.value:
        if not trigger_data:
            return False
        return trigger_data.get("delivery_ref") == params.get("delivery_ref") \
               and trigger_data.get("confirmed", False) is True

    elif condition_type == ConditionType.KYC_VERIFIED.value:
        # Check DB for KYC status — resolved at evaluation time
        return bool(trigger_data and trigger_data.get("kyc_cleared"))

    return False


# ─── Shared Transfer Helper ───────────────────────────────────────────────────

def _locked_transfer(
    db: Session,
    debit_id: str,
    credit_id: str,
    currency: str,
    amount: Decimal,
    narrative: str,
    txn_type: str,
    release_reserve: bool = False,
) -> Transaction:
    acquire_balance_lock(db, debit_id, currency)

    available = get_available_balance(db, debit_id, currency)
    if not release_reserve and available < amount:
        raise ValueError(
            f"Insufficient available: {available} < {amount}"
        )

    ref = f"PE-{uuid.uuid4().hex[:16].upper()}"

    txn = Transaction(
        txn_ref=ref,
        debit_account_id=debit_id,
        credit_account_id=credit_id,
        currency=currency,
        amount=amount,
        txn_type=txn_type,
        status=TxnStatus.COMPLETED,
        settled_at=datetime.now(timezone.utc),
    )
    db.add(txn)
    db.flush()

    record_journal_pair(
        db,
        account_id=debit_id,
        coa_code="INSTITUTION_LIABILITY",
        currency=currency,
        amount=amount,
        entry_type=txn_type,
        reference_id=str(txn.id),
        counter_account_id=credit_id,
        counter_coa_code="INSTITUTION_LIABILITY",
        narrative=narrative,
    )

    if release_reserve:
        hold = EscrowHold(
            hold_ref=f"REL-{uuid.uuid4().hex[:12]}",
            account_id=uuid.UUID(debit_id),
            currency=currency,
            amount=amount,
            hold_type="release",
            related_entity_type="escrow",
        )
        db.add(hold)
        # SQLite compat: also decrement legacy TokenBalance.reserved
        send_bal = db.execute(
            select(TokenBalance).where(
                TokenBalance.account_id == debit_id,
                TokenBalance.currency == currency,
            ).with_for_update()
        ).scalar_one_or_none()
        if send_bal:
            send_bal.reserved -= min(amount, send_bal.reserved)
            send_bal.version += 1

    db.flush()
    return txn


def _reserve_funds(
    db: Session,
    account_id: str,
    currency: str,
    amount: Decimal,
) -> None:
    """Create an EscrowHold to reserve funds for escrow/conditional payment."""
    available = get_available_balance(db, account_id, currency)
    if available < amount:
        raise ValueError(
            f"Insufficient available balance to reserve: {available}"
        )

    hold = EscrowHold(
        hold_ref=f"HOLD-{uuid.uuid4().hex[:12]}",
        account_id=uuid.UUID(account_id),
        currency=currency,
        amount=amount,
        hold_type="reserve",
        related_entity_type="escrow",
    )
    db.add(hold)
    db.flush()


# ─── API Schemas ──────────────────────────────────────────────────────────────

class CreateConditionalPaymentRequest(BaseModel):
    payer_account_id: str
    payee_account_id: str
    currency:         CurrencyCode
    amount:           Decimal = Field(gt=0)
    condition_type:   ConditionType
    condition_params: dict
    expires_at:       Optional[datetime] = None
    idempotency_key:  Optional[str] = None


class TriggerConditionalPaymentRequest(BaseModel):
    trigger_data: dict
    triggered_by: str = "system"


class CreateEscrowRequest(BaseModel):
    depositor_account_id:   str
    beneficiary_account_id: str
    currency:               CurrencyCode
    amount:                 Decimal = Field(gt=0)
    conditions:             dict
    expires_at:             datetime
    arbiter_account_id:     Optional[str] = None
    idempotency_key:        Optional[str] = None


class ReleaseEscrowRequest(BaseModel):
    release_to:   str = Field(pattern="^(beneficiary|depositor)$")
    triggered_by: str


# ─── Background Condition Checker ─────────────────────────────────────────────

def _check_conditions():
    """
    Background thread: polls for pending conditional payments whose conditions
    can be auto-evaluated (time locks, KYC status).  Oracle and multi-sig
    conditions are triggered via the API endpoint.
    """
    log.info("Condition checker started.")
    while True:
        try:
            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                # Auto-evaluatable conditions
                auto_types = [ConditionType.TIME_LOCK, ConditionType.KYC_VERIFIED]
                pending = db.execute(
                    select(ConditionalPayment).where(
                        ConditionalPayment.status == TxnStatus.PENDING,
                        ConditionalPayment.condition_type.in_(auto_types),
                    )
                ).scalars().all()

                for cp in pending:
                    # Check expiry
                    if cp.expires_at and now > cp.expires_at:
                        cp.status = TxnStatus.FAILED
                        db.flush()
                        record_status(
                            db, ConditionalPaymentStatusHistory,
                            "payment_id", cp.id,
                            TxnStatus.FAILED.value,
                            detail={"reason": "expired"},
                        )
                        log.info("Conditional payment expired: %s", cp.payment_ref)
                        continue

                    trigger = {}
                    if cp.condition_type == ConditionType.KYC_VERIFIED:
                        payee = db.get(Account, str(cp.payee_account_id))
                        trigger = {"kyc_cleared": payee and payee.kyc_verified and payee.aml_cleared}

                    if evaluate_condition(cp.condition_type.value, cp.condition_params, trigger):
                        _execute_conditional_payment(db, cp, trigger, "auto-checker")  # receipt ignored

                # Check escrow expiry
                expired_escrows = db.execute(
                    select(EscrowContract).where(
                        EscrowContract.status == EscrowStatus.ACTIVE,
                        EscrowContract.expires_at <= now,
                    )
                ).scalars().all()

                for escrow in expired_escrows:
                    _expire_escrow(db, escrow)

                db.commit()
            except Exception as exc:
                db.rollback()
                log.exception("Condition checker error: %s", exc)
            finally:
                db.close()
        except Exception as exc:
            log.exception("Checker outer error: %s", exc)
        time.sleep(5)


def _execute_conditional_payment(
    db: Session,
    cp: ConditionalPayment,
    trigger_data: dict,
    triggered_by: str,
) -> Transaction:
    cp.status = TxnStatus.PROCESSING
    cp.trigger_data = trigger_data
    db.flush()
    record_status(
        db, ConditionalPaymentStatusHistory,
        "payment_id", cp.id, TxnStatus.PROCESSING.value,
        detail={"triggered_by": triggered_by},
    )

    txn = _locked_transfer(
        db,
        debit_id=str(cp.payer_account_id),
        credit_id=str(cp.payee_account_id),
        currency=cp.currency.value,
        amount=cp.amount,
        narrative=(
            f"Conditional payment {cp.payment_ref}"
            f" triggered by {triggered_by}"
        ),
        txn_type="conditional_payment",
    )

    cp.status = TxnStatus.COMPLETED
    cp.executed_at = datetime.now(timezone.utc)
    cp.transaction_id = txn.id

    # Record on simulated blockchain
    receipt = record_on_chain(
        cp.payment_ref, "conditional_payment"
    )
    cp.trigger_data = {
        **(cp.trigger_data or {}), "blockchain": receipt
    }

    db.flush()
    record_status(
        db, ConditionalPaymentStatusHistory,
        "payment_id", cp.id, TxnStatus.COMPLETED.value,
    )

    insert_outbox_event(
        db,
        aggregate_id=cp.payment_ref,
        event_type="payment.conditional.completed",
        event=ConditionalPaymentCompleted(
            service=SERVICE,
            payment_ref=cp.payment_ref,
            transaction_id=str(txn.id),
            executed_at=cp.executed_at,
        ),
    )
    log.info("Conditional payment executed: %s", cp.payment_ref)
    return txn, receipt


def _expire_escrow(db: Session, escrow: EscrowContract):
    """Refund expired escrow to depositor."""
    depositor_id = str(escrow.depositor_account_id)

    # Insert release hold to free reserved funds
    hold = EscrowHold(
        hold_ref=f"REL-{uuid.uuid4().hex[:12]}",
        account_id=uuid.UUID(depositor_id),
        currency=escrow.currency.value
        if hasattr(escrow.currency, "value")
        else escrow.currency,
        amount=escrow.amount,
        hold_type="release",
        related_entity_type="escrow",
        related_entity_id=escrow.id,
    )
    db.add(hold)

    # SQLite compat: also decrement legacy TokenBalance.reserved
    bal = db.execute(
        select(TokenBalance).where(
            TokenBalance.account_id == depositor_id,
            TokenBalance.currency == escrow.currency,
        ).with_for_update()
    ).scalar_one_or_none()
    if bal:
        release_amt = min(escrow.amount, bal.reserved)
        bal.reserved -= release_amt
        bal.version += 1

    escrow.status = EscrowStatus.EXPIRED
    db.flush()
    record_status(
        db, EscrowStatusHistory,
        "escrow_id", escrow.id, EscrowStatus.EXPIRED.value,
    )

    insert_outbox_event(
        db,
        aggregate_id=escrow.contract_ref,
        event_type="escrow.expired",
        event=EscrowExpired(
            service=SERVICE,
            contract_ref=escrow.contract_ref,
            refunded_account_id=depositor_id,
            amount=escrow.amount,
        ),
    )
    log.info("Escrow expired and refunded: %s", escrow.contract_ref)


# ─── FastAPI App ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    checker = threading.Thread(target=_check_conditions, daemon=True, name="cond-checker")
    checker.start()
    log.info("Payment Engine started.")
    yield
    log.info("Payment Engine shutting down.")


app = FastAPI(
    title="Programmable Payment Engine",
    version="1.0.0",
    description="Conditional payments, escrow contracts, and programmable money logic",
    lifespan=lifespan,
)
instrument_app(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


# ── Conditional Payments ──────────────────────────────────────────────────────

@app.post("/payments/conditional", status_code=status.HTTP_201_CREATED)
def create_conditional_payment(
    req: CreateConditionalPaymentRequest,
    db: Session = Depends(get_db_session),
):
    """
    Create a conditional payment.  Funds are NOT moved until the condition is met.
    For time_lock conditions, the background checker will execute automatically.
    For oracle / multi_sig / delivery, call POST /payments/conditional/{ref}/trigger.
    """
    # Idempotency guard
    if req.idempotency_key:
        existing = db.execute(
            select(ConditionalPayment).where(
                ConditionalPayment.payment_ref == req.idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return {
                "payment_ref":    existing.payment_ref,
                "status":         existing.status.value,
                "condition_type": existing.condition_type.value,
                "amount":         str(existing.amount),
                "currency":       existing.currency.value,
            }

    payer = db.get(Account, req.payer_account_id)
    if not payer or not payer.is_active:
        raise HTTPException(status_code=404, detail="Payer account not found")
    payee = db.get(Account, req.payee_account_id)
    if not payee or not payee.is_active:
        raise HTTPException(status_code=404, detail="Payee account not found")

    amount = req.amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)
    ref    = req.idempotency_key or f"CP-{uuid.uuid4().hex[:16].upper()}"

    cp = ConditionalPayment(
        payment_ref=ref,
        payer_account_id=req.payer_account_id,
        payee_account_id=req.payee_account_id,
        currency=req.currency,
        amount=amount,
        condition_type=req.condition_type,
        condition_params=req.condition_params,
        status=TxnStatus.PENDING,
        expires_at=req.expires_at,
    )
    db.add(cp)
    db.flush()
    record_status(
        db, ConditionalPaymentStatusHistory,
        "payment_id", cp.id, TxnStatus.PENDING.value,
    )

    insert_outbox_event(
        db,
        aggregate_id=ref,
        event_type="payment.conditional.created",
        event=ConditionalPaymentCreated(
            service=SERVICE,
            payment_ref=ref,
            payer_account_id=req.payer_account_id,
            payee_account_id=req.payee_account_id,
            currency=req.currency.value,
            amount=amount,
            condition_type=req.condition_type.value,
            condition_params=req.condition_params,
        ),
    )

    return {
        "payment_ref":    ref,
        "status":         cp.status.value,
        "condition_type": req.condition_type.value,
        "amount":         str(amount),
        "currency":       req.currency.value,
    }


@app.post("/payments/conditional/{payment_ref}/trigger")
def trigger_conditional_payment(
    payment_ref: str,
    req: TriggerConditionalPaymentRequest,
    db: Session = Depends(get_db_session),
):
    """
    Provide trigger data (oracle value, signatures, delivery confirmation).
    Condition is re-evaluated; if satisfied, payment executes immediately.
    """
    cp = db.execute(
        select(ConditionalPayment).where(ConditionalPayment.payment_ref == payment_ref)
    ).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Conditional payment not found")
    if cp.status != TxnStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Payment is in state: {cp.status.value}")

    satisfied = evaluate_condition(
        cp.condition_type.value, cp.condition_params, req.trigger_data,
    )
    if not satisfied:
        insert_outbox_event(
            db,
            aggregate_id=payment_ref,
            event_type="payment.conditional.triggered",
            event=ConditionalPaymentTriggered(
                service=SERVICE,
                payment_ref=payment_ref,
                trigger_data=req.trigger_data,
                triggered_by=req.triggered_by,
            ),
        )
        return {"result": "condition_not_satisfied", "payment_ref": payment_ref}

    txn, receipt = _execute_conditional_payment(db, cp, req.trigger_data, req.triggered_by)
    return {
        "result":         "executed",
        "payment_ref":    payment_ref,
        "transaction_id": str(txn.id),
        "executed_at":    cp.executed_at,
        "blockchain":     receipt,
    }


@app.get("/payments/conditional/{payment_ref}")
def get_conditional_payment(payment_ref: str, db: Session = Depends(get_db_session)):
    cp = db.execute(
        select(ConditionalPayment).where(ConditionalPayment.payment_ref == payment_ref)
    ).scalar_one_or_none()
    if not cp:
        raise HTTPException(status_code=404, detail="Not found")
    return {
        "payment_ref":     cp.payment_ref,
        "status":          cp.status.value,
        "currency":        cp.currency.value,
        "amount":          str(cp.amount),
        "condition_type":  cp.condition_type.value,
        "condition_params": cp.condition_params,
        "trigger_data":    cp.trigger_data,
        "created_at":      cp.created_at,
        "executed_at":     cp.executed_at,
        "expires_at":      cp.expires_at,
        "blockchain":      (cp.trigger_data or {}).get("blockchain"),
    }


# ── Escrow ────────────────────────────────────────────────────────────────────

@app.post("/payments/escrow", status_code=status.HTTP_201_CREATED)
def create_escrow(req: CreateEscrowRequest, db: Session = Depends(get_db_session)):
    """
    Create an escrow contract.
    Funds are immediately reserved (locked) in the depositor's balance.
    Release conditions are stored on the contract.
    """
    # Idempotency guard
    if req.idempotency_key:
        existing = db.execute(
            select(EscrowContract).where(
                EscrowContract.contract_ref == req.idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return {
                "contract_ref":          existing.contract_ref,
                "status":                existing.status.value,
                "amount":                str(existing.amount),
                "currency":              existing.currency.value,
                "expires_at":            existing.expires_at,
                "depositor_account_id":  str(existing.depositor_account_id),
                "beneficiary_account_id": str(existing.beneficiary_account_id),
            }

    depositor = db.get(Account, req.depositor_account_id)
    if not depositor or not depositor.is_active:
        raise HTTPException(status_code=404, detail="Depositor account not found")
    beneficiary = db.get(Account, req.beneficiary_account_id)
    if not beneficiary or not beneficiary.is_active:
        raise HTTPException(status_code=404, detail="Beneficiary account not found")

    amount = req.amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)

    # Reserve funds immediately
    _reserve_funds(db, req.depositor_account_id, req.currency.value, amount)

    ref = f"ESC-{uuid.uuid4().hex[:16].upper()}"
    escrow = EscrowContract(
        contract_ref=req.contract_ref if hasattr(req, "contract_ref") else ref,
        depositor_account_id=req.depositor_account_id,
        beneficiary_account_id=req.beneficiary_account_id,
        currency=req.currency,
        amount=amount,
        conditions=req.conditions,
        status=EscrowStatus.ACTIVE,
        arbiter_account_id=req.arbiter_account_id,
        expires_at=req.expires_at,
    )
    escrow.contract_ref = ref
    db.add(escrow)
    db.flush()
    record_status(
        db, EscrowStatusHistory,
        "escrow_id", escrow.id, EscrowStatus.ACTIVE.value,
    )

    insert_outbox_event(
        db,
        aggregate_id=ref,
        event_type="escrow.created",
        event=EscrowCreated(
            service=SERVICE,
            contract_ref=ref,
            depositor_account_id=req.depositor_account_id,
            beneficiary_account_id=req.beneficiary_account_id,
            currency=req.currency.value,
            amount=amount,
            conditions=req.conditions,
            expires_at=req.expires_at,
        ),
    )

    return {
        "contract_ref":          ref,
        "status":                escrow.status.value,
        "amount":                str(amount),
        "currency":              req.currency.value,
        "expires_at":            req.expires_at,
        "depositor_account_id":  req.depositor_account_id,
        "beneficiary_account_id": req.beneficiary_account_id,
    }


@app.post("/payments/escrow/{contract_ref}/release")
def release_escrow(
    contract_ref: str,
    req: ReleaseEscrowRequest,
    db: Session = Depends(get_db_session),
):
    """
    Release escrow funds to beneficiary or refund to depositor.
    Can be called by the depositor, beneficiary, or arbiter.
    """
    escrow = db.execute(
        select(EscrowContract).where(EscrowContract.contract_ref == contract_ref)
        .with_for_update()
    ).scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="Escrow not found")
    if escrow.status != EscrowStatus.ACTIVE:
        raise HTTPException(status_code=409, detail=f"Escrow is in state: {escrow.status.value}")

    now = datetime.now(timezone.utc)
    if now > escrow.expires_at:
        _expire_escrow(db, escrow)
        raise HTTPException(status_code=410, detail="Escrow has expired and been refunded")

    if req.release_to == "beneficiary":
        # Transfer reserved funds to beneficiary
        txn = _locked_transfer(
            db,
            debit_id=str(escrow.depositor_account_id),
            credit_id=str(escrow.beneficiary_account_id),
            currency=escrow.currency.value,
            amount=escrow.amount,
            narrative=f"Escrow release {contract_ref} → beneficiary",
            txn_type="escrow_release",
            release_reserve=True,
        )
        escrow.status = EscrowStatus.RELEASED
        escrow.release_txn_id = txn.id
        escrow.released_at = now
        escrow.release_triggered_by = req.triggered_by
        db.flush()
        record_status(
            db, EscrowStatusHistory,
            "escrow_id", escrow.id, EscrowStatus.RELEASED.value,
        )

        insert_outbox_event(
            db,
            aggregate_id=contract_ref,
            event_type="escrow.released",
            event=EscrowReleased(
                service=SERVICE,
                contract_ref=contract_ref,
                release_txn_id=str(txn.id),
                released_to=str(escrow.beneficiary_account_id),
                triggered_by=req.triggered_by,
                amount=escrow.amount,
            ),
        )
        receipt = record_on_chain(
            contract_ref, "escrow_release"
        )
        return {
            "result": "released_to_beneficiary",
            "transaction_id": str(txn.id),
            "blockchain": receipt,
        }

    else:  # refund to depositor — un-reserve the funds
        depositor_id = str(escrow.depositor_account_id)
        currency_val = (
            escrow.currency.value
            if hasattr(escrow.currency, "value")
            else escrow.currency
        )

        # Insert release hold to free reserved funds
        hold = EscrowHold(
            hold_ref=f"REL-{uuid.uuid4().hex[:12]}",
            account_id=uuid.UUID(depositor_id),
            currency=currency_val,
            amount=escrow.amount,
            hold_type="release",
            related_entity_type="escrow",
            related_entity_id=escrow.id,
        )
        db.add(hold)

        # SQLite compat: also decrement legacy TokenBalance.reserved
        bal = db.execute(
            select(TokenBalance).where(
                TokenBalance.account_id == depositor_id,
                TokenBalance.currency == escrow.currency,
            ).with_for_update()
        ).scalar_one_or_none()
        if bal:
            release_amt = min(escrow.amount, bal.reserved)
            bal.reserved -= release_amt
            bal.version += 1

        escrow.status = EscrowStatus.REFUNDED
        escrow.released_at = now
        escrow.release_triggered_by = req.triggered_by
        db.flush()
        record_status(
            db, EscrowStatusHistory,
            "escrow_id", escrow.id, EscrowStatus.REFUNDED.value,
        )

        insert_outbox_event(
            db,
            aggregate_id=contract_ref,
            event_type="escrow.released",
            event=EscrowReleased(
                service=SERVICE,
                contract_ref=contract_ref,
                release_txn_id="",
                released_to=depositor_id,
                triggered_by=req.triggered_by,
                amount=escrow.amount,
            ),
        )
        return {"result": "refunded_to_depositor", "amount": str(escrow.amount)}


@app.get("/payments/escrow/{contract_ref}")
def get_escrow(contract_ref: str, db: Session = Depends(get_db_session)):
    escrow = db.execute(
        select(EscrowContract).where(EscrowContract.contract_ref == contract_ref)
    ).scalar_one_or_none()
    if not escrow:
        raise HTTPException(status_code=404, detail="Escrow not found")
    return {
        "contract_ref":          escrow.contract_ref,
        "status":                escrow.status.value,
        "currency":              escrow.currency.value,
        "amount":                str(escrow.amount),
        "conditions":            escrow.conditions,
        "expires_at":            escrow.expires_at,
        "released_at":           escrow.released_at,
        "release_triggered_by":  escrow.release_triggered_by,
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8003))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
