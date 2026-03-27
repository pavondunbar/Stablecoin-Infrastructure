"""
services/api-gateway/main.py
──────────────────────────────
API Gateway — sole internet-facing entry point

Responsibilities:
  • API-key authentication on every incoming request
  • Rate limiting per client key
  • Request routing to internal microservices (token-issuance, rtgs,
    payment-engine, fx-settlement) via httpx reverse proxy
  • Response pass-through with upstream error normalisation
  • Structured access logging to Kafka audit trail
  • Health aggregation across all downstream services

Trust boundary: this is the ONLY service with a port binding to the host
(docker-compose `ports:` mapping).  All other services are on the internal
Docker network only and cannot be reached from outside.
"""

import logging
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import sys
sys.path.insert(0, "/app/shared")

import kafka_client as kafka
from metrics import instrument_app
from events import AuditTrailEntry

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ─── Configuration ─────────────────────────────────────────────────────────────

SERVICE           = os.environ.get("SERVICE_NAME", "api-gateway")
GATEWAY_API_KEY   = os.environ["GATEWAY_API_KEY"]

UPSTREAM = {
    "token":   os.environ.get("TOKEN_ISSUANCE_URL", "http://token-issuance:8001"),
    "rtgs":    os.environ.get("RTGS_URL",            "http://rtgs:8002"),
    "payment": os.environ.get("PAYMENT_ENGINE_URL",  "http://payment-engine:8003"),
    "fx":      os.environ.get("FX_SETTLEMENT_URL",   "http://fx-settlement:8004"),
}

# ─── Rate Limiter (in-memory, per API key) ────────────────────────────────────

class SimpleRateLimiter:
    """Sliding window rate limiter — 1000 requests per 60-second window per key."""
    def __init__(self, window_secs: int = 60, max_requests: int = 1000):
        self.window    = window_secs
        self.max_req   = max_requests
        self._buckets: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now    = time.monotonic()
        cutoff = now - self.window
        bucket = self._buckets[key]
        # Purge expired timestamps
        self._buckets[key] = [t for t in bucket if t > cutoff]
        if len(self._buckets[key]) >= self.max_req:
            return False
        self._buckets[key].append(now)
        return True


limiter = SimpleRateLimiter()

# ─── HTTP Client ──────────────────────────────────────────────────────────────

_http_client: Optional[httpx.AsyncClient] = None


async def get_client() -> httpx.AsyncClient:
    return _http_client


# ─── Auth Dependency ──────────────────────────────────────────────────────────

async def require_api_key(request: Request):
    key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if not key or key != GATEWAY_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    if not limiter.is_allowed(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Max 1000 requests per 60 seconds.",
        )
    return key


# ─── Proxy Helper ─────────────────────────────────────────────────────────────

async def _proxy(
    request: Request,
    upstream_base: str,
    path: str,
    client: httpx.AsyncClient,
) -> Response:
    """Proxy the incoming request to an upstream service."""
    url    = f"{upstream_base}{path}"
    body   = await request.body()
    params = dict(request.query_params)
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "x-api-key", "content-length")
    }
    headers["X-Request-ID"] = request.state.request_id

    try:
        upstream_resp = await client.request(
            method=request.method,
            url=url,
            content=body,
            params=params,
            headers=headers,
            timeout=30.0,
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail=f"Upstream unavailable: {upstream_base}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Upstream timeout")

    return Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=dict(upstream_resp.headers),
        media_type=upstream_resp.headers.get("content-type", "application/json"),
    )


# ─── Middleware ───────────────────────────────────────────────────────────────

