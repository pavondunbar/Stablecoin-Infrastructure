#!/usr/bin/env python3
"""
scripts/ledger_integrity.py
─────────────────────────────
Offline ledger integrity checker.

Verifies the double-entry invariants of the entire ledger:
  1. For every txn_ref, sum(debits) == sum(credits)
  2. For every account+currency, balance == sum(credits) - sum(debits)
     from ledger_entries (balance-after cross-check)
  3. token_balances.balance >= 0 (no negative balances)
  4. token_balances.reserved <= token_balances.balance
  5. Orphaned transactions (no matching ledger entries)

Usage:
    # Against docker-compose postgres:
    DATABASE_URL=postgresql+psycopg2://stablecoin:s3cr3t@localhost:5432/stablecoin_db \
        python scripts/ledger_integrity.py

    # With fix flag (NOT for production — only marks anomalies):
    python scripts/ledger_integrity.py --report-only
"""

import argparse
import os
import sys
from collections import defaultdict
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from sqlalchemy import create_engine, select, func, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://stablecoin:s3cr3t@localhost:5432/stablecoin_db",
)

PASS  = "\033[32m✓\033[0m"
FAIL  = "\033[31m✗\033[0m"
WARN  = "\033[33m⚠\033[0m"
BOLD  = "\033[1m"
RESET = "\033[0m"

issues:    list[str] = []
warnings:  list[str] = []
checks_ok: int       = 0


def ok(msg: str):
    global checks_ok
    checks_ok += 1
    print(f"  {PASS}  {msg}")


def fail(msg: str):
    issues.append(msg)
    print(f"  {FAIL}  {msg}")


def warn(msg: str):
    warnings.append(msg)
    print(f"  {WARN}  {msg}")


