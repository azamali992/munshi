"""LangChain @tool wrappers around MunshiTools, bound to one instance."""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from munshi.domain.repository import DOMAIN_ERRORS as _DOMAIN_ERRORS
from munshi.tools.core import MunshiTools


class _Guarded:
    """A business-rule refusal (wrong OTP, no stock, over credit limit) is
    information the agent should relay, not an exception that ends the turn.
    Every tool call goes through here so the agent gets {"error": ...} back."""

    def __init__(self, ops: MunshiTools) -> None:
        self._ops = ops

    def __getattr__(self, name):
        fn = getattr(self._ops, name)
        if not callable(fn):
            return fn

        def guarded(*a, **kw):
            try:
                return fn(*a, **kw)
            except _DOMAIN_ERRORS as e:
                return {"error": str(e).strip("'"), "kind": type(e).__name__}
            except NotImplementedError:
                # a part of the books not switched on in this build yet (payroll / finance / proofs landing in stages)
                return {"error": "that part of Munshi isn't available yet", "kind": "NotAvailable"}
        return guarded


def build_tools(ops: MunshiTools) -> dict[str, BaseTool]:
    ops = _Guarded(ops)
    @tool
    def find_customer(text: str) -> dict:
        """Look up a customer by the name exactly as the user wrote it (Urdu script, Roman Urdu or English, any spelling), or a phone or ID.
        Returns their ID, tier, credit limit and outstanding balance -- or, when several customers match, `ambiguous` with the candidates:
        then ask the user which one. Always call this to get a customer_id; never ask the user for an ID and never guess one."""
        return ops.find_customer(text)

    @tool
    def get_customer_khata(customer_id: str) -> dict:
        """A customer's khata: outstanding balance, aging bucket, and recent ledger entries."""
        return ops.get_customer_khata(customer_id)

    @tool
    def search_products(text: str) -> list:
        """Search the catalogue by one product word as the user wrote it (name, alias, Urdu name or SKU), e.g. 'urea', 'makai'.
        Use the SKU it returns; never ask the user for a SKU."""
        return ops.search_products(text)

    @tool
    def get_stock(sku: str = "") -> list:
        """Stock for a SKU at every godown: on hand, reserved, available. Leave sku empty for every product's stock
        (e.g. 'aaj ka stock', 'sab maal kitna hai')."""
        return ops.get_stock(sku)

    @tool
    def list_orders(status: str = "", sku: str = "", customer_id: str = "", days: int = 0) -> list:
        """List recent orders, newest first. Optional filters: status (draft, confirmed, allocated, dispatched, delivered, short),
        sku (orders containing that product), customer_id, days (1 = today, 7 = last week)."""
        return ops.list_orders(status, sku, customer_id, days)

    @tool
    def get_order(order_id: str) -> dict:
        """One order with its lines, total and status."""
        return ops.get_order(order_id)

    @tool
    def list_routes() -> list:
        """Delivery routes with their godown and customer stop order."""
        return ops.list_routes()

    @tool
    def list_vehicles() -> list:
        """Vehicles with capacity class and capacity in load units."""
        return ops.list_vehicles()

    @tool
    def get_plan(plan_id: str) -> dict:
        """A dispatch plan with its stops."""
        return ops.get_plan(plan_id)

    @tool
    def list_stops(plan_id: str) -> list:
        """The stops on a dispatch plan, in route order, with status and cash collected."""
        return ops.list_stops(plan_id)

    @tool
    def aging_report(limit: int = 30) -> list:
        """Receivables aging, most overdue first: amount, days overdue, bucket, any promise. Default top 30."""
        return ops.aging_report(limit)

    @tool
    def get_digest() -> dict:
        """Today's numbers: orders, dispatch, cash, receivables, low stock."""
        return ops.get_digest()

    @tool
    def suggest_dispatch(plan_date: str = "") -> list:
        """Propose today's dispatch: allocated orders grouped by route with the smallest vehicle that fits. Creates nothing."""
        return ops.suggest_dispatch(plan_date)

    @tool
    def create_order(customer_id: str, items: list[dict], source_text: str = "") -> dict:
        """Create a draft order. customer_id comes from find_customer, SKUs from search_products; each qty is the number the user wrote.
        items: [{"sku": "UREA-50", "qty": 20}]. The customer must confirm before it proceeds."""
        return ops.create_order(customer_id, items, source_text)

    @tool
    def update_order(order_id: str, items: list[dict]) -> dict:
        """Change a DRAFT order (never a confirmed one): items are ONLY the lines that change, each {"sku", "qty"} setting that
        product's quantity on the draft (a product not on it is added); lines not listed stay as they are. Use this -- never
        create_order -- when the user changes an order they already placed ('X ke order mei npk 15 kar do')."""
        return ops.update_order(order_id, items)

    @tool
    def confirm_order(order_id: str) -> dict:
        """Confirm a draft order after the customer has said yes."""
        return ops.confirm_order(order_id)

    @tool
    def cancel_order(order_id: str, reason: str = "") -> dict:
        """Cancel a draft, confirmed or allocated order (releases any reserved stock)."""
        return ops.cancel_order(order_id, reason)

    @tool
    def allocate_order(order_id: str, warehouse_id: str = "") -> dict:
        """Reserve stock for a confirmed order at a godown (default godown if none given)."""
        return ops.allocate_order(order_id, warehouse_id)

    @tool
    def create_dispatch_plan(route_id: str, vehicle_id: str, order_ids: list[str], plan_date: str = "") -> dict:
        """Plan a delivery run: allocated orders onto a route and vehicle. Checks capacity."""
        return ops.create_dispatch_plan(route_id, vehicle_id, order_ids, plan_date)

    @tool
    def approve_dispatch_plan(plan_id: str) -> dict:
        """Approve a plan: stock leaves the godown and each stop gets an OTP."""
        return ops.approve_dispatch_plan(plan_id)

    @tool
    def adjust_stock(warehouse_id: str, sku: str, delta: int, reason: str) -> dict:
        """Adjust stock at a godown (restock positive, write-off negative) with a reason."""
        return ops.adjust_stock(warehouse_id, sku, delta, reason)

    @tool
    def transfer_stock(from_warehouse: str, to_warehouse: str, sku: str, qty: int) -> dict:
        """Move stock between two godowns."""
        return ops.transfer_stock(from_warehouse, to_warehouse, sku, qty)

    @tool
    def close_stop(stop_id: str, delivered_items: list[dict], returned_items: list[dict], cash_collected: float, otp: str, note: str = "") -> dict:
        """Close a delivery stop with what was delivered, what came back, cash taken, the customer's OTP, and an optional note (e.g. why it was short)."""
        return ops.close_stop(stop_id, delivered_items, returned_items, cash_collected, otp, note)

    @tool
    def record_deposit(plan_id: str, amount_counted: float, counted_by: str = "cashier") -> dict:
        """Record the cash a driver handed in for a plan; returns the variance against stops and suspect stops."""
        return ops.record_deposit(plan_id, amount_counted, counted_by)

    @tool
    def credit_note(customer_id: str, amount: float, reason: str) -> dict:
        """Issue a credit note against a customer's khata."""
        return ops.credit_note(customer_id, amount, reason)

    @tool
    def reverse_ledger_entry(entry_id: str, reason: str) -> dict:
        """Cancel one customer khata entry (a bounced cheque, a payment keyed to the wrong customer, a wrong invoice) by its ID,
        e.g. RCP-2026-000012 or INV-2026-000031, with a reason. Posts the exact negation; the original stays. Each entry can be reversed once."""
        return ops.reverse_ledger_entry(entry_id, reason)

    @tool
    def reverse_expense(expense_id: str, reason: str) -> dict:
        """Cancel a mis-keyed expense by its ID (EXP-...), with a reason. Posts a negative expense today; the original stays. Re-record the correct amount separately."""
        return ops.reverse_expense(expense_id, reason)

    @tool
    def record_payment(customer_id: str, amount: float, method: str = "cash", ref: str = "") -> dict:
        """Record a payment received at the office or by bank/JazzCash/Easypaisa/cheque; posts to the khata and drafts a receipt.
        customer_id comes from find_customer; amount is the figure the user wrote (e.g. '50 hazar' = 50000); method only if the user said it."""
        return ops.record_payment(customer_id, amount, method, ref)

    @tool
    def record_expense(category: str, amount: float, note: str = "", method: str = "cash") -> dict:
        """Record a business expense (fuel, salary, rent, repair, utilities, loading, food, misc)."""
        return ops.record_expense(category, amount, note, method)

    @tool
    def cashbook(day: str = "") -> dict:
        """The day's cash in (customer payments, driver hand-ins) and cash out (expenses, supplier payments)."""
        return ops.cashbook(day)

    @tool
    def find_supplier(text: str) -> dict:
        """Look up a supplier by the name exactly as the user wrote it (any script or spelling), or a phone or ID, with what we owe them.
        When several match it returns `ambiguous` with the candidates: ask which one. Always call this to get a supplier_id; never ask the user for an ID."""
        return ops.find_supplier(text)

    @tool
    def list_suppliers() -> list:
        """All suppliers with current payable balance."""
        return ops.list_suppliers()

    @tool
    def supplier_khata(supplier_id: str) -> dict:
        """A supplier's account: balance, recent bills and payments, recent purchases."""
        return ops.supplier_khata(supplier_id)

    @tool
    def payables_report() -> list:
        """Every supplier we owe money to, largest first."""
        return ops.payables_report()

    @tool
    def record_purchase(supplier_id: str, items: list[dict], warehouse_id: str = "", invoice_ref: str = "", paid_amount: float = 0) -> dict:
        """Receive stock from a supplier: items [{"sku","qty","unit_cost"}] go into the godown and the bill goes on the supplier's account."""
        return ops.record_purchase(supplier_id, items, warehouse_id, invoice_ref, paid_amount)

    @tool
    def pay_supplier(supplier_id: str, amount: float, method: str = "cash", ref: str = "") -> dict:
        """Pay a supplier against their balance."""
        return ops.pay_supplier(supplier_id, amount, method, ref)

    @tool
    def reverse_purchase(purchase_id: str, reason: str) -> dict:
        """Undo a mis-keyed purchase by its ID (PUR-...), with a reason: the goods leave the godown again and the bill (and any payment made with it)
        come off the supplier's account. Refused if the goods are no longer all in stock."""
        return ops.reverse_purchase(purchase_id, reason)

    @tool
    def reverse_supplier_entry(entry_id: str, reason: str) -> dict:
        """Cancel one supplier-account entry by its ID (a payment SPY-... that bounced or went to the wrong supplier, a wrong opening-balance bill), with a reason.
        A bill that came with a purchase is undone with reverse_purchase instead."""
        return ops.reverse_supplier_entry(entry_id, reason)

    @tool
    def broken_promises() -> list:
        """Customers whose promised payment date has passed without the payment."""
        return ops.broken_promises()

    @tool
    def sales_report(start: str = "", end: str = "") -> dict:
        """Sales between two dates (default last 30 days): revenue, by day, by product with margin, by customer."""
        return ops.sales_report(start, end)

    @tool
    def profit_summary(start: str = "", end: str = "") -> dict:
        """Revenue, cost of goods, gross margin, expenses and net for a period (default last 30 days)."""
        return ops.profit_summary(start, end)

    @tool
    def collection_report(start: str = "", end: str = "") -> dict:
        """Invoiced vs collected for a period, by payment method, the payments themselves (who paid how much), plus the aging
        summary. For 'who paid today' use start = end = today."""
        return ops.collection_report(start, end)

    @tool
    def stock_ledger(sku: str, warehouse_id: str = "") -> dict:
        """Every movement of one product in and out of the godown(s), newest first."""
        return ops.stock_ledger(sku, warehouse_id)

    @tool
    def stock_valuation() -> dict:
        """What the stock on hand is worth at cost and at sale price."""
        return ops.stock_valuation()

    @tool
    def slow_stock(days: int = 30) -> list:
        """Products with stock on hand and no sale in the last N days."""
        return ops.slow_stock(days)

    @tool
    def top_customers(days: int = 30) -> list:
        """Customers by invoiced revenue over the last N days."""
        return ops.top_customers(days)

    @tool
    def draft_reminder(customer_id: str, tier: str = "") -> dict:
        """Draft a templated payment reminder (gentle/firm/final) for an overdue customer."""
        return ops.draft_reminder(customer_id, tier)

    @tool
    def draft_due_reminders(min_days_overdue: int = 1) -> list:
        """Draft templated reminders for every customer overdue by at least N days who has none waiting."""
        return ops.draft_due_reminders(min_days_overdue)

    @tool
    def send_reminder(reminder_id: str) -> dict:
        """Send a drafted reminder."""
        return ops.send_reminder(reminder_id)

    @tool
    def log_promise(customer_id: str, amount: float, promised_date: str) -> dict:
        """Record a customer's promise to pay an amount by a date."""
        return ops.log_promise(customer_id, amount, promised_date)

    # ------------------------------------------------------------------ tankhwa (payroll)
    @tool
    def list_employees(status: str = "active") -> dict:
        """The staff list: name, employee number, designation, status (pay figures only for the owner)."""
        return ops.list_employees(status)

    @tool
    def find_employee(text: str) -> dict:
        """Look up an employee by the name as the user wrote it; returns the match or the candidates to ask about."""
        return ops.find_employee(text)

    @tool
    def payroll_preview(period: str = "", employee_ids: list[str] | None = None) -> dict:
        """The month's payroll worked out but not booked: each employee's gross, deductions, commission and net pay.
        period 'YYYY-MM' (default this month); employee_ids to see only some."""
        return ops.payroll_preview(period, employee_ids)

    @tool
    def payroll_register(period: str = "", run_id: str = "") -> dict:
        """The approved payroll register (the salary sheet) of a month, default the latest."""
        return ops.payroll_register(period, run_id)

    @tool
    def payslip(employee_id: str, period: str = "") -> dict:
        """One employee's payslip for a month (default the latest)."""
        return ops.payslip(employee_id, period)

    @tool
    def my_payslips() -> dict:
        """The signed-in person's OWN payslips. Takes no name: whose slips is decided by who is signed in."""
        return ops.my_payslips()

    @tool
    def staff_advances_report(employee_id: str = "", status: str = "open") -> dict:
        """Staff advances and loans: given, recovered, outstanding -- for one employee or everyone."""
        return ops.staff_advances_report(employee_id, status)

    @tool
    def statutory_summary(kind: str = "eobi", period: str = "") -> dict:
        """What is due for a month by kind: eobi, ss (social security) or income_tax (salary tax withheld)."""
        return ops.statutory_summary(kind, period)

    @tool
    def record_attendance(period: str, rows: list[dict]) -> dict:
        """Record attendance for a month: rows [{"employee_id", "days_worked"?, "casual_leave"?, "annual_leave"?, "sick_leave"?, "unpaid_absent"?, "ot_minutes"?}].
        Days and minutes only -- never a rupee figure."""
        return ops.record_attendance(period, rows)

    @tool
    def add_employee(name: str, designation: str = "", phone: str = "", basic: float = 0.0, pay_basis: str = "monthly") -> dict:
        """Add an employee with their monthly basic pay (the amount the user wrote)."""
        return ops.add_employee(name, designation, phone, basic, pay_basis)

    @tool
    def update_employee(employee_id: str, changes: dict) -> dict:
        """Change an employee's details (name, phone, designation...)."""
        return ops.update_employee(employee_id, changes)

    @tool
    def rehire_employee(employee_id: str, rejoined_on: str = "") -> dict:
        """Take back an employee who had left."""
        return ops.rehire_employee(employee_id, rejoined_on)

    @tool
    def set_pay_structure(employee_id: str, pay_basis: str = "monthly", basic: float = 0.0, daily_rate: float = 0.0, effective_from: str = "") -> dict:
        """Set an employee's pay: monthly basic, or a daily rate."""
        return ops.set_pay_structure(employee_id, pay_basis, basic, daily_rate, effective_from)

    @tool
    def set_commission_rule(employee_id: str, basis: str, rate_pct: float = 0.0, per_unit: float = 0.0, sku: str = "", effective_from: str = "") -> dict:
        """Set a salesman's commission: a percent of sales or collections, or an amount per unit."""
        return ops.set_commission_rule(employee_id, basis, rate_pct, per_unit, sku, effective_from)

    @tool
    def end_employment(employee_id: str, reason: str = "", left_on: str = "") -> dict:
        """An employee has left: end their employment (their app login is switched off)."""
        return ops.end_employment(employee_id, reason, left_on)

    @tool
    def add_payroll_adjustment(employee_id: str, code: str, amount: float, note: str = "", period: str = "", ref: str = "") -> dict:
        """Add to or take from this month's pay: code bonus, fine, loss_recovery, other_earning or other_deduction; amount as the user
        wrote it. A loss_recovery names the cash-shortage entry it recovers (ref, EXP-...)."""
        return ops.add_payroll_adjustment(employee_id, code, amount, note, period, ref)

    @tool
    def void_payroll_adjustment(adj_id: str) -> dict:
        """Cancel a payroll adjustment (ADJ-...) before the month is approved."""
        return ops.void_payroll_adjustment(adj_id)

    @tool
    def approve_payroll_run(period: str, fingerprint: str) -> dict:
        """Approve (book) a month's payroll exactly as previewed: fingerprint comes from payroll_preview."""
        return ops.approve_payroll_run(period, fingerprint)

    @tool
    def reverse_payroll_run(run_id: str, reason: str) -> dict:
        """Reverse an approved payroll run (PAY-...) with a reason."""
        return ops.reverse_payroll_run(run_id, reason)

    @tool
    def pay_salaries(run_id: str, payments: list[dict]) -> dict:
        """Pay salaries of an approved run: payments [{"employee_id", "method", "amount"?}] (bank, jazzcash, easypaisa, cheque)."""
        return ops.pay_salaries(run_id, payments)

    @tool
    def reverse_salary_payment(payment_id: str, reason: str) -> dict:
        """Reverse a salary payment (SPM-...) with a reason."""
        return ops.reverse_salary_payment(payment_id, reason)

    @tool
    def give_staff_advance(employee_id: str, amount: float, method: str, installment: float = 0.0, note: str = "") -> dict:
        """Give an employee an advance: the amount and the method the user said (bank, jazzcash, easypaisa, cheque)."""
        return ops.give_staff_advance(employee_id, amount, method, installment, note)

    @tool
    def repay_staff_advance(employee_id: str, amount: float, method: str) -> dict:
        """An employee paid back part of an advance directly."""
        return ops.repay_staff_advance(employee_id, amount, method)

    @tool
    def reverse_staff_advance(advance_id: str, reason: str) -> dict:
        """Reverse a staff advance (ADV-...) with a reason."""
        return ops.reverse_staff_advance(advance_id, reason)

    @tool
    def record_statutory_payment(kind: str, period: str, amount: float, method: str, challan_ref: str, paid_on: str = "") -> dict:
        """Record a challan paid: EOBI, social security or salary tax, for a month."""
        return ops.record_statutory_payment(kind, period, amount, method, challan_ref, paid_on)

    @tool
    def add_statutory_rate(key: str, value: str, effective_from: str, source: str, verified_on: str, source_url: str = "", grade: str = "C", note: str = "") -> dict:
        """Add a statutory rate override (minimum wage, EOBI rate...) with its source and the date it was checked."""
        return ops.add_statutory_rate(key, value, effective_from, source, verified_on, source_url, grade, note)

    @tool
    def set_payroll_settings(changes: dict) -> dict:
        """Change payroll settings (profile plc_2026 / legacy_1969, province, registrations, pay day)."""
        return ops.set_payroll_settings(changes)

    # ------------------------------------------------------------------ accounts (company finance)
    @tool
    def money_accounts() -> dict:
        """Every money account (cash in hand, banks, wallets) with its balance now."""
        return ops.money_accounts()

    @tool
    def account_book(account_id: str, start: str = "", end: str = "") -> dict:
        """One money account's book: money in and out with the running balance (default the last 30 days)."""
        return ops.account_book(account_id, start, end)

    @tool
    def reconciliation_status(account_id: str, statement_date: str = "") -> dict:
        """A bank account's reconciliation with its statement."""
        return ops.reconciliation_status(account_id, statement_date)

    @tool
    def trial_balance(as_of: str = "") -> dict:
        """The trial balance (owner)."""
        return ops.trial_balance(as_of)

    @tool
    def income_statement(start: str = "", end: str = "") -> dict:
        """Profit and loss for a period (default this month to date)."""
        return ops.income_statement(start, end)

    @tool
    def balance_sheet(as_of: str = "") -> dict:
        """The company's balance sheet (default today)."""
        return ops.balance_sheet(as_of)

    @tool
    def cash_flow(start: str = "", end: str = "") -> dict:
        """Where the money came from and went (default this month to date)."""
        return ops.cash_flow(start, end)

    @tool
    def owner_kpis(as_of: str = "") -> dict:
        """The owner's key numbers: margin, DSO, DPO, stock days, payroll share, collection rate."""
        return ops.owner_kpis(as_of)

    @tool
    def margins_report(by: str = "product", start: str = "", end: str = "") -> dict:
        """Margin by product, customer or route over a period."""
        return ops.margins_report(by, start, end)

    @tool
    def fixed_assets_register(as_of: str = "") -> dict:
        """Fixed assets with cost, depreciation and book value."""
        return ops.fixed_assets_register(as_of)

    @tool
    def loans_report(as_of: str = "") -> dict:
        """Loans taken: received, repaid, outstanding."""
        return ops.loans_report(as_of)

    @tool
    def period_status() -> dict:
        """Which months are closed."""
        return ops.period_status()

    @tool
    def list_attachments(entity: str, entity_id: str) -> list:
        """The payment proofs linked to an entry."""
        return ops.list_attachments(entity, entity_id)

    @tool
    def transfer_between_accounts(from_account: str, to_account: str, amount: float, ref: str = "", note: str = "") -> dict:
        """Move money between the business's own accounts (cash to bank, bank to cash, bank to wallet)."""
        return ops.transfer_between_accounts(from_account, to_account, amount, ref, note)

    @tool
    def count_cash(account_id: str, counted: float, note: str = "") -> dict:
        """Record a cash count of the galla (the amount the user counted)."""
        return ops.count_cash(account_id, counted, note)

    @tool
    def mark_cleared(account_id: str, items: list[dict], cleared_on: str = "", cleared: bool = True) -> dict:
        """Tick bank entries as cleared against the statement."""
        return ops.mark_cleared(account_id, items, cleared_on, cleared)

    @tool
    def save_reconciliation(account_id: str, statement_date: str, statement_balance: float) -> dict:
        """Save a bank reconciliation with the statement balance."""
        return ops.save_reconciliation(account_id, statement_date, statement_balance)

    @tool
    def add_money_account(kind: str, name: str, provider: str = "", number_last4: str = "", opening_balance: float = 0.0) -> dict:
        """Add a money account (cash, bank or wallet)."""
        return ops.add_money_account(kind, name, provider, number_last4, opening_balance)

    @tool
    def set_method_route(method: str, account_id: str, effective_from: str = "") -> dict:
        """Which account 'bank' / 'jazzcash' / ... money lands in."""
        return ops.set_method_route(method, account_id, effective_from)

    @tool
    def record_capital(amount: float, method: str = "cash", note: str = "") -> dict:
        """The owner put money into the business."""
        return ops.record_capital(amount, method, "", note)

    @tool
    def record_drawing(amount: float, method: str = "cash", note: str = "") -> dict:
        """The owner took money out of the business for himself (drawings)."""
        return ops.record_drawing(amount, method, "", note)

    @tool
    def record_loan(lender: str, amount: float, method: str = "bank", kind: str = "informal", terms: str = "") -> dict:
        """A loan received: the lender as the user named them, the amount, and how it came in."""
        return ops.record_loan(lender, amount, method, kind, "", terms)

    @tool
    def repay_loan(loan_id: str, principal: float, interest: float = 0.0, method: str = "bank") -> dict:
        """Repay part of a loan (LN-...)."""
        return ops.repay_loan(loan_id, principal, interest, method)

    @tool
    def add_fixed_asset(name: str, cost: float, category: str = "vehicle", life_months: int = 60, funded_by: str = "paid", method: str = "") -> dict:
        """A fixed asset bought (a vehicle, a generator...): name and cost as the user wrote them."""
        return ops.add_fixed_asset(name, cost, category, life_months, "", funded_by, method)

    @tool
    def dispose_fixed_asset(asset_id: str, proceeds: float, method: str = "cash") -> dict:
        """A fixed asset sold or scrapped."""
        return ops.dispose_fixed_asset(asset_id, proceeds, method)

    @tool
    def run_depreciation(through_period: str) -> dict:
        """Book depreciation up to a month ('YYYY-MM')."""
        return ops.run_depreciation(through_period)

    @tool
    def post_journal_entry(entry_date: str, memo: str, lines: list[dict], kind: str = "general") -> dict:
        """A manual journal entry (owner): lines [{"code", "debit"|"credit"}] that balance."""
        return ops.post_journal_entry(entry_date, memo, lines, kind)

    @tool
    def reverse_journal_entry(je_id: str, reason: str) -> dict:
        """Reverse a journal entry (JV-...)."""
        return ops.reverse_journal_entry(je_id, reason)

    @tool
    def reverse_account_transfer(transfer_id: str, reason: str) -> dict:
        """Reverse a transfer between own accounts (XFR-...)."""
        return ops.reverse_account_transfer(transfer_id, reason)

    @tool
    def post_cash_difference(count_id: str) -> dict:
        """Book the difference a cash count found (CC-...)."""
        return ops.post_cash_difference(count_id)

    @tool
    def record_opening_balances(as_of: str, money: list[dict], assets: list[dict] | None = None, loans: list[dict] | None = None) -> dict:
        """The books' opening balances: money accounts, assets and loans on a date."""
        return ops.record_opening_balances(as_of, money, assets, loans)

    @tool
    def close_period(through_date: str, note: str = "") -> dict:
        """Close the books through a date (a month end): nothing earlier can change after."""
        return ops.close_period(through_date, note)

    @tool
    def reopen_period(close_id: int, reason: str) -> dict:
        """Reopen a closed period, with a reason."""
        return ops.reopen_period(close_id, reason)

    money_tools = [list_employees, find_employee, payroll_preview, payroll_register, payslip, my_payslips, staff_advances_report, statutory_summary,
                   record_attendance, add_employee, update_employee, rehire_employee, set_pay_structure, set_commission_rule, end_employment,
                   add_payroll_adjustment, void_payroll_adjustment, approve_payroll_run, reverse_payroll_run, pay_salaries, reverse_salary_payment,
                   give_staff_advance, repay_staff_advance, reverse_staff_advance, record_statutory_payment, add_statutory_rate, set_payroll_settings,
                   money_accounts, account_book, reconciliation_status, trial_balance, income_statement, balance_sheet, cash_flow, owner_kpis,
                   margins_report, fixed_assets_register, loans_report, period_status, list_attachments, transfer_between_accounts, count_cash,
                   mark_cleared, save_reconciliation, add_money_account, set_method_route, record_capital, record_drawing, record_loan, repay_loan,
                   add_fixed_asset, dispose_fixed_asset, run_depreciation, post_journal_entry, reverse_journal_entry, reverse_account_transfer,
                   post_cash_difference, record_opening_balances, close_period, reopen_period]

    all_tools = money_tools + [find_customer, get_customer_khata, search_products, get_stock, list_orders, get_order,
                 list_routes, list_vehicles, get_plan, list_stops, aging_report, get_digest, suggest_dispatch,
                 create_order, update_order, confirm_order, cancel_order, allocate_order, create_dispatch_plan, approve_dispatch_plan,
                 adjust_stock, transfer_stock, close_stop, record_deposit, credit_note, record_payment, record_expense, cashbook,
                 reverse_ledger_entry, reverse_expense,
                 find_supplier, list_suppliers, supplier_khata, payables_report, record_purchase, pay_supplier,
                 reverse_purchase, reverse_supplier_entry,
                 draft_reminder, draft_due_reminders, send_reminder, log_promise, broken_promises,
                 sales_report, profit_summary, collection_report, stock_ledger, stock_valuation, slow_stock, top_customers]
    return {t.name: t for t in all_tools}
