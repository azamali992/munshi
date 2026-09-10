# Roadmap

## 1.0 (this release)
Seven agents, four roles, multi-business sign-in, persisted approvals, purchases and
payables, office payments, expenses and cashbook, reports, invoices/receipts/statements,
WhatsApp adapter with outbox, Excel import/export, backup, Urdu, offline driver mode,
Android APK, CI gate, browser walkthrough.

## 1.1 — pharma & food (batch/expiry)
Batch and expiry on stock moves and purchases; FEFO allocation; expiry alerts in Today;
returns by batch.

## 1.2 — principal & schemes
Scheme/discount rules per product and customer tier; claim register against the
principal (manufacturer); target vs. achievement per salesman.

## 1.3 — trust & scale
SMS/WhatsApp OTP on a new device for owners; registry on Postgres with per-business SQLite
kept; multi-replica deployment; Play Store listing (AAB, privacy policy); iOS via Capacitor.

## 1.4 — money in
JazzCash/Easypaisa payment links on invoices with automatic receipt posting; bank
statement import for reconciliation.

## 2.0 — the network
A retailer-side view (my invoices, my balance, pay now) reached from the invoice link;
supplier-side ordering between two Munshi businesses.

Not planned: GPS tracking of drivers (the OTP proves delivery; owners asked for cash
control, not surveillance), free-text messaging to customers, an ERP connector.
