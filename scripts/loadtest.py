#!/usr/bin/env python3
"""A small, honest load test against a running server: N concurrent phones
doing what phones do (badge polls, khata reads, chat turns, stop closes).
Reports throughput and latency percentiles per endpoint class.

    python3 scripts/loadtest.py http://127.0.0.1:8765 --users 30 --seconds 20
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time
import urllib.request

ap = argparse.ArgumentParser(); ap.add_argument("base", nargs="?", default="http://127.0.0.1:8765"); ap.add_argument("--users", type=int, default=20); ap.add_argument("--seconds", type=int, default=15)
A = ap.parse_args()
USERS = [("0300-0000001", "1111"), ("0300-0000002", "2222"), ("0300-0000003", "3333"), ("0300-0000004", "4444")]
lat: dict[str, list[float]] = {}; errors: dict[str, int] = {}; lock = threading.Lock()


def call(method, path, body=None, token=None, kind="?"):
    req = urllib.request.Request(A.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"X-Session": token} if token else {})})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode() or "null")
        ok = True
    except Exception:      # noqa: BLE001
        data = None; ok = False
        with lock: errors[kind] = errors.get(kind, 0) + 1
    with lock: lat.setdefault(kind, []).append((time.perf_counter() - t0) * 1000)
    return data if ok else None


def worker(i, stop):
    phone, pin = USERS[i % len(USERS)]
    time.sleep(i * 6.5)   # the sign-in limiter allows 10/min per address; real phones don't all sign in at once
    s = call("POST", "/api/session", {"phone": phone, "pin": pin, "device": f"load-{i}"}, kind="session")
    if not s: return
    tok, role = s["token"], s["me"]["role"]
    while time.time() < stop:
        r = random.random()
        if r < 0.45: call("GET", "/api/badge", token=tok, kind="badge")
        elif r < 0.65: call("GET", "/api/digest", token=tok, kind="digest")
        elif r < 0.8 and role != "driver": call("GET", "/api/khata", token=tok, kind="khata")
        elif r < 0.9 and role in ("clerk", "owner"):
            c = call("POST", "/api/chat", {"thread_id": f"load-{i}", "text": random.choice(["urea ka stock kitna hai", "kaun kitna baqi hai", "aaj ka cashbook", "profit this month"])}, token=tok, kind="chat")
        elif role in ("clerk", "owner"):
            c = call("POST", "/api/chat", {"thread_id": f"load-{i}", "text": "Chaudhry Farms ko 1 urea bhej do"}, token=tok, kind="chat+approve")
            if c and c.get("pending"): call("POST", f"/api/approvals/{c['pending']['approval_id']}", {"approve": False, "note": "load test"}, token=tok, kind="chat+approve")
        elif role == "driver": call("GET", "/api/driver/today", token=tok, kind="driver")
        else: call("GET", "/api/customers", token=tok, kind="customers")
        time.sleep(random.uniform(0.05, 0.3))


stop = time.time() + A.seconds
threads = [threading.Thread(target=worker, args=(i, stop), daemon=True) for i in range(A.users)]
t0 = time.time(); [t.start() for t in threads]; [t.join() for t in threads]; elapsed = time.time() - t0
total = sum(len(v) for v in lat.values())
print(f"{A.users} users · {A.seconds}s · {total} requests · {total / elapsed:.1f} req/s · errors {sum(errors.values())}")
print(f"{'endpoint':14}{'n':>6}{'p50 ms':>10}{'p95 ms':>10}{'max ms':>10}{'err':>6}")
for k, v in sorted(lat.items()):
    v.sort(); p = lambda q: v[min(len(v) - 1, int(q * len(v)))]   # noqa: E731
    print(f"{k:14}{len(v):>6}{statistics.median(v):>10.0f}{p(0.95):>10.0f}{v[-1]:>10.0f}{errors.get(k, 0):>6}")
