"""Plain dataclasses for a distributor's order-to-cash loop. Kept free of
any framework so tools, adapters and the web console all share one
vocabulary: customer, product, stock, route, vehicle, order, dispatch plan,
delivery stop, cash deposit, ledger entry, reminder, promise, audit row."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ---------------------------------------------------------------- money rule
# Every amount is STORED and COMPUTED as an integer number of paisa
# (1 rupee = 100 paisa). Rupees exist only at the edges: what callers pass in
# and what the dataclasses / report dicts hand back (JSON numbers, as before).
#
# Rounding rule, used everywhere and nowhere else: ROUND_HALF_UP on the
# decimal value as written, i.e. half a paisa rounds AWAY from zero
# (2.675 -> 268 paisa, -2.675 -> -268). A float is read through its shortest
# repr, so 2.675 means "2.675" and not the binary 2.67499999... it happens to
# be. Away-from-zero is symmetric, so a reversal (the negation) of an amount
# rounds to exactly the negation of its paisa, and nets to zero.
PAISA_PER_RUPEE = 100
# 10^14 paisa = Rs 1 trillion. Below this every paisa amount survives the
# rupee float round trip (to_rupees -> JSON -> to_paisa) exactly.
MAX_PAISA = 10 ** 14


def to_paisa(rupees) -> int:
    """Rupees (int, float, Decimal or numeric str) -> integer paisa, ROUND_HALF_UP."""
    if rupees is None or isinstance(rupees, bool):
        raise ValueError(f"not an amount: {rupees!r}")
    if isinstance(rupees, int):
        p = rupees * PAISA_PER_RUPEE
    else:
        try:
            d = rupees if isinstance(rupees, Decimal) else Decimal(str(rupees).strip())
        except (InvalidOperation, ValueError):
            raise ValueError(f"not an amount: {rupees!r}") from None
        if not d.is_finite():
            raise ValueError(f"not an amount: {rupees!r}")
        p = int((d * PAISA_PER_RUPEE).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    if abs(p) > MAX_PAISA:
        raise ValueError(f"amount out of range: {rupees!r}")
    return p


def to_rupees(paisa: int) -> float:
    """Integer paisa -> rupees for the API edge (the nearest float, e.g. 12345 -> 123.45)."""
    if isinstance(paisa, bool) or not isinstance(paisa, int):
        raise TypeError(f"paisa must be an int, got {type(paisa).__name__}: {paisa!r}")
    return paisa / PAISA_PER_RUPEE


def mul_div(amount: int, num: int, den: int) -> int:
    """round(amount * num / den) in exact integer arithmetic, half away from zero
    (the same rule as to_paisa). Used to apportion a paisa amount, e.g. the cost
    of 3 units out of a stock of 7 worth `amount`."""
    if den <= 0: raise ValueError("denominator must be positive")
    n = amount * num
    q, r = divmod(abs(n), den)
    if 2 * r >= den: q += 1
    return q if n >= 0 else -q


def discounted_paisa(price_paisa: int, discount_pct) -> int:
    """A price less a percentage discount, rounded to the paisa by the one rounding rule."""
    pct = Decimal(str(discount_pct or 0))
    return int((Decimal(price_paisa) * (100 - pct) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))

# ---------------------------------------------------------------- time rule
# Instants are stored in UTC (now_iso). Which *business day* an instant
# belongs to is always decided in Pakistan time, never by the server OS
# clock (Docker images default to UTC) and never by the UTC date prefix
# of the stored string. Pakistan has kept UTC+5 without DST since 2009,
# so a fixed offset is exact and needs no tzdata on the host.
BUSINESS_UTC_OFFSET_HOURS = 5
BUSINESS_TZ = timezone(timedelta(hours=BUSINESS_UTC_OFFSET_HOURS), "PKT")   # Asia/Karachi


def now_iso() -> str:
    """The current instant, UTC, for storage."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def business_now() -> datetime:
    """The current wall-clock time in Pakistan, independent of the server's TZ."""
    return datetime.now(UTC).astimezone(BUSINESS_TZ)


def business_today() -> date:
    return business_now().date()


def today_iso() -> str:
    """Today's business date (Asia/Karachi) as YYYY-MM-DD."""
    return business_today().isoformat()


def to_business_date(ts: str | datetime | date) -> date:
    """The Pakistan business day a stored timestamp belongs to. Naive stamps are
    taken to be UTC (the storage convention); aware ones are converted; a bare
    date is already a business date and is returned as is."""
    if isinstance(ts, str):
        s = ts.strip()
        if len(s) == 10: return date.fromisoformat(s)
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if not isinstance(ts, datetime): return ts
    if ts.tzinfo is None: ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(BUSINESS_TZ).date()


def sql_business_date(column: str) -> str:
    """SQLite expression giving the business date of a stored UTC ISO timestamp
    column. SQLite's date() honours a trailing +HH:MM / Z and treats naive
    stamps as UTC, matching to_business_date()."""
    return f"date({column}, '+{BUSINESS_UTC_OFFSET_HOURS} hours')"


@dataclass
class Customer:
    customer_id: str
    name: str
    phone: str
    tier: str = "standard"           # standard | wholesale | vip
    credit_limit: float = 0.0
    route_id: str | None = None
    language: str = "ur-en"           # for reminder templates
    address: str = ""
    discount_pct: float = 0.0         # standing discount off list price
    credit_days: int = 30             # invoice due after this many days
    active: bool = True


@dataclass
class Product:
    sku: str
    name: str
    unit_price: float
    aliases: list[str] = field(default_factory=list)
    units_per_load: int = 1           # how much vehicle capacity one unit takes
    cost_price: float = 0.0           # last purchase cost (reference only: margin and valuation use the
                                      # moving-average cost snapshotted on each stock move, never this)
    unit: str = "bag"                 # bag | ltr | pc | kg | box
    category: str = ""
    min_stock: int = 10               # low-stock alert threshold
    active: bool = True


