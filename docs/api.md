# API reference

Every route, its method and the permission it requires. Permissions map to roles in
`src/munshi/auth/principal.py`; `session` means any signed-in user; `public` needs no token.
In development the interactive explorer is at `/api/docs`.

Authentication: `POST /api/session` returns a token; send it as `X-Session: <token>` (or
`Authorization: Bearer <token>`). Errors are `{"detail": "..."}` with a 4xx status; validation
errors are 422 with Pydantic's field list. Sign-in and sign-up are rate-limited per address.

| Route | Method | Permission | Notes |
|---|---|---|---|
| `/api/approvals` | GET | `approvals:read` |  |
| `/api/approvals/history` | GET | `approvals:read` |  |
| `/api/approvals/{approval_id}` | POST | `approvals:decide` |  |
| `/api/audit` | GET | `audit:read` |  |
| `/api/backup` | GET | `backup` | The business's whole SQLite file, consistent (VACUUM INTO), for the owner to keep. |
| `/api/badge` | GET | `notifications` | One cheap call the app polls: pending approvals I can act on, unread notifications, my stops. |
| `/api/chat` | POST | `chat` |  |
| `/api/chat/{thread_id}` | GET | `chat` |  |
| `/api/config` | GET | `public` | What the sign-in screen needs before anyone is signed in. |
| `/api/credit-notes` | POST | `settings:write` |  |
| `/api/customers` | GET | `customers:read` |  |
| `/api/customers` | POST | `customers:write` |  |
| `/api/customers/{customer_id}` | PATCH | `customers:write` |  |
| `/api/digest` | GET | `chat` |  |
| `/api/dispatch/suggest` | GET | `dispatch:write` |  |
| `/api/documents/{entry_id}` | GET | `khata:read` |  |
| `/api/documents/{entry_id}/html` | GET | `khata:read` |  |
| `/api/documents/{entry_id}/pdf` | GET | `khata:read` |  |
| `/api/driver/today` | GET | `stops:close` |  |
| `/api/expenses` | GET | `reports:read` |  |
| `/api/expenses` | POST | `expenses:write` |  |
| `/api/export.xlsx` | GET | `export` |  |
| `/api/import` | POST | `setup:write` |  |
| `/api/import/template.xlsx` | GET | `setup:write` |  |
| `/api/khata` | GET | `khata:read` |  |
| `/api/khata/{customer_id}` | GET | `khata:read` |  |
| `/api/me` | GET | `session` |  |
| `/api/me/pin` | POST | `session` |  |
| `/api/me/sessions` | GET | `session` |  |
| `/api/methods` | GET | `chat` |  |
| `/api/notifications` | GET | `notifications` |  |
| `/api/notifications/read` | POST | `notifications` |  |
| `/api/orders` | GET | `orders:read` |  |
| `/api/orders` | POST | `orders:create` | A form-entered order. Salesmen's orders stay drafts for the office; a clerk's or owner's is a draft too — confirmation is a separate tap. |
| `/api/orders/{order_id}` | GET | `orders:read` |  |
| `/api/orders/{order_id}` | PATCH | `orders:create` |  |
| `/api/orders/{order_id}/allocate` | POST | `dispatch:write` |  |
| `/api/orders/{order_id}/cancel` | POST | `dispatch:write` |  |
| `/api/orders/{order_id}/confirm` | POST | `dispatch:write` |  |
| `/api/outbox` | GET | `reminders:send` |  |
| `/api/outbox/deliver` | POST | `reminders:send` |  |
| `/api/outbox/{msg_id}/retry` | POST | `reminders:send` |  |
| `/api/outbox/{msg_id}/sent` | POST | `reminders:send` |  |
| `/api/payables` | GET | `purchases:read` |  |
| `/api/payments` | POST | `payments:write` |  |
| `/api/plans` | GET | `dispatch:read` |  |
| `/api/plans` | POST | `dispatch:write` |  |
| `/api/plans/{plan_id}` | GET | `dispatch:read` |  |
| `/api/plans/{plan_id}/approve` | POST | `dispatch:write` |  |
| `/api/plans/{plan_id}/cancel` | POST | `dispatch:write` |  |
| `/api/plans/{plan_id}/deposit` | POST | `payments:write` |  |
| `/api/plans/{plan_id}/loading-sheet` | GET | `dispatch:read` |  |
| `/api/products` | GET | `customers:read` |  |
| `/api/products` | POST | `setup:write` |  |
| `/api/products/{sku}` | PATCH | `setup:write` |  |
| `/api/promises` | GET | `khata:read` |  |
| `/api/promises` | POST | `khata:read` |  |
| `/api/purchases` | GET | `purchases:read` |  |
| `/api/purchases` | POST | `purchases:write` |  |
| `/api/reminders` | GET | `reminders:read` |  |
| `/api/reminders` | POST | `reminders:send` |  |
| `/api/reminders/due` | POST | `reminders:send` |  |
| `/api/reminders/{reminder_id}/discard` | POST | `reminders:send` |  |
| `/api/reminders/{reminder_id}/send` | POST | `reminders:send` |  |
| `/api/reports/cashbook` | GET | `reports:read` |  |
| `/api/reports/collections` | GET | `reports:read` |  |
| `/api/reports/profit` | GET | `reports:read` |  |
| `/api/reports/sales` | GET | `reports:read` |  |
| `/api/reports/slow-stock` | GET | `reports:read` |  |
| `/api/reports/stock-ledger/{sku}` | GET | `stock:read` |  |
| `/api/reports/stock-valuation` | GET | `reports:read` |  |
| `/api/reports/top-customers` | GET | `reports:read` |  |
| `/api/roles` | GET | `session` |  |
| `/api/routes` | GET | `customers:read` |  |
| `/api/routes` | POST | `setup:write` |  |
| `/api/routes/{route_id}` | DELETE | `setup:write` |  |
| `/api/session` | POST | `public` |  |
| `/api/session/logout` | POST | `session` |  |
| `/api/session/switch` | POST | `session` |  |
| `/api/settings` | GET | `settings:write` |  |
| `/api/settings` | PATCH | `settings:write` |  |
| `/api/signup` | POST | `public` |  |
| `/api/staff` | GET | `staff:manage` |  |
| `/api/staff` | POST | `staff:manage` |  |
| `/api/staff/{user_id}` | PATCH | `staff:manage` |  |
| `/api/staff/{user_id}/pin` | POST | `staff:manage` |  |
| `/api/staff/{user_id}/signout` | POST | `staff:manage` |  |
| `/api/statements/{customer_id}/html` | GET | `khata:read` |  |
| `/api/statements/{customer_id}/link` | GET | `khata:read` | A signed link the customer can open without an account. |
| `/api/stock` | GET | `stock:read` |  |
| `/api/stock/adjust` | POST | `settings:write` |  |
| `/api/stock/transfer` | POST | `dispatch:write` |  |
| `/api/stops/{stop_id}/close` | POST | `stops:close` |  |
| `/api/suppliers` | GET | `purchases:read` |  |
| `/api/suppliers` | POST | `purchases:write` |  |
| `/api/suppliers/{supplier_id}` | PATCH | `purchases:write` |  |
| `/api/suppliers/{supplier_id}/khata` | GET | `purchases:read` |  |
| `/api/suppliers/{supplier_id}/pay` | POST | `settings:write` |  |
| `/api/vehicles` | GET | `dispatch:read` |  |
| `/api/vehicles` | POST | `setup:write` |  |
| `/api/vehicles/{vehicle_id}` | DELETE | `setup:write` |  |
| `/api/voice` | POST | `chat` |  |
| `/api/warehouses` | GET | `customers:read` |  |
| `/api/warehouses` | POST | `setup:write` |  |
| `/healthz` | GET | `public` |  |
| `/i/{token}` | GET | `public` | The link a customer gets on WhatsApp: <business>.<entry>.<signature>. The signature covers both |
| `/s/{token}` | GET | `public` |  |

107 routes. Generated by `scripts/api_doc.py`.
