# Stablecoin Infrastructure

<img width="1536" height="1024" alt="4a5ef1cf-30c8-4905-bd6a-1898bb03a573" src="https://github.com/user-attachments/assets/277554ed-4623-4159-b6f3-daa0abfaf115" />

> ⚠️ **SANDBOX / EDUCATIONAL USE ONLY — NOT FOR PRODUCTION**
> This codebase is a reference implementation designed for learning, prototyping, and architectural exploration. It is **not audited, not legally reviewed, and must not be used to issue real tokenized deposits, manage real funds, or interface with real payment rails (SWIFT, FedWire, TARGET2).** See the [Production Warning](#production-warning) section for full details.

Microservices platform for tokenized deposit issuance, real-time gross settlement (RTGS), programmable payments, and cross-border FX settlement. Modeled on institutional systems like JPMorgan Kinexys (JPM Coin), PayPal PYUSD, and the Regulated Liability Network (RLN).

The system implements a blockchain-grade double-entry ledger with immutability enforcement, transactional outbox event publishing, MPC-based transaction signing, role-based access control with separation of duties, and a trust-boundary network model where only the API gateway is internet-facing.

---

## Table of Contents

- [Architecture](#architecture)
- [Services](#services)
- [Data Model](#data-model)
- [Kafka Topics](#kafka-topics)
- [API Reference](#api-reference)
- [Getting Started](#getting-started)
- [Monitoring](#monitoring)
- [Scripts and Utilities](#scripts-and-utilities)
- [Testing](#testing)
- [Technical Design](#technical-design)
- [Production Warning](#production-warning)
- [License](#license)

---

## Architecture

```
                           Internet
                              |
                    +---------+---------+
                    |   API Gateway     |  :8000 (only exposed port)
                    |  RBAC + rate-limit|
                    +----+-+-+-+--------+
                         | | | |
              DMZ network| | | |internal network
         +---------------+ | | +---------------+
         |          +------+ +------+           |
         v          v               v           v
   +-----------+ +------+ +----------+ +----------+
   |  Token    | | RTGS | | Payment  | |    FX    |
   | Issuance  | |      | |  Engine  | |Settlement|
   |   :8001   | | :8002| |  :8003   | |  :8004   |
   +-----------+ +------+ +----------+ +----+-----+
         |          |           |             |
         +-----+----+-----+----+      signing network
               |          |                  |
         +-----+---+ +----+----+      +------+-------+
         |Postgres | | Kafka   |      |   Signing    |
         |  :5432  | |  :9092  |      |   Gateway    |
         +---------+ +----+----+      |    :8006     |
                          |           +--+---+---+---+
              +-----------+---+          |   |   |
              |               |       +--+  ++-  +-+
        +-----+------+  +----+----+   |MPC||MPC||MPC|
        |  Compliance|  |  Outbox |   | 1 || 2 || 3 |
        |   Monitor  |  |Publisher|   +---++---++---+
        |    :8005   |  |  :8010  |
        +------------+  +---------+
```

### Network Isolation

| Network | Services | Internet Access |
|---------|----------|-----------------|
| **dmz** | api-gateway, prometheus, grafana | Yes (host-reachable) |
| **internal** | All microservices, postgres, kafka, zookeeper, outbox-publisher | No (`internal: true`) |
| **signing** | signing-gateway, mpc-node-1, mpc-node-2, mpc-node-3 | No (`internal: true`) |

The API gateway bridges dmz and internal. The FX settlement service bridges internal and signing. All other backend services are unreachable from outside the Docker network. MPC nodes communicate only with the signing gateway.

### Infrastructure

| Component | Image | Purpose |
|-----------|-------|---------|
| PostgreSQL | `postgres:16-alpine` | Persistent storage, immutable double-entry ledger |
| Kafka | `confluentinc/cp-kafka:7.6.0` | Event streaming between services |
| Zookeeper | `confluentinc/cp-zookeeper:7.6.0` | Kafka coordination |
| Prometheus | `prom/prometheus:v2.54.1` | Metrics collection (15s scrape interval) |
| Grafana | `grafana/grafana:11.2.0` | Dashboards and visualization |

---

## Services

### API Gateway (port 8000)

The sole internet-facing service. Authenticates requests via `X-API-Key` header against SHA-256 hashed keys in the database, resolves the caller's role (admin, approver, signer, trader, auditor), enforces rate limits (1,000 requests per 60-second sliding window per API key), and reverse-proxies to internal services. Every request is logged to the `audit.trail` Kafka topic with `X-Request-ID`, actor identity, IP address, and elapsed time.

**Routes:**

| Path Prefix | Upstream | Port |
|-------------|----------|------|
| `/v1/tokens/*` | token-issuance | 8001 |
| `/v1/accounts/*` | token-issuance | 8001 |
| `/v1/settlements/*` | rtgs | 8002 |
| `/v1/payments/*` | payment-engine | 8003 |
| `/v1/fx/*` | fx-settlement | 8004 |

`GET /health` aggregates health from all upstream services (including signing-gateway), returning `200` if all are healthy or `207` if any are degraded.

### Token Issuance (port 8001)

Issues and redeems tokenized deposits against an omnibus reserve account. Every operation produces matching debit and credit journal entries (double-entry bookkeeping). Tokens are minted by debiting the omnibus reserve and crediting the recipient; redemptions reverse this. Events are published via the transactional outbox pattern.

- Validates KYC/AML status before issuance
- Supports idempotency keys to prevent duplicate processing
- 8-decimal-place precision with banker's rounding (`ROUND_HALF_EVEN`)
- Uses advisory database locks on omnibus account during issuance
- Publishes `token.issuance.completed`, `token.redemption.completed`, and `token.balance.updated` events via outbox

**System accounts seeded at startup:**

| UUID | Name | Balance |
|------|------|---------|
| `...0001` | OMNIBUS_RESERVE | 1B USD, 1B EUR, 1B GBP |
| `...0002` | FX_NOSTRO_USD | 500M USD |
| `...0003` | FX_NOSTRO_EUR | 500M EUR |
| `...0004` | FX_NOSTRO_GBP | 500M GBP |

### RTGS (port 8002)

Real-time gross settlement with priority-based queue processing and a multi-step approval workflow enforcing separation of duties.

- Settlement instructions are queued and processed by a background worker in priority order (urgent > high > normal > low), then FIFO within each priority level
- `SELECT FOR UPDATE` with `SKIP LOCKED` for concurrent worker safety
- Atomic balance transfers with advisory locks and row-level locking
- Retries up to 3 times for transient failures (not insufficient balance)
- Publishes lifecycle events via outbox: `submitted` -> `processing` -> `completed` or `failed`

**Approval workflow:**

| Step | Endpoint | Required Role | Constraint |
|------|----------|---------------|------------|
| Submit | `POST /settlements/submit` | admin, trader | Creates settlement in `pending` state |
| Approve | `POST /settlements/{ref}/approve` | admin, approver | Transitions to `approved` |
| Sign | `POST /settlements/{ref}/sign` | admin, signer | Transitions to `signed`; signer must differ from approver |

State machine: `pending -> approved -> signed -> processing -> broadcasted -> confirmed -> settled`

### Payment Engine (port 8003)

Programmable payments with two mechanisms: conditional payments and escrow contracts. All operations use idempotency keys and publish events via the transactional outbox.

**Conditional payments** execute when a condition is satisfied:

| Condition Type | Trigger |
|----------------|---------|
| `time_lock` | Releases after a specified timestamp (auto-evaluated) |
| `oracle_trigger` | External oracle provides matching key/value |
| `multi_sig` | Required threshold of signers met |
| `delivery_confirmation` | Delivery reference confirmed by external system |
| `kyc_verified` | Payee's KYC/AML status verified (auto-evaluated) |

A background thread polls for `time_lock` and `kyc_verified` conditions every 5 seconds. Other condition types require an explicit trigger via the `/trigger` endpoint.

**Escrow contracts** lock funds in a reserved bucket on the depositor's balance. They can be released to the beneficiary, refunded to the depositor, or auto-expired when `expires_at` passes. Fund reservations are tracked in an immutable `escrow_holds` table.

### FX Settlement (port 8004)

Cross-border payment-vs-payment (PvP) atomic swaps with MPC-signed blockchain transactions. Both legs (sell and buy) execute within a single database transaction -- if either fails, both roll back (eliminating Herstatt risk).

- Maintains a live FX rate book (fed by `fx.rate.updated` Kafka topic via background consumer)
- Generates quotes with configurable bid/ask spreads (basis points)
- Supports 5 settlement rails: `blockchain`, `swift`, `internal`, `fedwire`, `target2`
- Routes through nostro accounts per currency (USD/EUR/GBP)
- Calls the signing gateway for MPC-signed transaction hashes on blockchain rails
- Publishes events via outbox: `initiated`, `leg.completed` (sell and buy), `completed` or `failed`

**Seeded FX rates:** USD/EUR, USD/GBP, EUR/USD, EUR/GBP, GBP/USD, GBP/EUR

### Compliance Monitor (port 8005)

Consumes settlement, issuance, and payment events from Kafka and runs rule-based screening. Operates as fire-and-forget -- it never blocks the originating transaction. Deduplicates events via the `processed_events` table.

**Kafka topics consumed:**

`token.issuance.completed`, `token.redemption.completed`, `rtgs.settlement.completed`, `payment.conditional.completed`, `escrow.released`, `fx.settlement.completed`

**AML rules:**

| Rule | Threshold |
|------|-----------|
| Large transaction (CTR equivalent) | >= $1,000,000 USD equivalent |
| Structuring detection | $9,500 - $999,999 USD equivalent |
| Velocity limit | > 20 transactions per account per hour |
| Sanctions screening | Pattern match against SDN name fragments |

Risk scores range from 0 to 100 (cumulative, 25 points per flag). Results are persisted to `compliance_events` and published to the `compliance.event` Kafka topic (30-day retention) via outbox.

### Signing Gateway (port 8006)

Coordinates MPC signing requests across three independent MPC nodes in an isolated network. Fans out the signing payload to all nodes, collects partial signatures, and combines them when the 2-of-3 threshold (`(N // 2) + 1`) is met. Returns HTTP 503 if the threshold is not met.

### MPC Nodes (3 instances)

Stateless signing nodes that produce deterministic partial signatures (`SHA256(NODE_ID + sorted JSON)`). Each node runs independently in the isolated signing network with no database access and no internet access.

### Outbox Publisher (port 8010)

Polls the `outbox_events` database table every 100ms for unpublished events and forwards them to Kafka. Uses `FOR UPDATE SKIP LOCKED` for safe horizontal scaling. Publishes with `acks=all` and marks events as published within the same transaction. Processes batches of 100 events per cycle.

---

## Data Model

21 tables with PostgreSQL enums, check constraints, foreign keys, and immutability triggers.

### Core Tables

```
accounts                     chart_of_accounts
  +- id (UUID PK)             +- code (PK: OMNIBUS_RESERVE,
  +- entity_name                       INSTITUTION_LIABILITY,
  +- account_type (4 enums)            FX_NOSTRO_USD/EUR/GBP,
  +- bic_code (ISO 9362)               ESCROW_HOLDING,
  +- lei (unique)                       SETTLEMENT_PENDING,
  +- kyc_verified                       FEE_REVENUE)
  +- aml_cleared              +- normal_balance (debit|credit)
  +- risk_tier (1-5)          +- description

api_keys
  +- key_hash (SHA-256, unique)
  +- role (admin|approver|signer|trader|auditor)
  +- name
  +- is_active
```

### Ledger Tables (Immutable)

```
journal_entries (IMMUTABLE - triggers reject UPDATE/DELETE)
  +- id (UUID PK)
  +- journal_id (links debit+credit pair)
  +- account_id FK -> accounts
  +- coa_code FK -> chart_of_accounts
  +- currency
  +- debit NUMERIC(38,18)
  +- credit NUMERIC(38,18)
  +- entry_type (debit|credit)
  +- reference_id (FK to originating entity)
  +- narrative
  +- created_at

ledger_entries (IMMUTABLE - triggers reject UPDATE/DELETE)
  +- txn_ref (shared by debit+credit pair)
  +- entry_type (debit|credit)
  +- account_id FK
  +- amount NUMERIC(28,8)
  +- balance_after

escrow_holds (IMMUTABLE - triggers reject UPDATE/DELETE)
  +- account_id, currency
  +- amount, hold_type (reserve|release)
  +- reference_id
```

### Business Tables

```
token_balances                transactions
  +- account_id FK              +- txn_ref (unique)
  +- currency (7 enums)         +- debit_account_id FK
  +- balance NUMERIC(28,8)      +- credit_account_id FK
  +- reserved NUMERIC(28,8)     +- txn_type (6 types)
  +- version (optimistic lock)  +- idempotency_key (unique)
  +- UNIQUE(account_id, ccy)    +- CHECK(debit != credit)

token_issuances              rtgs_settlements
  +- issuance_ref (unique)     +- settlement_ref (unique)
  +- backing_ref (fiat dep.)   +- priority (urgent|high|normal|low)
  +- custodian                 +- approved_by, approved_at
  +- issuance_type             +- signed_by, signed_at
  +- request_id                +- retry_count
  +- status                    +- INDEX(status, priority, queued_at)

escrow_contracts             conditional_payments
  +- contract_ref (unique)     +- payment_ref (unique)
  +- depositor / beneficiary   +- condition_type (5 enums)
  +- arbiter_account_id        +- condition_params (JSONB)
  +- conditions (JSONB)        +- trigger_data (JSONB)
  +- expires_at                +- expires_at

fx_rates                     fx_settlements
  +- base/quote currency       +- settlement_ref (unique)
  +- mid/bid/ask rate          +- sell/buy currency + amount
  +- spread_bps                +- applied_rate
  +- is_active                 +- rails (5 enums)
                               +- sell_txn_id / buy_txn_id FK
compliance_events            +- blockchain_tx_hash
  +- entity_type + entity_id
  +- result (pass|fail|review)
  +- score NUMERIC(5,2)
```

### Event Infrastructure Tables

```
outbox_events (IMMUTABLE payload - only published_at is updatable)
  +- id (UUID PK)
  +- aggregate_id
  +- event_type (Kafka topic)
  +- payload (JSONB)
  +- published_at (NULL until outbox-publisher processes)
  +- created_at

processed_events (Kafka consumer idempotency)
  +- event_id (PK - prevents duplicate processing)
  +- topic
  +- processed_at
```

### Status History Tables (All Immutable)

Six append-only status history tables track every state transition with `request_id`, `actor_id`, `actor_service`, and timestamp. Database triggers on INSERT auto-sync the current status back to the parent entity.

| History Table | Parent Entity |
|---------------|---------------|
| `transaction_status_history` | `transactions` |
| `rtgs_settlement_status_history` | `rtgs_settlements` |
| `fx_settlement_status_history` | `fx_settlements` |
| `escrow_status_history` | `escrow_contracts` |
| `conditional_payment_status_history` | `conditional_payments` |
| `token_issuance_status_history` | `token_issuances` |

### Database Views

| View | Purpose |
|------|---------|
| `account_balances` | SUM(credit) - SUM(debit) for INSTITUTION_LIABILITY journal entries |
| `account_active_holds` | SUM(amount) from escrow_holds per account |
| `account_available_balances` | Balance minus active holds |

**Supported currencies:** USD, EUR, GBP, JPY, SGD, CHF, HKD

**Settlement rails:** blockchain, swift, internal, fedwire, target2

---

## Kafka Topics

25 topics provisioned at startup with LZ4 compression.

| Topic | Partitions | Retention | Purpose |
|-------|:----------:|:---------:|---------|
| `token.issuance.requested` | 4 | 7d | Issuance request queued |
| `token.issuance.completed` | 4 | 7d | Tokens minted |
| `token.redemption.requested` | 4 | 7d | Redemption request queued |
| `token.redemption.completed` | 4 | 7d | Tokens burned |
| `token.balance.updated` | 8 | 7d | Balance change (high volume) |
| `rtgs.settlement.submitted` | 4 | 7d | Settlement instruction queued |
| `rtgs.settlement.processing` | 4 | 7d | Settlement processing started |
| `rtgs.settlement.completed` | 4 | 7d | Settlement finalized |
| `rtgs.settlement.failed` | 2 | 1d | Settlement failure |
| `rtgs.queue.urgent` | 2 | 7d | Urgent priority lane |
| `payment.conditional.created` | 4 | 7d | Conditional payment created |
| `payment.conditional.triggered` | 4 | 7d | Condition trigger received |
| `payment.conditional.completed` | 4 | 7d | Payment executed |
| `escrow.created` | 4 | 7d | Escrow contract established |
| `escrow.released` | 4 | 7d | Escrow released or refunded |
| `escrow.expired` | 2 | 7d | Escrow expired |
| `fx.rate.updated` | 2 | 7d | Live FX rate update |
| `fx.settlement.initiated` | 4 | 7d | FX settlement queued |
| `fx.settlement.leg.completed` | 4 | 7d | One PvP leg settled |
| `fx.settlement.completed` | 4 | 7d | Both PvP legs settled |
| `fx.settlement.failed` | 2 | 1d | FX settlement failure |
| `compliance.event` | 4 | 30d | AML/sanctions screening results |
| `audit.trail` | 8 | 30d | Immutable request audit log |
| `dlq.default` | 2 | 30d | Dead letter queue for failed messages |
| `signing.request` | 2 | 7d | MPC signing request |
| `signing.completed` | 2 | 7d | MPC signing result |

---

## API Reference

All requests go through the API gateway at `http://localhost:8000`. Include the API key as `X-API-Key` header.

**Seeded API keys (demo only):**

| Key | Role | Purpose |
|-----|------|---------|
| Value from `GATEWAY_API_KEY` env var | admin | Full access |
| `approver-key-demo-001` | approver | Approve settlements |
| `signer-key-demo-001` | signer | Sign settlements |

### Token Issuance

```bash
# Create account
curl -X POST http://localhost:8000/v1/accounts \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "entity_name": "Acme Bank",
    "account_type": "institutional",
    "bic_code": "ACMEUS33XXX",
    "legal_entity_identifier": "529900T8BM49AURSDO55",
    "risk_tier": 2
  }'

# Issue tokens
curl -X POST http://localhost:8000/v1/tokens/issue \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "<account-uuid>",
    "currency": "USD",
    "amount": "1000000.00",
    "backing_ref": "FED-WIRE-REF-12345",
    "idempotency_key": "ISS-20240101-001"
  }'

# Redeem tokens
curl -X POST http://localhost:8000/v1/tokens/redeem \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "account_id": "<account-uuid>",
    "currency": "USD",
    "amount": "500000.00",
    "idempotency_key": "RED-20240101-001"
  }'

# Query balances
curl http://localhost:8000/v1/tokens/balance/<account-uuid> \
  -H "X-API-Key: $API_KEY"
```

### RTGS Settlement

```bash
# Submit settlement
curl -X POST http://localhost:8000/v1/settlements/submit \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sending_account_id": "<sender-uuid>",
    "receiving_account_id": "<receiver-uuid>",
    "currency": "USD",
    "amount": "250000.00",
    "priority": "urgent",
    "idempotency_key": "RTGS-20240101-001"
  }'

# Approve settlement (requires approver role)
curl -X POST http://localhost:8000/v1/settlements/<settlement-ref>/approve \
  -H "X-API-Key: approver-key-demo-001" \
  -H "Content-Type: application/json"

# Sign settlement (requires signer role, must differ from approver)
curl -X POST http://localhost:8000/v1/settlements/<settlement-ref>/sign \
  -H "X-API-Key: signer-key-demo-001" \
  -H "Content-Type: application/json"

# Query settlement status
curl http://localhost:8000/v1/settlements/<settlement-ref> \
  -H "X-API-Key: $API_KEY"

# List settlements (filterable by status)
curl http://localhost:8000/v1/settlements?status=settled \
  -H "X-API-Key: $API_KEY"
```

### Programmable Payments

```bash
# Create conditional payment (time lock)
curl -X POST http://localhost:8000/v1/payments/conditional \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "payer_account_id": "<payer-uuid>",
    "payee_account_id": "<payee-uuid>",
    "currency": "USD",
    "amount": "100000.00",
    "condition_type": "time_lock",
    "condition_params": {"release_at": "2025-06-01T00:00:00Z"}
  }'

# Trigger condition manually (for oracle/multi_sig/delivery types)
curl -X POST http://localhost:8000/v1/payments/conditional/<payment-ref>/trigger \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "trigger_data": {"oracle_key": "price_feed", "oracle_value": "above_threshold"},
    "triggered_by": "external-oracle"
  }'

# Create escrow
curl -X POST http://localhost:8000/v1/payments/escrow \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "depositor_account_id": "<depositor-uuid>",
    "beneficiary_account_id": "<beneficiary-uuid>",
    "currency": "EUR",
    "amount": "500000.00",
    "conditions": {"delivery_required": true},
    "expires_at": "2025-12-31T23:59:59Z"
  }'

# Release escrow to beneficiary
curl -X POST http://localhost:8000/v1/payments/escrow/<contract-ref>/release \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"release_to": "beneficiary"}'
```

### FX Settlement

```bash
# Get live rates
curl http://localhost:8000/v1/fx/rates \
  -H "X-API-Key: $API_KEY"

# Get quote
curl -X POST http://localhost:8000/v1/fx/quote \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sell_currency": "USD",
    "buy_currency": "EUR",
    "sell_amount": "1000000.00"
  }'

# Execute PvP settlement (with MPC signing on blockchain rails)
curl -X POST http://localhost:8000/v1/fx/settle \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sending_account_id": "<sender-uuid>",
    "receiving_account_id": "<receiver-uuid>",
    "sell_currency": "USD",
    "sell_amount": "1000000.00",
    "buy_currency": "EUR",
    "rails": "blockchain"
  }'
```

### Health

```bash
# Gateway health (aggregates all services including signing-gateway)
curl http://localhost:8000/health
```

---

## Getting Started

### Prerequisites

- Docker and Docker Compose
- 4 GB RAM minimum (Kafka + PostgreSQL + 9 services + 3 MPC nodes)

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set real values:

```bash
POSTGRES_PASSWORD=<strong-password>
GATEWAY_API_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
GRAFANA_PASSWORD=<grafana-password>
```

### 2. Start the platform

```bash
docker compose up -d --build
```

This starts 16 containers:

1. **postgres** -- database with schema, migrations, and seed data
2. **zookeeper** -- Kafka coordination
3. **kafka** -- event broker
4. **kafka-init** -- creates 25 topics, then exits
5. **token-issuance** -- tokenized deposit service
6. **rtgs** -- settlement service with approval workflow
7. **payment-engine** -- programmable payment service
8. **fx-settlement** -- FX PvP service with MPC signing
9. **compliance-monitor** -- AML/sanctions screening
10. **outbox-publisher** -- transactional outbox to Kafka relay
11. **signing-gateway** -- MPC signing coordinator
12. **mpc-node-1** -- MPC signing node
13. **mpc-node-2** -- MPC signing node
14. **mpc-node-3** -- MPC signing node
15. **api-gateway** -- reverse proxy (waits for all services to be healthy)
16. **prometheus** + **grafana** -- observability

### 3. Verify

```bash
# Check all containers are running
docker compose ps

# Test gateway health
curl http://localhost:8000/health

# Expected response when all services are healthy:
# {"gateway": "ok", "services": {"token": "ok", "rtgs": "ok", "payment": "ok", "fx": "ok", "signing": "ok"}}
```

### 4. Run the demo

```bash
pip install httpx
python scripts/demo.py
```

The demo exercises the full platform: account onboarding, token issuance/redemption, RTGS settlement with approval/signing workflow, conditional payments (time lock, oracle, multi-sig), escrow contracts, and FX PvP settlement with MPC-signed blockchain transactions.

### Teardown

```bash
docker compose down -v   # -v removes volumes (database, kafka data)
```

---

## Monitoring

| Service | URL | Credentials |
|---------|-----|-------------|
| Prometheus | http://localhost:9090 | None |
| Grafana | http://localhost:3000 | admin / `$GRAFANA_PASSWORD` |

Each microservice exposes a `/metrics` endpoint scraped by Prometheus every 15 seconds.

**Metrics collected:**

| Metric | Type | Labels |
|--------|------|--------|
| `http_requests_total` | Counter | service, method, path, status_code |
| `http_request_duration_seconds` | Histogram | service, method, path |
| `http_requests_in_flight` | Gauge | service |
| `tokens_issued_total` | Counter | currency, direction |
| `token_amount_issued_total` | Counter | currency |
| `settlements_processed_total` | Counter | status, priority |
| `settlement_amount_total` | Counter | currency |
| `settlement_queue_depth` | Gauge | - |
| `conditional_payments_total` | Counter | condition_type, event |
| `escrow_contracts_total` | Counter | event |
| `fx_settlements_total` | Counter | rails, status |
| `fx_volume_total` | Counter | currency |
| `compliance_events_total` | Counter | result, event_type |
| `kafka_publishes_total` | Counter | topic, status |
| `kafka_publish_latency_seconds` | Histogram | topic |

---

## Scripts and Utilities

| Script | Purpose |
|--------|---------|
| `scripts/demo.py` | End-to-end demo: onboarding, issuance, RTGS with approve/sign, conditional payments, escrow, FX with MPC signing |
| `scripts/load_test.py` | Concurrent load testing harness with configurable workers and duration |
| `scripts/ledger_integrity.py` | 12-check reconciliation engine: double-entry balance, chronological replay, orphan detection, webhook alerts |
| `scripts/kafka_tail.py` | Tail Kafka topics in real time |
| `scripts/migrate.py` | Run Alembic database migrations |

### Makefile Targets

```bash
make up              # Build and start all services
make down            # Stop containers (keep volumes)
make down-v          # Stop and remove volumes (full reset)
make logs            # Follow all service logs
make logs-svc SVC=rtgs  # Follow one service's logs
make test            # Full test suite
make demo            # Run live stack demo
make integrity       # Ledger double-entry integrity check
make kafka-tail      # Tail all Kafka topics
make db-balances     # Show all token balances
make db-ledger       # Show recent ledger entries
make health          # Check gateway health
```

---

## Testing

```bash
pip install -r tests/requirements-test.txt
pytest tests/ -q
```

**Test modules:**

| File | Coverage |
|------|----------|
| `test_token_issuance.py` | Issuance, redemption, balance queries, idempotency, precision |
| `test_rtgs.py` | Settlement submission, queue processing, priority, failure/retry |
| `test_payment_engine.py` | Conditional payments (all 5 condition types), escrow lifecycle |
| `test_fx_settlement.py` | Quote generation, PvP settlement, rate management |
| `test_compliance.py` | AML rules, sanctions screening, velocity tracking |
| `test_e2e_scenarios.py` | Multi-service integration flows |
| `test_metrics_and_shared.py` | Shared modules: database, Kafka client, metrics, events |
| `test_journal.py` | Double-entry journal pairs, balance derivation |
| `test_outbox.py` | Transactional outbox pattern |
| `test_rbac.py` | Role-based access control, key hashing |
| `test_state_machine.py` | Valid/invalid settlement state transitions |
| `test_idempotency.py` | Idempotency key deduplication |
| `test_audit_context.py` | Request context extraction, audit trail |
| `test_reconciliation.py` | Balance reconciliation, audit reports |
| `test_signing_gateway.py` | MPC coordination, threshold signing |
| `test_kafka_dlq.py` | Dead letter queue handling |
| `test_status_history.py` | Append-only status transitions |

---

## Technical Design

### Double-Entry Journal System

Every balance-changing operation creates exactly two `journal_entries` records: one debit and one credit, linked by a shared `journal_id`. Entries reference a chart of accounts (`chart_of_accounts` table) that defines the normal balance convention for each account type.

**Balance derivation** (no stored balances as source of truth):
- **Assets** (OMNIBUS_RESERVE, FX_NOSTRO_*): `SUM(debit) - SUM(credit)`
- **Liabilities** (INSTITUTION_LIABILITY): `SUM(credit) - SUM(debit)`

The `token_balances` table exists as a denormalized cache for read performance. The journal is the authoritative source; the reconciliation engine verifies consistency between the two.

### Immutability Enforcement

Database triggers prevent UPDATE and DELETE on critical tables:

| Table | Trigger | Behavior |
|-------|---------|----------|
| `journal_entries` | `reject_journal_entry_mutation` | Raises exception on UPDATE or DELETE |
| `ledger_entries` | `reject_journal_entry_mutation` | Raises exception on UPDATE or DELETE |
| `escrow_holds` | `reject_escrow_hold_mutation` | Raises exception on UPDATE or DELETE |
| `outbox_events` | `reject_outbox_event_mutation` | Only allows UPDATE to `published_at`; all other columns immutable |
| All 6 status history tables | Per-table reject triggers | Raises exception on UPDATE or DELETE |

### Settlement State Machine

Deterministic state transitions defined in `shared/state_machine.py`:

**RTGS transitions:**
```
pending -> approved -> signed -> processing -> broadcasted -> confirmed -> settled
                                     |                            |
                                     +-> failed -> pending (retry)
```

**FX transitions:**
```
queued -> processing -> settled
              |
              +-> failed -> queued (retry)
```

`validate_transition()` raises HTTP 409 on invalid transitions.

### Role-Based Access Control (RBAC)

Five roles enforce separation of duties:

| Role | Permissions |
|------|-------------|
| `admin` | Full access to all endpoints |
| `approver` | Approve settlements |
| `signer` | Sign settlements (must differ from approver) |
| `trader` | Submit settlements, issue/redeem tokens, execute FX |
| `auditor` | Read-only access to balances and rates |

API keys are stored as SHA-256 hashes. The API gateway resolves keys, sets `X-Actor-Role`, `X-Actor-ID`, and `X-Actor-Name` headers, and validates route-role mappings before proxying.

### MPC Transaction Signing

2-of-3 threshold signing for blockchain settlement rails:

1. FX settlement service sends signing request to signing gateway
2. Gateway fans out to all 3 MPC nodes in the isolated signing network
3. Each node produces a deterministic partial signature
4. Gateway combines partials once threshold (`(N // 2) + 1 = 2`) is met
5. Combined signature stored as `blockchain_tx_hash` on the FX settlement record

Simulated (deterministic hashes, not real cryptographic MPC) -- suitable for demonstration purposes.

### Transactional Outbox Pattern

Services write events to the `outbox_events` table atomically within the same database transaction as business operations. The dedicated `outbox-publisher` service polls this table every 100ms, publishes events to Kafka with `acks=all`, and marks them as published. This guarantees at-least-once delivery without distributed transactions.

### Dead Letter Queue

Failed Kafka messages are routed to `dlq.default` (30-day retention) after 3 retry attempts with exponential backoff. The DLQ allows manual inspection and replay of poison-pill messages.

### Reconciliation Engine

`scripts/ledger_integrity.py` performs 12 checks that implement the reconciliation cycle:

1. **Double-entry balance** -- for every `txn_ref`, `SUM(debits) == SUM(credits)`
2. **Running balance cross-check** -- `token_balances` vs `ledger_entries` totals
3. **No negative balances** -- all `token_balances.balance >= 0`
4. **Reserved within bounds** -- `reserved <= balance` for all accounts
5. **No orphaned transactions** -- completed transactions have ledger entries
6. **Settlement consistency** -- settled RTGS records have a `transaction_id`
7. **FX PvP leg consistency** -- settled FX records have both sell and buy legs
8. **Escrow bounds** -- active escrow amounts <= depositor reserved balance
9. **Journal balance** -- for every `journal_id`, `SUM(debit) == SUM(credit)`
10. **Journal vs account cross-check** -- journal totals match `token_balances`
11. **No orphaned journal entries** -- all reference IDs trace to parent entities
12. **Chronological replay** -- replays all journal entries in time order, recomputes balances from scratch, compares to stored state

Supports `--json` for structured output and `--webhook-url` for alerting on failures.

### Deterministic State Rebuild

Full system state can be reconstructed from the immutable ledger:

- **Balances**: Replay `journal_entries` ordered by `created_at ASC, id ASC` to recompute every account balance (check 12 in reconciliation engine)
- **Status history**: All status history tables are append-only; replaying them reconstructs the full lifecycle of every settlement, payment, issuance, and escrow
- **Audit trail**: `request_id`, `actor_id`, and `actor_service` on every status transition enables forensic reconstruction of who did what and when

### Concurrency Control

- **Advisory locks:** `pg_advisory_xact_lock` with deterministic hash of account+currency prevents double-spend
- **Row-level locking:** `SELECT FOR UPDATE` on `token_balances` during transfers
- **Skip-locked queuing:** `SKIP LOCKED` for multi-worker safety (RTGS, outbox-publisher)
- **Optimistic locking:** `version` column on `token_balances` incremented on every update
- **Transaction isolation:** `READ COMMITTED` enforced at the session level

### Idempotency

All state-changing endpoints accept an `idempotency_key`. If a request with the same key has already been processed, the existing result is returned without re-executing. Kafka consumers deduplicate via the `processed_events` table. This prevents duplicate settlements, double-minting, and payment replay on network retries.

### Kafka Guarantees

- **Producer:** Idempotent (`enable.idempotence=True`), waits for all in-sync replicas (`acks=all`), LZ4 compression, flushes after every publish.
- **Consumer:** Manual offset commits only after the handler succeeds. Exponential backoff retries (up to 3 attempts, max 30s). Failed messages routed to DLQ.
- **Outbox pattern:** Events are persisted atomically with business writes, then published asynchronously by the outbox-publisher service. This guarantees at-least-once delivery. Consumers must handle duplicates via `processed_events`.

### Precision and Rounding

- Token amounts: `NUMERIC(28,8)` -- 8 decimal places
- Journal entries: `NUMERIC(38,18)` -- 18 decimal places
- FX rates: `NUMERIC(18,8)` -- 8 decimal places
- Rounding: `ROUND_HALF_EVEN` (banker's rounding) for balance operations, `ROUND_DOWN` for FX quotes (bank's favor)

### Database

- Connection pool: 10 base + 20 overflow, recycled every 3,600 seconds
- Isolation: `READ COMMITTED` enforced via `SET SESSION CHARACTERISTICS`
- Auto-rollback on exception, auto-close on context exit

---

## Project Structure

```
stablecoin-infra/
+-- docker-compose.yml
+-- docker-compose.monitoring.yml
+-- Makefile
+-- .env.example
+-- shared/
|   +-- __init__.py
|   +-- models.py              # SQLAlchemy ORM (21 models, 8 enums)
|   +-- database.py            # Session factory, connection pooling
|   +-- kafka_client.py        # Idempotent producer, DLQ consumer
|   +-- events.py              # Pydantic event schemas (26 event types)
|   +-- metrics.py             # Prometheus middleware and business counters
|   +-- journal.py             # Double-entry journal with balance derivation
|   +-- outbox.py              # Transactional outbox insert
|   +-- rbac.py                # Role-based access control, key hashing
|   +-- state_machine.py       # Settlement state transition validation
|   +-- context.py             # Request context extraction (audit trail)
|   +-- status.py              # Append-only status history recording
+-- services/
|   +-- api-gateway/           # RBAC auth, rate limiting, reverse proxy
|   +-- token-issuance/        # Mint/burn with double-entry journal
|   +-- rtgs/                  # Priority queue settlement with approve/sign
|   +-- payment-engine/        # Conditional payments + escrow
|   +-- fx-settlement/         # PvP atomic swaps with MPC signing
|   +-- compliance-monitor/    # AML screening, Kafka consumer
|   +-- outbox-publisher/      # Transactional outbox -> Kafka relay
|   +-- signing-gateway/       # MPC signing coordinator
|   +-- mpc-node/              # MPC signing node (3 instances)
+-- init/
|   +-- kafka/
|   |   +-- create_topics.sh   # 25 topics with retention policies
|   +-- postgres/
|       +-- 01_schema.sql      # Base schema, enums, indexes, seed data
|       +-- 02_migrations.sql  # CoA, journal, immutability triggers, RBAC
+-- migrations/
|   +-- env.py
|   +-- apply_pending.sql
|   +-- versions/
|       +-- 0001_initial_schema.py
|       +-- 0002_perf_indexes.py
|       +-- 0003_blockchain_grade_architecture.py
|       +-- 0004_rbac_audit_idempotency.py
+-- scripts/
|   +-- demo.py                # Full end-to-end demo
|   +-- load_test.py           # Concurrent load testing
|   +-- ledger_integrity.py    # 12-check reconciliation engine
|   +-- kafka_tail.py          # Real-time Kafka topic monitor
|   +-- migrate.py             # Alembic migration runner
+-- monitoring/
|   +-- prometheus.yml
|   +-- grafana-dashboard.json
|   +-- grafana-datasource.yml
|   +-- grafana-dashboard-provider.yml
+-- tests/
    +-- conftest.py
    +-- test_token_issuance.py
    +-- test_rtgs.py
    +-- test_payment_engine.py
    +-- test_fx_settlement.py
    +-- test_compliance.py
    +-- test_e2e_scenarios.py
    +-- test_metrics_and_shared.py
    +-- test_journal.py
    +-- test_outbox.py
    +-- test_rbac.py
    +-- test_state_machine.py
    +-- test_idempotency.py
    +-- test_audit_context.py
    +-- test_reconciliation.py
    +-- test_signing_gateway.py
    +-- test_kafka_dlq.py
    +-- test_status_history.py
    +-- requirements-test.txt
```

---

## Production Warning

**This project is explicitly NOT suitable for production use.** Tokenized deposit platforms are among the most regulated, operationally complex, and legally sensitive activities in financial services. The following critical components are absent or stubbed:

| Missing Component | Risk if Absent |
|-------------------|----------------|
| Real banking license and regulatory approval | Cannot legally issue tokenized deposits |
| Real SWIFT Alliance Gateway / FIN integration | Cannot send actual MT103/MT202 messages |
| Real FedWire FedLine connection | Cannot initiate actual USD gross settlement |
| Real TARGET2 / TIPS connectivity | Cannot settle EUR in the Eurosystem |
| Real KYC/AML provider integration (Onfido, ComplyAdvantage, Chainalysis) | No actual identity verification or sanctions screening |
| HSM / MPC key management (Thales, Fireblocks) | No cryptographic signing for settlement authorization |
| Smart contract audit (if blockchain rail is real) | Exploitable vulnerabilities in token contracts |
| TLS / mTLS for service-to-service communication | Plaintext internal traffic |
| API key rotation and expiry | Static API keys with no lifecycle management |
| Request signing (HMAC / JWT) | No request integrity verification |
| Transaction limits by account risk tier | No controls on settlement size |
| Multi-sig authorization for large settlements | Single-party authorization for any amount |
| Cold storage / custody separation | All funds in hot operational accounts |
| Comprehensive test suite with mutation testing | Untested edge cases in fund handling |
| Disaster recovery and failover procedures | No tested recovery for settlement outages |
| Regulatory reporting (FinCEN CTR/SAR, FCA, MiFID II) | Post-trade reporting violations |
| Rate limiting per account (not just per API key) | No per-participant abuse prevention |
| Kafka cluster (replication factor > 1) | Single broker -- no fault tolerance |
| Database replication and failover | Single PostgreSQL instance -- no HA |

> Tokenized deposit platforms at institutional scale require: a banking charter or e-money license, central bank account access, SWIFT membership, regulatory approval from relevant authorities (OCC, FCA, MAS), AML/KYC compliance infrastructure, and legal agreements with all counterparties. **Do not use this code to issue, manage, or transfer any real tokenized deposits or funds.**

---

## License

This project is provided as-is for educational and reference purposes under the MIT License.

---

*Built with ♥️ by Pavon Dunbar -- Modeled on JPMorgan Kinexys (JPM Coin), PayPal PYUSD, and the Regulated Liability Network (RLN)*
