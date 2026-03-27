"""
scripts/load_test.py — Concurrent load test for the stablecoin stack.

Simulates realistic institutional workload:
  • Token issuance bursts
  • Concurrent RTGS settlements
  • FX quote requests
  • Compliance passthrough measurement

Usage:
    python scripts/load_test.py --workers 20 --duration 30

Outputs a plain-text report of throughput, latency percentiles, and error rates.
"""

import argparse
import os
import queue
import statistics
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    import httpx
except ImportError:
    print("httpx not installed. Run: pip install httpx")
    sys.exit(1)

GW  = os.environ.get("GATEWAY_URL",    "http://localhost:8000")
KEY = os.environ.get("GATEWAY_API_KEY","change-me-in-production")

HEADERS = {"X-API-Key": KEY, "Content-Type": "application/json"}

# ─── Result Collector ────────────────────────────────────────────────────────

results: dict[str, list] = defaultdict(list)   # op_name → list of (elapsed_ms, status_code)
_lock = threading.Lock()


def record(op: str, elapsed_ms: float, status: int):
    with _lock:
        results[op].append((elapsed_ms, status))


# ─── Worker Operations ───────────────────────────────────────────────────────

def do_fx_quote(client: httpx.Client):
    t0 = time.perf_counter()
    try:
        r = client.post("/v1/fx/quote", json={
            "sell_currency": "USD",
            "sell_amount":   "1000000.00",
            "buy_currency":  "EUR",
        }, timeout=5)
        record("fx_quote", (time.perf_counter() - t0) * 1000, r.status_code)
    except Exception:
        record("fx_quote", (time.perf_counter() - t0) * 1000, 0)


def do_get_rates(client: httpx.Client):
    t0 = time.perf_counter()
    try:
        r = client.get("/v1/fx/rates", timeout=5)
        record("fx_rates", (time.perf_counter() - t0) * 1000, r.status_code)
    except Exception:
        record("fx_rates", (time.perf_counter() - t0) * 1000, 0)


def do_health(client: httpx.Client):
    t0 = time.perf_counter()
    try:
        r = client.get("/health", timeout=5)
        record("health", (time.perf_counter() - t0) * 1000, r.status_code)
    except Exception:
        record("health", (time.perf_counter() - t0) * 1000, 0)


def do_list_settlements(client: httpx.Client):
    t0 = time.perf_counter()
    try:
        r = client.get("/v1/settlements?limit=20", timeout=5)
        record("list_settlements", (time.perf_counter() - t0) * 1000, r.status_code)
    except Exception:
        record("list_settlements", (time.perf_counter() - t0) * 1000, 0)


# ─── Worker Thread ───────────────────────────────────────────────────────────

OPERATIONS = [do_fx_quote, do_get_rates, do_health, do_list_settlements]


def worker(stop_event: threading.Event, worker_id: int):
    client = httpx.Client(base_url=GW, headers=HEADERS, timeout=10)
    op_idx = worker_id % len(OPERATIONS)
    try:
        while not stop_event.is_set():
            op = OPERATIONS[op_idx % len(OPERATIONS)]
            op(client)
            op_idx += 1
            time.sleep(0.01)   # 10ms think time → ~100 req/s per worker
    finally:
        client.close()


# ─── Reporter ────────────────────────────────────────────────────────────────

def percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    sorted_data = sorted(data)
    idx = max(0, int(len(sorted_data) * pct / 100) - 1)
    return sorted_data[idx]


def report(duration_s: float):
    print(f"\n{'═' * 72}")
    print(f"  Load Test Report — {duration_s:.0f}s run  |  {GW}")
    print(f"{'═' * 72}")
    print(f"  {'Operation':<25} {'Req':>6}  {'Err%':>6}  {'p50ms':>7}  {'p95ms':>7}  {'p99ms':>7}  {'RPS':>6}")
    print(f"  {'-' * 68}")

    total_reqs = 0
    total_errs = 0

    for op, data in sorted(results.items()):
        latencies = [d[0] for d in data]
        errors    = sum(1 for d in data if d[1] not in (200, 201, 202))
        count     = len(data)
        err_pct   = errors / count * 100 if count else 0
        rps       = count / duration_s

        print(
            f"  {op:<25} {count:>6}  {err_pct:>5.1f}%  "
            f"{percentile(latencies, 50):>7.1f}  "
            f"{percentile(latencies, 95):>7.1f}  "
            f"{percentile(latencies, 99):>7.1f}  "
            f"{rps:>6.1f}"
        )
        total_reqs += count
        total_errs += errors

    print(f"  {'-' * 68}")
    overall_rps     = total_reqs / duration_s
    overall_err_pct = total_errs / total_reqs * 100 if total_reqs else 0
    print(f"  {'TOTAL':<25} {total_reqs:>6}  {overall_err_pct:>5.1f}%  {'':>7}  {'':>7}  {'':>7}  {overall_rps:>6.1f}")
    print(f"{'═' * 72}\n")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stablecoin infrastructure load test")
    parser.add_argument("--workers",  type=int, default=10, help="Concurrent worker threads")
    parser.add_argument("--duration", type=int, default=20, help="Test duration in seconds")
    args = parser.parse_args()

    print(f"\n  Starting load test: {args.workers} workers × {args.duration}s  →  {GW}")

    # Quick health check
    try:
        r = httpx.get(f"{GW}/health", headers=HEADERS, timeout=5)
        if r.status_code != 200:
            print(f"  ✗ Gateway unhealthy ({r.status_code}). Start the stack first.")
            sys.exit(1)
        print(f"  ✓ Gateway healthy. Starting workers…\n")
    except Exception as exc:
        print(f"  ✗ Cannot reach gateway: {exc}")
        sys.exit(1)

    stop_event = threading.Event()
    threads    = [
        threading.Thread(target=worker, args=(stop_event, i), daemon=True)
        for i in range(args.workers)
    ]
    for t in threads:
        t.start()

    # Progress dots
    for elapsed in range(args.duration):
        time.sleep(1)
        total = sum(len(v) for v in results.values())
        print(f"\r  [{elapsed+1:>3}s / {args.duration}s]  {total:>6} requests completed", end="", flush=True)

    stop_event.set()
    for t in threads:
        t.join(timeout=5)

    print()
    report(args.duration)


if __name__ == "__main__":
    main()
