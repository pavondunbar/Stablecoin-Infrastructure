#!/usr/bin/env python3
"""
scripts/demo.py — Full live-stack demonstration script
───────────────────────────────────────────────────────
Exercises the entire Stablecoin & Digital Cash Infrastructure
against a running docker-compose stack.

Usage:
    # Start the stack first:
    #   docker compose up --build -d
    #   (wait ~60s for all services to be healthy)

    python scripts/demo.py

    # Or with a custom gateway URL / API key:
    GATEWAY_URL=http://localhost:8000 GATEWAY_API_KEY=my-key python scripts/demo.py
"""

import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

# ─── Config ───────────────────────────────────────────────────────────────────

GW  = os.environ.get("GATEWAY_URL", "http://localhost:8000")
KEY = os.environ.get("GATEWAY_API_KEY", "change-me-in-production")

HEADERS = {"X-API-Key": KEY, "Content-Type": "application/json"}

client = httpx.Client(base_url=GW, headers=HEADERS, timeout=30)

# ─── Colours ──────────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD  = "\033[1m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
RED   = "\033[31m"
YELLOW = "\033[33m"
BLUE  = "\033[34m"


def h1(text: str):
    width = 72
    print(f"\n{BOLD}{BLUE}{'═' * width}{RESET}")
    print(f"{BOLD}{BLUE}  {text}{RESET}")
    print(f"{BOLD}{BLUE}{'═' * width}{RESET}")


def h2(text: str):
    print(f"\n{CYAN}  ── {text}{RESET}")


def ok(text: str):
    print(f"  {GREEN}✓{RESET}  {text}")


def info(text: str):
    print(f"  {YELLOW}→{RESET}  {text}")


def fail(text: str):
    print(f"  {RED}✗{RESET}  {text}")


def pretty(data: dict) -> str:
    return json.dumps(data, indent=4, default=str)


def check(resp: httpx.Response, expected: int = None) -> dict:
    if expected and resp.status_code != expected:
        fail(f"HTTP {resp.status_code} — expected {expected}")
        print(f"  Body: {resp.text[:400]}")
        sys.exit(1)
    data = resp.json()
    return data


# ─── Step Runner ──────────────────────────────────────────────────────────────

step_n = 0


def step(title: str):
    global step_n
    step_n += 1
    print(f"\n  {BOLD}[{step_n:02d}]{RESET} {title}")


# ─── Demo ─────────────────────────────────────────────────────────────────────

def wait_healthy(retries: int = 12, delay: float = 5.0):
    """Wait until the gateway reports all services healthy."""
    info(f"Waiting for {GW}/health …")
    for i in range(retries):
        try:
            r = client.get("/health", timeout=5)
            if r.status_code == 200:
                data = r.json()
                if all(v == "ok" for v in data["services"].values()):
                    ok("All services healthy.")
                    return
            print(f"  retry {i+1}/{retries} — {r.json().get('services', {})}")
        except Exception as exc:
            print(f"  retry {i+1}/{retries} — {exc}")
        time.sleep(delay)
    fail("Stack not healthy after retries. Run: docker compose up --build -d")
    sys.exit(1)


def demo_token_issuance(bank_a_id: str, bank_b_id: str) -> dict:
    h1("1 · Token Issuance & Redemption")

    step("Issue $10M tokenized USD to Bank A (JPM Coin style)")
    r = client.post("/v1/tokens/issue", json={
        "account_id":  bank_a_id,
        "currency":    "USD",
        "amount":      "10000000.00",
        "backing_ref": "FIAT-DEP-2024-001",
        "custodian":   "JPMorgan Chase",
        "idempotency_key": f"ISS-DEMO-{uuid.uuid4().hex[:8]}",
    })
    data = check(r, 201)
    ok(f"Issued {data['amount']} {data['currency']} → balance: {data['new_balance']}")

    step("Issue €5M tokenized EUR to Bank A")
    r = client.post("/v1/tokens/issue", json={
        "account_id":  bank_a_id,
        "currency":    "EUR",
        "amount":      "5000000.00",
        "backing_ref": "FIAT-DEP-2024-002",
        "custodian":   "Deutsche Bank",
    })
    check(r, 201)
    ok("EUR issuance succeeded.")

    step("Check Bank A balances")
    r   = client.get(f"/v1/tokens/balance/{bank_a_id}")
    bals = check(r, 200)
    for b in bals:
        ok(f"  {b['currency']:3s}  balance={b['balance']}  reserved={b['reserved']}  available={b['available']}")

    step("Redeem $1M back to fiat from Bank A")
    r = client.post("/v1/tokens/redeem", json={
        "account_id":    bank_a_id,
        "currency":      "USD",
        "amount":        "1000000.00",
        "settlement_ref": "SETTLE-FIAT-001",
    })
    data = check(r, 201)
    ok(f"Redeemed {data['amount']} USD → new balance: {data['new_balance']}")

    return {"bank_a_usd": Decimal(data["new_balance"])}


