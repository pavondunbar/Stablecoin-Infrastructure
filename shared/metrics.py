"""
shared/metrics.py — Prometheus instrumentation shared across all services.

Each service imports this module and calls register_metrics(app) after
creating their FastAPI instance.  The module exposes:

  • HTTP request duration histogram (by route, method, status)
  • In-flight request gauge
  • Business-domain counters (tokens issued, settlements processed, etc.)
  • Kafka publish latency histogram

Usage:
    from metrics import register_metrics, TOKENS_ISSUED, SETTLEMENTS_PROCESSED
    register_metrics(app)

    # In business logic:
    TOKENS_ISSUED.labels(currency="USD", direction="issue").inc()
"""

import os
import time
from typing import Callable

try:
    from prometheus_client import (
        Counter, Gauge, Histogram,
        generate_latest, CONTENT_TYPE_LATEST, REGISTRY,
    )
    _PROM_AVAILABLE = True
except ImportError:
    _PROM_AVAILABLE = False

SERVICE_NAME = os.environ.get("SERVICE_NAME", "unknown")

if _PROM_AVAILABLE:
    HTTP_REQUESTS_TOTAL = Counter(
        "http_requests_total",
        "Total HTTP requests",
        ["service", "method", "path", "status_code"],
    )
    HTTP_REQUEST_DURATION = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration in seconds",
        ["service", "method", "path"],
        buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
    )
    HTTP_REQUESTS_IN_FLIGHT = Gauge(
        "http_requests_in_flight",
        "Current in-flight HTTP requests",
        ["service"],
    )
    TOKENS_ISSUED = Counter(
        "tokens_issued_total",
        "Total token issuance/redemption operations",
        ["service", "currency", "direction"],
    )
    TOKEN_AMOUNT_ISSUED = Counter(
        "token_amount_issued_total",
        "Total nominal value of tokens issued",
        ["service", "currency"],
    )
    SETTLEMENTS_PROCESSED = Counter(
        "settlements_processed_total",
        "RTGS settlements processed",
        ["service", "status", "priority"],
    )
    SETTLEMENT_AMOUNT = Counter(
        "settlement_amount_total",
        "Total nominal value settled",
        ["service", "currency"],
    )
    SETTLEMENT_QUEUE_DEPTH = Gauge(
        "settlement_queue_depth",
        "Number of RTGS settlements in QUEUED state",
        ["service"],
    )
    CONDITIONAL_PAYMENTS = Counter(
        "conditional_payments_total",
        "Conditional payment lifecycle events",
        ["service", "condition_type", "event"],
    )
    ESCROW_CONTRACTS = Counter(
        "escrow_contracts_total",
        "Escrow contract lifecycle events",
        ["service", "event"],
    )
    FX_SETTLEMENTS = Counter(
        "fx_settlements_total",
        "FX settlement operations",
        ["service", "rails", "status"],
    )
    FX_VOLUME = Counter(
        "fx_settlement_volume_total",
        "Total FX sell-side volume settled",
        ["service", "currency"],
    )
    COMPLIANCE_EVENTS = Counter(
        "compliance_events_total",
        "Compliance screening events",
        ["service", "result", "event_type"],
    )
    KAFKA_PUBLISHES = Counter(
        "kafka_publishes_total",
        "Kafka publish operations",
        ["service", "topic", "status"],
    )
    KAFKA_PUBLISH_LATENCY = Histogram(
        "kafka_publish_duration_seconds",
        "Kafka publish latency",
        ["service", "topic"],
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1],
    )

class _Noop:
    def __getattr__(self, _): return lambda *a, **kw: self
    def labels(self, **_): return self
    def inc(self, *_): pass
    def observe(self, *_): pass
    def set(self, *_): pass

if not _PROM_AVAILABLE:
    for _name in [
        "HTTP_REQUESTS_TOTAL","HTTP_REQUEST_DURATION","HTTP_REQUESTS_IN_FLIGHT",
        "TOKENS_ISSUED","TOKEN_AMOUNT_ISSUED","SETTLEMENTS_PROCESSED",
        "SETTLEMENT_AMOUNT","SETTLEMENT_QUEUE_DEPTH","CONDITIONAL_PAYMENTS",
        "ESCROW_CONTRACTS","FX_SETTLEMENTS","FX_VOLUME","COMPLIANCE_EVENTS",
        "KAFKA_PUBLISHES","KAFKA_PUBLISH_LATENCY",
    ]:
        globals()[_name] = _Noop()


def instrument_app(app, service_name: str = "") -> None:
    """Attach Prometheus middleware and /metrics endpoint to a FastAPI app."""
    global SERVICE_NAME
    if service_name:
        SERVICE_NAME = service_name
    register_metrics(app)


def record_business_event(
    metric_name: str, labels: dict, value: float = 1.0
) -> None:
    """Increment a named business metric by looking it up in globals."""
    metric = globals().get(metric_name)
    if metric is not None:
        metric.labels(**labels).inc(value)


def register_metrics(app) -> None:
    """Attach Prometheus middleware and /metrics endpoint to a FastAPI app."""
    if not _PROM_AVAILABLE:
        @app.get("/metrics", include_in_schema=False)
        def _stub():
            return {"status": "prometheus_client not installed"}
        return

    from starlette.middleware.base import BaseHTTPMiddleware

    class PrometheusMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path == "/metrics":
                return await call_next(request)
            path   = request.url.path
            method = request.method
            HTTP_REQUESTS_IN_FLIGHT.labels(service=SERVICE_NAME).inc()
            start = time.perf_counter()
            try:
                response = await call_next(request)
                status   = str(response.status_code)
                return response
            except Exception:
                status = "500"
                raise
            finally:
                elapsed = time.perf_counter() - start
                HTTP_REQUESTS_IN_FLIGHT.labels(service=SERVICE_NAME).dec()
                HTTP_REQUESTS_TOTAL.labels(
                    service=SERVICE_NAME, method=method,
                    path=path, status_code=status,
                ).inc()
                HTTP_REQUEST_DURATION.labels(
                    service=SERVICE_NAME, method=method, path=path,
                ).observe(elapsed)

    app.add_middleware(PrometheusMiddleware)

    @app.get("/metrics", include_in_schema=False)
    def metrics_endpoint():
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
