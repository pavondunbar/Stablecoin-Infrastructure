"""
services/compliance-monitor/main.py
─────────────────────────────────────
Compliance Monitor — Real-time AML, Sanctions & Risk Screening

Responsibilities:
  • Consumes every transaction/settlement/issuance event from Kafka
  • Runs rule-based AML screening (velocity, structuring, threshold alerts)
  • Runs simulated sanctions screening (OFAC SDN list pattern matching)
  • Writes compliance events to Postgres for auditors
  • Emits alerts back to Kafka (compliance.event) for downstream action
  • Exposes a health + metrics endpoint

In production this would integrate with:
  • OFAC/UN/EU sanctions APIs (Chainalysis, Elliptic, ComplyAdvantage)
  • Internal risk-scoring models (transaction graph analytics)
  • Regulatory reporting pipelines (FinCEN, FCA, MAS)
"""

import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

import sys
sys.path.insert(0, "/app/shared")

from database import SessionLocal
from models import Account, ComplianceEvent as ComplianceEventModel
import kafka_client as kafka
from metrics import instrument_app
from events import ComplianceEvent, AuditTrailEntry

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
SERVICE = os.environ.get("SERVICE_NAME", "compliance-monitor")

# ─── Rule Configuration ───────────────────────────────────────────────────────

AML_RULES = {
    "large_transaction_usd":  Decimal("1_000_000"),   # CTR threshold equivalent
    "structuring_window_usd": Decimal("9_500"),        # Structuring detection
    "velocity_per_hour":      20,                      # Max transactions per account/hour
    "high_risk_currencies":   {"XMR", "ZEC"},          # (not in our enum, illustrative)
    "sanctions_patterns": [                            # Simplified SDN name fragments
        "DPRK", "IRAN", "SUKHOI", "SBERBANK", "TETHER_FRAUD",
        "LAZARUS", "SANCTIONED", "BLOCKED_ENTITY",
    ],
}

# In-memory velocity tracker: account_id → list of recent tx timestamps
_velocity_tracker: dict[str, list[datetime]] = {}
_velocity_lock = threading.Lock()

# Running stats for the metrics endpoint
_stats = {
    "events_processed": 0,
    "alerts_raised":    0,
    "passes":           0,
    "failures":         0,
    "started_at":       datetime.now(timezone.utc).isoformat(),
}


# ─── Screening Functions ──────────────────────────────────────────────────────

def _check_large_transaction(amount: Decimal, currency: str) -> list[str]:
    """Flag transactions above reporting threshold (CTR equivalent)."""
    flags = []
    # Normalise to approximate USD (simplified; production uses live FX)
    fx_approx = {"EUR": Decimal("1.09"), "GBP": Decimal("1.27"), "USD": Decimal("1")}
    usd_equiv = amount * fx_approx.get(currency, Decimal("1"))
    if usd_equiv >= AML_RULES["large_transaction_usd"]:
        flags.append(f"LARGE_TRANSACTION: {amount} {currency} (~${usd_equiv:,.0f} USD equivalent)")
    return flags


def _check_structuring(amount: Decimal, currency: str) -> list[str]:
    """Detect potential structuring (transactions just below reporting threshold)."""
    flags = []
    fx_approx = {"EUR": Decimal("1.09"), "GBP": Decimal("1.27"), "USD": Decimal("1")}
    usd_equiv = amount * fx_approx.get(currency, Decimal("1"))
    threshold = AML_RULES["structuring_window_usd"]
    reporting = AML_RULES["large_transaction_usd"]
    if threshold <= usd_equiv < reporting:
        flags.append(f"POTENTIAL_STRUCTURING: {amount} {currency} just below reporting threshold")
    return flags


def _check_velocity(account_id: str) -> list[str]:
    """Detect high-frequency transaction velocity per account."""
    flags = []
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - 3600  # 1-hour window

    with _velocity_lock:
        history = _velocity_tracker.get(account_id, [])
        recent = [t for t in history if t.timestamp() > cutoff]
        recent.append(now)
        _velocity_tracker[account_id] = recent[-100:]  # cap list size

    if len(recent) > AML_RULES["velocity_per_hour"]:
        flags.append(f"HIGH_VELOCITY: {len(recent)} transactions in 1 hour for {account_id}")
    return flags