@dataclass
class Supplier:
    supplier_id: str
    name: str
    phone: str = ""
    address: str = ""
    active: bool = True


@dataclass
class Warehouse:
    warehouse_id: str
    name: str


@dataclass
class StockLevel:
    warehouse_id: str
    sku: str
    on_hand: int
    reserved: int = 0

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


@dataclass
class Route:
    route_id: str
    name: str
    warehouse_id: str
    stop_customer_ids: list[str]


@dataclass
class Vehicle:
    vehicle_id: str
    plate: str
    capacity_class: str               # small | medium | large
    capacity_units: int


@dataclass
class OrderItem:
    sku: str
    qty: int
    unit_price: float

    @property
    def unit_price_paisa(self) -> int:
        return to_paisa(self.unit_price)

    @property
    def line_total_paisa(self) -> int:
        return self.qty * self.unit_price_paisa

    @property
    def line_total(self) -> float:
        return to_rupees(self.line_total_paisa)


@dataclass
class Order:
    order_id: str
    customer_id: str
    items: list[OrderItem]
    status: str = "draft"             # draft | confirmed | allocated | dispatched | delivered | short | cancelled
    channel: str = "api"
    source_text: str = ""
    created_at: str = field(default_factory=now_iso)
    warehouse_id: str | None = None
    discount_pct: float = 0.0
    notes: str = ""
    created_by: str = ""

    @property
    def total_paisa(self) -> int:
        return sum(i.line_total_paisa for i in self.items)

    @property
    def total(self) -> float:
        return to_rupees(self.total_paisa)

    @property
    def load_units(self) -> int:
        return sum(i.qty for i in self.items)


@dataclass
class DispatchPlan:
    plan_id: str
    plan_date: str
    route_id: str
    vehicle_id: str
    warehouse_id: str
    order_ids: list[str]
    status: str = "planned"           # planned | approved | loaded | completed
    load_units: int = 0
    created_at: str = field(default_factory=now_iso)


@dataclass
class DeliveryStop:
    stop_id: str
    plan_id: str
    order_id: str
    customer_id: str
    sequence: int
    status: str = "pending"           # pending | delivered | short | skipped
    delivered_items: list[dict] = field(default_factory=list)
    returned_items: list[dict] = field(default_factory=list)
    cash_collected: float = 0.0
    otp: str | None = None
    otp_verified: bool = False
    closed_at: str | None = None
    note: str = ""


@dataclass
class CashDeposit:
    deposit_id: str
    plan_id: str
    amount_counted: float
    counted_by: str
    deposited_at: str = field(default_factory=now_iso)


@dataclass
class LedgerEntry:
    entry_id: str
    customer_id: str
    kind: str                         # invoice | payment | credit_note
    amount: float                     # invoices positive, payments/credits negative
    ref: str
    due_date: str | None = None
    created_at: str = field(default_factory=now_iso)
    method: str = ""                  # cash | bank | jazzcash | easypaisa | cheque | adjustment
    received_by: str = ""
    doc_no: str | None = None         # gapless customer-facing number (INV-2026-000001); None on pre-V5 rows
    reversal_of: str | None = None    # set on a reversing entry: the entry_id it cancels


@dataclass
class Purchase:
    purchase_id: str
    supplier_id: str
    warehouse_id: str
    items: list[dict]                 # [{sku, qty, unit_cost}]  (qty negative on a reversal)
    total: float
    invoice_ref: str = ""
    paid_amount: float = 0.0
    created_at: str = field(default_factory=now_iso)
    doc_no: str | None = None
    reversal_of: str | None = None


@dataclass
class SupplierLedgerEntry:
    entry_id: str
    supplier_id: str
    kind: str                         # bill | payment
    amount: float                     # bills positive, payments negative
    ref: str
    method: str = ""
    created_at: str = field(default_factory=now_iso)
    reversal_of: str | None = None


@dataclass
class Expense:
    expense_id: str
    category: str                     # fuel | salary | rent | repair | utilities | misc
    amount: float                     # negative only on a reversal
    note: str = ""
    method: str = "cash"
    paid_by: str = ""
    expense_date: str = field(default_factory=today_iso)
    created_at: str = field(default_factory=now_iso)
    reversal_of: str | None = None


@dataclass
class StockMove:
    move_id: str
    warehouse_id: str
    sku: str
    delta: int
    kind: str                         # sale | return | purchase | purchase_reversal | adjust | transfer_out | transfer_in | opening
    ref: str
    created_at: str = field(default_factory=now_iso)


@dataclass
class Notification:
    notif_id: str
    for_role: str                     # owner | clerk | salesman | driver | all
    kind: str                         # approval | variance | low_stock | delivery | digest
    text: str
    ref: str = ""
    read: bool = False
    created_at: str = field(default_factory=now_iso)


@dataclass
class Reminder:
    reminder_id: str
    customer_id: str
    tier: str                         # gentle | firm | final
    amount_due: float
    days_overdue: int
    message: str
    status: str = "drafted"           # drafted | approved | sent
    created_at: str = field(default_factory=now_iso)


@dataclass
class Promise:
    promise_id: str
    customer_id: str
    amount: float
    promised_date: str
    created_at: str = field(default_factory=now_iso)


@dataclass
class AuditRow:
    audit_id: str
    actor: str                        # agent name or human role
    action: str
    entity: str
    entity_id: str
    approved_by: str | None
    payload: dict
    created_at: str = field(default_factory=now_iso)
    user: str = ""                    # the signed-in human behind the action, when known
