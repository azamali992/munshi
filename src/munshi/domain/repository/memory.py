"""Learned names and order habits: memory that outlives a conversation (tables from migration V6).

LEARNED NAMES  A phrase a person of this business confirmed they mean by one customer / supplier / product
               ('Bhatti sahab' -> C-007). The repository stores and serves them; it does not decide what a message
               means (llm/resolve.py does) or when something was confirmed (platform.py does). The phrase comes in
               already normalised (`phrase_norm`, llm.text.phrase_key); this layer never normalises text.

               * newest confirmation wins: re-pointing a phrase closes its row and opens a new one that records the
                 previous entity, so the change is logged and the next card can say "(last time you meant ...)";
               * forgetting closes the row; nothing is ever deleted, and every change is audited (actor "memory");
               * the table lives in the business's own file, so another business can never see it.

CARD LINKS     What an approval card owes to memory, or may teach it (see migration V6). Settled when the card is
               decided: learning happens only for an approved card whose action ran and which raised no warnings.

HABITS         Read from the orders themselves, no table: a customer's recent confirmed orders (for "wahi order
               dobara"), the price a line was negotiated at, and how often a user has ordered for each customer (to
               rank "which one?" options -- never to pick one).

The learned-name table is cached per repository (per business) and re-read only when its signature -- row count,
highest id, active count, total uses -- changes: every write here bumps it, and so does anything that swaps the file's
contents underneath (a restore, another process), which a write-through flag alone would miss."""
from __future__ import annotations

import json

from munshi.domain.models import Order, now_iso
from munshi.domain.repository.approvals import ApprovalsMixin
from munshi.domain.repository.base import NotFoundError
from munshi.domain.repository.guarded import immediate_tx

ALIAS_KINDS = ("customer", "supplier", "product")
CONFIRMED = ("confirmed", "allocated", "dispatched", "delivered", "short")      # an order a human confirmed
OPEN = ("confirmed", "allocated", "dispatched")                                # ... and that is still on its way
MEMORY_ACTOR = "memory"


