"""Catalogue-aware parsing for the offline stub model: turns
'Chaudhry Farms ko 20 urea aur 5 dap bhej do' into a customer ID and order
lines using the real product aliases. A real LLM does this itself; the stub
needs it to keep the demo and tests deterministic without a key."""
from __future__ import annotations

import re

from munshi.domain.repository import MunshiRepository

_QTY_ITEM = re.compile(r"(\d+)\s*(?:bag|bags|bori|boriyan|kg|ltr|litre|liter|units?|x)?\s*(?:of\s+)?([A-Za-z؀-ۿ][A-Za-z0-9؀-ۿ\- ]{1,30}?)(?=\s*(?:,|\baur\b|\band\b|\bor\b|\.|$|\d))", re.IGNORECASE)
_ID = re.compile(r"\b([A-Z]{1,3}-[A-Z0-9-]{2,})\b")
_MONEY = re.compile(r"(?:rs\.?|pkr|₨)?\s*([0-9][0-9,]{2,})", re.IGNORECASE)
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def parse_items(text: str, repo: MunshiRepository) -> list[dict]:
    products = repo.list_products()
    items: list[dict] = []
    for qty, phrase in _QTY_ITEM.findall(text):
        p_txt = phrase.strip().lower()
        best = None
        for p in products:
            keys = [p.sku.lower(), p.name.lower(), *[a.lower() for a in p.aliases]]
            if any(k in p_txt or p_txt in k for k in keys if len(k) > 2):
                best = p; break
        if best:
            items.append({"sku": best.sku, "qty": int(qty)})
    return items


def parse_customer(text: str, repo: MunshiRepository) -> str | None:
    m = re.search(r"\b(C-\d{3})\b", text)
    if m: return m.group(1)
    t = text.lower()
    best = None
    for c in repo.list_customers():
        name = c.name.lower()
        first = name.split()[0]
        if name in t or (len(first) > 3 and first in t) or c.phone in t:
            best = c.customer_id; break
    return best


def ids_in(text: str, prefix: str) -> list[str]:
    return [i for i in _ID.findall(text) if i.startswith(prefix + "-")]


def money_in(text: str) -> float:
    m = _MONEY.search(text)
    return float(m.group(1).replace(",", "")) if m else 0.0


def date_in(text: str) -> str | None:
    m = _DATE.search(text); return m.group(1) if m else None


def int_in(text: str) -> int:
    m = re.search(r"-?\d+", text); return int(m.group(0)) if m else 0


def parse_supplier(text: str, repo: MunshiRepository) -> str | None:
    m = re.search(r"\b(S-\d{3})\b", text)
    if m: return m.group(1)
    t = text.lower()
    for s in repo.list_suppliers():
        name = s.name.lower()
        first = name.split()[0].strip("(),")
        if name in t or (len(first) > 3 and first in t):
            return s.supplier_id
    return None


def method_in(text: str) -> str:
    t = text.lower()
    for m in ("jazzcash", "easypaisa", "cheque", "bank"):
        if m in t: return m
    if "check" in t: return "cheque"
    return "cash"
