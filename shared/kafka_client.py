"""
shared/kafka_client.py — Confluent Kafka producer and consumer wrappers.

Design decisions:
  - Producer is a module-level singleton (thread-safe, connection-pooled)
  - All messages are JSON-serialised Pydantic events
  - Producer flushes after each publish for at-least-once delivery semantics
  - Consumer wraps confluent_kafka.Consumer with auto-deserialization
"""

import json
import logging
import os
import time
from typing import Any, Callable, Optional, Type, TypeVar

from confluent_kafka import Consumer, KafkaError, KafkaException, Producer
from pydantic import BaseModel

log = logging.getLogger(__name__)

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka:9092")
SERVICE_NAME    = os.environ.get("SERVICE_NAME", "unknown-service")

T = TypeVar("T", bound=BaseModel)

# ─── Producer ────────────────────────────────────────────────────────────────

_producer: Optional[Producer] = None


def _get_producer() -> Producer:
    global _producer
    if _producer is None:
        _producer = Producer(
            {
                "bootstrap.servers": KAFKA_BOOTSTRAP,
                "client.id": SERVICE_NAME,
                "acks": "all",                   # wait for all ISR replicas
                "retries": 5,
                "retry.backoff.ms": 300,
                "compression.type": "lz4",
                "enable.idempotence": True,      # exactly-once on the producer side
                "message.timeout.ms": 10000,
            }
        )
    return _producer


def _delivery_report(err, msg):
    if err:
        log.error("Kafka delivery failed | topic=%s err=%s", msg.topic(), err)
    else:
        log.debug(
            "Kafka delivered | topic=%s partition=%d offset=%d",
            msg.topic(), msg.partition(), msg.offset(),
        )


def publish(topic: str, event: BaseModel, key: Optional[str] = None) -> None:
    """Publish a Pydantic event to a Kafka topic."""
    producer = _get_producer()
    payload = event.model_dump_json().encode("utf-8")
    producer.produce(
        topic=topic,
        value=payload,
        key=key.encode("utf-8") if key else None,
        on_delivery=_delivery_report,
    )
    producer.flush(timeout=5)
    log.info("Published | topic=%s key=%s", topic, key)


def publish_dict(topic: str, data: dict, key: Optional[str] = None) -> None:
    """Publish a raw dict to a Kafka topic."""
    producer = _get_producer()
    payload = json.dumps(data, default=str).encode("utf-8")
    producer.produce(
        topic=topic,
        value=payload,
        key=key.encode("utf-8") if key else None,
        on_delivery=_delivery_report,
    )
    producer.flush(timeout=5)


# ─── Consumer ────────────────────────────────────────────────────────────────

def build_consumer(group_id: str, topics: list[str]) -> Consumer:
    """Create and subscribe a Kafka consumer."""
    consumer = Consumer(
        {
            "bootstrap.servers": KAFKA_BOOTSTRAP,
            "group.id": group_id,
            "client.id": f"{SERVICE_NAME}-{group_id}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,     # manual commit after processing
            "max.poll.interval.ms": 300000,
            "session.timeout.ms": 45000,
        }
    )
    consumer.subscribe(topics)
    log.info("Consumer subscribed | group=%s topics=%s", group_id, topics)
    return consumer


def consume_loop(
    consumer: Consumer,
    handler: Callable[[str, dict], None],
    poll_timeout: float = 1.0,
    max_errors: int = 10,
) -> None:
    """
    Blocking consume loop.  Calls handler(topic, payload_dict) for each message.
    Commits offset only after successful handler execution.
    """
    consecutive_errors = 0
    try:
        while True:
            msg = consumer.poll(timeout=poll_timeout)
            if msg is None:
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                log.error("Consumer error: %s", msg.error())
                consecutive_errors += 1
                if consecutive_errors >= max_errors:
                    raise KafkaException(msg.error())
                time.sleep(1)
                continue

            consecutive_errors = 0
            topic   = msg.topic()
            raw     = msg.value()
            try:
                payload = json.loads(raw)
                handler(topic, payload)
                consumer.commit(message=msg, asynchronous=False)
            except Exception as exc:
                log.exception("Handler error | topic=%s exc=%s", topic, exc)
                # In production: send to DLQ topic
    finally:
        consumer.close()
        log.info("Consumer closed.")
