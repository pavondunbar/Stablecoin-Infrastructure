#!/bin/bash
set -e

KAFKA="kafka:9092"
REPLICATION=1
WAIT_SEC=20

echo "⏳  Waiting ${WAIT_SEC}s for Kafka to be fully ready..."
sleep $WAIT_SEC

create_topic() {
    local TOPIC=$1
    local PARTITIONS=${2:-4}
    local RETENTION_MS=${3:-604800000}   # 7 days default

    kafka-topics --bootstrap-server "$KAFKA" \
        --create --if-not-exists \
        --topic "$TOPIC" \
        --partitions "$PARTITIONS" \
        --replication-factor "$REPLICATION" \
        --config retention.ms="$RETENTION_MS" \
        --config cleanup.policy=delete \
        --config compression.type=lz4
    echo "✅  Topic created: $TOPIC"
}

# ── Token Issuance ─────────────────────────────────────────────────────────
create_topic "token.issuance.requested"    4
create_topic "token.issuance.completed"    4
create_topic "token.redemption.requested"  4
create_topic "token.redemption.completed"  4
create_topic "token.balance.updated"       8   # high-volume

# ── RTGS Settlement ────────────────────────────────────────────────────────
create_topic "rtgs.settlement.submitted"   4
create_topic "rtgs.settlement.processing"  4
create_topic "rtgs.settlement.completed"   4
create_topic "rtgs.settlement.failed"      2   86400000   # 1 day retention
create_topic "rtgs.queue.urgent"           2              # priority lane

# ── Programmable Payments ──────────────────────────────────────────────────
create_topic "payment.conditional.created" 4
create_topic "payment.conditional.triggered" 4
create_topic "payment.conditional.completed" 4
create_topic "escrow.created"              4
create_topic "escrow.released"             4
create_topic "escrow.expired"              2

# ── FX Settlement ──────────────────────────────────────────────────────────
create_topic "fx.rate.updated"             2
create_topic "fx.settlement.initiated"     4
create_topic "fx.settlement.leg.completed" 4
create_topic "fx.settlement.completed"     4
create_topic "fx.settlement.failed"        2   86400000

# ── Compliance & Audit ─────────────────────────────────────────────────────
create_topic "compliance.event"            4   2592000000  # 30 days
create_topic "audit.trail"                 8   2592000000  # 30 days — immutable log

echo ""
echo "🎉  All Kafka topics created successfully."
kafka-topics --bootstrap-server "$KAFKA" --list
