"""Read-only views the owner runs the business on: the daily digest, sales,
margin, stock ledger, collection rate, top customers, slow stock."""
from __future__ import annotations

from datetime import date, timedelta

from munshi.domain.models import today_iso
from munshi.domain.repository.collections import CollectionsMixin


class ReportsMixin(CollectionsMixin):
    def digest(self, day: str | None = None) -> dict:
        day = day or today_iso()
        orders_today = self.list_orders(limit=500, day=day)
        plans = self.list_plans(day)
        stops = [s for p in plans for s in self.list_stops(p.plan_id)]
        cash = round(sum(s.cash_collected for s in stops), 2)
        deps = [d for p in plans for d in self.deposits(p.plan_id)]
        office = [e for e in self.ledger_between(day, day, "payment") if e.received_by != "driver"]
        invoiced = round(sum(e.amount for e in self.ledger_between(day, day, "invoice") if e.method != "adjustment"), 2)   # opening balances aren't sales
        expenses = self.expenses_between(day, day)
        summary = self.aging_summary()
        low = self.low_stock()
        return {
            "date": day,
            "orders": {"count": len(orders_today), "value": round(sum(o.total for o in orders_today), 2),
                       "draft": sum(o.status == "draft" for o in orders_today), "confirmed": len(self.list_orders("confirmed", limit=500)),
                       "allocated": len(self.list_orders("allocated", limit=500))},
            "dispatch": {"plans": len(plans), "stops": len(stops),
                         "delivered": sum(s.status == "delivered" for s in stops), "short": sum(s.status == "short" for s in stops),
                         "pending": sum(s.status == "pending" for s in stops)},
            "cash": {"collected": cash, "deposited": round(sum(d.amount_counted for d in deps), 2),
                     "office_payments": round(-sum(e.amount for e in office), 2), "expenses": round(sum(x.amount for x in expenses), 2)},
            "sales": {"invoiced": invoiced},
            "receivables": {"customers": summary["customers"], "total": summary["total"], "overdue_60": summary["buckets"]["60+"],
                            "overdue_30": round(summary["buckets"]["31-60"] + summary["buckets"]["60+"], 2)},
            "payables": round(sum(p["balance"] for p in self.payables()), 2),
            "low_stock": low[:8],
            "pending_reminders": len(self.list_reminders("drafted")),
            "broken_promises": len(self.broken_promises()),
        }

    def sales_report(self, start: str, end: str) -> dict:
        """Invoiced sales between two dates, by day, product and customer, with gross margin at current cost."""
        invoices = [e for e in self.ledger_between(start, end, "invoice") if e.method != "adjustment"]   # opening balances aren't sales
        by_day: dict[str, float] = {}
        by_cust: dict[str, float] = {}
        for e in invoices:
            by_day[e.created_at[:10]] = round(by_day.get(e.created_at[:10], 0) + e.amount, 2)
            by_cust[e.customer_id] = round(by_cust.get(e.customer_id, 0) + e.amount, 2)
        # product mix from what was actually delivered
        by_sku: dict[str, dict] = {}
        cost_total = 0.0
        prods = {p.sku: p for p in self.list_products(include_inactive=True)}
        for r in self._all("SELECT s.delivered_items, o.items FROM stops s JOIN orders o ON o.order_id=s.order_id WHERE s.status IN ('delivered','short') AND substr(s.closed_at,1,10) BETWEEN ? AND ?", (start, end)):
            import json
            prices = {i["sku"]: i["unit_price"] for i in json.loads(r["items"])}
            for d in json.loads(r["delivered_items"]):
                q = int(d["qty"]); sku = d["sku"]
                row = by_sku.setdefault(sku, {"sku": sku, "name": prods[sku].name if sku in prods else sku, "qty": 0, "revenue": 0.0, "cost": 0.0})
                row["qty"] += q; row["revenue"] = round(row["revenue"] + q * prices.get(sku, 0), 2)
                c = q * (prods[sku].cost_price if sku in prods else 0); row["cost"] = round(row["cost"] + c, 2); cost_total += c
        by_booker: dict[str, float] = {}
        for e in invoices:
            if e.ref.startswith("ORD-"):
                try:
                    who = self.get_order(e.ref).created_by or "office"
                except Exception:
                    who = "office"
                by_booker[who] = round(by_booker.get(who, 0) + e.amount, 2)
        revenue = round(sum(e.amount for e in invoices), 2)
        names = {c.customer_id: c.name for c in self.list_customers(include_inactive=True)}
        return {"start": start, "end": end, "revenue": revenue, "invoices": len(invoices),
                "cost_of_goods": round(cost_total, 2), "gross_margin": round(revenue - cost_total, 2),
                "margin_pct": round((revenue - cost_total) / revenue * 100, 1) if revenue else 0.0,
                "by_day": [{"date": d, "revenue": v} for d, v in sorted(by_day.items())],
                "by_product": sorted(by_sku.values(), key=lambda r: -r["revenue"]),
                "by_customer": sorted([{"customer_id": k, "name": names.get(k, k), "revenue": v} for k, v in by_cust.items()], key=lambda r: -r["revenue"])[:20],
                "by_booker": sorted([{"name": k, "revenue": v} for k, v in by_booker.items()], key=lambda r: -r["revenue"])}

    def collection_report(self, start: str, end: str) -> dict:
        invoiced = round(sum(e.amount for e in self.ledger_between(start, end, "invoice") if e.method != "adjustment"), 2)
        collected = round(-sum(e.amount for e in self.ledger_between(start, end, "payment")), 2)
        by_method: dict[str, float] = {}
        for e in self.ledger_between(start, end, "payment"):
            m = e.method or "cash"; by_method[m] = round(by_method.get(m, 0) - e.amount, 2)
        credits = round(-sum(e.amount for e in self.ledger_between(start, end, "credit_note")), 2)
        return {"start": start, "end": end, "invoiced": invoiced, "collected": collected, "credit_notes": credits,
                "collection_rate_pct": round(collected / invoiced * 100, 1) if invoiced else 0.0,
                "by_method": by_method, "aging": self.aging_summary()}

    def stock_ledger(self, sku: str, warehouse_id: str | None = None, limit: int = 100) -> dict:
        p = self.get_product(sku)
        moves = self.stock_moves(sku, warehouse_id, limit)
        levels = [l for l in self.stock_by_sku(sku) if not warehouse_id or l.warehouse_id == warehouse_id]
        return {"sku": sku, "name": p.name, "levels": [{"warehouse_id": l.warehouse_id, "on_hand": l.on_hand, "reserved": l.reserved, "available": l.available} for l in levels],
                "moves": [m.__dict__ for m in moves]}

    def stock_valuation(self) -> dict:
        prods = {p.sku: p for p in self.list_products(include_inactive=True)}
        rows, total_cost, total_sale = [], 0.0, 0.0
        for s in self.list_stock():
            p = prods.get(s.sku)
            if not p or s.on_hand == 0: continue
            cost = s.on_hand * p.cost_price; sale = s.on_hand * p.unit_price
            total_cost += cost; total_sale += sale
            rows.append({"warehouse_id": s.warehouse_id, "sku": s.sku, "name": p.name, "on_hand": s.on_hand, "at_cost": round(cost, 2), "at_sale": round(sale, 2)})
        return {"at_cost": round(total_cost, 2), "at_sale": round(total_sale, 2), "rows": sorted(rows, key=lambda r: -r["at_cost"])}

    def slow_stock(self, days: int = 30) -> list[dict]:
        since = (date.today() - timedelta(days=days)).isoformat()
        sold = {r["sku"] for r in self._all("SELECT DISTINCT sku FROM stock_moves WHERE kind='sale' AND substr(created_at,1,10)>=?", (since,))}
        prods = {p.sku: p for p in self.list_products()}
        out: dict[str, int] = {}
        for s in self.list_stock():
            if s.sku in prods and s.sku not in sold and s.on_hand > 0:
                out[s.sku] = out.get(s.sku, 0) + s.on_hand
        return sorted([{"sku": k, "name": prods[k].name, "on_hand": v, "value_at_cost": round(v * prods[k].cost_price, 2), "days_without_sale": days} for k, v in out.items()], key=lambda r: -r["value_at_cost"])

    def top_customers(self, days: int = 30, limit: int = 10) -> list[dict]:
        start = (date.today() - timedelta(days=days)).isoformat()
        return self.sales_report(start, today_iso())["by_customer"][:limit]

    def profit_summary(self, start: str, end: str) -> dict:
        sales = self.sales_report(start, end)
        expenses = self.expenses_between(start, end)
        by_cat: dict[str, float] = {}
        for x in expenses: by_cat[x.category] = round(by_cat.get(x.category, 0) + x.amount, 2)
        exp_total = round(sum(x.amount for x in expenses), 2)
        return {"start": start, "end": end, "revenue": sales["revenue"], "cost_of_goods": sales["cost_of_goods"], "gross_margin": sales["gross_margin"],
                "expenses": exp_total, "expenses_by_category": by_cat, "net": round(sales["gross_margin"] - exp_total, 2)}
