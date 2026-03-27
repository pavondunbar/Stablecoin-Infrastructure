-- ═══════════════════════════════════════════════════════════════════════════
-- Stablecoin & Digital Cash Infrastructure — PostgreSQL Schema
-- Covers: tokenized deposits, RTGS, escrow/conditional payments, FX settlement
-- ═══════════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ─── Enums ────────────────────────────────────────────────────────────────

CREATE TYPE account_type      AS ENUM ('institutional', 'bank', 'correspondent', 'central_bank');
CREATE TYPE token_status      AS ENUM ('active', 'suspended', 'redeemed');
CREATE TYPE txn_status        AS ENUM ('pending', 'processing', 'completed', 'failed', 'reversed');
CREATE TYPE settlement_status AS ENUM ('queued', 'processing', 'settled', 'failed', 'cancelled');
CREATE TYPE escrow_status     AS ENUM ('pending', 'active', 'released', 'refunded', 'expired');
CREATE TYPE currency_code     AS ENUM ('USD', 'EUR', 'GBP', 'JPY', 'SGD', 'CHF', 'HKD');
CREATE TYPE settlement_rails  AS ENUM ('blockchain', 'swift', 'internal', 'fedwire', 'target2');
CREATE TYPE condition_type    AS ENUM ('time_lock', 'oracle_trigger', 'multi_sig', 'delivery_confirmation', 'kyc_verified');

-- ─── Accounts ─────────────────────────────────────────────────────────────

CREATE TABLE accounts (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_name             VARCHAR(255) NOT NULL,
    account_type            account_type NOT NULL,
    bic_code                CHAR(11),                   -- ISO 9362
    legal_entity_identifier VARCHAR(20) UNIQUE,         -- LEI
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,
    kyc_verified            BOOLEAN NOT NULL DEFAULT FALSE,
    aml_cleared             BOOLEAN NOT NULL DEFAULT FALSE,
    risk_tier               SMALLINT NOT NULL DEFAULT 3 CHECK (risk_tier BETWEEN 1 AND 5),
    metadata                JSONB NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ
);

CREATE INDEX idx_accounts_lei ON accounts(legal_entity_identifier) WHERE legal_entity_identifier IS NOT NULL;
CREATE INDEX idx_accounts_bic ON accounts(bic_code) WHERE bic_code IS NOT NULL;

-- ─── Token Balances (double-entry ledger per account+currency) ─────────────

CREATE TABLE token_balances (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    currency        currency_code NOT NULL,
    balance         NUMERIC(28, 8) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    reserved        NUMERIC(28, 8) NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    token_status    token_status NOT NULL DEFAULT 'active',
    version         BIGINT NOT NULL DEFAULT 0,           -- optimistic locking
    last_updated    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id, currency)
);

CREATE INDEX idx_token_balances_account ON token_balances(account_id);

-- ─── Audit Ledger (immutable double-entry) ────────────────────────────────

