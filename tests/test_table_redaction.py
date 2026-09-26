"""Chat tables get the same per-role data policy as the reply text: a salesman never gets a cost, margin or
valuation column; a driver never gets balances or limits, nor rows about customers outside his runs."""
from __future__ import annotations

from munshi.llm import guardrails as GRD
from munshi.llm.answers import make_table


def _sales_table():
    return make_table("en", "Sales by product", "By product:",
                      [("name", "Product", "text"), ("qty", "Qty", "qty"), ("sales", "Sales", "money"), ("cost", "Cost", "money"),
                       ("margin", "Margin", "money"), ("margin_pct", "Margin %", "pct")],
                      [{"name": "Urea 50kg", "qty": 30, "sales": 115500, "cost": 108000, "margin": 7500, "margin_pct": 6.5}],
                      totals={"sales": 115500, "cost": 108000, "margin": 7500, "margin_pct": 6.5})


def _aging_table():
    return make_table("en", "Who owes", "7 customers owe:",
                      [("name", "Customer", "text"), ("balance", "Balance", "money"), ("days", "Days overdue", "days"),
                       ("limit", "Credit limit", "money"), ("last_paid", "Last paid", "date")],
                      [{"name": "Haji Sons", "balance": 84000, "days": 65, "limit": 200000, "last_paid": "2026-07-01"}],
                      totals={"balance": 84000})


def test_owner_and_clerk_see_every_column():
    for role in ("owner", "clerk"):
        t = GRD.redact_tables([_sales_table()], role)[0]
        assert [c["key"] for c in t["columns"]] == ["name", "qty", "sales", "cost", "margin", "margin_pct"]


def test_salesman_never_gets_cost_margin_or_valuation():
    t = GRD.redact_tables([_sales_table()], "salesman")[0]
    keys = [c["key"] for c in t["columns"]]
    assert keys == ["name", "qty", "sales"]
    assert set(t["rows"][0]) <= {"name", "qty", "sales"} and set(t["totals"]) <= {"name", "qty", "sales"}
    slow = make_table("en", "Slow stock", "Not sold:", [("name", "Product", "text"), ("on_hand", "On hand", "qty"),
                                                         ("value_at_cost", "Value at cost", "money")],
                      [{"name": "NPK 25kg", "on_hand": 125, "value_at_cost": 562500}], totals={"value_at_cost": 562500})
    t = GRD.redact_tables([slow], "salesman")[0]
    assert [c["key"] for c in t["columns"]] == ["name", "on_hand"] and t["totals"] is None


def test_driver_never_gets_balances_or_limits():
    t = GRD.redact_tables([_aging_table()], "driver")[0]
    assert [c["key"] for c in t["columns"]] == ["name", "days"]
    assert "balance" not in t["rows"][0] and "limit" not in t["rows"][0] and t["totals"] is None


def test_a_table_with_nothing_left_to_show_is_dropped():
    only_cost = make_table("en", "Cost", "Cost:", [("cost", "Cost", "money")], [{"cost": 1}, {"cost": 2}])
    assert GRD.redact_tables([only_cost, _sales_table()], "salesman")[0]["title"] == "Sales by product"
    assert len(GRD.redact_tables([only_cost], "salesman")) == 0
