# Gap analysis → build plan

Reviewed from five seats. "v0.1" is the portfolio build that shipped first; each gap
below is closed in the version named.

## From the user's seat

| Gap in v0.1 | Why it matters | Closed in |
|---|---|---|
| One fixed demo business, three shared PINs | Nobody can *use* it for their own shop | v1.0 — businesses, named users, phone + PIN sign-in |
| No way to add a customer, product, vehicle or route in-app | Setup was code | v1.0 — Setup screens + Excel import |
| Same screens for every role | Driver scrolls past khata; owner sees a driver's form | v1.0 — role-specific home, nav and screens |
| Stock only ever went *out* (plus manual adjust) | No purchases → stock is fiction after a week | v1.0 — suppliers, purchases (GRN), payables |
| Cash only entered via a driver's deposit | Retailers pay at the counter, by bank, JazzCash | v1.0 — office payments with method + receipt |
| No expenses, no cashbook, no margin | Owner can't see if the day made money | v1.0 — expenses, cashbook, gross margin |
| No invoice | Customer gets nothing after delivery | v1.0 — invoice/receipt PDF + WhatsApp share, public link |
| English-only UI | Half the users read Urdu first | v1.0 — English/Urdu toggle, RTL |
| Driver needs signal to close a stop | Routes have no signal | v1.0 — offline queue, sync on reconnect |
| Approvals lost on restart | A pending order vanishes when the server reboots | v1.0 — SQLite checkpointer + persisted approvals |

## From the owner/client's seat

| Gap | Closed in |
|---|---|
| "Who else can see my data?" — no tenant isolation, no per-user attribution | v1.0 — one SQLite file per business, audit rows carry the user |
| "What if the phone is stolen?" — sessions never expire, PINs stored in env | v1.0 — hashed PINs, expiring sessions, owner can sign staff out |
| "Can I get my data out?" | v0.1 Excel export; v1.0 adds full backup download |
| "Does it work without internet?" | v1.0 — app shell and driver mode offline; LLM optional |
| "What does it cost me?" | v1.0 — pricing page and unit economics doc |

## From the engineering team's seat

| Gap | Closed in |
|---|---|
| One 480-line repository file | v1.0 — repository split by bounded context (master, orders, dispatch, cash, purchases, reports) |
| No schema migrations | v1.0 — versioned migrations, applied on open |
| Auth logic inline in the web layer | v1.0 — `auth/` package: Principal, permission matrix, registry, sessions |
| No structured logging, no request ids | v1.0 |
| No lint, no dependency audit in CI | v1.0 — ruff + pip-audit |
| No architecture/threat-model docs | v1.0 — ARCHITECTURE.md, SECURITY.md, ADRs |

## From the business-development seat

| Gap | Closed in |
|---|---|
| No landing page, no pricing, no pitch | v1.0 — site/, pricing, one-pager, pitch outline |
| No sales script or objection handling | v1.0 — docs/business/sales-playbook.md |
| No onboarding path for a new business | v1.0 — signup → setup checklist → sample data or Excel import |
| No channel to the customer | v1.0 — WhatsApp Cloud API adapter (env-configured; outbox fallback) |

## Deliberately not in v1.0

Batch/expiry tracking (pharma/food need it; agri-inputs and hardware don't — a v1.1
column on stock moves), GPS tracking of vehicles, multi-currency, GST/FBR e-invoicing
integration, native app-store builds, principal (manufacturer) claim management.