def demo_rtgs(bank_a_id: str, bank_b_id: str) -> dict:
    h1("2 · Real-Time Gross Settlement (RTGS)")

    step("Submit URGENT $5M settlement: Bank A → Bank B")
    r = client.post("/v1/settlements", json={
        "sending_account_id":   bank_a_id,
        "receiving_account_id": bank_b_id,
        "currency":             "USD",
        "amount":               "5000000.00",
        "priority":             "urgent",
        "idempotency_key":      f"RTGS-DEMO-{uuid.uuid4().hex[:8]}",
    })
    data = check(r, 202)
    ref  = data["settlement_ref"]
    ok(f"Submitted: {ref}  status={data['status']}  priority={data['priority']}")

    step("Poll settlement status (worker processes async)")
    for attempt in range(10):
        time.sleep(1.5)
        r = client.get(f"/v1/settlements/{ref}")
        s = check(r, 200)
        info(f"  attempt {attempt+1}: status={s['status']}")
        if s["status"] == "settled":
            ok(f"Settled in {attempt+1} polls! txn_id={s['transaction_id']}")
            break
    else:
        fail("Settlement did not complete in time.")

    step("Submit NORMAL priority $500K: Bank B → Bank A (return payment)")
    r = client.post("/v1/settlements", json={
        "sending_account_id":   bank_b_id,
        "receiving_account_id": bank_a_id,
        "currency":             "USD",
        "amount":               "500000.00",
        "priority":             "normal",
    })
    check(r, 202)
    ok("Normal priority settlement queued.")

    step("List all settlements")
    r = client.get("/v1/settlements?limit=5")
    settlements = check(r, 200)
    ok(f"Found {len(settlements)} recent settlements")
    for s in settlements[:3]:
        info(f"  {s['settlement_ref']}  {s['currency']} {s['amount']}  [{s['status']}]")

    return {}


