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
        return guarded


def build_tools(ops: MunshiTools) -> dict[str, BaseTool]:
    ops = _Guarded(ops)
    @tool
    def find_customer(text: str) -> dict:
        """Find a customer by name, phone or ID. Returns their ID, tier, credit limit and outstanding balance."""
        return ops.find_customer(text)

    @tool
    def get_customer_khata(customer_id: str) -> dict:
        """A customer's khata: outstanding balance, aging bucket, and recent ledger entries."""
        return ops.get_customer_khata(customer_id)

    @tool
    def search_products(text: str) -> list:
        """Search the catalogue by name, alias (including Urdu names) or SKU."""
        return ops.search_products(text)

    @tool
    def get_stock(sku: str) -> list:
        """Stock for a SKU at every godown: on hand, reserved, available."""
        return ops.get_stock(sku)

    @tool
    def list_orders(status: str = "") -> list:
        """List recent orders, optionally by status (draft, confirmed, allocated, dispatched, delivered, short)."""
        return ops.list_orders(status)

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
        """Create a draft order. items: [{"sku": "UREA-50", "qty": 20}]. The customer must confirm before it proceeds."""
        return ops.create_order(customer_id, items, source_text)

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
        """Record a payment received at the office or by bank/JazzCash/Easypaisa/cheque; posts to the khata and drafts a receipt."""
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
        """Find a supplier by name, phone or ID, with what we owe them."""
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
        """Invoiced vs collected for a period, by payment method, plus the aging summary."""
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

    all_tools = [find_customer, get_customer_khata, search_products, get_stock, list_orders, get_order,
                 list_routes, list_vehicles, get_plan, list_stops, aging_report, get_digest, suggest_dispatch,
                 create_order, confirm_order, cancel_order, allocate_order, create_dispatch_plan, approve_dispatch_plan,
                 adjust_stock, transfer_stock, close_stop, record_deposit, credit_note, record_payment, record_expense, cashbook,
                 reverse_ledger_entry, reverse_expense,
                 find_supplier, list_suppliers, supplier_khata, payables_report, record_purchase, pay_supplier,
                 reverse_purchase, reverse_supplier_entry,
                 draft_reminder, draft_due_reminders, send_reminder, log_promise, broken_promises,
                 sales_report, profit_summary, collection_report, stock_ledger, stock_valuation, slow_stock, top_customers]
    return {t.name: t for t in all_tools}
