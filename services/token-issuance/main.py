"""
services/token-issuance/main.py
────────────────────────────────
Tokenized Deposit Issuance & Redemption Engine

Responsibilities:
  • Issue tokenized deposits against a verified fiat backing reference
  • Redeem tokens back to fiat (triggers reverse settlement)
  • Maintain atomic double-entry ledger for every operation
  • Publish issuance/redemption/balance events to Kafka
  • Run a background consumer for settlement-triggered redemptions

Trust boundary: only reachable from the internal Docker network.
"""

import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

# ── path setup so we can import the shared package ────────────────────────────
import sys
sys.path.insert(0, "/app/shared")

from database import get_db_session
from metrics import instrument_app, record_business_event
from models import (
    Account, LedgerEntry, TokenBalance, TokenIssuance, Transaction,
    CurrencyCode, TxnStatus,
)
import kafka_client as kafka
from events import (
    TokenIssuanceCompleted, TokenIssuanceRequested,
    TokenRedemptionCompleted, TokenRedemptionRequested,
    TokenBalanceUpdated, AuditTrailEntry,
)

# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

SERVICE = os.environ.get("SERVICE_NAME", "token-issuance")
OMNIBUS_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"

PRECISION = Decimal("0.00000001")   # 8 decimal places


# ─── Helpers ─────────────────────────────────────────────────────────────────

def normalize(amount: Decimal) -> Decimal:
    return amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)


def _get_or_create_balance(
    db: Session, account_id: str, currency: str
) -> TokenBalance:
    bal = db.execute(
        select(TokenBalance).where(
            TokenBalance.account_id == account_id,
            TokenBalance.currency == currency,
        ).with_for_update()   # row-level lock during mutation
    ).scalar_one_or_none()

    if bal is None:
        bal = TokenBalance(
            account_id=account_id,
            currency=currency,
            balance=Decimal("0"),
            reserved=Decimal("0"),
        )
        db.add(bal)
        db.flush()
    return bal


def _record_ledger(
    db: Session,
    txn_ref: str,
    entry_type: str,
    account_id: str,
    currency: str,
    amount: Decimal,
    balance_after: Decimal,
    narrative: str,
) -> None:
    db.add(LedgerEntry(
        txn_ref=txn_ref,
        entry_type=entry_type,
        account_id=account_id,
        currency=currency,
        amount=amount,
        balance_after=balance_after,
        narrative=narrative,
    ))


# ─── API Schemas ──────────────────────────────────────────────────────────────

class IssueTokensRequest(BaseModel):
    account_id:    str
    currency:      CurrencyCode
    amount:        Decimal = Field(gt=0)
    backing_ref:   str                          # reference to underlying fiat deposit
    custodian:     Optional[str] = None
    idempotency_key: Optional[str] = None


class RedeemTokensRequest(BaseModel):
    account_id:   str
    currency:     CurrencyCode
    amount:       Decimal = Field(gt=0)
    settlement_ref: Optional[str] = None
    idempotency_key: Optional[str] = None


class TokenBalanceResponse(BaseModel):
    account_id:   str
    currency:     str
    balance:      Decimal
    reserved:     Decimal
    available:    Decimal


class IssuanceResponse(BaseModel):
    issuance_ref: str
    account_id:   str
    currency:     str
    amount:       Decimal
    new_balance:  Decimal
    status:       str


# ─── Business Logic ───────────────────────────────────────────────────────────