class MemoryMixin(ApprovalsMixin):
    # ------------------------------------------------------------ learned names: reads (cached)
    def _alias_signature(self) -> tuple:
        r = self._one("SELECT COUNT(*) n, COALESCE(MAX(alias_id), 0) m, COALESCE(SUM(active), 0) a, COALESCE(SUM(uses), 0) u FROM learned_aliases")
        return (r["n"], r["m"], r["a"], r["u"])

    def learned_aliases(self, kind: str | None = None) -> list[dict]:
        """The ACTIVE learned names (all kinds, or one), from the per-business cache."""
        with self._lock:
            sig = self._alias_signature()
            cache = getattr(self, "_alias_cache", None)
            if cache is None or cache[0] != sig:
                rows = [dict(r) for r in self._all("SELECT * FROM learned_aliases WHERE active=1 ORDER BY alias_id")]
                by_kind: dict[str, list[dict]] = {k: [] for k in ALIAS_KINDS}
                for r in rows:
                    by_kind.setdefault(r["entity_kind"], []).append(r)
                cache = (sig, rows, by_kind)
                self._alias_cache = cache
            return list(cache[2].get(kind, [])) if kind else list(cache[1])

    def invalidate_alias_cache(self) -> None:
        self._alias_cache = None

    def get_alias(self, alias_id: int) -> dict:
        r = self._one("SELECT * FROM learned_aliases WHERE alias_id=?", (int(alias_id),))
        if not r: raise NotFoundError(f"no such learned name: {alias_id}")
        return dict(r)

    def alias_history(self, limit: int = 200) -> list[dict]:
        """Every learned name, active or not, newest first (the log of what was taught, re-pointed and forgotten)."""
        return [dict(r) for r in self._all("SELECT * FROM learned_aliases ORDER BY alias_id DESC LIMIT ?", (limit,))]

    def _entity_name(self, kind: str, entity_id: str) -> str:
        getter = {"customer": self.get_customer, "supplier": self.get_supplier, "product": self.get_product}[kind]
        return getter(entity_id).name

    # ------------------------------------------------------------ learned names: writes
    def learn_alias(self, phrase_norm: str, phrase: str, kind: str, entity_id: str, source: str,
                    taught_by: str = "", confirmed_by: str = "") -> dict:
        """Remember that `phrase` means this entity. Returns {"status": "learned" | "same" | "repointed", "alias",
        "previous" (the closed row, when re-pointed)}. The newest confirmation wins; a change is logged, never
        overwritten."""
        if kind not in ALIAS_KINDS: raise ValueError(f"unknown kind: {kind}")
        phrase_norm, phrase = str(phrase_norm or "").strip(), str(phrase or "").strip()[:80]
        if not phrase_norm or len(phrase_norm) > 80: raise ValueError("a learned name needs a phrase")
        self._entity_name(kind, entity_id)                       # NotFoundError for an entity that doesn't exist
        who = taught_by or self._current_user()
        with immediate_tx(self) as c:
            cur = self._one("SELECT * FROM learned_aliases WHERE entity_kind=? AND phrase_norm=? AND active=1", (kind, phrase_norm))
            if cur and cur["entity_id"] == entity_id:
                return {"status": "same", "alias": dict(cur), "previous": None}
            stamp = now_iso()
            if cur:
                # the partial unique index allows one ACTIVE row per phrase: close the old one before opening the new one
                c.execute("UPDATE learned_aliases SET active=0, ended_at=?, ended_by=?, end_reason='repointed' WHERE alias_id=?",
                          (stamp, confirmed_by or who, cur["alias_id"]))
            c.execute("INSERT INTO learned_aliases (phrase_norm, phrase, entity_kind, entity_id, source, taught_by, confirmed_by, taught_at, previous_entity_id) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (phrase_norm, phrase, kind, entity_id, source, who, confirmed_by or who, stamp, cur["entity_id"] if cur else None))
            new_id_ = c.lastrowid
            if cur:
                c.execute("UPDATE learned_aliases SET superseded_by=? WHERE alias_id=?", (new_id_, cur["alias_id"]))
            self.audit(MEMORY_ACTOR, "alias_repointed" if cur else "alias_learned", "learned_alias", str(new_id_),
                       {"phrase": phrase, "kind": kind, "entity_id": entity_id, "previous_entity_id": cur["entity_id"] if cur else None,
                        "source": source, "taught_by": who, "confirmed_by": confirmed_by or who})
        self.invalidate_alias_cache()
        return {"status": "repointed" if cur else "learned", "alias": self.get_alias(new_id_), "previous": dict(cur) if cur else None}

    def forget_alias(self, phrase_norm: str, kind: str | None = None, by: str = "") -> list[dict]:
        """Stop using a learned name (every kind, or one). Returns the rows closed. Nothing is deleted."""
        who = by or self._current_user()
        closed = []
        with immediate_tx(self) as c:
            q, a = "SELECT * FROM learned_aliases WHERE phrase_norm=? AND active=1", [str(phrase_norm or "").strip()]
            if kind: q += " AND entity_kind=?"; a.append(kind)
            for r in self._all(q, tuple(a)):
                c.execute("UPDATE learned_aliases SET active=0, ended_at=?, ended_by=?, end_reason='forgotten' WHERE alias_id=?", (now_iso(), who, r["alias_id"]))
                self.audit(MEMORY_ACTOR, "alias_forgotten", "learned_alias", str(r["alias_id"]),
                           {"phrase": r["phrase"], "kind": r["entity_kind"], "entity_id": r["entity_id"], "by": who})
                closed.append(dict(r))
        self.invalidate_alias_cache()
        return closed

    def note_alias_use(self, alias_id: int) -> None:
        """A card that relied on this learned name was approved: one more confirmed use."""
        with self._tx() as c:
            c.execute("UPDATE learned_aliases SET uses=uses+1, last_used_at=? WHERE alias_id=?", (now_iso(), int(alias_id)))
        self.invalidate_alias_cache()

    # ------------------------------------------------------------ approval cards and memory
    def link_card(self, approval_id: str, links: list[dict]) -> None:
        """Record what a (just raised) card owes to memory ('used') or may teach it ('learn')."""
        with self._tx() as c:
            for ln in links:
                c.execute("INSERT OR IGNORE INTO alias_card_links (approval_id, link, entity_kind, phrase_norm, phrase, entity_id, alias_id, "
                          "previous_entity_id, source, taught_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (approval_id, ln["link"], ln["kind"], ln["phrase_norm"], ln["phrase"], ln["entity_id"], ln.get("alias_id"),
                           ln.get("previous_entity_id"), ln.get("source", ""), ln.get("taught_by", ""), now_iso()))

    def card_links(self, approval_id: str) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM alias_card_links WHERE approval_id=? ORDER BY link DESC, entity_kind, phrase_norm", (approval_id,))]

    def settle_card_links(self, approval_id: str, approved: bool, learn: bool, by: str = "", why_not: str = "") -> list[dict]:
        """The card was decided. Approved: each learned name it relied on gets a confirmed use, and -- only when
        `learn` (its action ran and it raised no warnings) -- the wording it came from is learned. Rejected: nothing
        is learned or counted. Each link records what happened to it. Returns the learn results."""
        out = []
        for ln in self.card_links(approval_id):
            if ln["outcome"]:
                continue                                       # already settled
            if not approved:
                outcome = "rejected"
            elif ln["link"] == "used":
                if ln["alias_id"]:
                    self.note_alias_use(ln["alias_id"])
                outcome = "confirmed"
            elif not learn:
                outcome = f"skipped:{why_not or 'not learnable'}"[:60]
            else:
                try:
                    res = self.learn_alias(ln["phrase_norm"], ln["phrase"], ln["entity_kind"], ln["entity_id"], f"{ln['source'] or 'card'}:{approval_id}",
                                           taught_by=ln["taught_by"], confirmed_by=by)
                    outcome = res["status"]
                    out.append(res)
                except (NotFoundError, ValueError) as e:
                    outcome = f"skipped:{e}"[:60]
            with self._tx() as c:
                c.execute("UPDATE alias_card_links SET outcome=?, settled_at=? WHERE approval_id=? AND link=? AND entity_kind=? AND phrase_norm=?",
                          (outcome, now_iso(), approval_id, ln["link"], ln["entity_kind"], ln["phrase_norm"]))
        return out

    # ------------------------------------------------------------ habits (computed from orders, no table)
    def confirmed_orders(self, customer_id: str, n: int = 3) -> list[Order]:
        """The customer's last `n` orders that a human confirmed (confirmed or further along), newest first."""
        marks = ",".join("?" * len(CONFIRMED))
        rows = self._all(f"SELECT * FROM orders WHERE customer_id=? AND status IN ({marks}) ORDER BY created_at DESC, rowid DESC LIMIT ?",
                         (customer_id, *CONFIRMED, int(n)))
        return [self._order_from_row(r) for r in rows]

    def negotiated_prices(self, order_id: str) -> dict[str, float]:
        """{sku: rupees} for the lines of this order that were priced by hand (not the customer's list price), as the
        order's own audit recorded it when it was created or last edited."""
        r = self._one("SELECT payload FROM audit WHERE entity_id=? AND action IN ('create_order', 'order_edited') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                      (order_id,))
        if not r:
            return {}
        try:
            return {str(o["sku"]): float(o["price"]) for o in json.loads(r["payload"]).get("price_overrides") or []}
        except (ValueError, TypeError, KeyError):
            return {}

    def customer_usage(self, user: str, days: int = 180) -> dict[str, int]:
        """How many orders this user has booked for each customer lately: ranks the options of a "which one?" question."""
        if not str(user or "").strip():
            return {}
        rows = self._all("SELECT customer_id, COUNT(*) n FROM orders WHERE lower(created_by)=lower(?) AND created_at >= datetime('now', ?) GROUP BY customer_id",
                         (user.strip(), f"-{int(days)} days"))
        return {r["customer_id"]: int(r["n"]) for r in rows}
