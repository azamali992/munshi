# Personas and the jobs they hire Munshi for

Four people touch the app every day. Each one gets a different app: different tools bound
to their agents, different screens, different approvals.

## Seth sahib — the owner

*Owns the business, owns the risk, rarely at a desk.*

Jobs: know today's cash and receivables without calling anyone; approve the things that
can hurt (stock adjustments, credit notes, supplier payments, big orders over a limit);
catch leakage the same day; not be the bottleneck for routine work.

What he checks: **Today** at 8pm — cash collected vs. deposited, who's over 60 days, low
stock. The **Approvals** badge. The **Audit** log when something feels off.

What would make him churn: an action he didn't approve; a number he can't reconcile with
his paper; a driver who could close a stop without the customer.

## Munshi ji — the clerk

*Runs the desk: takes orders, allocates stock, plans the vans, reconciles the drivers.*

Jobs: turn a WhatsApp voice note into an order in ten seconds; know what's in the godown
without walking there; build tomorrow's dispatch without a whiteboard; reconcile each
driver's cash against his stops in one screen; draft reminders without composing them.

Where the time goes today: re-typing orders, checking stock by phone, arguing about
cash shortfalls with no evidence, remembering who promised to pay when.

## Driver — on the route

*Loads the van, delivers, collects cash, comes back.*

Jobs: see his stops in order; record what was delivered, returned and collected in under
a minute per stop, one-handed, often with no signal; not be blamed for a shortfall he
didn't cause.

Non-negotiables: works offline and syncs later; big buttons; nothing he can't fix by
tapping back; the OTP is the customer's, so a closed stop is proof.

## Salesman / order booker — on the route

*Visits retailers, books orders, notes promises.*

Jobs: book an order at the counter and know immediately whether the customer is over
limit; see the khata before asking for money; log "he'll pay 50,000 on Friday" so the
office chases it.

He must not: confirm orders, touch stock, approve anything.

## The retailer (not a user, but the customer)

Receives an OTP for each delivery, a WhatsApp invoice after it, a templated reminder
when overdue. Never free text. Sees a public invoice link, never the app.

## Jobs the product must do end to end

| # | Job | Munshi surface |
|---|---|---|
| 1 | Take an order in Urdu/English/voice | Order Munshi → draft → clerk approval |
| 2 | Hold orders that breach credit | `CreditHoldError` → draft saved, owner decides |
| 3 | Allocate against real stock | Godown Munshi `allocate_order` |
| 4 | Plan the vans within capacity | `suggest_dispatch`, `create_dispatch_plan` |
| 5 | Load, and issue OTPs | `approve_dispatch_plan` |
| 6 | Deliver, return, collect — offline | Driver mode, `close_stop`, offline queue |
| 7 | Invoice the customer | Auto on close; PDF/WhatsApp share |
| 8 | Reconcile the driver's cash | Hisaab `record_deposit` with suspect attribution |
| 9 | Receive payments at the office / bank / JazzCash | Hisaab `record_payment` |
| 10 | Buy stock in, track what's owed to suppliers | Khareed Munshi `record_purchase`, `pay_supplier` |
| 11 | Record expenses, see the day's cashbook | Hisaab `record_expense`, Report Munshi |
| 12 | Age receivables, chase in the right tone | Wasooli `aging_report`, reminders, promises |
| 13 | See sales, margin, stock movement, top/slow items | Report Munshi, Reports screen, Excel export |
| 14 | Manage staff, customers, products, routes, vehicles | Setup screens, Excel import |
| 15 | Prove who did what | Audit log, per-user attribution |
| 16 | Keep the data | Backup download, self-host option |