def _issue_tokens(
    db: Session,
    account_id: str,
    currency: str,
    amount: Decimal,
    backing_ref: str,
    custodian: Optional[str],
    idempotency_key: Optional[str],
) -> TokenIssuance:
    amount = normalize(amount)

    # Idempotency guard
    if idempotency_key:
        existing = db.execute(
            select(TokenIssuance).where(TokenIssuance.issuance_ref == idempotency_key)
        ).scalar_one_or_none()
        if existing:
            return existing

    # Verify account exists and is KYC/AML cleared
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.kyc_verified or not account.aml_cleared:
        raise HTTPException(status_code=403, detail="Account not KYC/AML cleared")
    if not account.is_active:
        raise HTTPException(status_code=403, detail="Account is not active")

    issuance_ref = idempotency_key or f"ISS-{uuid.uuid4().hex[:16].upper()}"

    # Create issuance record
    issuance = TokenIssuance(
        issuance_ref=issuance_ref,
        account_id=account_id,
        currency=currency,
        amount=amount,
        backing_ref=backing_ref,
        custodian=custodian,
        issuance_type="initial",
        status=TxnStatus.PROCESSING,
    )
    db.add(issuance)
    db.flush()

    # Debit omnibus reserve
    omnibus_bal = _get_or_create_balance(db, OMNIBUS_ACCOUNT_ID, currency)
    if omnibus_bal.balance < amount:
        raise HTTPException(status_code=422, detail="Insufficient reserve balance")
    omnibus_bal.balance  -= amount
    omnibus_bal.version  += 1
    _record_ledger(db, issuance_ref, "debit", OMNIBUS_ACCOUNT_ID, currency, amount,
                   omnibus_bal.balance, f"Token issuance to {account_id}")

    # Credit recipient account
    recv_bal = _get_or_create_balance(db, account_id, currency)
    old_balance = recv_bal.balance
    recv_bal.balance  += amount
    recv_bal.version  += 1
    _record_ledger(db, issuance_ref, "credit", account_id, currency, amount,
                   recv_bal.balance, f"Token issuance ref={backing_ref}")

    # Create transaction record
    txn = Transaction(
        txn_ref=issuance_ref,
        debit_account_id=OMNIBUS_ACCOUNT_ID,
        credit_account_id=account_id,
        currency=currency,
        amount=amount,
        txn_type="issuance",
        status=TxnStatus.COMPLETED,
        idempotency_key=idempotency_key,
        settled_at=datetime.now(timezone.utc),
    )
    db.add(txn)

    issuance.status    = TxnStatus.COMPLETED
    issuance.issued_at = datetime.now(timezone.utc)
    db.flush()

    # Publish events (after flush, before commit)
    kafka.publish(
        "token.issuance.completed",
        TokenIssuanceCompleted(
            service=SERVICE,
            issuance_ref=issuance_ref,
            account_id=account_id,
            currency=currency,
            amount=amount,
            new_balance=recv_bal.balance,
        ),
        key=account_id,
    )
    kafka.publish(
        "token.balance.updated",
        TokenBalanceUpdated(
            service=SERVICE,
            account_id=account_id,
            currency=currency,
            old_balance=old_balance,
            new_balance=recv_bal.balance,
            reserved=recv_bal.reserved,
            reason="issuance",
        ),
        key=account_id,
    )

    return issuance


def _redeem_tokens(
    db: Session,
    account_id: str,
    currency: str,
    amount: Decimal,
    settlement_ref: Optional[str],
    idempotency_key: Optional[str],
) -> TokenIssuance:
    amount = normalize(amount)

    if idempotency_key:
        existing = db.execute(
            select(TokenIssuance).where(TokenIssuance.issuance_ref == idempotency_key)
        ).scalar_one_or_none()
        if existing:
            return existing

    account = db.get(Account, account_id)
    if not account or not account.is_active:
        raise HTTPException(status_code=404, detail="Account not found or inactive")

    # Debit the account
    acct_bal = _get_or_create_balance(db, account_id, currency)
    if acct_bal.available < amount:
        raise HTTPException(
            status_code=422,
            detail=f"Insufficient available balance: {acct_bal.available} {currency}"
        )

    redemption_ref = idempotency_key or f"RED-{uuid.uuid4().hex[:16].upper()}"

    issuance = TokenIssuance(
        issuance_ref=redemption_ref,
        account_id=account_id,
        currency=currency,
        amount=amount,
        backing_ref=settlement_ref,
        issuance_type="redemption",
        status=TxnStatus.PROCESSING,
    )
    db.add(issuance)
    db.flush()

    old_balance = acct_bal.balance
    acct_bal.balance  -= amount
    acct_bal.version  += 1
    _record_ledger(db, redemption_ref, "debit", account_id, currency, amount,
                   acct_bal.balance, f"Token redemption ref={settlement_ref}")

    # Credit omnibus reserve
    omnibus_bal = _get_or_create_balance(db, OMNIBUS_ACCOUNT_ID, currency)
    omnibus_bal.balance  += amount
    omnibus_bal.version  += 1
    _record_ledger(db, redemption_ref, "credit", OMNIBUS_ACCOUNT_ID, currency, amount,
                   omnibus_bal.balance, f"Token redemption from {account_id}")

    txn = Transaction(
        txn_ref=redemption_ref,
        debit_account_id=account_id,
        credit_account_id=OMNIBUS_ACCOUNT_ID,
        currency=currency,
        amount=amount,
        txn_type="redemption",
        status=TxnStatus.COMPLETED,
        idempotency_key=idempotency_key,
        settled_at=datetime.now(timezone.utc),
    )
    db.add(txn)

    issuance.status      = TxnStatus.COMPLETED
    issuance.redeemed_at = datetime.now(timezone.utc)
    db.flush()

    kafka.publish(
        "token.redemption.completed",
        TokenRedemptionCompleted(
            service=SERVICE,
            issuance_ref=redemption_ref,
            account_id=account_id,
            currency=currency,
            amount=amount,
            new_balance=acct_bal.balance,
        ),
        key=account_id,
    )
    kafka.publish(
        "token.balance.updated",
        TokenBalanceUpdated(
            service=SERVICE,
            account_id=account_id,
            currency=currency,
            old_balance=old_balance,
            new_balance=acct_bal.balance,
            reserved=acct_bal.reserved,
            reason="redemption",
        ),
        key=account_id,
    )

    return issuance


