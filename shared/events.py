"""
shared/events.py — Canonical Pydantic schemas for all Kafka events.

Every service uses these schemas to publish and consume messages, ensuring
a shared contract across the platform.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ─── Base ─────────────────────────────────────────────────────────────────────

class BaseEvent(BaseModel):
    event_id:   str      = Field(default_factory=_uuid)
    event_time: datetime = Field(default_factory=_now)
    service:    str      = "unknown"

    class Config:
        json_encoders = {Decimal: str, datetime: lambda v: v.isoformat()}


# ─── Token Issuance Events ────────────────────────────────────────────────────

class TokenIssuanceRequested(BaseEvent):
    issuance_ref:  str
    account_id:    str
    currency:      str
    amount:        Decimal
    backing_ref:   Optional[str] = None
    custodian:     Optional[str] = None
    issuance_type: str           = "initial"


class TokenIssuanceCompleted(BaseEvent):
    issuance_ref: str
    account_id:   str
    currency:     str
    amount:       Decimal
    new_balance:  Decimal


class TokenRedemptionRequested(BaseEvent):
    issuance_ref:  str
    account_id:    str
    currency:      str
    amount:        Decimal
    settlement_ref: Optional[str] = None


class TokenRedemptionCompleted(BaseEvent):
    issuance_ref: str
    account_id:   str
    currency:     str
    amount:       Decimal
    new_balance:  Decimal


class TokenBalanceUpdated(BaseEvent):
    account_id:   str
    currency:     str
    old_balance:  Decimal
    new_balance:  Decimal
    reserved:     Decimal
    reason:       str


# ─── RTGS Settlement Events ───────────────────────────────────────────────────

class RTGSSettlementSubmitted(BaseEvent):
    settlement_ref:      str
    sending_account_id:  str
    receiving_account_id: str
    currency:            str
    amount:              Decimal
    priority:            str


class RTGSSettlementProcessing(BaseEvent):
    settlement_ref: str
    started_at:     datetime


class RTGSSettlementCompleted(BaseEvent):
    settlement_ref:      str
    sending_account_id:  str
    receiving_account_id: str
    currency:            str
    amount:              Decimal
    transaction_id:      str
    settled_at:          datetime


class RTGSSettlementFailed(BaseEvent):
    settlement_ref: str
    reason:         str
    retry_count:    int


# ─── Programmable Payment Events ─────────────────────────────────────────────

class ConditionalPaymentCreated(BaseEvent):
    payment_ref:      str
    payer_account_id: str
    payee_account_id: str
    currency:         str
    amount:           Decimal
    condition_type:   str
    condition_params: dict


class ConditionalPaymentTriggered(BaseEvent):
    payment_ref:  str
    trigger_data: dict
    triggered_by: str


class ConditionalPaymentCompleted(BaseEvent):
    payment_ref:     str
    transaction_id:  str
    executed_at:     datetime


class EscrowCreated(BaseEvent):
    contract_ref:          str
    depositor_account_id:  str
    beneficiary_account_id: str
    currency:              str
    amount:                Decimal
    conditions:            dict
    expires_at:            datetime


class EscrowReleased(BaseEvent):
    contract_ref:     str
    release_txn_id:   str
    released_to:      str
    triggered_by:     str
    amount:           Decimal


class EscrowExpired(BaseEvent):
    contract_ref:        str
    refunded_account_id: str
    amount:              Decimal


# ─── FX Settlement Events ────────────────────────────────────────────────────

class FXRateUpdated(BaseEvent):
    base_currency:  str
    quote_currency: str
    mid_rate:       Decimal
    bid_rate:       Optional[Decimal] = None
    ask_rate:       Optional[Decimal] = None
    source:         str


class FXSettlementInitiated(BaseEvent):
    settlement_ref:      str
    sending_account_id:  str
    receiving_account_id: str
    sell_currency:       str
    sell_amount:         Decimal
    buy_currency:        str
    buy_amount:          Decimal
    applied_rate:        Decimal
    rails:               str


class FXSettlementLegCompleted(BaseEvent):
    settlement_ref: str
    leg:            str    # "sell" or "buy"
    transaction_id: str
    amount:         Decimal
    currency:       str


class FXSettlementCompleted(BaseEvent):
    settlement_ref:   str
    sell_txn_id:      str
    buy_txn_id:       str
    blockchain_hash:  Optional[str] = None
    settled_at:       datetime


class FXSettlementFailed(BaseEvent):
    settlement_ref: str
    reason:         str


# ─── Compliance Events ────────────────────────────────────────────────────────

class ComplianceEvent(BaseEvent):
    entity_type: str
    entity_id:   str
    event_type:  str
    result:      str
    score:       Optional[Decimal] = None
    details:     dict = Field(default_factory=dict)


class AuditTrailEntry(BaseEvent):
    actor_service: str
    action:        str
    entity_type:   str
    entity_id:     str
    before_state:  Optional[dict] = None
    after_state:   Optional[dict] = None
    ip_address:    Optional[str]  = None