CREATE TABLE ledger_entries (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    txn_ref         VARCHAR(64) NOT NULL,
    entry_type      VARCHAR(20) NOT NULL CHECK (entry_type IN ('debit', 'credit')),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    currency        currency_code NOT NULL,
    amount          NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    balance_after   NUMERIC(28, 8) NOT NULL,
    narrative       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ledger_txn_ref   ON ledger_entries(txn_ref);
CREATE INDEX idx_ledger_account   ON ledger_entries(account_id, created_at DESC);

-- ─── Token Issuance / Redemption ──────────────────────────────────────────

CREATE TABLE token_issuances (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    issuance_ref        VARCHAR(64) UNIQUE NOT NULL,
    account_id          UUID NOT NULL REFERENCES accounts(id),
    currency            currency_code NOT NULL,
    amount              NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    backing_ref         VARCHAR(255),                   -- underlying fiat deposit ref
    custodian           VARCHAR(100),                   -- custodian bank
    issuance_type       VARCHAR(20) NOT NULL CHECK (issuance_type IN ('initial', 'additional', 'redemption')),
    status              txn_status NOT NULL DEFAULT 'pending',
    compliance_check_id UUID,
    issued_at           TIMESTAMPTZ,
    redeemed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_issuances_account ON token_issuances(account_id);
CREATE INDEX idx_issuances_status  ON token_issuances(status);

-- ─── Transactions ─────────────────────────────────────────────────────────

CREATE TABLE transactions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    txn_ref             VARCHAR(64) UNIQUE NOT NULL,
    debit_account_id    UUID NOT NULL REFERENCES accounts(id),
    credit_account_id   UUID NOT NULL REFERENCES accounts(id),
    currency            currency_code NOT NULL,
    amount              NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    txn_type            VARCHAR(50) NOT NULL,           -- issuance, redemption, transfer, settlement, escrow
    status              txn_status NOT NULL DEFAULT 'pending',
    idempotency_key     VARCHAR(128) UNIQUE,
    parent_txn_id       UUID REFERENCES transactions(id),
    metadata            JSONB NOT NULL DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at          TIMESTAMPTZ,
    CHECK (debit_account_id <> credit_account_id)
);

CREATE INDEX idx_txn_debit   ON transactions(debit_account_id, created_at DESC);
CREATE INDEX idx_txn_credit  ON transactions(credit_account_id, created_at DESC);
CREATE INDEX idx_txn_status  ON transactions(status);
CREATE INDEX idx_txn_idem    ON transactions(idempotency_key) WHERE idempotency_key IS NOT NULL;

-- ─── RTGS Settlements ─────────────────────────────────────────────────────

CREATE TABLE rtgs_settlements (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    settlement_ref      VARCHAR(64) UNIQUE NOT NULL,
    sending_account_id  UUID NOT NULL REFERENCES accounts(id),
    receiving_account_id UUID NOT NULL REFERENCES accounts(id),
    currency            currency_code NOT NULL,
    amount              NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    priority            VARCHAR(10) NOT NULL DEFAULT 'normal' CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
    status              settlement_status NOT NULL DEFAULT 'queued',
    transaction_id      UUID REFERENCES transactions(id),
    scheduled_at        TIMESTAMPTZ,
    queued_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processing_started  TIMESTAMPTZ,
    settled_at          TIMESTAMPTZ,
    failure_reason      TEXT,
    retry_count         SMALLINT NOT NULL DEFAULT 0,
    metadata            JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_rtgs_status    ON rtgs_settlements(status, priority, queued_at);
CREATE INDEX idx_rtgs_sending   ON rtgs_settlements(sending_account_id);
CREATE INDEX idx_rtgs_receiving ON rtgs_settlements(receiving_account_id);

-- ─── Escrow Contracts ─────────────────────────────────────────────────────

CREATE TABLE escrow_contracts (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    contract_ref            VARCHAR(64) UNIQUE NOT NULL,
    depositor_account_id    UUID NOT NULL REFERENCES accounts(id),
    beneficiary_account_id  UUID NOT NULL REFERENCES accounts(id),
    currency                currency_code NOT NULL,
    amount                  NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    conditions              JSONB NOT NULL,             -- release conditions
    status                  escrow_status NOT NULL DEFAULT 'pending',
    lock_txn_id             UUID REFERENCES transactions(id),
    release_txn_id          UUID REFERENCES transactions(id),
    arbiter_account_id      UUID REFERENCES accounts(id),
    expires_at              TIMESTAMPTZ NOT NULL,
    released_at             TIMESTAMPTZ,
    release_triggered_by    VARCHAR(100),
    metadata                JSONB NOT NULL DEFAULT '{}',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_escrow_status      ON escrow_contracts(status);
CREATE INDEX idx_escrow_depositor   ON escrow_contracts(depositor_account_id);
CREATE INDEX idx_escrow_beneficiary ON escrow_contracts(beneficiary_account_id);
CREATE INDEX idx_escrow_expires     ON escrow_contracts(expires_at) WHERE status = 'active';

-- ─── Conditional Payments ─────────────────────────────────────────────────

CREATE TABLE conditional_payments (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    payment_ref         VARCHAR(64) UNIQUE NOT NULL,
    payer_account_id    UUID NOT NULL REFERENCES accounts(id),
    payee_account_id    UUID NOT NULL REFERENCES accounts(id),
    currency            currency_code NOT NULL,
    amount              NUMERIC(28, 8) NOT NULL CHECK (amount > 0),
    condition_type      condition_type NOT NULL,
    condition_params    JSONB NOT NULL,
    status              txn_status NOT NULL DEFAULT 'pending',
    trigger_data        JSONB,
    transaction_id      UUID REFERENCES transactions(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at         TIMESTAMPTZ,
    expires_at          TIMESTAMPTZ
);

CREATE INDEX idx_condpay_status ON conditional_payments(status);
CREATE INDEX idx_condpay_payer  ON conditional_payments(payer_account_id);

-- ─── FX Rates ─────────────────────────────────────────────────────────────

CREATE TABLE fx_rates (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    base_currency   currency_code NOT NULL,
    quote_currency  currency_code NOT NULL,
    mid_rate        NUMERIC(18, 8) NOT NULL,
    bid_rate        NUMERIC(18, 8),
    ask_rate        NUMERIC(18, 8),
    spread_bps      NUMERIC(8, 4),
    source          VARCHAR(50) NOT NULL DEFAULT 'internal',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    valid_from      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until     TIMESTAMPTZ,
    UNIQUE (base_currency, quote_currency, valid_from)
);

CREATE INDEX idx_fx_pair_active ON fx_rates(base_currency, quote_currency, is_active);

-- ─── FX Settlements ───────────────────────────────────────────────────────

CREATE TABLE fx_settlements (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    settlement_ref          VARCHAR(64) UNIQUE NOT NULL,
    sending_account_id      UUID NOT NULL REFERENCES accounts(id),
    receiving_account_id    UUID NOT NULL REFERENCES accounts(id),
    sell_currency           currency_code NOT NULL,
    sell_amount             NUMERIC(28, 8) NOT NULL CHECK (sell_amount > 0),
    buy_currency            currency_code NOT NULL,
    buy_amount              NUMERIC(28, 8) NOT NULL CHECK (buy_amount > 0),
    applied_rate            NUMERIC(18, 8) NOT NULL,
    fx_rate_id              UUID REFERENCES fx_rates(id),
    rails                   settlement_rails NOT NULL DEFAULT 'blockchain',
    status                  settlement_status NOT NULL DEFAULT 'queued',
    sell_txn_id             UUID REFERENCES transactions(id),
    buy_txn_id              UUID REFERENCES transactions(id),
    blockchain_tx_hash      VARCHAR(255),
    value_date              DATE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    settled_at              TIMESTAMPTZ,
    metadata                JSONB NOT NULL DEFAULT '{}',
    CHECK (sell_currency <> buy_currency)
);

CREATE INDEX idx_fx_settle_status ON fx_settlements(status);
CREATE INDEX idx_fx_settle_sender ON fx_settlements(sending_account_id);

-- ─── Compliance Events ────────────────────────────────────────────────────

CREATE TABLE compliance_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entity_type     VARCHAR(50) NOT NULL,   -- transaction, account, settlement
    entity_id       UUID NOT NULL,
    event_type      VARCHAR(50) NOT NULL,   -- aml_check, sanctions_screen, kyc_verify
    result          VARCHAR(20) NOT NULL,   -- pass, fail, review
    score           NUMERIC(5, 2),
    details         JSONB NOT NULL DEFAULT '{}',
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_compliance_entity ON compliance_events(entity_type, entity_id);

-- ─── Seed: System Accounts ────────────────────────────────────────────────

INSERT INTO accounts (id, entity_name, account_type, kyc_verified, aml_cleared, risk_tier)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'OMNIBUS_RESERVE', 'central_bank', TRUE, TRUE, 1),
    ('00000000-0000-0000-0000-000000000002', 'FX_NOSTRO_USD',   'bank',         TRUE, TRUE, 1),
    ('00000000-0000-0000-0000-000000000003', 'FX_NOSTRO_EUR',   'bank',         TRUE, TRUE, 1),
    ('00000000-0000-0000-0000-000000000004', 'FX_NOSTRO_GBP',   'bank',         TRUE, TRUE, 1);

-- Bootstrap balances for system accounts
INSERT INTO token_balances (account_id, currency, balance)
VALUES
    ('00000000-0000-0000-0000-000000000001', 'USD', 1000000000.00000000),
    ('00000000-0000-0000-0000-000000000001', 'EUR', 1000000000.00000000),
    ('00000000-0000-0000-0000-000000000001', 'GBP', 1000000000.00000000),
    ('00000000-0000-0000-0000-000000000002', 'USD', 500000000.00000000),
    ('00000000-0000-0000-0000-000000000003', 'EUR', 500000000.00000000),
    ('00000000-0000-0000-0000-000000000004', 'GBP', 500000000.00000000);

-- Bootstrap FX rates
INSERT INTO fx_rates (base_currency, quote_currency, mid_rate, bid_rate, ask_rate, spread_bps, source)
VALUES
    ('USD', 'EUR', 0.91800000, 0.91750000, 0.91850000, 10.9, 'internal'),
    ('USD', 'GBP', 0.79200000, 0.79150000, 0.79250000, 12.6, 'internal'),
    ('EUR', 'USD', 1.08930000, 1.08880000, 1.08980000, 9.2,  'internal'),
    ('EUR', 'GBP', 0.86300000, 0.86250000, 0.86350000, 11.6, 'internal'),
    ('GBP', 'USD', 1.26260000, 1.26210000, 1.26310000, 7.9,  'internal'),
    ('GBP', 'EUR', 1.15870000, 1.15820000, 1.15920000, 8.6,  'internal');
