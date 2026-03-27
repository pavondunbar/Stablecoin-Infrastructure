#!/usr/bin/env python3
"""
scripts/kafka_tail.py — Real-time Kafka topic monitor for debugging.

Usage:
    # Tail all platform topics:
    KAFKA_BOOTSTRAP=localhost:9092 python scripts/kafka_tail.py

    # Tail specific topics:
    python scripts/kafka_tail.py --topics rtgs.settlement.completed fx.settlement.completed

    # From the beginning of each topic:
    python scripts/kafka_tail.py --from-beginning

    # Show raw JSON (no pretty-print):
    python scripts/kafka_tail.py --raw
"""

import argparse
import json
import os
import signal
import sys
from datetime import datetime

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")

ALL_TOPICS = [
    "token.issuance.requested",
    "token.issuance.completed",
    "token.redemption.requested",
    "token.redemption.completed",
    "token.balance.updated",
    "rtgs.settlement.submitted",
    "rtgs.settlement.processing",
    "rtgs.settlement.completed",
    "rtgs.settlement.failed",
    "rtgs.queue.urgent",
    "payment.conditional.created",
    "payment.conditional.triggered",
    "payment.conditional.completed",
    "escrow.created",
    "escrow.released",
    "escrow.expired",
    "fx.rate.updated",
    "fx.settlement.initiated",
    "fx.settlement.leg.completed",
    "fx.settlement.completed",
    "fx.settlement.failed",
    "compliance.event",
    "audit.trail",
]

TOPIC_COLOURS = {
    "token":      "\033[36m",   # cyan
    "rtgs":       "\033[33m",   # yellow
    "payment":    "\033[35m",   # magenta
    "escrow":     "\033[35m",   # magenta
    "fx":         "\033[34m",   # blue
    "compliance": "\033[31m",   # red
    "audit":      "\033[90m",   # grey
}
RESET = "\033[0m"
BOLD  = "\033[1m"


def topic_colour(topic: str) -> str:
    for prefix, colour in TOPIC_COLOURS.items():
        if topic.startswith(prefix):
            return colour
    return ""


def format_event(topic: str, payload: dict, raw: bool) -> str:
    ts    = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    col   = topic_colour(topic)
    reset = RESET

    if raw:
        return f"{col}[{ts}] {topic}{reset}\n{json.dumps(payload)}\n"

    # Key fields to highlight based on topic prefix
    highlights = []
    for key in ("settlement_ref", "payment_ref", "contract_ref",
                 "issuance_ref", "event_id", "amount", "currency",
                 "status", "result", "sell_amount", "buy_amount"):
        if key in payload:
            highlights.append(f"{key}={payload[key]}")

    summary = "  ".join(highlights[:6])
    lines   = [f"{col}{BOLD}[{ts}] {topic}{reset}"]
    lines.append(f"  {summary}")

    # Show a few more interesting keys
    skip = set(highlights[:6]) | {"event_id", "event_time", "service"}
    extras = {k: v for k, v in payload.items()
              if k not in skip and not isinstance(v, dict) and v is not None}
    if extras:
        for k, v in list(extras.items())[:4]:
            lines.append(f"  {k}: {v}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Tail Kafka topics")
    parser.add_argument("--topics", nargs="+", default=ALL_TOPICS,
                        metavar="TOPIC", help="Topics to subscribe to")
    parser.add_argument("--from-beginning", action="store_true",
                        help="Start from earliest offset")
    parser.add_argument("--raw", action="store_true",
                        help="Print raw JSON without formatting")
    parser.add_argument("--group-id", default="kafka-tail-debug",
                        help="Consumer group ID (default: kafka-tail-debug)")
    args = parser.parse_args()

    try:
        from confluent_kafka import Consumer, KafkaError
    except ImportError:
        print("confluent-kafka not installed. Run: pip install confluent-kafka")
        sys.exit(1)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id":          args.group_id,
        "auto.offset.reset": "earliest" if args.from_beginning else "latest",
        "enable.auto.commit": False,
    })

    consumer.subscribe(args.topics)

    print(f"\033[1mKafka Tail — {KAFKA_BOOTSTRAP}\033[0m")
    print(f"Subscribed to {len(args.topics)} topics. Ctrl-C to stop.\n")
    print("─" * 72)

    def shutdown(_sig, _frame):
        consumer.close()
        print("\nConsumer closed.")
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    msg_count = 0
    while True:
        msg = consumer.poll(timeout=1.0)
        if msg is None:
            continue
        if msg.error():
            if msg.error().code() != KafkaError._PARTITION_EOF:
                print(f"Error: {msg.error()}", file=sys.stderr)
            continue

        msg_count += 1
        try:
            payload = json.loads(msg.value())
        except Exception:
            payload = {"_raw": msg.value().decode("utf-8", errors="replace")}

        print(format_event(msg.topic(), payload, args.raw))
        print()

        consumer.commit(message=msg, asynchronous=False)


if __name__ == "__main__":
    main()
