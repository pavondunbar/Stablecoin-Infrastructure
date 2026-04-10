"""
services/token-issuance/main.py
--------------------------------
Tokenized Deposit Issuance & Redemption Engine

Responsibilities:
  - Issue tokenized deposits against a verified fiat backing reference
  - Redeem tokens back to fiat (triggers reverse settlement)
  - Record immutable double-entry journal pairs for every operation
  - Write events to the transactional outbox for reliable publishing
  - Track status transitions via append-only status history

Trust boundary: only reachable from the internal Docker network.
"""

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

# ── path setup so we can import the shared package ──────────────────
import sys
sys.path.insert(0, "/app/shared")

from database import get_db_session
from metrics import instrument_app, record_business_event
from models import (
    Account, TokenIssuance, Transaction,
    CurrencyCode, TxnStatus,
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
    TokenIssuanceStatusHistory,
    TransactionStatusHistory,
    JournalEntry,
)
from shared.blockchain_sim import record_on_chain
from events import (
    TokenIssuanceCompleted, TokenIssuanceRequested,
    TokenRedemptionCompleted, TokenRedemptionRequested,
    TokenBalanceUpdated, AuditTrailEntry,
)

# ─────────────────────────────────────────────────────────────────────
log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

SERVICE = os.environ.get("SERVICE_NAME", "token-issuance")
OMNIBUS_ACCOUNT_ID = "00000000-0000-0000-0000-000000000001"

PRECISION = Decimal("0.000000000000000001")  # 18 decimal places


# ─── Helpers ────────────────────────────────────────────────────────

def normalize(amount: Decimal) -> Decimal:
    return amount.quantize(PRECISION, rounding=ROUND_HALF_EVEN)


# ─── API Schemas ────────────────────────────────────────────────────

class IssueTokensRequest(BaseModel):
    account_id:    str
    currency:      CurrencyCode
    amount:        Decimal = Field(gt=0)
    backing_ref:   str
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
    blockchain:   Optional[dict] = None


# ─── Business Logic ─────────────────────────────────────────────────

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
            select(TokenIssuance).where(
                TokenIssuance.issuance_ref == idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return existing, None

    # Verify account exists and is KYC/AML cleared
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(
            status_code=404, detail="Account not found"
        )
    if not account.kyc_verified or not account.aml_cleared:
        raise HTTPException(
            status_code=403, detail="Account not KYC/AML cleared"
        )
    if not account.is_active:
        raise HTTPException(
            status_code=403, detail="Account is not active"
        )

    issuance_ref = (
        idempotency_key
        or f"ISS-{uuid.uuid4().hex[:16].upper()}"
    )

    # Acquire advisory lock on omnibus account
    acquire_balance_lock(db, OMNIBUS_ACCOUNT_ID, currency)

    # Check omnibus reserve balance via journal (asset account)
    omnibus_balance = journal_get_balance(
        db, OMNIBUS_ACCOUNT_ID, currency, coa_code="OMNIBUS_RESERVE"
    )
    if omnibus_balance < amount:
        raise HTTPException(
            status_code=422,
            detail="Insufficient reserve balance",
        )

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

    # Record journal pair: debit OMNIBUS_RESERVE, credit
    # INSTITUTION_LIABILITY
    narrative = f"Token issuance to {account_id}"
    record_journal_pair(
        db,
        OMNIBUS_ACCOUNT_ID,
        "OMNIBUS_RESERVE",
        currency,
        amount,
        "token_issuance",
        str(issuance.id),
        str(account.id),
        "INSTITUTION_LIABILITY",
        narrative,
    )

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
    db.flush()

    # Record on simulated blockchain and status history
    receipt = record_on_chain(
        issuance_ref, "token_issuance", currency
    )
    record_status(
        db,
        TransactionStatusHistory,
        "transaction_id",
        txn.id,
        "completed",
        tx_hash=receipt["tx_hash"],
        block_number=receipt["block_number"],
    )

    # Mark issuance completed (keep direct assignment for
    # SQLite test compat; DB trigger handles it in prod)
    issuance.status = TxnStatus.COMPLETED
    issuance.issued_at = datetime.now(timezone.utc)
    record_status(
        db,
        TokenIssuanceStatusHistory,
        "issuance_id",
        issuance.id,
        "completed",
    )
    db.flush()

    # Derive new balance from journal
    new_balance = journal_get_balance(db, account_id, currency)

    # Write events to transactional outbox
    insert_outbox_event(
        db,
        issuance_ref,
        "token.issuance.completed",
        TokenIssuanceCompleted(
            service=SERVICE,
            issuance_ref=issuance_ref,
            account_id=account_id,
            currency=currency,
            amount=amount,
            new_balance=new_balance,
        ),
    )
    insert_outbox_event(
        db,
        issuance_ref,
        "token.balance.updated",
        TokenBalanceUpdated(
            service=SERVICE,
            account_id=account_id,
            currency=currency,
            old_balance=new_balance - amount,
            new_balance=new_balance,
            reserved=Decimal("0"),
            reason="issuance",
        ),
    )

    return issuance, receipt


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
            select(TokenIssuance).where(
                TokenIssuance.issuance_ref == idempotency_key
            )
        ).scalar_one_or_none()
        if existing:
            return existing, None

    account = db.get(Account, account_id)
    if not account or not account.is_active:
        raise HTTPException(
            status_code=404,
            detail="Account not found or inactive",
        )

    # Acquire advisory lock on the account
    acquire_balance_lock(db, account_id, currency)

    # Check available balance via journal
    available = get_available_balance(db, account_id, currency)
    if available < amount:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Insufficient available balance:"
                f" {available} {currency}"
            ),
        )

    redemption_ref = (
        idempotency_key
        or f"RED-{uuid.uuid4().hex[:16].upper()}"
    )

    # Capture old balance before journal entry
    old_balance = journal_get_balance(db, account_id, currency)

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

    # Record journal pair: debit INSTITUTION_LIABILITY, credit
    # OMNIBUS_RESERVE
    narrative = f"Token redemption ref={settlement_ref}"
    record_journal_pair(
        db,
        str(account.id),
        "INSTITUTION_LIABILITY",
        currency,
        amount,
        "token_redemption",
        str(issuance.id),
        OMNIBUS_ACCOUNT_ID,
        "OMNIBUS_RESERVE",
        narrative,
    )

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
    db.flush()

    # Record on simulated blockchain and status history
    receipt = record_on_chain(
        redemption_ref, "token_redemption", currency
    )
    record_status(
        db,
        TransactionStatusHistory,
        "transaction_id",
        txn.id,
        "completed",
        tx_hash=receipt["tx_hash"],
        block_number=receipt["block_number"],
    )

    # Mark redemption completed
    issuance.status = TxnStatus.COMPLETED
    issuance.redeemed_at = datetime.now(timezone.utc)
    record_status(
        db,
        TokenIssuanceStatusHistory,
        "issuance_id",
        issuance.id,
        "completed",
    )
    db.flush()

    # Derive new balance from journal
    new_balance = journal_get_balance(db, account_id, currency)

    # Write events to transactional outbox
    insert_outbox_event(
        db,
        redemption_ref,
        "token.redemption.completed",
        TokenRedemptionCompleted(
            service=SERVICE,
            issuance_ref=redemption_ref,
            account_id=account_id,
            currency=currency,
            amount=amount,
            new_balance=new_balance,
        ),
    )
    insert_outbox_event(
        db,
        redemption_ref,
        "token.balance.updated",
        TokenBalanceUpdated(
            service=SERVICE,
            account_id=account_id,
            currency=currency,
            old_balance=old_balance,
            new_balance=new_balance,
            reserved=Decimal("0"),
            reason="redemption",
        ),
    )

    return issuance, receipt


