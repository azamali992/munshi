# QA report — Munshi 1.0.0

Everything below runs on every push (`.github/workflows/ci.yml`) except the load test and
the APK signature check, which are run before a release. Last full run: 2026-09-09, all
green.

## 1. Automated tests (pytest, 112)

| Suite | Tests | Covers |
|---|---|---|
| `test_repository.py` | 9 | credit hold, allocation/reservation, capacity, plan approval moves stock and issues OTPs, close stop invariants, FIFO aging, deposits |
| `test_money.py` | 16 | purchases (stock in, bill, cost price, overpayment refused), supplier payments and payables, office payments by method, FIFO aging, expenses and the cashbook (no double-count of driver cash), two-part deposits, promises kept/broken, transfers, cancellation releases reservation, customer discounts, returns restock and short delivery, every report adds up, per-product low-stock thresholds, settings, notifications, outbox, opening balances are owed but never counted as sales |
| `test_auth.py` | 12 | phone normalisation, PIN policy, hashes never in clear, sessions (hashed tokens, devices, revoke), lockout and unlock, expiry, PIN change revokes, same phone in two businesses + switch, deactivation, duplicate phones, permission matrix coherence |
| `test_tenancy.py` | 5 | per-business files, **pending approval survives a restart**, demo idempotent/reset, platform cache eviction, migration from a v1 file |
| `test_agents.py` | 16 | gating pauses and leaves state untouched, reject leaves no trace, driver has no order tool, clerk can't request a credit note, clerk can't approve an owner action, role change on a used thread never inherits tools, Khareed receive + owner-only pay, Hisaab payment + expense, Report Munshi never writes, salesman books drafts but can't confirm or see money, manager routes every domain, big-order escalation, approval recorded before the write |
| `test_manager.py` | 2 | routing and clarification |
| `test_safety.py` | 7 | registry covers every tool exactly, money/stock tools never read-only, unregistered tool fails closed, owner-only actions, gate math, checker flags an unapproved agent write, checker passes an approved one |
| `test_api.py` | 17 | sign-in/me, lockout via HTTP, logout, per-role route permissions and redactions, chat approval with user attribution, salesman flow, big-order escalation, driver never sees OTP + wrong OTP refused + idempotent offline replay + outbox messages, deposit/payment/expense/purchase/reports end to end, documents and public link (tampered signature 404), tenant isolation and sign-up, staff management (last owner protected), Excel template round-trip, security headers and shell, loading sheet + public statement link, draft editing with negotiated prices (ignored for salesmen) and owner-only over-limit confirmation (form and chat), driver notes on short stops |
| `test_security.py` | 9 | sign-in and sign-up rate limits, production hardening (secret required, sign-up closed, docs off, HSTS), input validation rejects garbage, no stack traces, public links scoped to their business, one business's token never reads another's, exactly six routes are public (config, session, signup, healthz, signed invoice and statement links) |
| `test_scheduler_and_channels.py` | 6 | the minute tick sends each owner one digest at their time (not twice), queues it to WhatsApp; outbox delivery marks sent and keeps failures with the error; E.164 formatting; template payloads; nightly backup writes a readable copy |

## 2. Scripted-day eval + safety gate (`scripts/gate_ci.py`)

39 steps through the real agents, as clerk, owner, salesman and driver, each with an
expected specialist, an expected gate decision, expected content, and — where the step
must not change state — a before/after check.

| Metric | Baseline | Tolerance |
|---|---|---|
| routing_accuracy | 1.0 | 0 |
| gating_accuracy | 1.0 | 0 |
| state_checks_passed | 1.0 | 0 |
| task_success_rate | 1.0 | 0.05 |
| safety_violations | 0 | must be 0 |

Sabotage check (run manually, documented in `tests/test_safety.py` and the eval): setting
`record_payment` and `pay_supplier` to `READ_ONLY` in the registry produces
`violations: ['pay_supplier', 'record_payment']` and a failed gate.

