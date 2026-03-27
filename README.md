# Stablecoin Infrastructure

Microservices platform for tokenized deposit issuance, real-time gross settlement (RTGS), programmable payments, and cross-border FX settlement. Modeled on institutional systems like JPMorgan Kinexys (JPM Coin), PayPal PYUSD, and the Regulated Liability Network (RLN).

The system implements a double-entry ledger with atomic balance transfers, event-driven architecture via Kafka, and a trust-boundary network model where only the API gateway is internet-facing.

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
                    |  auth + rate-limit|
                    +----+-+-+-+--------+
                         | | | |
              DMZ network| | | |internal network
         ┌───────────────┘ | | └───────────────┐
         |          ┌──────┘ └──────┐           |
         v          v               v           v
   +-----------+ +------+ +----------+ +----------+
   |  Token    | | RTGS | | Payment  | |    FX    |
   | Issuance  | |      | |  Engine  | |Settlement|
   |   :8001   | | :8002| |  :8003   | |  :8004   |
   +-----------+ +------+ +----------+ +----------+
         |          |           |           |
         +-----+----+-----+----+-----------+
               |          |
         +-----+---+ +----+----+
         |Postgres | | Kafka   |
         |  :5432  | |  :9092  |
         +---------+ +---------+
                          |
                   +------+-------+
                   |  Compliance  |
                   |   Monitor    |
                   |    :8005     |
                   +--------------+