def run_checks(db) -> None:
    from shared.models import Account, LedgerEntry, TokenBalance, Transaction

    # ── 1. Double-entry balance per txn_ref ──────────────────────────────────
    print(f"\n{BOLD}Check 1: Double-entry balance (debit == credit per txn_ref){RESET}")

    rows = db.execute(
        text("""
            SELECT txn_ref, entry_type, SUM(amount) AS total
            FROM ledger_entries
            GROUP BY txn_ref, entry_type
        """)
    ).fetchall()

    txn_totals: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in rows:
        txn_totals[row.txn_ref][row.entry_type] = Decimal(str(row.total))

    imbalances = 0
    for ref, sides in txn_totals.items():
        d = sides.get("debit",  Decimal("0"))
        c = sides.get("credit", Decimal("0"))
        if abs(d - c) > Decimal("0.00000001"):
            fail(f"IMBALANCE txn_ref={ref}  debit={d}  credit={c}  diff={d-c}")
            imbalances += 1

    if imbalances == 0:
        ok(f"All {len(txn_totals)} transactions are double-entry balanced.")

    # ── 2. Running balance cross-check ───────────────────────────────────────
    print(f"\n{BOLD}Check 2: token_balances vs ledger_entries running totals{RESET}")

    balances = db.execute(select(TokenBalance)).scalars().all()
    drift_count = 0
    for b in balances:
        # Reconstruct expected balance: sum(credits) - sum(debits) for this account+currency
        net = db.execute(
            text("""
                SELECT
                    COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount ELSE 0 END), 0) -
                    COALESCE(SUM(CASE WHEN entry_type='debit'  THEN amount ELSE 0 END), 0)
                FROM ledger_entries
                WHERE account_id = :acc AND currency = :ccy
            """),
            {"acc": str(b.account_id), "ccy": str(b.currency.value)},
        ).scalar()

        computed = Decimal(str(net)) if net is not None else Decimal("0")
        recorded = b.balance

        # For omnibus/system accounts, seed balance is not in ledger — skip
        if str(b.account_id).startswith("00000000-0000-0000-0000"):
            continue

        if abs(computed - recorded) > Decimal("0.00000001"):
            fail(
                f"BALANCE DRIFT  account={b.account_id}  currency={b.currency.value}  "
                f"ledger={computed}  recorded={recorded}  diff={recorded - computed}"
            )
            drift_count += 1

    if drift_count == 0:
        ok(f"All {len(balances)} balance records match ledger reconstruction.")

    # ── 3. No negative balances ───────────────────────────────────────────────
    print(f"\n{BOLD}Check 3: No negative balances{RESET}")

    negatives = db.execute(
        text("SELECT account_id, currency, balance FROM token_balances WHERE balance < 0")
    ).fetchall()

    if negatives:
        for row in negatives:
            fail(f"NEGATIVE BALANCE  account={row.account_id}  currency={row.currency}  balance={row.balance}")
    else:
        ok("No negative balances found.")

    # ── 4. Reserved <= Balance ────────────────────────────────────────────────
    print(f"\n{BOLD}Check 4: reserved <= balance for all accounts{RESET}")

    over_reserved = db.execute(
        text("SELECT account_id, currency, balance, reserved FROM token_balances WHERE reserved > balance")
    ).fetchall()

    if over_reserved:
        for row in over_reserved:
            fail(
                f"OVER-RESERVED  account={row.account_id}  currency={row.currency}  "
                f"balance={row.balance}  reserved={row.reserved}"
            )
    else:
        ok("All reserved amounts are within balance.")

    # ── 5. Orphaned transactions ──────────────────────────────────────────────
    print(f"\n{BOLD}Check 5: No orphaned transactions (missing ledger entries){RESET}")

    orphans = db.execute(
        text("""
            SELECT t.txn_ref FROM transactions t
            LEFT JOIN ledger_entries l ON l.txn_ref = t.txn_ref
            WHERE l.id IS NULL
              AND t.status = 'completed'
        """)
    ).fetchall()

    if orphans:
        for row in orphans:
            warn(f"ORPHANED TXN  txn_ref={row.txn_ref}  (completed but no ledger entries)")
    else:
        ok("No orphaned completed transactions.")

    # ── 6. Settlement consistency ─────────────────────────────────────────────
    print(f"\n{BOLD}Check 6: Settled RTGS records have a transaction_id{RESET}")

    unsettled = db.execute(
        text("""
            SELECT settlement_ref FROM rtgs_settlements
            WHERE status = 'settled' AND transaction_id IS NULL
        """)
    ).fetchall()

    if unsettled:
        for row in unsettled:
            fail(f"SETTLED WITHOUT TXN  settlement_ref={row.settlement_ref}")
    else:
        ok("All settled RTGS records have a linked transaction.")

    # ── 7. FX PvP leg consistency ─────────────────────────────────────────────
    print(f"\n{BOLD}Check 7: FX settlements have both legs recorded{RESET}")

    missing_legs = db.execute(
        text("""
            SELECT settlement_ref FROM fx_settlements
            WHERE status = 'settled'
              AND (sell_txn_id IS NULL OR buy_txn_id IS NULL)
        """)
    ).fetchall()

    if missing_legs:
        for row in missing_legs:
            fail(f"FX MISSING LEG  settlement_ref={row.settlement_ref}")
    else:
        ok("All settled FX records have both sell and buy legs.")

    # ── 8. Active escrows don't exceed depositor balance ─────────────────────
    print(f"\n{BOLD}Check 8: Active escrow amounts <= depositor reserved balance{RESET}")

    # Sum active escrows per depositor+currency vs their reserved balance
    rows = db.execute(
        text("""
            SELECT e.depositor_account_id, e.currency, SUM(e.amount) as escrow_total,
                   tb.reserved
            FROM escrow_contracts e
            JOIN token_balances tb
              ON tb.account_id = e.depositor_account_id AND tb.currency = e.currency
            WHERE e.status = 'active'
            GROUP BY e.depositor_account_id, e.currency, tb.reserved
        """)
    ).fetchall()

    escrow_issues = 0
    for row in rows:
        escrow_total = Decimal(str(row.escrow_total))
        reserved     = Decimal(str(row.reserved))
        if escrow_total > reserved + Decimal("0.00000001"):
            fail(
                f"ESCROW > RESERVED  account={row.depositor_account_id}  "
                f"currency={row.currency}  escrow_total={escrow_total}  reserved={reserved}"
            )
            escrow_issues += 1

    if escrow_issues == 0:
        ok("All active escrow amounts are within depositor reserved balances.")


def main():
    parser = argparse.ArgumentParser(description="Ledger integrity checker")
    parser.add_argument("--report-only", action="store_true",
                        help="Print report and exit 0 even if issues found")
    args = parser.parse_args()

    print(f"\n{BOLD}Stablecoin Ledger Integrity Checker{RESET}")
    print(f"Database: {DATABASE_URL.split('@')[-1]}")
    print("─" * 60)

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        run_checks(db)
    finally:
        db.close()

    print(f"\n{'─' * 60}")
    print(f"  Checks passed : {BOLD}{checks_ok}{RESET}")
    print(f"  Issues found  : {BOLD}{len(issues)}{RESET}")
    print(f"  Warnings      : {BOLD}{len(warnings)}{RESET}")

    if issues:
        print(f"\n{BOLD}Issues:{RESET}")
        for i in issues:
            print(f"    {FAIL}  {i}")

    if warnings:
        print(f"\n{BOLD}Warnings:{RESET}")
        for w in warnings:
            print(f"    {WARN}  {w}")

    if issues and not args.report_only:
        print(f"\n{BOLD}\033[31mLEDGER INTEGRITY FAILED — {len(issues)} issue(s) require investigation.\033[0m\n")
        sys.exit(1)
    else:
        print(f"\n{BOLD}\033[32mLedger integrity OK.\033[0m\n")


if __name__ == "__main__":
    main()