async def audit_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()

    response = await call_next(request)

    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)

    # Async fire-and-forget audit log to Kafka
    try:
        kafka.publish_dict(
            "audit.trail",
            {
                "event_id":     request_id,
                "actor_service": SERVICE,
                "action":       f"{request.method} {request.url.path}",
                "entity_type":  "http_request",
                "entity_id":    request_id,
                "after_state": {
                    "status_code": response.status_code,
                    "elapsed_ms":  elapsed_ms,
                    "path":        str(request.url.path),
                    "method":      request.method,
                },
                "ip_address":   request.client.host if request.client else None,
                "event_time":   datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        pass  # Never fail a request because of audit logging

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
    log.info("%s %s → %d  %.1fms  req=%s",
             request.method, request.url.path, response.status_code, elapsed_ms, request_id)
    return response


# ─── App ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    _http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        timeout=httpx.Timeout(30.0),
    )
    log.info("API Gateway started. Upstreams: %s", UPSTREAM)
    yield
    await _http_client.aclose()
    log.info("API Gateway shut down.")


app = FastAPI(
    title="Stablecoin Infrastructure — API Gateway",
    version="1.0.0",
    description=(
        "Unified gateway for JPM Coin / PYUSD-style tokenized deposit infrastructure. "
        "Provides token issuance, RTGS settlement, programmable payments, and FX rails."
    ),
    lifespan=lifespan,
)

app.middleware("http")(audit_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # restrict to known client domains in production
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health", tags=["Meta"])
async def health(client: httpx.AsyncClient = Depends(get_client)):
    """Aggregate health check across all downstream services."""
    checks = {}
    for name, base in UPSTREAM.items():
        try:
            r = await client.get(f"{base}/health", timeout=3.0)
            checks[name] = "ok" if r.status_code == 200 else f"degraded ({r.status_code})"
        except Exception as exc:
            checks[name] = f"unreachable: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 207,
        content={"gateway": "ok", "services": checks},
    )


# ─── Token Issuance Routes ────────────────────────────────────────────────────

@app.api_route(
    "/v1/tokens/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    tags=["Token Issuance"],
    dependencies=[Depends(require_api_key)],
)
async def token_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Token Issuance Service.
    Endpoints: /tokens/issue  /tokens/redeem  /tokens/balance/{account_id}  /accounts
    """
    return await _proxy(request, UPSTREAM["token"], f"/tokens/{path}", client)


@app.api_route(
    "/v1/accounts/{path:path}",
    methods=["GET", "POST"],
    tags=["Token Issuance"],
    dependencies=[Depends(require_api_key)],
)
async def accounts_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(request, UPSTREAM["token"], f"/accounts/{path}", client)


@app.api_route(
    "/v1/accounts",
    methods=["POST"],
    tags=["Token Issuance"],
    dependencies=[Depends(require_api_key)],
)
async def accounts_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(request, UPSTREAM["token"], "/accounts", client)


# ─── RTGS Routes ──────────────────────────────────────────────────────────────

@app.api_route(
    "/v1/settlements/{path:path}",
    methods=["GET", "POST"],
    tags=["RTGS Settlement"],
    dependencies=[Depends(require_api_key)],
)
async def rtgs_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the RTGS Settlement Service.
    Endpoints: /settlements/submit  /settlements/{ref}  /settlements
    """
    return await _proxy(request, UPSTREAM["rtgs"], f"/settlements/{path}", client)


@app.api_route(
    "/v1/settlements",
    methods=["GET", "POST"],
    tags=["RTGS Settlement"],
    dependencies=[Depends(require_api_key)],
)
async def rtgs_root_proxy(
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    return await _proxy(request, UPSTREAM["rtgs"], "/settlements", client)


# ─── Payment Engine Routes ────────────────────────────────────────────────────

@app.api_route(
    "/v1/payments/{path:path}",
    methods=["GET", "POST"],
    tags=["Programmable Payments"],
    dependencies=[Depends(require_api_key)],
)
async def payment_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the Payment Engine.
    Endpoints: /payments/conditional  /payments/escrow  /payments/escrow/{ref}/release
    """
    return await _proxy(request, UPSTREAM["payment"], f"/payments/{path}", client)


# ─── FX Settlement Routes ─────────────────────────────────────────────────────

@app.api_route(
    "/v1/fx/{path:path}",
    methods=["GET", "POST"],
    tags=["FX Settlement"],
    dependencies=[Depends(require_api_key)],
)
async def fx_proxy(
    path: str,
    request: Request,
    client: httpx.AsyncClient = Depends(get_client),
):
    """
    Proxy to the FX Settlement Service.
    Endpoints: /fx/rates  /fx/quote  /fx/settle  /fx/settlements/{ref}
    """
    return await _proxy(request, UPSTREAM["fx"], f"/fx/{path}", client)


# ─── API Reference ────────────────────────────────────────────────────────────

@app.get("/", tags=["Meta"])
def root():
    return {
        "name":    "Stablecoin & Digital Cash Infrastructure",
        "version": "1.0.0",
        "docs":    "/docs",
        "endpoints": {
            "token_issuance": {
                "POST /v1/accounts":                     "Onboard institutional participant",
                "POST /v1/tokens/issue":                 "Issue tokenized deposits",
                "POST /v1/tokens/redeem":                "Redeem tokens back to fiat",
                "GET  /v1/tokens/balance/{account_id}":  "Get all token balances",
            },
            "rtgs": {
                "POST /v1/settlements":                  "Submit gross settlement instruction",
                "GET  /v1/settlements/{ref}":            "Get settlement status",
                "GET  /v1/settlements?status=queued":    "List settlements by status",
            },
            "programmable_payments": {
                "POST /v1/payments/conditional":                    "Create conditional payment",
                "POST /v1/payments/conditional/{ref}/trigger":      "Trigger condition check",
                "POST /v1/payments/escrow":                         "Create escrow contract",
                "POST /v1/payments/escrow/{ref}/release":           "Release or refund escrow",
            },
            "fx_settlement": {
                "GET  /v1/fx/rates":                     "Live FX rate book",
                "POST /v1/fx/quote":                     "Get FX quote with spread",
                "POST /v1/fx/settle":                    "Execute cross-border FX settlement",
                "GET  /v1/fx/settlements/{ref}":         "Get FX settlement status",
            },
        },
        "auth": "Pass X-API-Key header on all /v1/* requests",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, log_level="info")
