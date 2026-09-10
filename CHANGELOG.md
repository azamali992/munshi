# Changelog

## 1.0.0 — 2026-09-09

The product release. Everything below is new since 0.1 (the portfolio build).

**Businesses and people** — sign up a business from the app; owner, clerk, salesman and
driver roles with phone + PIN sign-in (scrypt, lockout, expiring sessions, remote sign-out);
staff management; one SQLite file per business; Excel import with a template; backup download.

**Agents** — two new munshis (Khareed for purchases and payables, Report for read-only
analytics); every agent now knows all four roles; pending approvals persist across
restarts (SQLite checkpointer + approvals table); big orders escalate to the owner; every
audit row names the human behind it.

**Money and stock** — draft editing and negotiated line prices (owner/clerk); over-limit orders need the owner to confirm, in the app and in chat; driver notes on stops; office payments by method with receipts; expenses and a cashbook;
purchases that put stock in and the bill on the supplier; supplier payments (owner);
stock transfers and a full movement ledger; order cancellation; customer discounts and
credit days; low-stock thresholds per product; promises kept/broken.

**Documents and channels** — invoice, receipt and statement as HTML/PDF with signed public
links; loading sheet for the godown; public statement links; WhatsApp Cloud API adapter (text or approved template) with an outbox, retry and one-tap `wa.me` fallback; delivery
codes, invoices, receipts and reminders queued automatically; owner's evening digest.

**App** — role-specific home screens and navigation; bottom-sheet forms for everything;
reports with charts; Urdu (RTL, Nastaliq); driver offline queue with idempotent replay;
notifications; light theme; Android APK (Capacitor shell).

**Engineering** — nightly backups; repository split by bounded context; versioned migrations; permission
matrix; security headers, rate limits, production mode; JSON logs with request ids; CLI;
39-step eval with a registry-independent safety audit; 102 tests; browser walkthrough of
all four roles in CI; load test; ruff + pip-audit; docs: architecture, security/threat
model, deployment, QA, user guide (EN/UR), business (market, personas, pricing, GTM,
sales playbook, competitors, roadmap); landing site.

## 0.1.0 — 2026-09-08

Five munshis + manager, PWA with three demo PINs, 21-step eval, 45 tests.