def _check_sanctions(entity_name: str, account_id: str) -> list[str]:
    """
    Simplified sanctions screening against SDN pattern list.
    Production: call ComplyAdvantage / Chainalysis API with full name + LEI.
    """
    flags = []
    name_upper = (entity_name or "").upper()
    for pattern in AML_RULES["sanctions_patterns"]:
        if pattern in name_upper:
            flags.append(f"SANCTIONS_HIT: '{pattern}' matched in entity name '{entity_name}'")
    return flags


def _resolve_account_name(db, account_id: str) -> str:
    try:
        acct = db.get(Account, account_id)
        return acct.entity_name if acct else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _write_compliance_event(
    entity_type: str,
    entity_id: str,
    event_type: str,
    result: str,
    score: Decimal,
    details: dict,
):
    """Persist compliance event to Postgres."""
    try:
        db = SessionLocal()
        try:
            ev = ComplianceEventModel(
                entity_type=entity_type,
                entity_id=uuid.UUID(entity_id) if len(entity_id) == 36 else uuid.uuid4(),
                event_type=event_type,
                result=result,
                score=score,
                details=details,
            )
            db.add(ev)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    except Exception as exc:
        log.error("Failed to write compliance event: %s", exc)


def _run_screening(
    entity_type: str,
    entity_id: str,
    account_id: str,
    amount: Decimal,
    currency: str,
    event_type: str,
):
    """Run all AML/sanctions rules and emit results to Kafka."""
    db = SessionLocal()
    try:
        entity_name = _resolve_account_name(db, account_id)
    finally:
        db.close()

    all_flags = []
    all_flags += _check_large_transaction(amount, currency)
    all_flags += _check_structuring(amount, currency)
    all_flags += _check_velocity(account_id)
    all_flags += _check_sanctions(entity_name, account_id)

    result = "fail" if all_flags else "pass"
    score  = Decimal(str(min(100, len(all_flags) * 25)))  # 0-100 risk score

    details = {
        "account_id":   account_id,
        "entity_name":  entity_name,
        "amount":       str(amount),
        "currency":     currency,
        "flags":        all_flags,
    }

    _write_compliance_event(entity_type, entity_id, event_type, result, score, details)

    # Publish to compliance.event topic
    kafka.publish(
        "compliance.event",
        ComplianceEvent(
            service=SERVICE,
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            result=result,
            score=score,
            details=details,
        ),
        key=entity_id,
    )

    if result == "fail":
        _stats["alerts_raised"] += 1
        log.warning(
            "🚨 COMPLIANCE ALERT | entity=%s id=%s flags=%s",
            entity_type, entity_id, all_flags,
        )
    else:
        _stats["passes"] += 1
        log.debug("✅ Compliance PASS | entity=%s id=%s", entity_type, entity_id)

    _stats["events_processed"] += 1


# ─── Event Handlers ───────────────────────────────────────────────────────────