# ─── FastAPI App ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Token Issuance Service starting up...")
    yield
    log.info("Token Issuance Service shutting down.")


app = FastAPI(
    title="Token Issuance Service",
    version="1.0.0",
    description=(
        "Issues and redeems tokenized bank deposits"
        " (JPM Coin / PYUSD style)"
    ),
    lifespan=lifespan,
)
instrument_app(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.post(
    "/tokens/issue",
    response_model=IssuanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def issue_tokens(
    req: IssueTokensRequest,
    db: Session = Depends(get_db_session),
):
    """Issue tokenized deposits to an institutional account.

    The `backing_ref` must correspond to a verified fiat deposit
    held by a custodian.
    Atomic double-entry: omnibus reserve is debited, recipient
    is credited.
    """
    issuance, receipt = _issue_tokens(
        db,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=req.amount,
        backing_ref=req.backing_ref,
        custodian=req.custodian,
        idempotency_key=req.idempotency_key,
    )

    new_balance = journal_get_balance(
        db, str(req.account_id), req.currency.value
    )

    return IssuanceResponse(
        issuance_ref=issuance.issuance_ref,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=issuance.amount,
        new_balance=new_balance,
        status=issuance.status.value,
        blockchain=receipt,
    )


@app.post(
    "/tokens/redeem",
    response_model=IssuanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def redeem_tokens(
    req: RedeemTokensRequest,
    db: Session = Depends(get_db_session),
):
    """Redeem tokenized deposits back to fiat.

    Atomically burns tokens and credits the omnibus reserve for
    fiat payout.
    """
    issuance, receipt = _redeem_tokens(
        db,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=req.amount,
        settlement_ref=req.settlement_ref,
        idempotency_key=req.idempotency_key,
    )

    new_balance = journal_get_balance(
        db, str(req.account_id), req.currency.value
    )

    return IssuanceResponse(
        issuance_ref=issuance.issuance_ref,
        account_id=str(req.account_id),
        currency=req.currency.value,
        amount=issuance.amount,
        new_balance=new_balance,
        status=issuance.status.value,
        blockchain=receipt,
    )


@app.get(
    "/tokens/balance/{account_id}",
    response_model=list[TokenBalanceResponse],
)
def get_balances(
    account_id: str,
    db: Session = Depends(get_db_session),
):
    """Return all tokenized deposit balances for an account."""
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(
            status_code=404, detail="Account not found"
        )

    # Get distinct currencies from journal entries for this account
    acct_uuid = uuid.UUID(account_id)
    currencies = db.execute(
        select(JournalEntry.currency).where(
            JournalEntry.account_id == acct_uuid,
            JournalEntry.coa_code == "INSTITUTION_LIABILITY",
        ).distinct()
    ).scalars().all()

    results = []
    for curr in currencies:
        balance = journal_get_balance(db, account_id, curr)
        available = get_available_balance(db, account_id, curr)
        reserved = balance - available
        results.append(
            TokenBalanceResponse(
                account_id=account_id,
                currency=curr,
                balance=balance,
                reserved=reserved,
                available=available,
            )
        )

    return results


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
    uvicorn.run(
        "main:app", host="0.0.0.0", port=port, log_level="info"
    )
