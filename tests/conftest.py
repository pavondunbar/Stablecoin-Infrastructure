"""
tests/conftest.py — Shared pytest fixtures for the stablecoin test suite.

Uses an in-memory SQLite database for speed; all ORM models are created fresh
per test session.  Kafka calls are monkeypatched to a no-op collector so tests
don't require a running broker.
"""

import sys
import uuid
from decimal import Decimal
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, "/home/claude/stablecoin-infra")
sys.path.insert(0, "/home/claude/stablecoin-infra/shared")

# Patch env before any service module is imported
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("KAFKA_BOOTSTRAP", "localhost:9999")
os.environ.setdefault("SERVICE_NAME",    "test-service")
os.environ.setdefault("GATEWAY_API_KEY", "test-api-key-secret")
os.environ.setdefault("TOKEN_ISSUANCE_URL", "http://token-issuance:8001")
os.environ.setdefault("RTGS_URL",           "http://rtgs:8002")
os.environ.setdefault("PAYMENT_ENGINE_URL", "http://payment-engine:8003")
os.environ.setdefault("FX_SETTLEMENT_URL",  "http://fx-settlement:8004")
os.environ.setdefault("COMPLIANCE_URL",     "http://compliance-monitor:8005")

from shared.models import Base, Account, TokenBalance, FXRate
from shared.models import AccountType, CurrencyCode, TxnStatus


# ─── Database ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def engine():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite doesn't enforce CHECK constraints by default
    @sa_event.listens_for(eng, "connect")
    def enable_fk(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    return eng


@pytest.fixture(scope="session")
def SessionFactory(engine):
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@pytest.fixture
def db(SessionFactory) -> Generator[Session, None, None]:
    """Provides a transactional session that is rolled back after each test."""
    connection = SessionFactory.kw["bind"].connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


# ─── Kafka mock ───────────────────────────────────────────────────────────────

class KafkaSpy:
    """Records all publish() calls so tests can assert on events emitted."""
    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, topic: str, event, key=None):
        self.calls.append({"topic": topic, "event": event, "key": key})

    def publish_dict(self, topic: str, data: dict, key=None):
        self.calls.append({"topic": topic, "event": data, "key": key})

    def reset(self):
        self.calls.clear()

    def events_for(self, topic: str) -> list:
        return [c["event"] for c in self.calls if c["topic"] == topic]

    def last(self, topic: str):
        events = self.events_for(topic)
        return events[-1] if events else None


@pytest.fixture(scope="session")
def kafka_spy() -> KafkaSpy:
    return KafkaSpy()


@pytest.fixture(autouse=True)
def mock_kafka(kafka_spy, monkeypatch):
    """Auto-applied: replace kafka_client.publish with the spy for every test."""
    import shared.kafka_client as kc
    monkeypatch.setattr(kc, "publish",      kafka_spy.publish)
    monkeypatch.setattr(kc, "publish_dict", kafka_spy.publish_dict)
    kafka_spy.reset()
    yield kafka_spy


# ─── Account / Balance Factories ─────────────────────────────────────────────

OMNIBUS_ID = "00000000-0000-0000-0000-000000000001"


def make_account(
    db: Session,
    entity_name: str = "Test Bank",
    account_type: str = "bank",
    kyc: bool = True,
    aml: bool = True,
    active: bool = True,
    account_id: str = None,
) -> Account:
    acct = Account(
        id=uuid.UUID(account_id) if account_id else uuid.uuid4(),
        entity_name=entity_name,
        account_type=account_type,
        kyc_verified=kyc,
        aml_cleared=aml,
        is_active=active,
        risk_tier=2,
    )
    db.add(acct)
    db.flush()
    return acct


def make_balance(
    db: Session,
    account_id: str,
    currency: str,
    balance: Decimal,
    reserved: Decimal = Decimal("0"),
) -> TokenBalance:
    bal = TokenBalance(
        account_id=uuid.UUID(account_id) if isinstance(account_id, str) else account_id,
        currency=currency,
        balance=balance,
        reserved=reserved,
    )
    db.add(bal)
    db.flush()
    return bal


def make_fx_rate(
    db: Session,
    base: str,
    quote: str,
    mid: Decimal,
    spread_bps: Decimal = Decimal("10"),
) -> FXRate:
    rate = FXRate(
        base_currency=base,
        quote_currency=quote,
        mid_rate=mid,
        bid_rate=mid * (1 - spread_bps / 20000),
        ask_rate=mid * (1 + spread_bps / 20000),
        spread_bps=spread_bps,
        source="test",
        is_active=True,
    )
    db.add(rate)
    db.flush()
    return rate


@pytest.fixture
def omnibus(db) -> Account:
    """The omnibus reserve account that backs all token issuances."""
    acct = make_account(db, "OMNIBUS_RESERVE", "central_bank",
                        account_id=OMNIBUS_ID)
    make_balance(db, OMNIBUS_ID, "USD", Decimal("1_000_000_000"))
    make_balance(db, OMNIBUS_ID, "EUR", Decimal("1_000_000_000"))
    make_balance(db, OMNIBUS_ID, "GBP", Decimal("1_000_000_000"))
    return acct


@pytest.fixture
def alice(db, omnibus) -> Account:
    acct = make_account(db, "Alice Institutional Bank")
    make_balance(db, str(acct.id), "USD", Decimal("50_000_000"))
    make_balance(db, str(acct.id), "EUR", Decimal("20_000_000"))
    return acct


@pytest.fixture
def bob(db, omnibus) -> Account:
    acct = make_account(db, "Bob Trust Company")
    make_balance(db, str(acct.id), "USD", Decimal("30_000_000"))
    return acct


@pytest.fixture
def unverified_account(db) -> Account:
    return make_account(db, "Unverified Corp", kyc=False, aml=False)