def handle_event(topic: str, payload: dict):
    """Route each Kafka topic to the appropriate screening function."""
    try:
        if topic in ("token.issuance.completed", "token.redemption.completed"):
            _run_screening(
                entity_type="token_issuance",
                entity_id=payload.get("event_id", str(uuid.uuid4())),
                account_id=payload.get("account_id", ""),
                amount=Decimal(str(payload.get("amount", 0))),
                currency=payload.get("currency", "USD"),
                event_type="issuance_screen",
            )

        elif topic in ("rtgs.settlement.completed",):
            _run_screening(
                entity_type="rtgs_settlement",
                entity_id=payload.get("settlement_ref", str(uuid.uuid4())),
                account_id=payload.get("sending_account_id", ""),
                amount=Decimal(str(payload.get("amount", 0))),
                currency=payload.get("currency", "USD"),
                event_type="rtgs_screen",
            )
            # Also screen the receiving side
            _run_screening(
                entity_type="rtgs_settlement",
                entity_id=payload.get("settlement_ref", str(uuid.uuid4())),
                account_id=payload.get("receiving_account_id", ""),
                amount=Decimal(str(payload.get("amount", 0))),
                currency=payload.get("currency", "USD"),
                event_type="rtgs_screen_receiver",
            )

        elif topic == "payment.conditional.completed":
            _run_screening(
                entity_type="conditional_payment",
                entity_id=payload.get("payment_ref", str(uuid.uuid4())),
                account_id=payload.get("payer_account_id", ""),
                amount=Decimal(str(payload.get("amount", 0))),
                currency=payload.get("currency", "USD"),
                event_type="conditional_payment_screen",
            )

        elif topic == "escrow.released":
            _run_screening(
                entity_type="escrow",
                entity_id=payload.get("contract_ref", str(uuid.uuid4())),
                account_id=payload.get("released_to", ""),
                amount=Decimal(str(payload.get("amount", 0))),
                currency=payload.get("currency", "USD"),
                event_type="escrow_screen",
            )

        elif topic == "fx.settlement.completed":
            _run_screening(
                entity_type="fx_settlement",
                entity_id=payload.get("settlement_ref", str(uuid.uuid4())),
                account_id=payload.get("sending_account_id", ""),
                amount=Decimal(str(payload.get("sell_amount", 0))),
                currency=payload.get("sell_currency", "USD"),
                event_type="fx_screen",
            )

        else:
            log.debug("Unhandled topic: %s", topic)

    except Exception as exc:
        _stats["failures"] += 1
        log.exception("Handler error | topic=%s exc=%s", topic, exc)


# ─── Kafka Consumer Thread ────────────────────────────────────────────────────

MONITORED_TOPICS = [
    "token.issuance.completed",
    "token.redemption.completed",
    "rtgs.settlement.completed",
    "payment.conditional.completed",
    "escrow.released",
    "fx.settlement.completed",
]


def _start_consumer():
    log.info("Compliance consumer subscribing to: %s", MONITORED_TOPICS)
    consumer = kafka.build_consumer(
        group_id="compliance-monitor-group",
        topics=MONITORED_TOPICS,
    )
    kafka.consume_loop(consumer, handle_event)


# ─── FastAPI Health / Metrics Endpoint ───────────────────────────────────────

app = FastAPI(title="Compliance Monitor", version="1.0.0")

instrument_app(app, SERVICE)


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
def metrics():
    """Real-time compliance screening statistics."""
    alert_rate = 0.0
    total = _stats["events_processed"]
    if total > 0:
        alert_rate = round(_stats["alerts_raised"] / total * 100, 2)

    return {
        "service":          SERVICE,
        "events_processed": _stats["events_processed"],
        "alerts_raised":    _stats["alerts_raised"],
        "passes":           _stats["passes"],
        "failures":         _stats["failures"],
        "alert_rate_pct":   alert_rate,
        "monitored_topics": MONITORED_TOPICS,
        "started_at":       _stats["started_at"],
        "uptime_seconds":   round(
            (datetime.now(timezone.utc) -
             datetime.fromisoformat(_stats["started_at"])).total_seconds()
        ),
    }


@app.get("/rules")
def get_rules():
    """Return the active AML rule configuration."""
    return {
        "large_transaction_threshold_usd": str(AML_RULES["large_transaction_usd"]),
        "structuring_window_usd":          str(AML_RULES["structuring_window_usd"]),
        "velocity_limit_per_hour":         AML_RULES["velocity_per_hour"],
        "sanctions_patterns_count":        len(AML_RULES["sanctions_patterns"]),
    }


# ─── Startup ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    consumer_thread = threading.Thread(target=_start_consumer, daemon=True, name="compliance-consumer")
    consumer_thread.start()
    log.info("Compliance Monitor started. Consuming from Kafka…")

    port = int(os.environ.get("PORT", 8005))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
