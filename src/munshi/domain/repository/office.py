"""The office console's reads and writes (web/routes/office.py, the desktop /office screens).

Nothing here bypasses a rule the rest of the repository keeps:
  * prices change with an UPDATE of `products`; the V7 triggers write price_history (old -> new, when) for every
    change on every path, and this module stamps WHO on the rows its own writes created, in the same transaction;
  * stock only ever changes through adjust_stock -> move_stock (the stock ledger, the moving-average pool and the
    never-below-zero guard), never by writing stock.on_hand;
  * the client list reads balances and ages for every customer in a fixed number of queries (no query per customer),
    computing days overdue with the same FIFO rule as CollectionsMixin.aging.
Money is integer paisa inside, rupees only on the way out."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from munshi.domain.models import mul_div, to_paisa, to_rupees, today_iso
from munshi.domain.repository.base import InsufficientStockError, NotFoundError, RepositoryBase, StateError
from munshi.domain.repository.guarded import immediate_tx

PRICE_MODES = ("set", "pct", "add")
OPEN_ORDER_STATES = ("draft", "confirmed", "allocated", "dispatched")
_ATTRIBUTION_WINDOW_S = 10          # an unattributed price change is matched to an audit row this close after it


def _ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


class OfficeMixin(RepositoryBase):
    # ------------------------------------------------------------ prices
    def _price_history_max_id(self) -> int:
        return int(self._one("SELECT COALESCE(MAX(history_id), 0) m FROM price_history")["m"])

    def _stamp_price_history(self, since_id: int, who: str, source: str) -> int:
        """Name the person behind the price_history rows the current transaction just created (the triggers
        can't know who is signed in). Only unattributed rows, only once -- the V7 append-only guard allows nothing else."""
        with self._tx() as c:
            return c.execute("UPDATE price_history SET changed_by=?, source=? WHERE history_id > ? AND changed_by = ''",
                             (who[:80], source[:40], since_id)).rowcount

    def new_price_paisa(self, old_p: int, mode: str, value, round_to: int = 1) -> int:
        """One product's new list price. set: `value` rupees. add: old + `value` rupees (negative lowers it).
        pct: old x (100 + value)% rounded half-up to the nearest `round_to` rupees (0 = to the paisa)."""
        if mode not in PRICE_MODES: raise ValueError(f"mode must be one of {', '.join(PRICE_MODES)}")
        if mode == "set":
            return to_paisa(value)
        if mode == "add":
            return old_p + to_paisa(value)
        pct = Decimal(str(value))
        exact = Decimal(old_p) * (Decimal(100) + pct) / Decimal(100)
        step = Decimal(int(round_to) * 100) if round_to else Decimal(1)
        return int((exact / step).quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)

    def preview_price_change(self, skus: list[str], mode: str, value, round_to: int = 1) -> list[dict]:
        """Old -> new list price for each product, nothing saved. A row that can't be applied carries `error`."""
        if not skus: raise ValueError("pick at least one product")
        out = []
        for sku in dict.fromkeys(skus):
            r = self._one("SELECT sku, name, unit, unit_price FROM products WHERE sku=?", (sku,))
            if not r: raise NotFoundError(f"no such product: {sku}")
            old_p = int(r["unit_price"])
            new_p = self.new_price_paisa(old_p, mode, value, round_to)
            err = None
            if new_p <= 0: err = f"the new price would be Rs {to_rupees(new_p):,.2f} -- a list price must stay above zero"
            out.append({"sku": sku, "name": r["name"], "unit": r["unit"], "old": to_rupees(old_p), "new": to_rupees(new_p),
                        "change": to_rupees(new_p - old_p), "change_pct": round((new_p - old_p) / old_p * 100, 2) if old_p else None,
                        "unchanged": new_p == old_p, "error": err})
        return out

    def apply_price_change(self, skus: list[str], mode: str, value, expected: dict[str, float], round_to: int, reason: str,
                           actor: str, approved_by: str, who: str, source: str = "office") -> list[dict]:
        """Save what preview_price_change shows, all or nothing, in one write-locked transaction. `expected` is
        the old price the person saw for each product: if any has changed since (someone else saved a price),
        nothing is saved (StateError) and they preview again."""
        reason = (reason or "").strip()[:120]
        with immediate_tx(self):
            rows = self.preview_price_change(skus, mode, value, round_to)
            bad = [r for r in rows if r["error"]]
            if bad: raise ValueError("; ".join(f"{r['sku']}: {r['error']}" for r in bad))
            for r in rows:
                if r["sku"] not in expected:
                    raise ValueError(f"{r['sku']}: the old price you saw is missing -- preview again")
                if to_paisa(expected[r["sku"]]) != to_paisa(r["old"]):
                    raise StateError(f"{r['name']}'s price changed to Rs {r['old']:,.2f} since you previewed (you saw Rs {float(expected[r['sku']]):,.2f}) "
                                     "-- nothing was saved; preview again")
            since = self._price_history_max_id()
            changed = [r for r in rows if not r["unchanged"]]
            with self._tx() as c:
                for r in changed:
                    c.execute("UPDATE products SET unit_price=? WHERE sku=?", (to_paisa(r["new"]), r["sku"]))
            self._stamp_price_history(since, who, source)
            for r in changed:
                self.audit(actor, "price_changed", "product", r["sku"], {"field": "unit_price", "old": r["old"], "new": r["new"], "mode": mode,
                                                                         "value": value, "reason": reason}, approved_by)
        return rows

    def price_history(self, sku: str, include_cost: bool, limit: int = 200) -> list[dict]:
        """Every list-price (and, for the owner, cost-price) change of one product, newest first: old, new, when,
        who. A change made outside the office console (the phone app's product form, a purchase updating the last
        cost, an Excel import) has no stamped name; it is attributed from the audit trail written with it."""
        self.get_product(sku)
        q = "SELECT * FROM price_history WHERE sku=?" + ("" if include_cost else " AND field='unit_price'") + " ORDER BY history_id DESC LIMIT ?"
        rows = [dict(r) for r in self._all(q, (sku, limit))]
        audits = None
        out = []
        for r in rows:
            by, source = r["changed_by"], r["source"]
            if not by:
                if audits is None: audits = self._price_attribution_candidates(sku)
                by, source = self._attribute(r, audits)
            out.append({"history_id": r["history_id"], "field": r["field"], "old": to_rupees(int(r["old_paisa"])), "new": to_rupees(int(r["new_paisa"])),
                        "change": to_rupees(int(r["new_paisa"]) - int(r["old_paisa"])), "at": r["changed_at"], "by": by, "source": source})
        return out

    def _price_attribution_candidates(self, sku: str) -> list[dict]:
        """The audit rows that can explain a price change of `sku` made outside the console: the product form's own
        audit, a purchase of this product (it moves the reference cost), an Excel import. Each with its audit rowid."""
        rows = [dict(a) for a in self._all("SELECT rowid seq, action, entity_id, user, approved_by, actor, created_at FROM audit WHERE entity_id=? "
                                           "AND action IN ('product_updated', 'product_added')", (sku,))]
        bought = {p["purchase_id"] for p in self._all("SELECT purchase_id FROM purchases WHERE reversal_of IS NULL AND items LIKE ?", (f'%"{sku}"%',))}
        rows += [dict(a) for a in self._all("SELECT rowid seq, action, entity_id, user, approved_by, actor, created_at FROM audit WHERE action='record_purchase'")
                 if a["entity_id"] in bought]
        rows += [dict(a) for a in self._all("SELECT rowid seq, action, entity_id, user, approved_by, actor, created_at FROM audit WHERE action='import_xlsx'")]
        return sorted(rows, key=lambda a: a["seq"])

    @staticmethod
    def _attribute(h: dict, audits: list[dict]) -> tuple[str, str]:
        """The first candidate audit row written after the change (audit rowid > the change's audit_seq), provided it
        was written soon after it; else unattributed. Sequence, not the clock, decides: two changes in one second
        still land on the right rows."""
        at = _ts(h["changed_at"])
        match = None
        for a in audits:                                   # in audit order
            if a["seq"] <= int(h.get("audit_seq") or 0): continue
            if a["action"] == "record_purchase" and h["field"] != "cost_price": continue
            t = _ts(a["created_at"])
            window = 900 if a["action"] == "import_xlsx" else _ATTRIBUTION_WINDOW_S     # an import audits once, at the end
            if at and t and -1 <= (t - at).total_seconds() <= window:
                match = a
            break                                          # only the FIRST later candidate can be the one
        if not match: return "", "not recorded"
        a = match
        who = a.get("user") or a.get("approved_by") or a.get("actor") or ""
        roles = ("owner", "clerk", "salesman", "driver")
        role = a.get("actor") if a.get("actor") in roles else str(a.get("approved_by") or "").split(":", 1)[0]
        if a.get("user") and role in roles:
            who = f"{a['user']} ({role})"                # the same shape as the console's own stamp
        src = {"product_updated": "product form", "product_added": "product form", "record_purchase": f"purchase {a['entity_id']}",
               "import_xlsx": "Excel import"}[a["action"]]
        return who, src

    def office_products(self, include_cost: bool) -> list[dict]:
        """Every product (inactive too) with its stock across godowns and the last list-price change."""
        stock = {}
        for r in self._all("SELECT sku, COALESCE(SUM(on_hand),0) oh, COALESCE(SUM(reserved),0) rs FROM stock GROUP BY sku"):
            stock[r["sku"]] = (int(r["oh"]), int(r["rs"]))
        low = {}
        for r in self._all("SELECT s.sku, s.warehouse_id, s.on_hand - s.reserved av FROM stock s JOIN products p ON p.sku=s.sku WHERE s.on_hand - s.reserved <= p.min_stock"):
            low.setdefault(r["sku"], []).append({"warehouse_id": r["warehouse_id"], "available": int(r["av"])})
        last = {r["sku"]: r for r in self._all("SELECT sku, MAX(history_id) hid, changed_at FROM price_history WHERE field='unit_price' GROUP BY sku")}
        pools = {r["sku"]: int(r["value_paisa"]) for r in self._all("SELECT sku, value_paisa FROM inventory_value")}
        out = []
        for r in self._all("SELECT * FROM products ORDER BY name"):
            oh, rs = stock.get(r["sku"], (0, 0))
            d = {"sku": r["sku"], "name": r["name"], "unit": r["unit"], "category": r["category"], "unit_price": to_rupees(int(r["unit_price"])),
                 "aliases": json.loads(r["aliases"] or "[]"), "units_per_load": int(r["units_per_load"] or 1), "min_stock": int(r["min_stock"]),
                 "active": bool(r["active"]), "on_hand": oh, "reserved": rs, "available": oh - rs,
                 "low": bool(r["active"]) and bool(low.get(r["sku"])), "low_godowns": low.get(r["sku"], []),
                 "price_changed_at": last[r["sku"]]["changed_at"] if r["sku"] in last else None}
            if include_cost:
                d["cost_price"] = to_rupees(int(r["cost_price"]))
                d["avg_cost"] = to_rupees(mul_div(pools.get(r["sku"], 0), 1, oh)) if oh > 0 else to_rupees(int(r["cost_price"]))
                d["stock_value"] = to_rupees(pools.get(r["sku"], 0))
            out.append(d)
        return out

    # ------------------------------------------------------------ stock
    def stock_matrix(self, include_value: bool) -> dict:
        """On hand per product x godown, with totals; for the owner, the moving-average value of each holding
        (the same apportioning as the stock-valuation report). Also checks the two ledger invariants: stock_moves
        replays to on_hand, and holdings add up to each product's value pool."""
        whs = self.list_warehouses()
        prods = self._all("SELECT sku, name, unit, category, min_stock, active, unit_price FROM products ORDER BY name")
        levels = {(r["warehouse_id"], r["sku"]): (int(r["on_hand"]), int(r["reserved"])) for r in self._all("SELECT warehouse_id, sku, on_hand, reserved FROM stock")}
        values = self._valuation_paisa() if include_value else {}
        rows, wh_units, wh_value = [], {w.warehouse_id: 0 for w in whs}, {w.warehouse_id: 0 for w in whs}
        for p in prods:
            cells, tot, tot_res, tot_val = {}, 0, 0, 0
            for w in whs:
                oh, rs = levels.get((w.warehouse_id, p["sku"]), (0, 0))
                cell = {"on_hand": oh, "reserved": rs, "available": oh - rs, "low": bool(p["active"]) and (w.warehouse_id, p["sku"]) in levels and oh - rs <= int(p["min_stock"])}
                if include_value:
                    v = values.get((w.warehouse_id, p["sku"]), 0); cell["value"] = to_rupees(v); tot_val += v; wh_value[w.warehouse_id] += v
                cells[w.warehouse_id] = cell; tot += oh; tot_res += rs; wh_units[w.warehouse_id] += oh
            if not p["active"] and tot == 0: continue          # a retired product with nothing left: nothing to count
            row = {"sku": p["sku"], "name": p["name"], "unit": p["unit"], "category": p["category"], "active": bool(p["active"]), "min_stock": int(p["min_stock"]),
                   "cells": cells, "on_hand": tot, "reserved": tot_res, "available": tot - tot_res}
            if include_value:
                row["value"] = to_rupees(tot_val); row["avg_cost"] = to_rupees(mul_div(tot_val, 1, tot)) if tot > 0 else None
            rows.append(row)
        replay = self.replay_stock_ledger()
        mismatches = [{"warehouse_id": w, "sku": s, "on_hand": oh, "ledger": replay.get((w, s), 0)} for (w, s), (oh, _) in levels.items() if replay.get((w, s), 0) != oh]
        mismatches += [{"warehouse_id": w, "sku": s, "on_hand": 0, "ledger": q} for (w, s), q in replay.items() if (w, s) not in levels and q]
        out = {"warehouses": [{"warehouse_id": w.warehouse_id, "name": w.name, "units": wh_units[w.warehouse_id]} | ({"value": to_rupees(wh_value[w.warehouse_id])} if include_value else {})
                              for w in whs],
               "rows": rows, "units": sum(wh_units.values()), "ledger_ok": not mismatches, "ledger_mismatches": mismatches[:20]}
        if include_value:
            pool = int(self._one("SELECT COALESCE(SUM(value_paisa), 0) s FROM inventory_value")["s"])
            out["value"] = to_rupees(sum(wh_value.values())); out["pool_value"] = to_rupees(pool)
        return out

    def stock_moves_detail(self, sku: str, warehouse_id: str | None = None, limit: int = 200, include_value: bool = False) -> dict:
        """One product's stock movements, newest first, each with the godown's balance after it. Balances are walked
        back from today's on_hand, which the ledger reproduces exactly, so they are right however far back the page goes."""
        p = self.get_product(sku)
        if warehouse_id: self.get_warehouse(warehouse_id)
        q, a = "SELECT move_id, warehouse_id, sku, delta, kind, ref, created_at, value_paisa, order_id FROM stock_moves WHERE sku=?", [sku]
        if warehouse_id: q += " AND warehouse_id=?"; a.append(warehouse_id)
        rows = self._all(q + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*a, int(limit)))
        bal = {r["warehouse_id"]: int(r["on_hand"]) for r in self._all("SELECT warehouse_id, on_hand FROM stock WHERE sku=?", (sku,))}
        moves = []
        for r in rows:
            wh = r["warehouse_id"]
            m = {"move_id": r["move_id"], "at": r["created_at"], "warehouse_id": wh, "kind": r["kind"], "delta": int(r["delta"]), "ref": r["ref"],
                 "order_id": r["order_id"], "balance_after": bal.get(wh, 0)}
            if include_value: m["value"] = to_rupees(int(r["value_paisa"]))
            bal[wh] = bal.get(wh, 0) - int(r["delta"])
            moves.append(m)
        total = int(self._one("SELECT COUNT(*) n FROM stock_moves WHERE sku=?" + (" AND warehouse_id=?" if warehouse_id else ""), tuple(a))["n"])
        levels = self._all("SELECT warehouse_id, on_hand, reserved FROM stock WHERE sku=?" + (" AND warehouse_id=?" if warehouse_id else "") + " ORDER BY warehouse_id", tuple(a))
        return {"sku": sku, "name": p.name, "unit": p.unit, "moves": moves, "total_moves": total,
                "levels": [{"warehouse_id": r["warehouse_id"], "on_hand": int(r["on_hand"]), "reserved": int(r["reserved"])} for r in levels]}

    def _count_rows(self, warehouse_id: str, counts: list[dict], include_value: bool) -> list[dict]:
        self.get_warehouse(warehouse_id)
        out, seen = [], set()
        for ln in counts:
            sku = str(ln["sku"]).strip().upper()
            if sku in seen: raise ValueError(f"{sku} is counted twice")
            seen.add(sku)
            p = self.get_product(sku)
            counted = ln["counted"]
            if isinstance(counted, bool) or int(counted) != counted: raise ValueError(f"{sku}: a count must be a whole number")
            counted = int(counted)
            s = self.get_stock(warehouse_id, sku)
            diff = counted - s.on_hand
            err = None
            if counted < 0: err = "a count can't be below zero"
            elif counted < s.reserved:
                err = f"{s.reserved} are reserved for allocated orders not yet loaded -- they should still be on the shelf; recount, or cancel those allocations first"
            row = {"sku": sku, "name": p.name, "unit": p.unit, "system": s.on_hand, "reserved": s.reserved, "counted": counted, "diff": diff, "error": err}
            if include_value:
                row["value_change"] = to_rupees(diff * self.avg_cost_paisa(sku)) if diff else 0.0     # estimate at today's average
            out.append(row)
        return out

    def preview_stock_count(self, warehouse_id: str, counts: list[dict], include_value: bool = False) -> dict:
        rows = self._count_rows(warehouse_id, counts, include_value)
        changed = [r for r in rows if r["diff"] and not r["error"]]
        out = {"warehouse_id": warehouse_id, "rows": rows, "lines": len(rows), "differences": len(changed), "errors": sum(1 for r in rows if r["error"]),
               "units_up": sum(r["diff"] for r in changed if r["diff"] > 0), "units_down": -sum(r["diff"] for r in changed if r["diff"] < 0)}
        if include_value: out["value_change"] = round(sum(r["value_change"] for r in changed), 2)
        return out

    def post_stock_count(self, warehouse_id: str, counts: list[dict], count_date: str | None, actor: str, approved_by: str) -> dict:
        """Post a physical count's differences as stock adjustments, all or nothing, in one write-locked transaction.
        Each line carries the system quantity the person saw at preview: if stock has moved since (a delivery was
        loaded, a purchase came in), nothing is posted and they preview again. Every adjustment goes through
        adjust_stock -> move_stock, so the ledger, the value pool and the never-below-zero guard all apply."""
        day = count_date or today_iso()
        try:
            future = date.fromisoformat(day) > date.fromisoformat(today_iso())
        except ValueError:
            future = True
        if future: raise ValueError("the count date must be a real date, not in the future")
        reason = f"stock count {day}"
        with immediate_tx(self):
            rows = self._count_rows(warehouse_id, counts, False)
            bad = [r for r in rows if r["error"]]
            if bad: raise ValueError("; ".join(f"{r['sku']}: {r['error']}" for r in bad))
            seen = {str(ln["sku"]).strip().upper(): ln.get("system") for ln in counts}
            moved = [r for r in rows if seen.get(r["sku"]) is None or int(seen[r["sku"]]) != r["system"]]
            if moved:
                raise StateError("stock moved since you previewed (" + ", ".join(f"{r['sku']}: you saw {seen.get(r['sku'])}, now {r['system']}" for r in moved[:5])
                                 + ") -- nothing was posted; preview the count again")
            posted = []
            for r in rows:
                if not r["diff"]: continue
                if r["system"] + r["diff"] < 0:           # (unreachable after the checks above; the ledger refuses it anyway)
                    raise InsufficientStockError(f"{r['sku']}: the count would take stock below zero")
                s = self.adjust_stock(warehouse_id, r["sku"], r["diff"], reason, actor, approved_by)
                posted.append({"sku": r["sku"], "name": r["name"], "from": r["system"], "to": s.on_hand, "diff": r["diff"]})
            self.audit(actor, "stock_count_posted", "stock", warehouse_id, {"date": day, "lines": len(rows), "adjusted": len(posted),
                                                                            "units_up": sum(p["diff"] for p in posted if p["diff"] > 0),
                                                                            "units_down": -sum(p["diff"] for p in posted if p["diff"] < 0)}, approved_by)
        return {"warehouse_id": warehouse_id, "reason": reason, "lines": len(rows), "posted": posted}

    # ------------------------------------------------------------ clients
    def client_rows(self, q: str = "", include_inactive: bool = True, as_of: str | None = None) -> list[dict]:
        """Every customer with balance (= outstanding: the sum of their khata), days overdue (the oldest unpaid
        invoice after FIFO, as in aging()), credit use and open orders -- in four queries whatever the count."""
        conds, args = [], []
        if not include_inactive: conds.append("c.active=1")
        if q.strip():
            t = f"%{q.strip().lower()}%"
            conds.append("(lower(c.name) LIKE ? OR c.phone LIKE ? OR lower(c.customer_id) LIKE ? OR lower(COALESCE(r.name,'')) LIKE ? OR lower(COALESCE(c.address,'')) LIKE ?)")
            args += [t, t, t, t, t]
        rows = self._all("SELECT c.*, COALESCE(b.bal, 0) bal, r.name route_name FROM customers c "
                         "LEFT JOIN (SELECT customer_id, SUM(amount) bal FROM ledger GROUP BY customer_id) b ON b.customer_id = c.customer_id "
                         "LEFT JOIN routes r ON r.route_id = c.route_id" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY c.name", tuple(args))
        entries: dict[str, list] = {}
        for e in self._all("SELECT entry_id, customer_id, kind, amount, due_date, created_at, reversal_of FROM ledger ORDER BY created_at, rowid"):
            entries.setdefault(e["customer_id"], []).append(e)
        placeholders = ",".join("?" * len(OPEN_ORDER_STATES))
        open_orders = {r["customer_id"]: int(r["n"]) for r in self._all(f"SELECT customer_id, COUNT(*) n FROM orders WHERE status IN ({placeholders}) GROUP BY customer_id", OPEN_ORDER_STATES)}
        as_of_d = date.fromisoformat(as_of or today_iso())
        out = []
        for r in rows:
            cid = r["customer_id"]
            days, overdue_p = self._fifo_overdue(entries.get(cid, []), as_of_d)
            limit_p, bal_p = int(r["credit_limit"] or 0), int(r["bal"])
            out.append({"customer_id": cid, "name": r["name"], "phone": r["phone"], "address": r["address"] or "", "route_id": r["route_id"],
                        "route_name": r["route_name"], "tier": r["tier"], "language": r["language"], "credit_limit": to_rupees(limit_p),
                        "credit_days": int(r["credit_days"] or 0), "discount_pct": float(r["discount_pct"] or 0), "active": bool(r["active"]),
                        "balance": to_rupees(bal_p), "days_overdue": days, "overdue_balance": to_rupees(overdue_p),
                        "credit_used_pct": round(bal_p / limit_p * 100, 1) if limit_p > 0 else None, "over_limit": limit_p > 0 and bal_p > limit_p,
                        "open_orders": open_orders.get(cid, 0)})
        return out

    @staticmethod
    def _fifo_overdue(entries: list, as_of_d: date) -> tuple[int, int]:
        """(days the oldest unpaid invoice is past due, the unpaid amount past due) -- CollectionsMixin.aging's FIFO:
        a reversed entry and its reversal drop out, payments and credits settle the oldest invoices first."""
        if not entries: return 0, 0
        reversed_ids = {e["reversal_of"] for e in entries if e["reversal_of"]}
        live = [e for e in entries if not e["reversal_of"] and e["entry_id"] not in reversed_ids]
        credits = -sum(int(e["amount"]) for e in live if e["kind"] != "invoice")
        open_inv = []
        for e in (e for e in live if e["kind"] == "invoice"):
            amt = int(e["amount"])
            if credits >= amt: credits -= amt
            else: open_inv.append((e, amt - credits)); credits = 0
        if sum(a for _, a in open_inv) <= 0: return 0, 0
        oldest = min(open_inv, key=lambda t: t[0]["created_at"])[0]
        due = date.fromisoformat(oldest["due_date"]) if oldest["due_date"] else as_of_d
        overdue_p = sum(a for e, a in open_inv if e["due_date"] and date.fromisoformat(e["due_date"]) < as_of_d)
        return max(0, (as_of_d - due).days), overdue_p
