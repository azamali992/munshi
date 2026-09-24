"""Read-only views the owner runs the business on: the daily digest, sales,
margin, stock ledger, collection rate, top customers, slow stock.

Every "day" here is a Pakistan business day (Asia/Karachi): stored UTC
timestamps are converted with sql_business_date / to_business_date, and
"today" comes from today_iso(), never from the server's OS clock.

Money is summed in integer paisa and converted to rupees only on the way out.
Cost of goods sold comes from sale_lines: the moving-average cost
snapshotted when the goods left the godown. A later purchase at a different
price, or an edit of a product's cost_price, never changes a past sale's
margin. Stock valuation is units x moving-average cost (inventory_value)."""
from __future__ import annotations

from datetime import timedelta

from munshi.domain.models import Order, business_today, mul_div, sql_business_date, to_business_date, to_paisa, to_rupees, today_iso
from munshi.domain.repository.collections import CollectionsMixin


def _p(e) -> int:
    return to_paisa(e.amount)


class ReportsMixin(CollectionsMixin):
    def digest(self, day: str | None = None) -> dict:
        day = day or today_iso()
        orders_today = self._orders_on(day)
        plans = self.list_plans(day)
        stops = [s for p in plans for s in self.list_stops(p.plan_id)]
        cash_p = sum(cash for p in plans for _, _, cash in self._stop_cash_paisa(p.plan_id))
        deposited_p = sum(self._deposited_paisa(p.plan_id) for p in plans)
        office_p = -sum(_p(e) for e in self.ledger_between(day, day, "payment") if e.received_by != "driver")
        invoiced_p = sum(_p(e) for e in self.ledger_between(day, day, "invoice") if e.method != "adjustment")   # opening balances aren't sales
        summary = self.aging_summary()
        low = self.low_stock()
        return {
            "date": day,
            "orders": {"count": len(orders_today), "value": to_rupees(sum(o.total_paisa for o in orders_today)),
                       "draft": sum(o.status == "draft" for o in orders_today), "confirmed": len(self.list_orders("confirmed", limit=500)),
                       "allocated": len(self.list_orders("allocated", limit=500))},
            "dispatch": {"plans": len(plans), "stops": len(stops),
                         "delivered": sum(s.status == "delivered" for s in stops), "short": sum(s.status == "short" for s in stops),
                         "pending": sum(s.status == "pending" for s in stops)},
            "cash": {"collected": to_rupees(cash_p), "deposited": to_rupees(deposited_p),
                     "office_payments": to_rupees(office_p), "expenses": to_rupees(self.expenses_paisa(day, day))},
            "sales": {"invoiced": to_rupees(invoiced_p)},
            "receivables": {"customers": summary["customers"], "total": summary["total"], "overdue_60": summary["buckets"]["60+"],
                            "overdue_30": to_rupees(to_paisa(summary["buckets"]["31-60"]) + to_paisa(summary["buckets"]["60+"]))},
            "payables": to_rupees(sum(to_paisa(p["balance"]) for p in self.payables())),
            "low_stock": low[:8],
            "pending_reminders": len(self.list_reminders("drafted")),
            "broken_promises": len(self.broken_promises()),
        }

    def _orders_on(self, day: str, limit: int = 500) -> list[Order]:
        """Orders booked on a Pakistan business day. (OrdersMixin.list_orders(day=...)
        filters on the UTC date prefix, which is a day behind between 00:00 and 05:00 PKT.)"""
        rows = self._all("SELECT * FROM orders WHERE " + sql_business_date("created_at") + "=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (day, limit))
        return [self._order_from_row(r) for r in rows]

    def _sales_paisa(self, start: str, end: str) -> dict:
        """The sales figures in paisa: revenue from the khata's invoices (net of reversals, which land on
        the day they are posted), cost of goods from the sale_lines cost snapshots."""
        invoices = [e for e in self.ledger_between(start, end, "invoice") if e.method != "adjustment"]   # opening balances aren't sales
        revenue = sum(_p(e) for e in invoices)
        by_day: dict[str, int] = {}
        by_cust: dict[str, int] = {}
        by_booker: dict[str, int] = {}
        for e in invoices:
            d = to_business_date(e.created_at).isoformat()
            by_day[d] = by_day.get(d, 0) + _p(e)
            by_cust[e.customer_id] = by_cust.get(e.customer_id, 0) + _p(e)
            if e.ref.startswith("ORD-"):
                try:
                    who = self.get_order(e.ref).created_by or "office"
                except Exception:
                    who = "office"
                by_booker[who] = by_booker.get(who, 0) + _p(e)
        # product mix and cost from what was actually delivered, at the cost snapshotted when it left the godown
        by_sku: dict[str, dict] = {}
        cost = 0
        for r in self._all("SELECT sku, SUM(qty) q, SUM(revenue_paisa) rev, SUM(cost_paisa) cost FROM sale_lines WHERE "
                           + sql_business_date("created_at") + " BETWEEN ? AND ? GROUP BY sku", (start, end)):
            by_sku[r["sku"]] = {"qty": int(r["q"]), "revenue": int(r["rev"]), "cost": int(r["cost"])}
            cost += int(r["cost"])
        return {"revenue": revenue, "invoices": sum(1 for e in invoices if not e.reversal_of), "cost": cost,
                "by_day": by_day, "by_cust": by_cust, "by_booker": by_booker, "by_sku": by_sku}

    def sales_report(self, start: str, end: str) -> dict:
        """Invoiced sales between two dates, by day, product and customer, with gross margin at the cost of the
        goods when they were sold (moving average, snapshotted), never at today's cost."""
        s = self._sales_paisa(start, end)
        revenue, cost = s["revenue"], s["cost"]
        prods = {p.sku: p for p in self.list_products(include_inactive=True)}
        names = {c.customer_id: c.name for c in self.list_customers(include_inactive=True)}
        by_product = [{"sku": k, "name": prods[k].name if k in prods else k, "qty": v["qty"], "revenue": to_rupees(v["revenue"]), "cost": to_rupees(v["cost"]),
                       "margin": to_rupees(v["revenue"] - v["cost"])} for k, v in s["by_sku"].items()]
        return {"start": start, "end": end, "revenue": to_rupees(revenue), "invoices": s["invoices"],
                "cost_of_goods": to_rupees(cost), "gross_margin": to_rupees(revenue - cost),
                "margin_pct": round((revenue - cost) / revenue * 100, 1) if revenue else 0.0,
                "by_day": [{"date": d, "revenue": to_rupees(v)} for d, v in sorted(s["by_day"].items())],
                "by_product": sorted(by_product, key=lambda r: -r["revenue"]),
                "by_customer": sorted([{"customer_id": k, "name": names.get(k, k), "revenue": to_rupees(v)} for k, v in s["by_cust"].items()], key=lambda r: -r["revenue"])[:20],
                "by_booker": sorted([{"name": k, "revenue": to_rupees(v)} for k, v in s["by_booker"].items()], key=lambda r: -r["revenue"])}

    def invoice_lines(self, invoice_id: str) -> list[dict]:
        """What a delivery invoice billed, product by product, from the sale record (rupees)."""
        return [{"sku": r["sku"], "qty": int(r["qty"]), "unit_price": to_rupees(mul_div(int(r["revenue_paisa"]), 1, int(r["qty"]))),
                 "total": to_rupees(int(r["revenue_paisa"]))}
                for r in self._all("SELECT sku, qty, revenue_paisa FROM sale_lines WHERE invoice_id=? ORDER BY line_id", (invoice_id,))]

    def cost_of_goods_sold_paisa(self) -> int:
        """All-time cost of goods delivered, from the sale_lines snapshots."""
        return int(self._one("SELECT COALESCE(SUM(cost_paisa), 0) s FROM sale_lines")["s"])

    def collection_report(self, start: str, end: str) -> dict:
        invoiced = sum(_p(e) for e in self.ledger_between(start, end, "invoice") if e.method != "adjustment")
        payments = self.ledger_between(start, end, "payment")
        collected = -sum(_p(e) for e in payments)
        by_method: dict[str, int] = {}
        for e in payments:
            m = e.method or "cash"; by_method[m] = by_method.get(m, 0) - _p(e)
        credits = -sum(_p(e) for e in self.ledger_between(start, end, "credit_note"))
        return {"start": start, "end": end, "invoiced": to_rupees(invoiced), "collected": to_rupees(collected), "credit_notes": to_rupees(credits),
                "collection_rate_pct": round(collected / invoiced * 100, 1) if invoiced else 0.0,
                "by_method": {k: to_rupees(v) for k, v in by_method.items()}, "aging": self.aging_summary()}

    def stock_ledger(self, sku: str, warehouse_id: str | None = None, limit: int = 100) -> dict:
        p = self.get_product(sku)
        moves = self.stock_moves(sku, warehouse_id, limit)
        levels = [l for l in self.stock_by_sku(sku) if not warehouse_id or l.warehouse_id == warehouse_id]
        return {"sku": sku, "name": p.name, "levels": [{"warehouse_id": l.warehouse_id, "on_hand": l.on_hand, "reserved": l.reserved, "available": l.available} for l in levels],
                "moves": [m.__dict__ for m in moves]}

    def _valuation_paisa(self) -> dict[tuple[str, str], int]:
        """Moving-average value of each (godown, sku) holding, in paisa. A product's pool is shared across
        its godowns in proportion to units; the last holding takes the rounding remainder, so the
        holdings of a product always add up to its pool exactly."""
        pools = {r["sku"]: int(r["value_paisa"]) for r in self._all("SELECT sku, value_paisa FROM inventory_value")}
        by_sku: dict[str, list] = {}
        for s in self.list_stock():
            if s.on_hand > 0: by_sku.setdefault(s.sku, []).append(s)
        out: dict[tuple[str, str], int] = {}
        for sku, holdings in by_sku.items():
            pool, units, left = pools.get(sku, 0), sum(h.on_hand for h in holdings), pools.get(sku, 0)
            for i, h in enumerate(sorted(holdings, key=lambda h: h.warehouse_id)):
                v = left if i == len(holdings) - 1 else mul_div(pool, h.on_hand, units)
                out[(h.warehouse_id, sku)] = v; left -= v
        return out

    def stock_valuation(self) -> dict:
        """Stock on hand at cost (moving average) and at list price."""
        prods = {p.sku: p for p in self.list_products(include_inactive=True)}
        values = self._valuation_paisa()
        rows, total_cost, total_sale = [], 0, 0
        for s in self.list_stock():
            p = prods.get(s.sku)
            if not p or s.on_hand <= 0: continue
            cost = values.get((s.warehouse_id, s.sku), 0); sale = s.on_hand * to_paisa(p.unit_price)
            total_cost += cost; total_sale += sale
            rows.append({"warehouse_id": s.warehouse_id, "sku": s.sku, "name": p.name, "on_hand": s.on_hand, "unit_cost": to_rupees(mul_div(cost, 1, s.on_hand)),
                         "at_cost": to_rupees(cost), "at_sale": to_rupees(sale)})
        return {"at_cost": to_rupees(total_cost), "at_sale": to_rupees(total_sale), "costing": "moving_average", "rows": sorted(rows, key=lambda r: -r["at_cost"])}

    def slow_stock(self, days: int = 30) -> list[dict]:
        since = (business_today() - timedelta(days=days)).isoformat()
        sold = {r["sku"] for r in self._all("SELECT DISTINCT sku FROM stock_moves WHERE kind='sale' AND " + sql_business_date("created_at") + ">=?", (since,))}
        prods = {p.sku: p for p in self.list_products()}
        values = self._valuation_paisa()
        out: dict[str, int] = {}
        val: dict[str, int] = {}
        for s in self.list_stock():
            if s.sku in prods and s.sku not in sold and s.on_hand > 0:
                out[s.sku] = out.get(s.sku, 0) + s.on_hand
                val[s.sku] = val.get(s.sku, 0) + values.get((s.warehouse_id, s.sku), 0)
        return sorted([{"sku": k, "name": prods[k].name, "on_hand": v, "value_at_cost": to_rupees(val[k]), "days_without_sale": days} for k, v in out.items()], key=lambda r: -r["value_at_cost"])

    def top_customers(self, days: int = 30, limit: int = 10) -> list[dict]:
        start = (business_today() - timedelta(days=days)).isoformat()
        return self.sales_report(start, today_iso())["by_customer"][:limit]

    def profit_summary(self, start: str, end: str) -> dict:
        s = self._sales_paisa(start, end)
        margin = s["revenue"] - s["cost"]
        by_cat: dict[str, int] = {}
        for x in self.expenses_between(start, end): by_cat[x.category] = by_cat.get(x.category, 0) + to_paisa(x.amount)
        exp_total = sum(by_cat.values())
        return {"start": start, "end": end, "revenue": to_rupees(s["revenue"]), "cost_of_goods": to_rupees(s["cost"]), "gross_margin": to_rupees(margin),
                "expenses": to_rupees(exp_total), "expenses_by_category": {k: to_rupees(v) for k, v in by_cat.items()}, "net": to_rupees(margin - exp_total)}
