"""Initial schema — tokenized deposit platform baseline

Revision ID: 0001_initial
Revises:
Create Date: 2024-11-01 00:00:00.000000 UTC

This migration creates the full initial schema.  In production, if the
schema was bootstrapped directly from init/postgres/01_schema.sql, stamp
this migration as already applied:

    alembic stamp 0001_initial
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# ── Revision identifiers ─────────────────────────────────────────────────────

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extensions ────────────────────────────────────────────────────────────
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')

    # ── Enums ─────────────────────────────────────────────────────────────────
    account_type_enum = postgresql.ENUM(
        "institutional", "bank", "correspondent", "central_bank",
        name="account_type",
    )
    account_type_enum.create(op.get_bind())

    token_status_enum = postgresql.ENUM(
        "active", "suspended", "redeemed",
        name="token_status",
    )
    token_status_enum.create(op.get_bind())

    txn_status_enum = postgresql.ENUM(
        "pending", "processing", "completed", "failed", "reversed",
        name="txn_status",
    )
    txn_status_enum.create(op.get_bind())

    settlement_status_enum = postgresql.ENUM(
        "queued", "processing", "settled", "failed", "cancelled",
        name="settlement_status",
    )
    settlement_status_enum.create(op.get_bind())

    escrow_status_enum = postgresql.ENUM(
        "pending", "active", "released", "refunded", "expired",
        name="escrow_status",
    )
    escrow_status_enum.create(op.get_bind())

    currency_code_enum = postgresql.ENUM(
        "USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
        name="currency_code",
    )
    currency_code_enum.create(op.get_bind())

    settlement_rails_enum = postgresql.ENUM(
        "blockchain", "swift", "internal", "fedwire", "target2",
        name="settlement_rails",
    )
    settlement_rails_enum.create(op.get_bind())

    condition_type_enum = postgresql.ENUM(
        "time_lock", "oracle_trigger", "multi_sig",
        "delivery_confirmation", "kyc_verified",
        name="condition_type",
    )
    condition_type_enum.create(op.get_bind())

    # ── Tables ────────────────────────────────────────────────────────────────
    op.create_table(
        "accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("entity_name", sa.String(255), nullable=False),
        sa.Column("account_type", sa.Enum("institutional", "bank", "correspondent",
                  "central_bank", name="account_type"), nullable=False),
        sa.Column("bic_code", sa.String(11)),
        sa.Column("legal_entity_identifier", sa.String(20), unique=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("kyc_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("aml_cleared", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("risk_tier", sa.SmallInteger, nullable=False, server_default="3"),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "token_balances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("balance", sa.Numeric(28, 8), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Numeric(28, 8), nullable=False, server_default="0"),
        sa.Column("token_status", sa.Enum("active", "suspended", "redeemed",
                  name="token_status"), nullable=False, server_default="active"),
        sa.Column("version", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("last_updated", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("account_id", "currency"),
        sa.CheckConstraint("balance >= 0"),
        sa.CheckConstraint("reserved >= 0"),
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("txn_ref", sa.String(64), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("balance_after", sa.Numeric(28, 8), nullable=False),
        sa.Column("narrative", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("txn_ref", sa.String(64), unique=True, nullable=False),
        sa.Column("debit_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("credit_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("txn_type", sa.String(50), nullable=False),
        sa.Column("status", sa.Enum("pending", "processing", "completed", "failed", "reversed",
                  name="txn_status"), nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column("parent_txn_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "token_issuances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("issuance_ref", sa.String(64), unique=True, nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("backing_ref", sa.String(255)),
        sa.Column("custodian", sa.String(100)),
        sa.Column("issuance_type", sa.String(20), nullable=False),
        sa.Column("status", sa.Enum("pending", "processing", "completed", "failed", "reversed",
                  name="txn_status"), nullable=False, server_default="pending"),
        sa.Column("compliance_check_id", postgresql.UUID(as_uuid=True)),
        sa.Column("issued_at", sa.DateTime(timezone=True)),
        sa.Column("redeemed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "rtgs_settlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("settlement_ref", sa.String(64), unique=True, nullable=False),
        sa.Column("sending_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("receiving_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False, server_default="normal"),
        sa.Column("status", sa.Enum("queued", "processing", "settled", "failed", "cancelled",
                  name="settlement_status"), nullable=False, server_default="queued"),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("scheduled_at", sa.DateTime(timezone=True)),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("processing_started", sa.DateTime(timezone=True)),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.Text),
        sa.Column("retry_count", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
    )

    op.create_table(
        "escrow_contracts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("contract_ref", sa.String(64), unique=True, nullable=False),
        sa.Column("depositor_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("beneficiary_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("conditions", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Enum("pending", "active", "released", "refunded", "expired",
                  name="escrow_status"), nullable=False, server_default="pending"),
        sa.Column("lock_txn_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("release_txn_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("arbiter_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("release_triggered_by", sa.String(100)),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "conditional_payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("payment_ref", sa.String(64), unique=True, nullable=False),
        sa.Column("payer_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("payee_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("condition_type", sa.Enum(
                  "time_lock", "oracle_trigger", "multi_sig",
                  "delivery_confirmation", "kyc_verified",
                  name="condition_type"), nullable=False),
        sa.Column("condition_params", postgresql.JSONB, nullable=False),
        sa.Column("status", sa.Enum("pending", "processing", "completed", "failed", "reversed",
                  name="txn_status"), nullable=False, server_default="pending"),
        sa.Column("trigger_data", postgresql.JSONB),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "fx_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("base_currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("quote_currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("mid_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("bid_rate", sa.Numeric(18, 8)),
        sa.Column("ask_rate", sa.Numeric(18, 8)),
        sa.Column("spread_bps", sa.Numeric(8, 4)),
        sa.Column("source", sa.String(50), nullable=False, server_default="internal"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("valid_from", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("valid_until", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "fx_settlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("settlement_ref", sa.String(64), unique=True, nullable=False),
        sa.Column("sending_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("receiving_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("sell_currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("sell_amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("buy_currency", sa.Enum("USD", "EUR", "GBP", "JPY", "SGD", "CHF", "HKD",
                  name="currency_code"), nullable=False),
        sa.Column("buy_amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("applied_rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("fx_rate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("fx_rates.id")),
        sa.Column("rails", sa.Enum("blockchain", "swift", "internal", "fedwire", "target2",
                  name="settlement_rails"), nullable=False, server_default="blockchain"),
        sa.Column("status", sa.Enum("queued", "processing", "settled", "failed", "cancelled",
                  name="settlement_status"), nullable=False, server_default="queued"),
        sa.Column("sell_txn_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("buy_txn_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("transactions.id")),
        sa.Column("blockchain_tx_hash", sa.String(255)),
        sa.Column("value_date", sa.Date),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default="{}"),
    )

    op.create_table(
        "compliance_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("uuid_generate_v4()")),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("result", sa.String(20), nullable=False),
        sa.Column("score", sa.Numeric(5, 2)),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.create_index("idx_accounts_lei", "accounts", ["legal_entity_identifier"],
                    postgresql_where=sa.text("legal_entity_identifier IS NOT NULL"))
    op.create_index("idx_token_balances_account", "token_balances", ["account_id"])
    op.create_index("idx_ledger_txn_ref", "ledger_entries", ["txn_ref"])
    op.create_index("idx_ledger_account",  "ledger_entries", ["account_id", "created_at"])
    op.create_index("idx_txn_debit",   "transactions", ["debit_account_id",  "created_at"])
    op.create_index("idx_txn_credit",  "transactions", ["credit_account_id", "created_at"])
    op.create_index("idx_txn_status",  "transactions", ["status"])
    op.create_index("idx_rtgs_status", "rtgs_settlements", ["status", "priority", "queued_at"])
    op.create_index("idx_escrow_status", "escrow_contracts", ["status"])
    op.create_index("idx_escrow_expires", "escrow_contracts", ["expires_at"],
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index("idx_condpay_status", "conditional_payments", ["status"])
    op.create_index("idx_fx_pair_active", "fx_rates",
                    ["base_currency", "quote_currency", "is_active"])
    op.create_index("idx_fx_settle_status", "fx_settlements", ["status"])
    op.create_index("idx_compliance_entity", "compliance_events",
                    ["entity_type", "entity_id"])


def downgrade() -> None:
    # Drop tables in reverse dependency order
    tables = [
        "compliance_events", "fx_settlements", "fx_rates",
        "conditional_payments", "escrow_contracts", "rtgs_settlements",
        "token_issuances", "transactions", "ledger_entries",
        "token_balances", "accounts",
    ]
    for t in tables:
        op.drop_table(t)

    # Drop enums
    enums = [
        "condition_type", "settlement_rails", "currency_code",
        "escrow_status", "settlement_status", "txn_status",
        "token_status", "account_type",
    ]
    for e in enums:
        op.execute(f"DROP TYPE IF EXISTS {e}")