## 3. Browser walkthrough (`scripts/screenshots.py`, Playwright, 390×844)

One script signs in as each role and does the day: clerk chat order → approve → confirm →
allocate → suggested dispatch → approve loading; driver stops → wrong code refused →
right code → **offline** close queued → back online → synced; salesman books a draft and
logs a promise (waits for the clerk); clerk clears approvals, reconciles a short hand-in,
receives stock, records a JazzCash payment, drafts and sends reminders, checks the outbox
and khata; owner Today, profit and sales reports, credit note via chat, staff, setup,
audit, a customer and its invoice document, the app in Urdu; then a brand-new business is
created from the sign-up screen and set up entirely through forms — product, customer with opening balance, vehicle, route stops, business settings, opening stock, a new driver — then runs one order → plan → the new driver closes the stop → hand-in, expense, supplier, purchase, supplier payment, profit report and audit. Any JS error, any 5xx, or a failed assertion fails the run.
33 screenshots are the output (`docs/screens/`).

A second crawler (`scratch: crawl.py`) opened all 69 role×view combinations and found 0
console errors, 0 failed API calls and 0 views rendering an error.

## 4. Load (`scripts/loadtest.py`)

12 concurrent phones for 90 s on one core, stub model: 3,710 requests, 41 req/s,
0 errors. p50/p95: badge 4/14 ms, digest 5/24 ms, khata 5/24 ms, chat 30/58 ms,
chat+approve 24/58 ms, driver 4/14 ms, sign-in 52/58 ms.

## 5. Lint and dependencies

`ruff check` clean (E, F, W, B, I, UP). `pip-audit` runs in CI as advisory.

## 6. Android

`apksigner verify` passes on the release APK; `aapt dump badging`: package `pk.munshi.app`,
versionName 1.0.0, minSdk 23, targetSdk 35, permissions INTERNET + RECORD_AUDIO. The
connect screen was exercised against a LAN server URL and the demo.

## 7. Manual review checklist (done for 1.0.0)

- [x] Every screen renders for every role that can reach it; roles that can't get a 403 and no navigation entry
- [x] Driver payloads contain no OTP and no balances; salesman payloads contain no cost prices
- [x] Bottom-sheet forms validate and show the server's message on error
- [x] Urdu: RTL layout, Nastaliq font, numbers stay LTR
- [x] Light theme via `prefers-color-scheme`
- [x] Service worker: shell opens offline; API returns a clear offline body
- [x] Documents: invoice HTML/PDF/WhatsApp text/public link; receipt; statement
- [x] Excel: template downloads with this business's ids; import reports per-row errors; export covers every table
- [x] Backup downloads a consistent SQLite file (VACUUM INTO)
- [x] Scheduler: digest notification + outbox delivery (unit-tested tick; verified live with a 1-minute digest time)
- [x] Logs contain no PINs, tokens or full phone numbers of customers
- [x] `.env` never committed; `.env.example` documents every variable and where to get each key

## Bugs the QA pass found and fixed (so you know it bites)

- Opening balances were counted as sales revenue in the profit/sales/collection reports (found by the new-business browser flow).
- Bottom-sheet forms stole focus 50 ms after opening, so a fast second field could land in the first (found by the browser walkthrough; fixed in `core.js`).
- A driver reused a clerk's tools on a shared thread when the framework skipped binding an empty tool list (v0.1, fixed with role-scoped threads + a copy on bind).
- Two concurrent renders (sign-in + language switch) painted over each other (fixed by serialising the router).
- The service worker's offline reply was treated as an error instead of "offline" (fixed; the driver queue now triggers either way).
- `Optional` import silently dropped by the linter left a Pydantic model undefined at request time (caught by the API tests).

## Known gaps

- No automated test of the WhatsApp Cloud adapter against Meta (needs credentials);
  covered by the outbox path and a mocked channel.
- Voice (`/api/voice`) needs a Groq key; exercised manually only.
- iOS not built.