```

### Network Isolation

| Network | Services | Internet Access |
|---------|----------|-----------------|
| **dmz** | api-gateway, prometheus, grafana | Yes (host-reachable) |
| **internal** | All microservices, postgres, kafka, zookeeper | No (`internal: true`) |

The API gateway bridges both networks. All backend services are unreachable from outside the Docker network.

### Infrastructure

| Component | Image | Purpose |
|-----------|-------|---------|
| PostgreSQL | `postgres:16-alpine` | Persistent storage, double-entry ledger |
| Kafka | `confluentinc/cp-kafka:7.6.0` | Event streaming between services |
| Zookeeper | `confluentinc/cp-zookeeper:7.6.0` | Kafka coordination |
| Prometheus | `prom/prometheus:v2.54.1` | Metrics collection (15s scrape interval) |
| Grafana | `grafana/grafana:11.2.0` | Dashboards and visualization |

---

## Services

### API Gateway (port 8000)

The sole internet-facing service. Authenticates requests via `X-API-Key` header, enforces rate limits (1,000 requests per 60-second sliding window per API key), and reverse-proxies to internal services. Every request is logged to the `audit.trail` Kafka topic with a unique `X-Request-ID`.

**Routes:**

| Path Prefix | Upstream | Port |
|-------------|----------|------|
| `/v1/tokens/*` | token-issuance | 8001 |
| `/v1/accounts/*` | token-issuance | 8001 |
| `/v1/settlements/*` | rtgs | 8002 |
| `/v1/payments/*` | payment-engine | 8003 |
| `/v1/fx/*` | fx-settlement | 8004 |

`GET /health` aggregates health from all upstream services, returning `200` if all are healthy or `207` if any are degraded.

### Token Issuance (port 8001)

Issues and redeems tokenized deposits against an omnibus reserve account. Every operation produces matching debit and credit ledger entries (double-entry bookkeeping). Tokens are minted by debiting the omnibus reserve and crediting the recipient; redemptions reverse this.

- Validates KYC/AML status before issuance
- Supports idempotency keys to prevent duplicate processing
- 8-decimal-place precision with banker's rounding (`ROUND_HALF_EVEN`)
- Publishes `token.issuance.completed`, `token.redemption.completed`, and `token.balance.updated` events

**System accounts seeded at startup:**

| UUID | Name | Balance |
|------|------|---------|
| `...0001` | OMNIBUS_RESERVE | 1B USD, 1B EUR, 1B GBP |
| `...0002` | FX_NOSTRO_USD | 500M USD |
| `...0003` | FX_NOSTRO_EUR | 500M EUR |
| `...0004` | FX_NOSTRO_GBP | 500M GBP |

### RTGS (port 8002)

Real-time gross settlement with priority-based queue processing. Settlement instructions are queued and processed by a background worker thread that drains the queue in priority order (urgent > high > normal > low), then FIFO within each priority level.

- `SELECT FOR UPDATE` with `skip_locked=True` for concurrent worker safety
- Atomic balance transfers with row-level locking
- Retries up to 3 times for transient failures (not insufficient balance)
- Publishes lifecycle events: `submitted` -> `processing` -> `completed` or `failed`

### Payment Engine (port 8003)

Programmable payments with two mechanisms: conditional payments and escrow contracts.

**Conditional payments** execute when a condition is satisfied:

| Condition Type | Trigger |
|----------------|---------|
| `time_lock` | Releases after a specified timestamp (auto-evaluated) |
| `oracle_trigger` | External oracle provides matching key/value |
| `multi_sig` | Required threshold of signers met |
| `delivery_confirmation` | Delivery reference confirmed by external system |
| `kyc_verified` | Payee's KYC/AML status verified (auto-evaluated) |

A background thread polls for `time_lock` and `kyc_verified` conditions every 5 seconds. Other condition types require an explicit trigger via the `/trigger` endpoint.

**Escrow contracts** lock funds in a reserved bucket on the depositor's balance. They can be released to the beneficiary, refunded to the depositor, or auto-expired when `expires_at` passes.

### FX Settlement (port 8004)

Cross-border payment-vs-payment (PvP) atomic swaps. Both legs (sell and buy) execute within a single database transaction — if either fails, both roll back.

- Maintains a live FX rate book (fed by `fx.rate.updated` Kafka topic)
- Generates quotes with configurable bid/ask spreads (basis points)
- Supports 5 settlement rails: `blockchain`, `swift`, `internal`, `fedwire`, `target2`
- Routes through nostro accounts per currency (USD/EUR/GBP)
- Generates simulated blockchain hashes for on-chain rails

**Seeded FX rates:** USD/EUR, USD/GBP, EUR/USD, EUR/GBP, GBP/USD, GBP/EUR

### Compliance Monitor (port 8005)

Consumes settlement, issuance, and payment events from Kafka and runs rule-based screening. Operates as fire-and-forget — it never blocks the originating transaction.

**AML rules:**

| Rule | Threshold |
|------|-----------|
| Large transaction (CTR equivalent) | >= $1,000,000 USD equivalent |
| Structuring detection | $9,500 - $999,999 USD equivalent |
| Velocity limit | > 20 transactions per account per hour |
| Sanctions screening | Pattern match against SDN name fragments |

Risk scores range from 0 to 100 (cumulative, 25 points per flag). Results are persisted to `compliance_events` and published to the `compliance.event` Kafka topic with 30-day retention.

---

## Data Model

12 tables with PostgreSQL enums, check constraints, and foreign keys.

```
accounts                     token_balances
  ├─ id (UUID PK)             ├─ account_id FK → accounts
  ├─ entity_name              ├─ currency (7 enums)
  ├─ account_type (4 enums)   ├─ balance NUMERIC(28,8)
  ├─ bic_code (ISO 9362)      ├─ reserved NUMERIC(28,8)
  ├─ lei (unique)             ├─ version (optimistic lock)
  ├─ kyc_verified             └─ UNIQUE(account_id, currency)
  ├─ aml_cleared
  └─ risk_tier (1-5)

ledger_entries               transactions
  ├─ txn_ref                   ├─ txn_ref (unique)
  ├─ entry_type (debit|credit) ├─ debit_account_id FK
  ├─ account_id FK             ├─ credit_account_id FK
  ├─ amount                    ├─ txn_type (6 types)
  └─ balance_after             ├─ idempotency_key (unique)
                               └─ CHECK(debit != credit)

token_issuances              rtgs_settlements
  ├─ issuance_ref (unique)     ├─ settlement_ref (unique)
  ├─ backing_ref (fiat dep.)   ├─ priority (urgent|high|normal|low)
  ├─ issuance_type             ├─ retry_count
  └─ status                    └─ INDEX(status, priority, queued_at)

escrow_contracts             conditional_payments
  ├─ contract_ref (unique)     ├─ payment_ref (unique)
  ├─ depositor / beneficiary   ├─ condition_type (5 enums)
  ├─ arbiter_account_id        ├─ condition_params (JSONB)
  ├─ conditions (JSONB)        ├─ trigger_data (JSONB)
  └─ expires_at                └─ expires_at

fx_rates                     fx_settlements
  ├─ base/quote currency       ├─ settlement_ref (unique)
  ├─ mid/bid/ask rate          ├─ sell/buy currency + amount
  ├─ spread_bps                ├─ applied_rate
  └─ is_active                 ├─ rails (5 enums)
                               ├─ sell_txn_id / buy_txn_id FK
compliance_events            └─ blockchain_tx_hash
  ├─ entity_type + entity_id
  ├─ result (pass|fail|review)
  └─ score NUMERIC(5,2)
```

**Supported currencies:** USD, EUR, GBP, JPY, SGD, CHF, HKD

**Settlement rails:** blockchain, swift, internal, fedwire, target2

---

## Kafka Topics

23 topics provisioned at startup with LZ4 compression.

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

---

## API Reference

All requests go through the API gateway at `http://localhost:8000`. Include the API key as `X-API-Key` header.

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

# Query settlement status
curl http://localhost:8000/v1/settlements/<settlement-ref> \
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

# Execute PvP settlement
curl -X POST http://localhost:8000/v1/fx/settle \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "sending_account_id": "<sender-uuid>",
    "receiving_account_id": "<receiver-uuid>",
    "sell_currency": "USD",
    "sell_amount": "1000000.00",
    "buy_currency": "EUR",
    "rails": "swift"
  }'
```

### Health

```bash
# Gateway health (aggregates all services)
curl http://localhost:8000/health
```

---

## Getting Started

### Prerequisites

- Docker and Docker Compose
- 4 GB RAM minimum (Kafka + PostgreSQL + 6 services)

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

This starts 11 containers:

1. **postgres** — database with schema and seed data
2. **zookeeper** — Kafka coordination
3. **kafka** — event broker
4. **kafka-init** — creates 23 topics, then exits
5. **token-issuance** — tokenized deposit service
6. **rtgs** — settlement service
7. **payment-engine** — programmable payment service
8. **fx-settlement** — FX PvP service
9. **compliance-monitor** — AML/sanctions screening
10. **api-gateway** — reverse proxy (waits for all services to be healthy)
11. **prometheus** + **grafana** — observability

### 3. Verify

```bash
# Check all containers are running
docker compose ps

# Test gateway health
curl http://localhost:8000/health

# Expected response when all services are healthy:
# {"gateway": "ok", "services": {"token": "ok", "rtgs": "ok", "payment": "ok", "fx": "ok"}}
```

### 4. Run the demo

```bash
pip install httpx
python scripts/demo.py
```

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
| `scripts/demo.py` | End-to-end demo: account creation, issuance, RTGS, FX, escrow |
| `scripts/load_test.py` | Concurrent load testing harness |
| `scripts/ledger_integrity.py` | Balance reconciliation and audit verification |
| `scripts/kafka_tail.py` | Tail Kafka topics in real time |
| `scripts/migrate.py` | Run Alembic database migrations |

---

## Testing

```bash
pip install -r tests/requirements-test.txt
pytest tests/ -q
```

**Test modules:**

| File | Coverage |
|------|----------|
| `test_token_issuance.py` | Issuance, redemption, balance queries, idempotency |
| `test_rtgs.py` | Settlement submission, queue processing, failure/retry |
| `test_payment_engine.py` | Conditional payments (all 5 condition types), escrow lifecycle |
| `test_fx_settlement.py` | Quote generation, PvP settlement, rate management |
| `test_compliance.py` | AML rules, sanctions screening, velocity tracking |
| `test_e2e_scenarios.py` | Multi-service integration flows |
| `test_metrics_and_shared.py` | Shared modules: database, Kafka client, metrics, events |

---

## Technical Design

### Double-Entry Ledger

Every balance-changing operation creates exactly two `ledger_entries` records: one debit and one credit. The `balance_after` field on each entry enables point-in-time auditing. Token balances in `token_balances` are the mutable running totals; ledger entries are the immutable audit trail.

### Concurrency Control

- **Row-level locking:** `SELECT FOR UPDATE` on `token_balances` during transfers prevents concurrent modifications to the same balance.
- **Skip-locked queuing:** The RTGS worker uses `WITH FOR UPDATE (skip_locked=True)` so multiple workers can drain the queue without deadlocks.
- **Optimistic locking:** A `version` column on `token_balances` is incremented on every update. Stale reads are detectable by comparing versions.
- **Transaction isolation:** `READ COMMITTED` enforced at the session level prevents dirty reads while allowing concurrent access.

### Idempotency

All state-changing endpoints accept an `idempotency_key`. If a request with the same key has already been processed, the existing result is returned without re-executing. This prevents duplicate settlements, double-minting, and payment replay on network retries.

### Kafka Guarantees

- **Producer:** Idempotent (`enable.idempotence=True`), waits for all in-sync replicas (`acks=all`), LZ4 compression, flushes after every publish.
- **Consumer:** Manual offset commits only after the handler succeeds. If processing fails, the message is re-delivered on next poll.
- **Events are published after `db.flush()` but before `db.commit()`.** If the commit fails, the Kafka event was already sent (at-least-once semantics). Consumers must handle duplicates.

### Precision and Rounding

- Token amounts: `NUMERIC(28,8)` — 8 decimal places
- FX rates: `NUMERIC(18,8)` — 8 decimal places
- Rounding: `ROUND_HALF_EVEN` (banker's rounding) for balance operations, `ROUND_DOWN` for FX quotes (bank's favor)

### Database

- Connection pool: 10 base + 20 overflow, recycled every 3,600 seconds
- Isolation: `READ COMMITTED` enforced via `SET SESSION CHARACTERISTICS`
- Auto-rollback on exception, auto-close on context exit

---

## Project Structure

```
stablecoin-infra/
├── docker-compose.yml
├── .env.example
├── shared/
│   ├── __init__.py
│   ├── models.py              # SQLAlchemy ORM (12 models, 8 enums)
│   ├── database.py            # Session factory, connection pooling
│   ├── kafka_client.py        # Idempotent producer, manual-commit consumer
│   ├── events.py              # Pydantic event schemas (20+ event types)
│   └── metrics.py             # Prometheus middleware and business counters
├── services/
│   ├── api-gateway/
│   │   ├── main.py            # Auth, rate limiting, reverse proxy
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── token-issuance/
│   │   ├── main.py            # Mint/burn with double-entry ledger
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── rtgs/
│   │   ├── main.py            # Priority queue settlement worker
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── payment-engine/
│   │   ├── main.py            # Conditional payments + escrow
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   ├── fx-settlement/
│   │   ├── main.py            # PvP atomic swaps, rate book
│   │   ├── Dockerfile
│   │   └── requirements.txt
│   └── compliance-monitor/
│       ├── main.py            # AML screening, Kafka consumer
│       ├── Dockerfile
│       └── requirements.txt
├── init/
│   ├── kafka/
│   │   └── create_topics.sh   # 23 topics with retention policies
│   └── postgres/
│       └── 01_schema.sql      # Schema, enums, indexes, seed data
├── migrations/
│   ├── env.py
│   └── versions/
│       ├── 0001_initial_schema.py
│       └── 0002_perf_indexes.py
├── scripts/
│   ├── demo.py
│   ├── load_test.py
│   ├── ledger_integrity.py
│   ├── kafka_tail.py
│   └── migrate.py
├── monitoring/
│   ├── prometheus.yml
│   ├── grafana-dashboard.json
│   ├── grafana-datasource.yml
│   └── grafana-dashboard-provider.yml
└── tests/
    ├── conftest.py
    ├── test_token_issuance.py
    ├── test_rtgs.py
    ├── test_payment_engine.py
    ├── test_fx_settlement.py
    ├── test_compliance.py
    ├── test_e2e_scenarios.py
    ├── test_metrics_and_shared.py
    └── requirements-test.txt
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
| Kafka cluster (replication factor > 1) | Single broker — no fault tolerance |
| Database replication and failover | Single PostgreSQL instance — no HA |

> Tokenized deposit platforms at institutional scale require: a banking charter or e-money license, central bank account access, SWIFT membership, regulatory approval from relevant authorities (OCC, FCA, MAS), AML/KYC compliance infrastructure, and legal agreements with all counterparties. **Do not use this code to issue, manage, or transfer any real tokenized deposits or funds.**

---

## License

This project is provided as-is for educational and reference purposes under the MIT License.

---

*Built with ❤️ by Pavon Dunbar — Modeled on JPMorgan Kinexys (JPM Coin), PayPal PYUSD, and the Regulated Liability Network (RLN)*
