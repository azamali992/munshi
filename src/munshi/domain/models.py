"""Plain dataclasses for a distributor's order-to-cash loop. Kept free of
any framework so tools, adapters and the web console all share one
vocabulary: customer, product, stock, route, vehicle, order, dispatch plan,
delivery stop, cash deposit, ledger entry, reminder, promise, audit row."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone

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
    cost_price: float = 0.0           # last purchase cost, for margin
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
    def line_total(self) -> float:
        return round(self.qty * self.unit_price, 2)


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
    def total(self) -> float:
        return round(sum(i.line_total for i in self.items), 2)

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


@dataclass
class Purchase:
    purchase_id: str
    supplier_id: str
    warehouse_id: str
    items: list[dict]                 # [{sku, qty, unit_cost}]
    total: float
    invoice_ref: str = ""
    paid_amount: float = 0.0
    created_at: str = field(default_factory=now_iso)


@dataclass
class SupplierLedgerEntry:
    entry_id: str
    supplier_id: str
    kind: str                         # bill | payment
    amount: float                     # bills positive, payments negative
    ref: str
    method: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass
class Expense:
    expense_id: str
    category: str                     # fuel | salary | rent | repair | utilities | misc
    amount: float
    note: str = ""
    method: str = "cash"
    paid_by: str = ""
    expense_date: str = field(default_factory=today_iso)
    created_at: str = field(default_factory=now_iso)


@dataclass
class StockMove:
    move_id: str
    warehouse_id: str
    sku: str
    delta: int
    kind: str                         # sale | return | purchase | adjust | transfer_out | transfer_in
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