# ─── FastAPI App ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Token Issuance Service starting up…")
    yield
    log.info("Token Issuance Service shutting down.")


app = FastAPI(
    title="Token Issuance Service",
    version="1.0.0",
    description="Issues and redeems tokenized bank deposits (JPM Coin / PYUSD style)",
    lifespan=lifespan,
)
instrument_app(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.post("/tokens/issue", response_model=IssuanceResponse, status_code=status.HTTP_201_CREATED)
def issue_tokens(req: IssueTokensRequest, db: Session = Depends(get_db_session)):
    """
    Issue tokenized deposits to an institutional account.
    The `backing_ref` must correspond to a verified fiat deposit held by a custodian.
    Atomic double-entry: omnibus reserve is debited, recipient is credited.
    """
    issuance = _issue_tokens(
        db,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=req.amount,
        backing_ref=req.backing_ref,
        custodian=req.custodian,
        idempotency_key=req.idempotency_key,
    )

    bal = db.execute(
        select(TokenBalance).where(
            TokenBalance.account_id == req.account_id,
            TokenBalance.currency   == req.currency,
        )
    ).scalar_one()

    return IssuanceResponse(
        issuance_ref=issuance.issuance_ref,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=issuance.amount,
        new_balance=bal.balance,
        status=issuance.status.value,
    )


@app.post("/tokens/redeem", response_model=IssuanceResponse, status_code=status.HTTP_201_CREATED)
def redeem_tokens(req: RedeemTokensRequest, db: Session = Depends(get_db_session)):
    """
    Redeem tokenized deposits back to fiat.
    Atomically burns tokens and credits the omnibus reserve for fiat payout.
    """
    issuance = _redeem_tokens(
        db,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=req.amount,
        settlement_ref=req.settlement_ref,
        idempotency_key=req.idempotency_key,
    )

    bal = db.execute(
        select(TokenBalance).where(
            TokenBalance.account_id == req.account_id,
            TokenBalance.currency   == req.currency,
        )
    ).scalar_one()

    return IssuanceResponse(
        issuance_ref=issuance.issuance_ref,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=issuance.amount,
        new_balance=bal.balance,
        status=issuance.status.value,
    )


@app.get("/tokens/balance/{account_id}", response_model=list[TokenBalanceResponse])
def get_balances(account_id: str, db: Session = Depends(get_db_session)):
    """Return all tokenized deposit balances for an account."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    balances = db.execute(
        select(TokenBalance).where(TokenBalance.account_id == account_id)
    ).scalars().all()

    return [
        TokenBalanceResponse(
            account_id=account_id,
            currency=b.currency.value,
            balance=b.balance,
            reserved=b.reserved,
            available=b.available,
        )
        for b in balances
    ]


@app.post("/accounts", status_code=status.HTTP_201_CREATED)
def create_account(
    entity_name: str,
    account_type: str,
    bic_code: Optional[str] = None,
    lei: Optional[str] = None,
    db: Session = Depends(get_db_session),
):
    """Onboard a new institutional participant."""
    account = Account(
        entity_name=entity_name,
        account_type=account_type,
        bic_code=bic_code,
        legal_entity_identifier=lei,
    )
    db.add(account)
    db.flush()
    return {"account_id": str(account.id), "entity_name": entity_name}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