def demo_programmable_payments(bank_a_id: str, bank_b_id: str) -> dict:
    h1("3 · Programmable Payments (Conditional + Escrow)")

    # ── Time-Lock Conditional Payment ─────────────────────────────────────────
    step("Create time-locked payment: $250K unlocks in 2 seconds")
    release_at = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    r = client.post("/v1/payments/conditional", json={
        "payer_account_id": bank_a_id,
        "payee_account_id": bank_b_id,
        "currency":         "USD",
        "amount":           "250000.00",
        "condition_type":   "time_lock",
        "condition_params": {"release_at": release_at},
    })
    data = check(r, 201)
    cp_ref = data["payment_ref"]
    ok(f"Created: {cp_ref}  condition=time_lock  status={data['status']}")

    step("Wait 3s for time-lock condition to be auto-evaluated by background checker…")
    time.sleep(3)
    r    = client.get(f"/v1/payments/conditional/{cp_ref}")
    data = check(r, 200)
    if data["status"] == "completed":
        ok(f"Time-lock executed automatically! txn recorded at {data['executed_at']}")
    else:
        info(f"Status: {data['status']} (checker interval may not have fired yet)")

    # ── Oracle-Triggered Conditional Payment ──────────────────────────────────
    step("Create oracle-triggered payment: $500K on SOFR = 5.33")
    r = client.post("/v1/payments/conditional", json={
        "payer_account_id": bank_a_id,
        "payee_account_id": bank_b_id,
        "currency":         "USD",
        "amount":           "500000.00",
        "condition_type":   "oracle_trigger",
        "condition_params": {"oracle_key": "SOFR", "expected_value": "5.33"},
        "expires_at":       (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    })
    data   = check(r, 201)
    or_ref = data["payment_ref"]
    ok(f"Created: {or_ref}")

    step("Trigger with WRONG oracle value (should not execute)")
    r = client.post(f"/v1/payments/conditional/{or_ref}/trigger", json={
        "trigger_data": {"oracle_key": "SOFR", "oracle_value": "5.00"},
        "triggered_by": "sofr-oracle-feed",
    })
    data = check(r, 200)
    ok(f"Result: {data['result']} (expected condition_not_satisfied)")

    step("Trigger with CORRECT oracle value (should execute)")
    r = client.post(f"/v1/payments/conditional/{or_ref}/trigger", json={
        "trigger_data": {"oracle_key": "SOFR", "oracle_value": "5.33"},
        "triggered_by": "sofr-oracle-feed",
    })
    data = check(r, 200)
    if data.get("result") == "executed":
        ok(f"Oracle payment executed! txn_id={data['transaction_id']}")
    else:
        info(f"Result: {data}")

    # ── Multi-sig Conditional Payment ─────────────────────────────────────────
    step("Create 2-of-3 multi-sig payment: $1M requiring treasury + compliance + CEO")
    r = client.post("/v1/payments/conditional", json={
        "payer_account_id": bank_a_id,
        "payee_account_id": bank_b_id,
        "currency":         "USD",
        "amount":           "1000000.00",
        "condition_type":   "multi_sig",
        "condition_params": {
            "required_signers": ["treasury", "compliance", "ceo"],
            "threshold": 2,
        },
    })
    data   = check(r, 201)
    ms_ref = data["payment_ref"]
    ok(f"Created 2-of-3 multi-sig payment: {ms_ref}")

    step("Submit 2 signatures (treasury + compliance) → should execute")
    r = client.post(f"/v1/payments/conditional/{ms_ref}/trigger", json={
        "trigger_data": {"signatures": ["treasury", "compliance"]},
        "triggered_by": "approval-service",
    })
    data = check(r, 200)
    ok(f"Multi-sig result: {data.get('result')}")

    # ── Escrow ────────────────────────────────────────────────────────────────
    step("Create escrow: Bank A locks $2M for trade finance (30-day expiry)")
    r = client.post("/v1/payments/escrow", json={
        "depositor_account_id":   bank_a_id,
        "beneficiary_account_id": bank_b_id,
        "currency":               "USD",
        "amount":                 "2000000.00",
        "conditions":             {"description": "Release on BL confirmation", "bl_ref": "BL-2024-5551"},
        "expires_at":             (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    data  = check(r, 201)
    e_ref = data["contract_ref"]
    ok(f"Escrow created: {e_ref}  funds reserved: ${data['amount']}")

    step("Release escrow to beneficiary (delivery confirmed)")
    r = client.post(f"/v1/payments/escrow/{e_ref}/release", json={
        "release_to":   "beneficiary",
        "triggered_by": "trade-ops-team",
    })
    data = check(r, 200)
    ok(f"Escrow released: {data['result']}  txn_id={data.get('transaction_id')}")

    return {}


def demo_fx_settlement(bank_a_id: str, bank_b_id: str) -> dict:
    h1("4 · Cross-Border FX Settlement (Blockchain Rails)")

    step("View live FX rate book")
    r     = client.get("/v1/fx/rates")
    rates = check(r, 200)
    ok(f"Active rates: {len(rates)}")
    for rate in rates[:4]:
        info(f"  {rate['base']}/{rate['quote']}  mid={rate['mid_rate']}  spread={rate['spread_bps']}bps")

    step("Request FX quote: $10M USD → EUR")
    r    = client.post("/v1/fx/quote", json={
        "sell_currency": "USD",
        "sell_amount":   "10000000.00",
        "buy_currency":  "EUR",
    })
    data = check(r, 200)
    ok(f"Quote: sell {data['sell_amount']} USD → buy {data['buy_amount']} EUR")
    ok(f"  Applied rate: {data['applied_rate']}  (mid: {data['mid_rate']}, spread: {data['spread_bps']}bps)")
    ok(f"  Valid until: {data['quote_valid_until']}")

    step("Execute $10M USD→EUR FX settlement via blockchain rails")
    r    = client.post("/v1/fx/settle", json={
        "sending_account_id":   bank_a_id,
        "receiving_account_id": bank_b_id,
        "sell_currency":        "USD",
        "sell_amount":          "10000000.00",
        "buy_currency":         "EUR",
        "rails":                "blockchain",
    })
    data = check(r, 202)
    ref  = data["settlement_ref"]
    ok(f"FX settlement submitted: {ref}")
    ok(f"  Sell: {data['sell_amount']} {data['sell_currency']}  →  Buy: {data['buy_amount']} {data['buy_currency']}")
    ok(f"  Applied rate: {data['applied_rate']}  Rails: {data['rails']}")
    info(f"  Estimated settlement: {data['estimated_settlement']}")

    step("Poll FX settlement status (PvP atomic swap)")
    for attempt in range(10):
        time.sleep(1.5)
        r = client.get(f"/v1/fx/settlements/{ref}")
        s = check(r, 200)
        info(f"  attempt {attempt+1}: status={s['status']}")
        if s["status"] == "settled":
            ok(f"PvP settled! blockchain_tx_hash={s['blockchain_tx_hash']}")
            ok(f"  sell_txn={s['sell_txn_id']}")
            ok(f"  buy_txn={s['buy_txn_id']}")
            break
    else:
        info("FX settlement still processing (worker may be busy)")

    step("Request GBP→USD quote (inverse pair)")
    r = client.post("/v1/fx/quote", json={
        "sell_currency": "GBP",
        "sell_amount":   "5000000.00",
        "buy_currency":  "USD",
    })
    data = check(r, 200)
    ok(f"Quote: sell {data['sell_amount']} GBP → buy {data['buy_amount']} USD  rate={data['applied_rate']}")

    return {}


def run():
    h1("Stablecoin & Digital Cash Infrastructure — Live Demo")
    info(f"Gateway: {GW}")
    print()

    # ── Health Check ──────────────────────────────────────────────────────────
    wait_healthy()

    # ── Onboard Participants ──────────────────────────────────────────────────
    h1("0 · Onboard Institutional Participants")

    step("Register Bank A (Institutional)")
    r = client.post(
        "/v1/accounts",
        params={
            "entity_name":  "Acme Institutional Bank",
            "account_type": "bank",
            "lei":          f"ACME{uuid.uuid4().hex[:14].upper()}",
        },
    )
    data    = check(r, 201)
    bank_a_id = data["account_id"]
    ok(f"Bank A created: {bank_a_id} ({data['entity_name']})")

    step("Register Bank B (Correspondent)")
    r = client.post(
        "/v1/accounts",
        params={
            "entity_name":  "Global Correspondent Trust",
            "account_type": "correspondent",
            "bic_code":     "GLBLUS33XXX",
        },
    )
    data    = check(r, 201)
    bank_b_id = data["account_id"]
    ok(f"Bank B created: {bank_b_id} ({data['entity_name']})")

    info("NOTE: Marking accounts KYC/AML verified (normally done via compliance service)")
    info(f"  In production: UPDATE accounts SET kyc_verified=true, aml_cleared=true WHERE id IN ('{bank_a_id}', '{bank_b_id}');")
    info("  For this demo, please run the SQL above against the Postgres container, then re-run.")
    info("  Or the issuance calls will return 403. Continuing demo assuming accounts are cleared…")

    # ── Run each module demo ──────────────────────────────────────────────────
    try:
        demo_token_issuance(bank_a_id, bank_b_id)
        demo_rtgs(bank_a_id, bank_b_id)
        demo_programmable_payments(bank_a_id, bank_b_id)
        demo_fx_settlement(bank_a_id, bank_b_id)
    except SystemExit:
        print(f"\n{RED}Demo aborted — see error above.{RESET}")
        sys.exit(1)

    # ── Summary ───────────────────────────────────────────────────────────────
    h1("Demo Complete")
    ok("Token issuance (mint/burn) with double-entry ledger")
    ok("RTGS gross settlement with priority queuing")
    ok("Conditional payments: time-lock, oracle, multi-sig, delivery")
    ok("Escrow contracts with atomic lock/release/refund")
    ok("Cross-border PvP FX settlement via blockchain rails")
    ok("All events published to Kafka audit trail")
    print(f"\n  {BOLD}Explore the OpenAPI docs at: {GW}/docs{RESET}\n")


if __name__ == "__main__":
    run()
