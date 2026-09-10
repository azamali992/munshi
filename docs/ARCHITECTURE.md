# Architecture

Munshi is one Python process, one data directory, and one web app. This document is
for an engineer joining the project: how a message becomes a write, where every rule
lives, and which decisions are deliberate.

## The shape

```
phone (PWA / Android shell)
   │  HTTPS, X-Session token
   ▼
web/app.py ── security headers · rate limits · request ids · scheduler
   │
web/deps.py ── token → Principal (user, business, role) → permission check → Ctx
   │
   ├─ web/routes/*  form-based writes → repository directly (human is actor + approver)
   │
   └─ platform.py  chat → Manager (routes) → Specialist (create_agent) ──┐
                                                                      │ tool call
              safety/middleware: role-gated tools + HITL from risk.py ◄─┘
                     │ gated? persist approval, return card          │ read-only or approved
                     ▼                                                ▼
              repository.approvals                          tools/core.py → domain/repository/*
                                                                      │
                                                    SQLite: data/tenants/<business>.db
                                                    checkpoints: data/tenants/<business>.agents.db
```

### Layers and the one rule between them

| Layer | Knows about | Never knows about |
|---|---|---|
| `domain/` (models, migrations, repository) | SQLite, business invariants | LLMs, HTTP, roles |
| `tools/core.py` | the repository | LangChain |
| `tools/langchain_tools.py`, `safety/`, `agents/`, `platform.py` | LangChain, the registry, roles | HTTP |
| `auth/`, `tenancy/` | the registry DB, files | agents |
| `web/` | everything above, via `Ctx` | SQL |
| `web/static/` | `/api/*` JSON | Python |

Every business rule lives in `domain/repository/`. Agents, the API and the tests all go
through it; nothing writes SQL anywhere else (the two exceptions — seed data and the
opening-balance import — are marked).

## One message, end to end

1. `POST /api/chat` → `deps.context("chat")` resolves the session token to a `Principal`
   (`auth/registry.py`), checks the `chat` permission (`auth/principal.py`), opens the
   business's platform (`tenancy/hub.py`, cached) and stamps the human's name on the
   repository so every audit row carries it.
2. `platform.handle_message()` takes the per-business lock, logs the message, and asks the
   **Manager** to classify. The Manager is a `create_agent` graph whose only tools are
   `route_to_*`; it returns a specialist name or `None` (clarify).
3. If that specialist already has a pending approval on this thread for this role, the
   message is answered without touching the paused graph.
4. The specialist graph runs with `{"messages": [...], "role": role}` and a thread id of
   `<thread>:<role>:<specialist>`. Two middlewares from `safety/middleware.py` shape the
   model request: `role_gated_tools` overrides the bound tool list with the role's list,
   and `role_gated_prompt` swaps the system prompt. The HITL middleware is generated from
   `safety/risk.py`: any tool tagged `LOW_RISK` or `HIGH_RISK` interrupts before executing.
5. On interrupt, `platform` builds a `PendingApproval`, decides who may approve it
   (`approver_for()`, escalated to the owner for orders above `big_order_limit`),
   **persists it** in the business's `approvals` table, writes a notification for that role,
   and returns the card text.
6. `POST /api/approvals/{id}` → `platform.resolve()`: permission check, then the approval is
   recorded and audited **before** the graph resumes (so the audit trail never shows a
   write ahead of its approval), then `Command(resume=...)` continues the graph from the
   SQLite checkpoint and the tool runs.
7. The tool (`tools/langchain_tools.py`) calls `tools/core.py` through `_Guarded`, which
   turns domain exceptions into `{"error": ...}` so the agent can answer instead of crash.
8. The repository does the write inside one transaction, audits it with actor, approver and
   user, and — where a customer should hear — queues a message in the outbox. The platform
   pushes the outbox through the channel when one is configured.

## Data

- **Registry** (`data/registry.db`): businesses, users (scrypt PIN hashes), sessions (token
  hashes). One file for the installation.
- **Tenant** (`data/tenants/<id>.db`): everything about one business. Schema is versioned
  (`domain/migrations.py`); opening a file applies whatever is missing.
- **Checkpoints** (`data/tenants/<id>.agents.db`): LangGraph's `SqliteSaver`. Losing this
  file loses paused graphs (the approval stays recorded as unexecuted) — not data.
- **Backups**: `VACUUM INTO` gives a consistent copy while running (`/api/backup`, `cli backup`).

Isolation is by file: a request that resolved to business A holds a repository that can
only see A's file. There is no cross-tenant query to get wrong.

## Concurrency

FastAPI runs sync routes on a thread pool. Each repository has a re-entrant lock around
transactions and every read; each platform has a lock around a whole agent turn, so one
business's turns are serialised (agents are not reentrant per thread anyway) while
different businesses proceed in parallel. SQLite runs in WAL mode with a 5 s busy timeout.
The measured cost: ~40 req/s and p95 < 60 ms for 12 concurrent phones on one core
(`scripts/loadtest.py`), which is far more than a distributor generates.

## The safety story, mechanically

- `safety/risk.py` is the **only** place a tool's tier is declared. `build_tools()` and the
  registry are tested to cover exactly the same set; an unregistered tool raises.
- `eval/run_eval.py::safety_violations()` reads the audit log chronologically and demands an
  `approval_granted` row (or an `otp:` approver) for every agent write in `ALWAYS_GATED` —
  a hardcoded set, deliberately not derived from the registry. `tests/test_safety.py` and
  the eval both prove that un-gating a tool in the registry produces named violations.
- `scripts/gate_ci.py` fails the build on any violation, and on any drop in routing,
  gating or state-check accuracy.

## The stub model

`llm/stub_model.py` implements `BaseChatModel` with rules: the first rule whose predicate
matches the latest human message decides; if that rule's tool is bound for the role it is
called, otherwise the role's prompt fallback answers (the way a real model refuses).
Tool results are summarised as `Done -- {json}` (or `Couldn't do that: …` for
`{"error": …}`), which the app renders into sentences. `LLM_PROVIDER=groq` replaces the
stub with `ChatGroq`; nothing else changes.

## The app

`web/static/` is a no-build PWA: `core.js` (state, API client, i18n, serialised router,
per-role navigation, bottom-sheet forms, the driver's offline queue, badge polling),
`views.js` (every screen), `i18n.js` (English/Urdu). The service worker caches the shell
network-first and answers `/api/*` with a 503 `offline` body when the network is gone, which
the client treats as offline and — for stop closes — queues with an idempotency key.

`mobile/` wraps the same app in a Capacitor Android shell that loads a server URL the user
enters once.

## Decisions worth knowing (ADRs in one line each)

- **SQLite per tenant, not Postgres with tenant ids** — isolation by construction, trivial
  backup/restore, zero ops for the target customer; revisit at a few hundred businesses.
- **Approvals persisted in the tenant DB + SqliteSaver** — a restart must never lose a
  pending action; the graph state and the business record of it live next to each other.
- **Form-based writes bypass the agent** — a clerk tapping *Confirm* is already the
  approver; routing it through an LLM would add latency and nothing else. Both paths hit
  the same repository and audit.
- **OTP is the customer's, delivered out of band** — the app is not the trust anchor for
  delivery; the customer is.
- **Stub model in CI, real model in production** — determinism where control flow is
  tested, comprehension where it is used.
- **No free text to customers, ever** — reminders, codes, invoices and receipts are
  templated; the model cannot compose a message to a customer.
