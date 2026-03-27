"""Add compliance and performance indexes

Revision ID: 0002_perf_indexes
Revises: 0001_initial
Create Date: 2024-11-15 00:00:00.000000 UTC

Adds covering indexes for the most common query patterns observed
in production workloads after the initial schema baseline.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002_perf_indexes"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Faster lookup for RTGS settlements by account (intraday position queries)
    op.create_index(
        "idx_rtgs_sending_status",
        "rtgs_settlements",
        ["sending_account_id", "status"],
    )
    op.create_index(
        "idx_rtgs_receiving_status",
        "rtgs_settlements",
        ["receiving_account_id", "status"],
    )

    # FX settlement account lookups
    op.create_index(
        "idx_fx_sender",
        "fx_settlements",
        ["sending_account_id", "created_at"],
    )

    # Compliance: recent events per entity (used by the AML dashboard)
    op.create_index(
        "idx_compliance_entity_time",
        "compliance_events",
        ["entity_type", "entity_id", "checked_at"],
    )

    # Conditional payment: poll for expired payments (background checker)
    op.create_index(
        "idx_condpay_expires",
        "conditional_payments",
        ["expires_at"],
        postgresql_where=sa.text("status = 'pending' AND expires_at IS NOT NULL"),
    )

    # Ledger: last N entries per account (balance history)
    op.create_index(
        "idx_ledger_account_ccy",
        "ledger_entries",
        ["account_id", "currency", "created_at"],
    )

    # Issuance: active tokens by custodian (reconciliation queries)
    op.create_index(
        "idx_issuance_custodian",
        "token_issuances",
        ["custodian", "status"],
        postgresql_where=sa.text("custodian IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_issuance_custodian",    table_name="token_issuances")
    op.drop_index("idx_ledger_account_ccy",    table_name="ledger_entries")
    op.drop_index("idx_condpay_expires",       table_name="conditional_payments")
    op.drop_index("idx_compliance_entity_time",table_name="compliance_events")
    op.drop_index("idx_fx_sender",             table_name="fx_settlements")
    op.drop_index("idx_rtgs_receiving_status", table_name="rtgs_settlements")
    op.drop_index("idx_rtgs_sending_status",   table_name="rtgs_settlements")
