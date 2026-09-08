"""Plain dataclasses for a distributor's order-to-cash loop. Kept free of
any framework so tools, adapters and the web console all share one
vocabulary: customer, product, stock, route, vehicle, order, dispatch plan,
delivery stop, cash deposit, ledger entry, reminder, promise, audit row."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, date


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_iso() -> str:
    return date.today().isoformat()


@dataclass
class Customer:
    customer_id: str
    name: str
    phone: str
    tier: str = "standard"           # standard | wholesale | vip
    credit_limit: float = 0.0
    route_id: str | None = None
    language: str = "ur-en"           # for reminder templates


@dataclass
class Product:
    sku: str
    name: str
    unit_price: float
    aliases: list[str] = field(default_factory=list)
    units_per_load: int = 1           # how much vehicle capacity one unit takes


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
