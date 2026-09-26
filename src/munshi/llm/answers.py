"""Tool results as short sentences a shop owner reads: names instead of IDs, "Rs 84,000" instead of 84000.0,
the top few rows of a list with "...and 12 more", and a helpful sentence for an empty list. No JSON, no
internal field names.

`render(tool, data, repo, text, args)` returns the sentence for one tool result, in the language of the
message it answers (English, Roman Urdu, or Urdu script), or None when there is no formatter for it (the
caller then keeps whatever it had). It never raises. Reads only (repository getters, for names).

The platform shows the sentence and keeps the raw result after it as a folded "details" block
(platform.DETAILS), so nothing the old reply carried is lost for the app or for the audit of a turn.

NATIVE-SPEAKER CHECK WANTED: the Urdu-script strings (the "ur" entries) were written carefully but not by a
native speaker; the ones marked (?) most need a look."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from munshi.llm.text import fold, is_urdu, words

log = logging.getLogger("munshi.answers")

TOP = 8
WHOLE_UP_TO = 15        # a list this short is shown whole (cutting 10 products to 8 "...and 2 more" only made people ask again)

# A message is Roman Urdu (not English) if it uses any of these everyday words.
_RU = frozenset("""hai hain hein he hy ka ki ke ko kya kia kitna kitni kitne kis kaun kon ne se aur mein mei baqi baaki dikhao dikha batao bata
aaj aj kal wala wali wale karo kardo kar dena dene lene lena bhej bhejo jama diye diya tha thi ye yeh wo woh sab maal paise paisay hisaab hisab
khata udhaar udhar abhi kab kahan konsa konsi kaunsa naya nayi pichla pichle hua hui huay gaya gayi aaye aaya aayi ayein brha barha""".split())


def lang_of(text: str) -> str:
    """'ur' for Urdu script, 'ru' for Roman Urdu, 'en' for English."""
    if is_urdu(text or ""):
        return "ur"
    return "ru" if set(words(fold(text or ""))) & _RU else "en"


def rs(v: Any) -> str:
    try:
        x = float(v or 0)
    except (TypeError, ValueError):
        return str(v)
    s = f"Rs {abs(x):,.0f}" if float(x).is_integer() else f"Rs {abs(x):,.2f}"
    return ("-" if x < 0 else "") + s


def _n(v: Any) -> str:
    try:
        x = float(v)
        return f"{int(x):,}" if x.is_integer() else f"{x:,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _day(iso: Any) -> str:
    """'2026-09-25T...' -> '25 Sep', on the BUSINESS's calendar: a timestamp (stored in UTC) is read in the business's
    time zone, so a payment made at 02:00 in Pakistan says today, not yesterday. A bare date is taken as it is."""
    s = str(iso or "")
    if len(s) > 10 and re.match(r"\d{4}-\d{2}-\d{2}[T ]", s):
        try:
            from munshi.domain.models import to_business_date
            s = to_business_date(s).isoformat()
        except Exception:
            pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return str(iso or "")
    mon = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[int(m.group(2)) - 1]
    return f"{int(m.group(3))} {mon}"


class _Ctx:
    def __init__(self, repo, text: str, args: dict) -> None:
        self.repo, self.text, self.args = repo, text or "", dict(args or {})
        self.lang = lang_of(self.text)
        # 'for all the products?', 'sab dikhao', 'puri list': the whole list, never cut to the top few
        self.all = bool(re.search(r"\b(all|sab|sabhi|saare|sare|saari|sari|tamam|puri|poori|pura|poora|full|complete|every|mukammal)\b|سب|تمام|پوری", fold(self.text)))

    def t(self, en: str, ru: str | None = None, ur: str | None = None, **kw) -> str:
        s = {"en": en, "ru": ru or en, "ur": ur or en}[self.lang]
        return s.format(**kw)

    def more(self, n: int) -> str:
        if n <= 0:
            return ""
        return self.t(" ...and {n} more.", " ...aur {n} mazeed.", " ...اور {n} مزید۔", n=n)

    def _get(self, fn, key):
        try:
            return fn(key)
        except Exception:
            return None

    def product(self, sku: str) -> str:
        p = self._get(self.repo.get_product, str(sku or ""))
        return p.name if p else str(sku or "")

    def godown(self, wid: str) -> str:
        w = self._get(self.repo.get_warehouse, str(wid or ""))
        return w.name if w else str(wid or "")

    def customer(self, cid: str) -> str:
        c = self._get(self.repo.get_customer, str(cid or ""))
        return c.name if c else str(cid or "")

    def supplier(self, sid: str) -> str:
        s = self._get(self.repo.get_supplier, str(sid or ""))
        return s.name if s else str(sid or "")

    def route(self, rid: str) -> str:
        r = self._get(self.repo.get_route, str(rid or ""))
        return r.name if r else str(rid or "")

    def vehicle(self, vid: str) -> str:
        v = self._get(self.repo.get_vehicle, str(vid or ""))
        return v.plate if v else str(vid or "")

    def items(self, items) -> str:
        return ", ".join(f"{_n(i.get('qty'))} {self.product(i.get('sku'))}" for i in items or [] if isinstance(i, dict))

    def join(self, parts: list[str]) -> str:
        return "; ".join(p for p in parts if p)


def _listing(c: _Ctx, rows: list, fmt: Callable[[Any], str], top: int | None = None) -> str:
    top = len(rows) if c.all or (top is None and len(rows) <= WHOLE_UP_TO) else (top or TOP)
    shown = [fmt(r) for r in rows[:top]]
    return c.join(shown) + (c.more(len(rows) - top) if len(rows) > top else ".")


# ------------------------------------------------------------------ reads
_LOW_ASK = re.compile(r"\b(kam|low|khatam|reorder|mangwana|mangwa\w*|short|running out)\b|کم")


def _stock(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not rows:
        return c.t("No stock is recorded yet.", "Abhi koi stock darj nahi.", "ابھی کوئی اسٹاک درج نہیں۔")
    # 'aur vehari mei?' / 'sirf vehari ka': only the godown the question names
    try:
        from munshi.llm.parse import warehouses_in
        named = warehouses_in(c.text, c.repo) if c.text else []
    except Exception:
        named = []
    if len(named) == 1 and any(r.get("warehouse_id") == named[0] for r in rows):
        rows = [r for r in rows if r.get("warehouse_id") == named[0]]
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r.get("sku"), []).append(r)
    many_godowns = len({r.get("warehouse_id") for r in rows}) > 1
    only = c.godown(named[0]) if len(named) == 1 and not many_godowns else ""

    def one(sku, levels):
        total = sum(int(x.get("available") or 0) for x in levels)
        where = ""
        if many_godowns:
            where = " (" + ", ".join(f"{c.godown(x.get('warehouse_id'))} {_n(x.get('available'))}" for x in levels if int(x.get("on_hand") or 0) or int(x.get("available") or 0)) + ")"
            where = "" if where == " ()" else where
        reserved = sum(int(x.get("reserved") or 0) for x in levels)
        res = c.t(", {r} reserved", ", {r} reserved", "، {r} ریزرو", r=_n(reserved)) if reserved else ""
        low = ""
        try:
            p = c.repo.get_product(sku)
            if getattr(p, "min_stock", 0) and total <= p.min_stock:
                low = c.t(" -- LOW (reorder level {m})", " -- KAM hai (reorder {m})", " — کم ہے (ری آرڈر {m})", m=_n(p.min_stock))
        except Exception:
            pass
        return f"{c.product(sku)}: {_n(total)}" + c.t(" available", " available", " دستیاب") + res + where + low
    if len(by) == 1:
        sku, levels = next(iter(by.items()))
        return c.t("Stock -- ", "Stock -- ", "اسٹاک — ") + (f"{only}: " if only else "") + one(sku, levels) + "."
    order = sorted(by.items(), key=lambda kv: c.product(kv[0]))
    if len(by) > 1 and _LOW_ASK.search(fold(c.text)):
        # 'kya kya kam hai jo mangwana chahiye': only what is at or below its reorder level (at the godown named, if any)
        def low(kv):
            try:
                return sum(int(x.get("available") or 0) for x in kv[1]) <= int(c.repo.get_product(kv[0]).min_stock or 0)
            except Exception:
                return False
        lows = [kv for kv in order if low(kv)]
        where = f" ({c.godown(named[0])})" if len(named) == 1 else ""
        if not lows:
            return c.t("Nothing is below its reorder level{w}.", "Koi cheez reorder level se kam nahi{w}.", "کوئی چیز ری آرڈر سے کم نہیں{w}۔", w=where)
        return c.t("Running low{w}, order these: ", "Kam hai{w}, ye mangwa lein: ", "کم ہے{w}، یہ منگوا لیں: ", w=where) + _listing(c, lows, lambda kv: one(*kv))
    head = c.t("Stock available now, {n} products: ", "Is waqt stock, {n} products: ", "اس وقت اسٹاک، {n} اشیاء: ", n=len(order))
    return head + _listing(c, order, lambda kv: one(*kv))


def _khata(d, c: _Ctx) -> str:
    cust = (d or {}).get("customer") or {}
    name = cust.get("name") or c.customer(cust.get("customer_id"))
    bal = float((d or {}).get("outstanding") or 0)
    ag = (d or {}).get("aging") or {}
    if bal > 0:
        s = c.t("{name} owes {amt}", "{name} ke {amt} baqi hain", "{name} کے ذمے {amt} باقی ہیں", name=name, amt=rs(bal))
        if ag.get("days_overdue"):
            s += c.t(" (oldest unpaid bill {d} days overdue)", " (sab se purana bill {d} din se overdue)", " (سب سے پرانا بل {d} دن سے واجب الادا)", d=int(ag["days_overdue"]))
    elif bal < 0:
        s = c.t("{name} has {amt} in advance", "{name} ka {amt} advance hai", "{name} کا {amt} ایڈوانس ہے", name=name, amt=rs(-bal))
    else:
        s = c.t("{name} owes nothing", "{name} ka kuch baqi nahi", "{name} کا کچھ باقی نہیں", name=name)
    s += "."
    pays = [e for e in (d or {}).get("recent") or [] if e.get("kind") == "payment" and float(e.get("amount") or 0) < 0]
    if pays:
        p = pays[-1]
        s += c.t(" Last payment {amt} on {day}.", " Aakhri payment {amt}, {day}.", " آخری ادائیگی {amt}، {day}۔", amt=rs(-float(p["amount"])), day=_day(p.get("created_at")))
    pr = (d or {}).get("promise")
    if isinstance(pr, dict) and pr.get("amount"):
        s += c.t(" Promised {amt} by {day}.", " {amt} ka wada, {day} tak.", " {day} تک {amt} کا وعدہ۔", amt=rs(pr["amount"]),
                 day=_day(pr.get("promised_date") or pr.get("date") or ""))
    elif re.search(r"\b(wada|waada|promise)\b|وعدہ", fold(c.text)):
        s += c.t(" No open promise to pay is on record.", " Koi khula wada darj nahi.", " کوئی کھلا وعدہ درج نہیں۔")
    if re.search(r"\b(pdf|statement|ledger)\b|اسٹیٹمنٹ", fold(c.text)):
        # a statement is a document: the app shares it from the customer's page -- chat never sends a file itself
        s += c.t(" The statement is on {name}'s page in Customers: 'Statement' opens it (print or save as PDF), 'Share statement' sends its link on "
                 "WhatsApp. I can't send files from chat.",
                 " Statement Customers mein {name} ke page par hai: 'Statement' se khulta hai (print / PDF), 'Share statement' se WhatsApp par link jata hai. "
                 "Chat se file nahi bhej sakta.",
                 " اسٹیٹمنٹ گاہکوں میں {name} کے صفحے پر ہے (پرنٹ یا واٹس ایپ پر لنک)؛ چیٹ سے فائل نہیں بھیج سکتا۔", name=name)
    limit = float(cust.get("credit_limit") or 0)
    if limit and re.search(r"\blimit\b|\bcredit\b|حد", fold(c.text)):
        s += c.t(" Credit limit {lim}; room left {room}.", " Credit limit {lim}; abhi {room} ki gunjaish.", " کریڈٹ کی حد {lim}؛ ابھی {room} کی گنجائش۔",
                 lim=rs(limit), room=rs(max(0.0, limit - bal)))
    return s


def _aging(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not rows:
        return c.t("Nobody owes us anything right now.", "Is waqt kisi ka kuch baqi nahi.", "اس وقت کسی کے ذمے کچھ باقی نہیں۔")
    total = sum(float(r.get("balance") or 0) for r in rows)
    head = c.t("{n} customers owe {amt} in all, most overdue first: ", "{n} customers ke kul {amt} baqi hain, sab se purane pehle: ",
               "{n} گاہکوں کے ذمے کل {amt} باقی ہیں، سب سے پرانے پہلے: ", n=len(rows), amt=rs(total))
    if re.search(r"sab se (zyada|ziada|zaida)|\b(most|largest|biggest|highest)\b|سب سے زیادہ", fold(c.text)):
        # 'sab se zyada kis ka he': the answer is the largest balance, read from the rows in code -- then the list
        top = max(rows, key=lambda r: float(r.get("balance") or 0))
        head = c.t("{w} owes the most: {a}. ", "Sab se zyada {w} ke baqi hain: {a}. ", "سب سے زیادہ {w} کے ذمے ہیں: {a}۔ ", w=top.get("name"),
                   a=rs(top.get("balance"))) + head
    return head + _listing(c, rows, lambda r: f"{r.get('name')} {rs(r.get('balance'))}" + (c.t(" ({d} days)", " ({d} din)", " ({d} دن)", d=int(r["days_overdue"])) if r.get("days_overdue") else ""))


_NOT_GONE = re.compile(r"nahi (gaye|gaya|gayi|gay|nikle|nikla)|not (yet )?(gone|dispatched|sent|delivered)|abhi tak nahi|pending|khule|open orders|"
                       r"kaun se orders baqi|نہیں گئے")
_HOW_MANY = re.compile(r"\b(kitne|kitni|kitna|how many|count)\b|کتنے|کتنی")


def _orders(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not c.args.get("status") and _NOT_GONE.search(fold(c.text)):
        # 'kaunse orders abhi tak nahi gaye': only what hasn't left the godown -- never delivered or cancelled ones
        rows = [r for r in rows if r.get("status") in ("draft", "confirmed", "allocated")]
        if not rows:
            return c.t("Every order has gone out: nothing is waiting as a draft, confirmed or reserved.", "Sab orders ja chuke hain: koi draft, confirmed ya reserve order baqi nahi.",
                       "سب آرڈر جا چکے ہیں: کوئی آرڈر باقی نہیں۔")
    a = c.args
    what = c.product(a["sku"]) + " " if a.get("sku") else ""
    who = c.customer(a["customer_id"]) if a.get("customer_id") else ""
    status = f"{a['status']} " if a.get("status") else ""
    days = int(a.get("days") or 0)
    if not rows:
        if who:
            return c.t("No {s}orders for {who} yet.", "{who} ka koi {s}order nahi.", "{who} کا کوئی {s}آرڈر نہیں۔", s=status, who=who)
        if days == 1:
            return c.t("No {w}{s}orders yet today.", "Aaj abhi tak koi {w}{s}order nahi aaya.", "آج ابھی تک کوئی {w}{s}آرڈر نہیں آیا۔", w=what, s=status)
        if what:
            return c.t("No {w}orders in the last {d} days.", "Pichle {d} din mein {w}ka koi order nahi.", "پچھلے {d} دن میں {w}کا کوئی آرڈر نہیں۔", w=what, d=days or 30)
        return c.t("No {s}orders yet.", "Abhi koi {s}order nahi.", "ابھی کوئی {s}آرڈر نہیں۔", s=status)
    scope = (c.t(" for {who}", " -- {who}", " — {who}", who=who) if who else "") + (c.t(" with {w}", " ({w})", " ({w})", w=what.strip()) if what else "")
    head = c.t("Latest {s}orders{sc}: ", "Taaza {s}orders{sc}: ", "تازہ {s}آرڈر{sc}: ", s=status, sc=scope)
    if _HOW_MANY.search(fold(c.text)):
        # 'aaj kitne orders aaye': the count first, read from the rows in code
        n = len(rows)
        head = (c.t("{n} {s}order(s){sc}{when}: ", "{n} {s}orders{sc}{when}: ", "{n} {s}آرڈر{sc}{when}: ", n=n, s=status, sc=scope,
                    when=c.t(" today", " aaj", " آج") if int(a.get("days") or 0) == 1 else ""))
    # people know an order by who it is for, what is on it and when -- not by its code (the ids stay in the details)
    return head + _listing(c, rows, lambda o: f"{o.get('customer_name') or c.customer(o.get('customer_id'))} -- {c.items(o.get('items'))}, "
                           f"{rs(o.get('total'))} ({o.get('status')}, {_day(o.get('created_at'))})")


def _order(d, c: _Ctx) -> str:
    o = d or {}
    return c.t("Order {id} for {who}: {items}, {amt} -- {st}.", "Order {id} ({who}): {items}, {amt} -- {st}.", "آرڈر {id} ({who}): {items}، {amt} — {st}۔",
               id=o.get("order_id"), who=o.get("customer_name") or c.customer(o.get("customer_id")), items=c.items(o.get("items")), amt=rs(o.get("total")), st=o.get("status"))


_METHOD_OF = {"cash": "cash", "bank": "bank", "online": "bank", "transfer": "bank", "jazzcash": "jazzcash", "easypaisa": "easypaisa", "cheque": "cheque",
              "check": "cheque"}
_METHOD_ASK = (("bank", r"\b(bank|online|transfer|ibft)\b|بینک"), ("jazzcash", r"\bjazz ?cash\b|جاز"), ("easypaisa", r"\beasy ?paisa\b|ایزی"),
               ("cheque", r"\b(cheque|check|chq)\b|چیک"), ("cash", r"\b(cash|naqd|nakad|naqad)\b|نقد|کیش"))


def _method_asked(text: str) -> str:
    """The payment method a question is about ('bank mei kitna aya'), or ''."""
    f = fold(text)
    return next((m for m, rx in _METHOD_ASK if re.search(rx, f)), "")


def _payments_block(d, c: _Ctx) -> str:
    rows = [p for p in (d or {}).get("payments") or [] if isinstance(p, dict)]
    # a reversal (a bounced cheque) is not a payment: it is neither counted nor listed as one, but it is said
    revs = [p for p in rows if p.get("reversal_of") or float(p.get("amount") or 0) < 0]
    cancelled = {p.get("reversal_of") for p in revs if p.get("reversal_of")}
    # payments still in effect: not a reversal, and not the payment a reversal cancelled (the domain's payment_count)
    pays = [p for p in rows if not p.get("reversal_of") and float(p.get("amount") or 0) > 0 and p.get("entry_id") not in cancelled]
    start, end = (d or {}).get("start"), (d or {}).get("end")
    from munshi.domain.models import business_today
    today = business_today().isoformat()
    when = c.t("today", "aaj", "آج") if start == end == today else (_day(start) if start == end else f"{_day(start)} - {_day(end)}")
    rv = (d or {}).get("reversals") if isinstance((d or {}).get("reversals"), dict) else {}
    n_rev = int(rv.get("count") or len(revs))
    amt_rev = abs(float(rv.get("amount") if rv.get("amount") is not None else sum(float(p.get("amount") or 0) for p in revs)))
    rev_txt = c.t(" {n} payment(s) reversed (e.g. a bounced cheque): {a}.", " {n} payment reverse hui (maslan cheque bounce): {a}.",
                  " {n} ادائیگی واپس ہوئی (مثلاً چیک باؤنس): {a}۔", n=n_rev, a=rs(amt_rev)) if n_rev else ""
    if not pays:
        return c.t("No customer payments recorded {w}.", "{w} koi customer payment darj nahi hui.", "{w} کسی گاہک کی ادائیگی درج نہیں ہوئی۔", w=when) + rev_txt
    total = sum(float(p.get("amount") or 0) for p in pays)
    n = len(pays)
    how = _method_asked(c.text)
    if how:
        # 'bank mei kitna aya aaj': that method's payments first, then the day's total -- all read from the rows in code
        mine = [p for p in pays if _METHOD_OF.get(str(p.get("method") or "cash").lower(), "cash") == how]
        label = {"bank": "bank", "cash": "cash", "jazzcash": "JazzCash", "easypaisa": "Easypaisa", "cheque": "cheque"}[how]
        amt = sum(float(p.get("amount") or 0) for p in mine)
        part = (c.t("{w} by {m}: {a} in {n} payment(s): ", "{w} {m} se {a} aaye ({n} payment): ", "{w} {m} سے {a} آئے ({n} ادائیگی): ", w=when, m=label, a=rs(amt), n=len(mine))
                + _listing(c, mine, lambda p: f"{p.get('name')} {rs(p.get('amount'))}")) if mine else \
            c.t("Nothing came by {m} {w}.", "{w} {m} se kuch nahi aaya.", "{w} {m} سے کچھ نہیں آیا۔", w=when, m=label)
        return part + c.t(" All payments {w}: {a} ({n}).", " {w} kul payments: {a} ({n}).", " {w} کل ادائیگیاں: {a} ({n})۔", w=when, a=rs(total), n=n) + rev_txt
    head = c.t("Payments received {w}: {amt} in {n} payment(s): ", "{w} {n} payments aayin, kul {amt}: ", "{w} {n} ادائیگیاں آئیں، کل {amt}: ",
               w=when, amt=rs(total), n=n)
    return head + _listing(c, pays, lambda p: f"{p.get('name')} {rs(p.get('amount'))} ({p.get('method') or 'cash'})") + rev_txt


def _collection(d, c: _Ctx) -> str:
    d = d or {}
    if d.get("start") and d.get("start") == d.get("end"):
        return _payments_block(d, c)
    s = c.t("From {a} to {b}: collected {col} against {inv} invoiced ({pct}%).", "{a} se {b} tak: {inv} ke bill, {col} wusool ({pct}%).",
            "{a} سے {b} تک: {inv} کے بل، {col} وصول ({pct}%)۔", a=_day(d.get("start")), b=_day(d.get("end")), col=rs(d.get("collected")), inv=rs(d.get("invoiced")),
            pct=d.get("collection_rate_pct", 0))
    ag = d.get("aging") or {}
    if ag.get("total"):
        s += c.t(" Still owed: {amt}.", " Abhi baqi: {amt}.", " ابھی باقی: {amt}۔", amt=rs(ag["total"]))
    return s


def _cashbook(d, c: _Ctx) -> str:
    d = d or {}
    ins = [x for x in d.get("cash_in") or [] if isinstance(x, dict)]
    s = c.t("Cash book {day}: in {i}, driver hand-ins {h}, out {o}; net {n}.", "Cash book {day}: aaye {i}, driver se {h}, gaye {o}; net {n}.",
            "کیش بک {day}: آئے {i}، ڈرائیور سے {h}، گئے {o}؛ خالص {n}۔", day=_day(d.get("date")), i=rs(d.get("total_in")), h=rs(d.get("total_handins")),
            o=rs(d.get("total_out")), n=rs(d.get("net")))
    if ins:
        s += c.t(" Cash payments: ", " Cash payments: ", " نقد ادائیگیاں: ") + _listing(c, ins, lambda x: f"{x.get('who')} {rs(x.get('amount'))}")
    outs = [x for x in d.get("cash_out") or [] if isinstance(x, dict)]
    if outs and re.search(r"\b(kharch\w*|expense\w*|gaye|out)\b|خرچ", fold(c.text)):
        s += c.t(" Paid out: ", " Kharche: ", " خرچے: ") + _listing(c, outs, lambda x: f"{x.get('who')} {rs(x.get('amount'))}")
    # driver cash that should have come in and didn't: a memo beside the drawer figure (never inside `net`), always said
    short = float(d.get("total_shortfall") or 0)
    if short:
        who = ", ".join(dict.fromkeys(_no_codes(str(x.get("who") or "")) for x in d.get("shortfalls") or [] if isinstance(x, dict) and x.get("who")))
        s += c.t(" Driver cash short {a}{w}.", " Driver ka cash {a} short{w}.", " ڈرائیور کی نقدی {a} کم{w}۔", a=rs(short), w=f" ({who})" if who else "")
    elif re.search(r"\b(driver|short|pura|poora)\b|ڈرائیور", fold(c.text)):
        s += c.t(" No driver cash is short.", " Driver ka cash pura hai, kuch short nahi.", " ڈرائیور کی نقدی پوری ہے۔")
    return s


_CODES = re.compile(r"\b(?:DSP|DEP|STP|ORD|REM|PRM|TRF)-[A-Z0-9]{4,}\b")


def _no_codes(note: str) -> str:
    """A stored note ('DSP-FCD8E6DF (MNK-4521) cash short on DEP-6D5E9179; check STP-20509BF3 Bhatti Kisan Store Rs 15,000') as a
    person reads it ('MNK-4521 cash short; check Bhatti Kisan Store Rs 15,000'): internal record codes out."""
    t = _CODES.sub("", note)
    t = re.sub(r"\(([^()]*)\)", r"\1", t)
    t = re.sub(r"\b(on|for|at)\s*(?=[;,.]|$)", "", t)
    t = re.sub(r"\s+([;,.])", r"\1", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ;,")


def _digest(d, c: _Ctx) -> str:
    d = d or {}
    o, disp, cash, rec = d.get("orders") or {}, d.get("dispatch") or {}, d.get("cash") or {}, d.get("receivables") or {}
    s = c.t("Today: {no} orders worth {ov}; {st} stops ({dl} delivered); office payments {op}, expenses {ex}; customers owe {rv}.",
            "Aaj: {no} orders, {ov}; {st} stops ({dl} deliver); office payments {op}, kharcha {ex}; customers ke {rv} baqi.",
            "آج: {no} آرڈر، {ov}؛ {st} اسٹاپ ({dl} ڈیلیور)؛ دفتر میں ادائیگیاں {op}، خرچہ {ex}؛ گاہکوں کے ذمے {rv}۔",
            no=o.get("count", 0), ov=rs(o.get("value")), st=disp.get("stops", 0), dl=disp.get("delivered", 0), op=rs(cash.get("office_payments")),
            ex=rs(cash.get("expenses")), rv=rs(rec.get("total")))
    low = [x for x in d.get("low_stock") or [] if isinstance(x, dict)]
    if low:
        s += c.t(" Low stock: ", " Kam stock: ", " کم اسٹاک: ") + _listing(c, low, lambda x: f"{c.product(x.get('sku'))} {_n(x.get('available'))} ({c.godown(x.get('warehouse_id'))})")
    return s


def _slow(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    days = int(c.args.get("days") or (rows[0].get("days_without_sale") if rows else 30) or 30)
    if not rows:
        return c.t("Everything in stock has sold in the last {d} days.", "Pichle {d} din mein sab maal bika hai.", "پچھلے {d} دن میں سارا مال بکا ہے۔", d=days)
    head = c.t("Not sold in the last {d} days ({n} products): ", "Pichle {d} din se nahi bika ({n} products): ", "پچھلے {d} دن سے نہیں بکا ({n} اشیاء): ", d=days, n=len(rows))
    return head + _listing(c, rows, lambda r: f"{r.get('name') or c.product(r.get('sku'))} {_n(r.get('on_hand'))}" + c.t(" on hand", " pada", " موجود")
                           + f" ({rs(r.get('value_at_cost'))})")


def _top(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    days = int(c.args.get("days") or 30)
    if not rows:
        return c.t("No sales in the last {d} days.", "Pichle {d} din mein koi sale nahi.", "پچھلے {d} دن میں کوئی سیل نہیں۔", d=days)
    head = c.t("Top customers, last {d} days: ", "Pichle {d} din ke top customers: ", "پچھلے {d} دن کے بڑے گاہک: ", d=days)
    return head + _listing(c, rows, lambda r: f"{r.get('name')} {rs(r.get('revenue'))}")


def _sales(d, c: _Ctx) -> str:
    d = d or {}
    s = c.t("Sales {a} to {b}: {rev} from {n} invoices, margin {m} ({p}%).", "Sale {a} se {b}: {rev}, {n} bill, margin {m} ({p}%).",
            "سیل {a} سے {b}: {rev}، {n} بل، منافع {m} ({p}%)۔", a=_day(d.get("start")), b=_day(d.get("end")), rev=rs(d.get("revenue")), n=d.get("invoices", 0),
            m=rs(d.get("gross_margin")), p=d.get("margin_pct", 0))
    cust = [x for x in d.get("by_customer") or [] if isinstance(x, dict)]
    if cust:
        s += c.t(" Biggest buyers: ", " Sab se zyada: ", " سب سے زیادہ: ") + _listing(c, cust[:3], lambda x: f"{x.get('name')} {rs(x.get('revenue'))}")
    prods = [x for x in d.get("by_product") or [] if isinstance(x, dict)]
    if prods and re.search(r"\b(margin|munafa|profit)\b|منافع", fold(c.text)) and re.search(r"\b(cheez|cheezen|product|products|maal|item|items)\b|چیز|مال", fold(c.text)):
        # margin BY PRODUCT, largest first, from the report's own product lines
        ranked = sorted(prods, key=lambda x: float(x.get("margin") or 0), reverse=True)
        def pct(x):
            r = float(x.get("revenue") or 0)
            return f" ({float(x.get('margin') or 0) / r * 100:.1f}%)" if r else ""
        s += c.t(" Margin by product: ", " Product ke hisaab se margin: ", " ہر چیز کا منافع: ") + _listing(
            c, ranked, lambda x: f"{x.get('name') or c.product(x.get('sku'))} {rs(x.get('margin'))}{pct(x)}")
        return s + _caveat(d, c)
    if prods and re.search(r"\b(cheez|cheezen|product|products|maal|item|items|bik\w*|seller)\b|چیز|مال", fold(c.text)):
        key = lambda x: float(x.get("revenue") or x.get("qty") or 0)  # noqa: E731
        s += c.t(" Best sellers: ", " Sab se zyada bikne wali: ", " سب سے زیادہ بکنے والی: ") + _listing(
            c, sorted(prods, key=key, reverse=True)[:3], lambda x: f"{x.get('name') or c.product(x.get('sku'))} {rs(x.get('revenue'))}")
    return s + _caveat(d, c)


def _caveat(d: dict, c: _Ctx) -> str:
    """When some sales have no cost on record, the margin is overstated: say so, with the margin on the costed sales only,
    instead of presenting (say) 100% as fact."""
    if d.get("margin_reliable") is not False:
        return ""
    miss = d.get("cost_missing") or {}
    costed = d.get("costed_margin_pct")
    cm = c.t(" Margin on the sales that do have a cost: {p}%.", " Jin ki laagat maloom hai un par margin: {p}%.", " جن کی لاگت معلوم ہے ان پر منافع: {p}%۔",
             p=costed) if costed is not None else ""
    if c.lang == "en" and d.get("caveat"):
        return f" Note: {str(d['caveat']).rstrip('.')}." + cm
    return c.t(" Note: {amt} of sales ({n} bill(s)) have no cost on record, so the margin above is too high.",
               " Note: {amt} ki sale ({n} bill) ki laagat darj nahi, is liye upar wala munafa zyada dikh raha hai.",
               " نوٹ: {amt} کی سیل ({n} بل) کی لاگت درج نہیں، اس لیے اوپر کا منافع زیادہ دکھ رہا ہے۔",
               amt=rs(miss.get("revenue")), n=int(miss.get("count") or 0)) + cm


def _profit(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("{a} to {b}: sales {rev}, cost of goods {cogs}, gross profit {gm}, expenses {ex}, net profit {net}.",
               "{a} se {b}: sale {rev}, maal ki laagat {cogs}, gross munafa {gm}, kharche {ex}, net munafa {net}.",
               "{a} سے {b}: سیل {rev}، مال کی لاگت {cogs}، مجموعی منافع {gm}، خرچے {ex}، خالص منافع {net}۔",
               a=_day(d.get("start")), b=_day(d.get("end")), rev=rs(d.get("revenue")), cogs=rs(d.get("cost_of_goods")), gm=rs(d.get("gross_margin")),
               ex=rs(d.get("expenses")), net=rs(d.get("net"))) + _caveat(d, c)


def _valuation(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Stock on hand is worth {cost} at cost ({sale} at sale price).", "Pade maal ki qeemat laagat par {cost} (bechne par {sale}).",
               "موجود مال کی قیمت لاگت پر {cost} (فروخت پر {sale})۔", cost=rs(d.get("at_cost")), sale=rs(d.get("at_sale")))


def _stock_ledger(d, c: _Ctx) -> str:
    d = d or {}
    moves = [m for m in d.get("moves") or [] if isinstance(m, dict)]
    head = c.t("{p} -- last movements: ", "{p} -- aakhri movements: ", "{p} — آخری نقل و حرکت: ", p=d.get("name") or c.product(d.get("sku")))
    if not moves:
        return head + c.t("none.", "koi nahi.", "کوئی نہیں۔")
    return head + _listing(c, moves, lambda m: f"{int(m.get('delta') or 0):+,} {m.get('kind')} {c.godown(m.get('warehouse_id'))} ({_day(m.get('created_at'))})")


def _payables(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict) and float(r.get("balance") or 0)]
    if not rows:
        return c.t("We don't owe any supplier anything.", "Kisi supplier ka kuch dena nahi.", "کسی سپلائر کا کچھ دینا نہیں۔")
    total = sum(float(r.get("balance") or 0) for r in rows)
    head = c.t("We owe suppliers {amt}: ", "Suppliers ko kul {amt} dena hai: ", "سپلائرز کو کل {amt} دینا ہے: ", amt=rs(total))
    return head + _listing(c, rows, lambda r: f"{r.get('name')} {rs(r.get('balance'))}")


def _supplier_khata(d, c: _Ctx) -> str:
    d = d or {}
    name = (d.get("supplier") or {}).get("name") or ""
    bal = float(d.get("balance") or 0)
    s = (c.t("We owe {n} {a}.", "{n} ko {a} dena hai.", "{n} کو {a} دینا ہے۔", n=name, a=rs(bal)) if bal > 0 else
         c.t("We owe {n} nothing.", "{n} ka kuch dena nahi.", "{n} کا کچھ دینا نہیں۔", n=name))
    pays = [e for e in d.get("recent") or [] if e.get("kind") == "payment"]
    if pays:
        s += c.t(" Last payment {a} on {day}.", " Aakhri payment {a}, {day}.", " آخری ادائیگی {a}، {day}۔", a=rs(-float(pays[-1].get("amount") or 0)), day=_day(pays[-1].get("created_at")))
    return s


def _suppliers(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    return c.t("Suppliers: ", "Suppliers: ", "سپلائرز: ") + _listing(c, rows, lambda r: f"{r.get('name')}" + (c.t(" (we owe {a})", " ({a} dena)", " ({a} دینا)", a=rs(r["balance"])) if float(r.get("balance") or 0) else ""))


def _broken(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not rows:
        return c.t("No broken promises -- everyone who promised has paid or still has time.", "Koi wada nahi toota.", "کوئی وعدہ نہیں ٹوٹا۔")
    head = c.t("{n} broken promise(s): ", "{n} wade toote: ", "{n} وعدے ٹوٹے: ", n=len(rows))
    return head + _listing(c, rows, lambda r: c.t("{who} promised {a} by {day}", "{who} ne {a} ka wada kiya tha, {day} tak", "{who} نے {day} تک {a} کا وعدہ کیا تھا",
                                                  who=r.get("name") or c.customer(r.get("customer_id")), a=rs(r.get("amount")), day=_day(r.get("date"))))


def _suggest(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not rows:
        return c.t("Nothing is ready to dispatch: no reserved (allocated) orders waiting.", "Dispatch ke liye kuch tayyar nahi.", "ڈسپیچ کے لیے کچھ تیار نہیں۔")
    return c.t("Suggested dispatch: ", "Dispatch ki tajweez: ", "ڈسپیچ کی تجویز: ") + _listing(c, rows, lambda r: (
        f"{c.route(r.get('route_id'))}: {len(r.get('order_ids') or [])} order(s), {_n(r.get('load_units'))} units"
        + (f" on {c.vehicle(r['vehicle_id'])}" if r.get("vehicle_id") else f" -- {r.get('note')}")))


def _one_stop(s: dict, c: _Ctx) -> str:
    """One stop the way a driver needs it at the door: who, where, what was loaded, and the bill for it."""
    who = s.get("customer_name") or c.customer(s.get("customer_id"))
    out = c.t("{w} (stop {n}, {st})", "{w} (stop {n}, {st})", "{w} (اسٹاپ {n}، {st})", w=who, n=s.get("sequence"), st=s.get("status"))
    if s.get("items"):
        out += c.t(": {i}", ": {i}", ": {i}", i=c.items(s["items"]))
    if s.get("order_total") is not None:
        out += c.t(" -- bill {a}, collect up to {a}", " -- bill {a}, zyada se zyada {a} lene hein", " — بل {a}", a=rs(s["order_total"]))
    if s.get("address"):
        out += c.t(". Address: {a}", ". Address: {a}", "۔ پتہ: {a}", a=s["address"])
    if s.get("phone") and _PHONE_ASK.search(fold(c.text)):
        out += c.t(". Phone: {p}", ". Phone: {p}", "۔ فون: {p}", p=s["phone"])
    if float(s.get("cash_collected") or 0):
        out += c.t(", cash taken {x}", ", cash liya {x}", "، نقد {x}", x=rs(s["cash_collected"]))
    return out + "."


_PHONE_ASK = re.compile(r"\b(number|phone|fone|mobile|contact|nmbr|no\.)\b|فون|نمبر")
_CASH_ASK = re.compile(r"(?=.*\b(cash|paise|paisay|raqam|collection|collected)\b)(?=.*\b(total|kul|mere paas|mere pas|jama karwana|jama karwane|hand in|"
                       r"collected so far|ab tak)\b)|(?=.*(نقد|کیش))(?=.*(کل|میرے پاس))")
_FIRST_STOP = re.compile(r"\b(pehla|pehle|pahla|first|agla|next)\b|پہلا|اگلا")


def _stops(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not rows:
        return c.t("No stops on this plan.", "Is plan mein koi stop nahi.", "اس پلان میں کوئی اسٹاپ نہیں۔")
    f = fold(c.text)
    if _CASH_ASK.search(f):
        # the driver's running cash: what he has collected on this run so far, stop by stop (to hand in)
        paid = [s for s in rows if float(s.get("cash_collected") or 0)]
        tot = sum(float(s.get("cash_collected") or 0) for s in paid)
        head = c.t("Cash collected on this run: {a}", "Is chakkar ka cash: {a}", "اس چکر کی نقدی: {a}", a=rs(tot))
        return head + (" (" + ", ".join(f"{s.get('customer_name') or c.customer(s.get('customer_id'))} {rs(s.get('cash_collected'))}" for s in paid) + ")"
                       if paid else "") + c.t(". Hand it in at the office.", ". Office mein jama karwa dein.", "۔ دفتر میں جمع کروا دیں۔")
    if (re.search(r"\b(address|pata|kahan|location)\b|پتہ", f) or _PHONE_ASK.search(f)) and _FIRST_STOP.search(f):
        # 'pehla stop kahan he, address?': the next open stop, with where it is
        nxt = next((s for s in sorted(rows, key=lambda s: int(s.get("sequence") or 0)) if s.get("status") == "pending"), None)
        if nxt is not None:
            return _one_stop(nxt, c)
    # a question about one customer's stop ('kitne paise lene hein chaudhry farms se', 'address kya he'): just theirs
    try:
        from munshi.llm.parse import customer_resolution
        who = customer_resolution(c.text, c.repo) if c.text else None
    except Exception:
        who = None
    if who is not None and who.ok:
        mine = [s for s in rows if s.get("customer_id") == who.id]
        if len(mine) == 1:
            return _one_stop(mine[0], c)
    return c.t("Stops, in order: ", "Stops, tarteeb se: ", "اسٹاپ، ترتیب سے: ") + _listing(c, rows, lambda s: (
        f"{s.get('sequence')}. {s.get('customer_name') or c.customer(s.get('customer_id'))} -- {s.get('status')}"
        + (f", {c.items(s['items'])}" if s.get("items") else "")
        + (f", {rs(s['order_total'])}" if s.get("order_total") is not None else "")
        + (f", cash {rs(s['cash_collected'])}" if float(s.get("cash_collected") or 0) else "")))


def _plan(d, c: _Ctx) -> str:
    d = d or {}
    s = c.t("Plan: {r} on {v}, {n} order(s), {st}.", "Plan: {r}, gaari {v}, {n} order, {st}.", "پلان: {r}، گاڑی {v}، {n} آرڈر، {st}۔",
            r=c.route(d.get("route_id")), v=c.vehicle(d.get("vehicle_id")), n=len(d.get("order_ids") or []), st=d.get("status"))
    if d.get("stops"):
        s += " " + _stops(d["stops"], c)
    return s


def _find_customer(d, c: _Ctx) -> str:
    d = d or {}
    if d.get("found"):
        return c.t("{n}: owes {a}.", "{n}: {a} baqi.", "{n}: {a} باقی۔", n=d.get("name"), a=rs(d.get("outstanding")))
    if d.get("ambiguous"):
        return c.t("More than one customer matches: ", "Kai customers milte hain: ", "کئی گاہک ملتے ہیں: ") + ", ".join(x.get("name", "") for x in d.get("candidates") or []) + "."
    return c.t("No customer by that name.", "Is naam ka koi customer nahi.", "اس نام کا کوئی گاہک نہیں۔")


def _search_products(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict)]
    if not rows:
        return c.t("No product by that name.", "Is naam ka koi maal nahi.", "اس نام کا کوئی مال نہیں۔")
    code = bool(re.search(r"\b(sku|code)\b", fold(c.text)))        # the product code only when that is what was asked
    return _listing(c, rows, lambda p: f"{p.get('name')}" + (f" (SKU {p.get('sku')})" if code else "") + f" {rs(p.get('unit_price'))}/{p.get('unit') or 'bag'}")


def _routes(d, c: _Ctx) -> str:
    return c.t("Routes: ", "Routes: ", "روٹ: ") + _listing(c, [r for r in d or [] if isinstance(r, dict)], lambda r: f"{r.get('name')}")


def _vehicles(d, c: _Ctx) -> str:
    return c.t("Vehicles: ", "Gaariyan: ", "گاڑیاں: ") + _listing(c, [r for r in d or [] if isinstance(r, dict)], lambda v: f"{v.get('plate')} ({v.get('capacity_units')} units)")


# ------------------------------------------------------------------ writes (after approval)
def _created_order(d, c: _Ctx) -> str:
    o = d or {}
    return c.t("Draft order {id} saved for {who}: {items}, {amt}. Nothing is owed until it is delivered.",
               "Draft order {id} ban gaya ({who}): {items}, {amt}. Delivery tak kuch baqi nahi hota.",
               "ڈرافٹ آرڈر {id} بن گیا ({who}): {items}، {amt}۔", id=o.get("order_id"), who=o.get("customer_name") or c.customer(o.get("customer_id")),
               items=c.items(o.get("items")), amt=rs(o.get("total")))


def _changed_order(d, c: _Ctx) -> str:
    o = d or {}
    return c.t("Order {id} for {who} changed: {items}, {amt}. It is still a draft.",
               "Order {id} ({who}) badal gaya: {items}, {amt}. Abhi draft hi hai.",
               "آرڈر {id} ({who}) بدل گیا: {items}، {amt}۔ ابھی ڈرافٹ ہے۔", id=o.get("order_id"), who=o.get("customer_name") or c.customer(o.get("customer_id")),
               items=c.items(o.get("items")), amt=rs(o.get("total")))


def _status_order(d, c: _Ctx) -> str:
    o = d or {}
    return c.t("Order {id} for {who} is now {st} ({amt}).", "Order {id} ({who}) ab {st} hai ({amt}).", "آرڈر {id} ({who}) اب {st} ہے ({amt})۔",
               id=o.get("order_id"), who=o.get("customer_name") or c.customer(o.get("customer_id")), st=o.get("status"), amt=rs(o.get("total")))


def _allocated(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Order {id} is {st}: stock reserved at {g}.", "Order {id} {st}: {g} mein stock reserve.", "آرڈر {id} {st}: {g} میں اسٹاک ریزرو۔",
               id=d.get("order_id"), st=d.get("status") or "allocated", g=c.godown(d.get("warehouse_id")))


def _plan_made(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Dispatch plan is {st}: {r} on {v}, {n} order(s), {u} units, {day}.", "Dispatch plan {st}: {r}, gaari {v}, {n} order, {u} units, {day}.",
               "ڈسپیچ پلان {st}: {r}، گاڑی {v}، {n} آرڈر، {day}۔", st=d.get("status"), r=c.route(d.get("route_id")),
               v=c.vehicle(d.get("vehicle_id")), n=len(d.get("order_ids") or []), u=_n(d.get("load_units")), day=_day(d.get("plan_date")))


def _adjusted(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Stock updated: {p} at {g} is now {q} on hand.", "Stock update: {g} mein {p} ab {q}.", "اسٹاک اپڈیٹ: {g} میں {p} اب {q}۔",
               p=c.product(d.get("sku")), g=c.godown(d.get("warehouse_id")), q=_n(d.get("on_hand")))


def _transferred(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Moved {q} {p} from {a} to {b}.", "{q} {p} {a} se {b} bhej diye.", "{q} {p} {a} سے {b} منتقل۔",
               q=_n(d.get("qty")), p=c.product(d.get("sku")), a=c.godown(d.get("from")), b=c.godown(d.get("to")))


def _closed(d, c: _Ctx) -> str:
    d = d or {}
    s = c.t("{who}'s stop closed: {st}. Invoice {inv} for {amt}", "{who} ka stop band: {st}. Bill {inv}, {amt}", "{who} کا اسٹاپ بند: {st}۔ بل {inv}، {amt}",
            who=c.customer(d.get("customer_id")) if d.get("customer_id") else c.t("The", "Ye", "یہ"), st=d.get("status"), inv=d.get("invoice_id") or "-",
            amt=rs(d.get("invoiced")))
    if float(d.get("cash_collected") or 0):
        s += c.t(", cash {c} (receipt {r})", ", cash {c} (raseed {r})", "، نقد {c} (رسید {r})", c=rs(d["cash_collected"]), r=d.get("receipt_id") or "-")
    return s + "."


def _run(plan_id, c: _Ctx) -> str:
    """A dispatch plan as people name it: its route and gaari ('Vehari Road, MNK-4521')."""
    try:
        pl = c.repo.get_plan(str(plan_id or ""))
        return f"{c.route(pl.route_id)}, {c.vehicle(pl.vehicle_id)}"
    except Exception:
        return str(plan_id or "")


def _deposit(d, c: _Ctx) -> str:
    d = d or {}
    v = float(d.get("variance") or 0)
    s = c.t("Cash hand-in recorded for {p}: counted {cnt} against {exp} expected", "{p} ka cash jama: gine {cnt}, hone chahiye {exp}", "{p} کی نقدی جمع: گنے {cnt}، ہونے چاہییں {exp}",
            p=_run(d.get("plan_id"), c), cnt=rs(d.get("counted")), exp=rs(d.get("expected")))
    if v < 0:
        s += c.t(" -- short by {g}", " -- {g} kam", " — {g} کم", g=rs(-v))
        sus = [x for x in d.get("suspect_stops") or [] if isinstance(x, dict)]
        if sus:
            s += c.t(" (check: ", " (dekhein: ", " (دیکھیں: ") + ", ".join(f"{c.customer(x.get('customer_id'))}" for x in sus[:3]) + ")"
    elif v > 0:
        s += c.t(" -- {g} more than expected", " -- {g} zyada", " — {g} زیادہ", g=rs(v))
    return s + "."


def _payment(d, c: _Ctx) -> str:
    d = d or {}
    who = d.get("customer_name") or c.customer(d.get("customer_id"))
    s = c.t("Recorded: {a} received from {w} by {m} (receipt {r}).", "Darj ho gaya: {w} se {a} wusool, {m} (raseed {r}).", "درج: {w} سے {a} وصول، {m} (رسید {r})۔",
            a=rs(-float(d.get("amount") or 0)), w=who, m=d.get("method") or "cash", r=d.get("doc_no") or d.get("entry_id"))
    if "outstanding" in d:
        out = float(d.get("outstanding") or 0)
        s += (c.t(" They now owe {o}.", " Ab {o} baqi.", " اب {o} باقی۔", o=rs(out)) if out > 0 else
              c.t(" Nothing is owed now.", " Ab kuch baqi nahi.", " اب کچھ باقی نہیں۔") if out == 0 else
              c.t(" They are {o} in advance.", " {o} advance.", " {o} ایڈوانس۔", o=rs(-out)))
    return s


def _expense(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Recorded expense {id}: {a} {cat} ({m}).", "Kharcha darj {id}: {a} {cat} ({m}).", "خرچہ درج {id}: {a} {cat} ({m})۔",
               id=d.get("expense_id"), a=rs(d.get("amount")), cat=d.get("category"), m=d.get("method") or "cash")


def _credit(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Credit note {id}: {a} off {w}'s khata.", "Credit note {id}: {w} ke khate se {a} kam.", "کریڈٹ نوٹ {id}: {w} کے کھاتے سے {a} کم۔",
               id=d.get("doc_no") or d.get("entry_id"), a=rs(abs(float(d.get("amount") or 0))), w=c.customer(d.get("customer_id")))


def _reversed(d, c: _Ctx) -> str:
    d = d or {}
    rid = d.get("entry_id") or d.get("expense_id") or d.get("purchase_id") or ""
    s = c.t("Reversed: {id} (cancels {orig}).", "Reverse ho gaya: {id} ({orig} cancel).", "ریورس: {id} ({orig} منسوخ)۔", id=rid, orig=d.get("reversal_of") or "-")
    if "outstanding" in d:
        s += c.t(" {w} now owes {o}.", " {w} ke ab {o} baqi.", " {w} کے اب {o} باقی۔", w=d.get("customer_name") or "", o=rs(d.get("outstanding")))
    if "balance" in d and d.get("supplier_name"):
        s += c.t(" We now owe {w} {o}.", " {w} ko ab {o} dena.", " {w} کو اب {o} دینا۔", w=d["supplier_name"], o=rs(d.get("balance")))
    return s


def _purchase(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Received from {s} into {g}: {items}, bill {amt} ({id}). We now owe {s} {bal}.", "{s} se {g} mein aaya: {items}, bill {amt} ({id}). Ab {s} ko {bal} dena.",
               "{s} سے {g} میں آیا: {items}، بل {amt} ({id})۔ اب {s} کو {bal} دینا۔", s=d.get("supplier_name") or c.supplier(d.get("supplier_id")),
               g=c.godown(d.get("warehouse_id")), items=c.items(d.get("items")), amt=rs(d.get("total")), id=d.get("doc_no") or d.get("purchase_id"), bal=rs(d.get("balance")))


def _paid_supplier(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Paid {s} {a} by {m} ({id}). We now owe them {bal}.", "{s} ko {a} de diye, {m} ({id}). Ab {bal} dena.", "{s} کو {a} ادا، {m} ({id})۔ اب {bal} دینا۔",
               s=d.get("supplier_name") or c.supplier(d.get("supplier_id")), a=rs(-float(d.get("amount") or 0)), m=d.get("method") or "cash", id=d.get("entry_id"), bal=rs(d.get("balance")))


def _reminder(d, c: _Ctx) -> str:
    d = d or {}
    if d.get("drafted") is False:
        return c.t("No reminder drafted: nothing is outstanding.", "Reminder nahi bana: kuch baqi nahi.", "یاد دہانی نہیں بنی: کچھ باقی نہیں۔")
    kw = {"tier": d.get("tier"), "id": d.get("reminder_id"), "w": c.customer(d.get("customer_id")), "a": rs(d.get("amount_due"))}
    if d.get("status") == "sent":
        return c.t("Reminder sent to {w} on WhatsApp about {a}.", "{w} ko {a} ka reminder WhatsApp par bhej diya.", "{w} کو {a} کی یاد دہانی واٹس ایپ پر بھیج دی۔", **kw)
    return c.t("{tier} reminder drafted for {w} about {a}. Sending it is a separate approval.",
               "{w} ke liye {tier} reminder tayyar ({a}). Bhejne ke liye alag manzoori chahiye.",
               "{w} کے لیے {tier} یاد دہانی تیار ({a})۔ بھیجنے کی الگ منظوری ہوگی۔", **kw)


def _reminders(d, c: _Ctx) -> str:
    rows = [r for r in d or [] if isinstance(r, dict) and r.get("reminder_id")]
    if not rows:
        return c.t("No reminders needed: nobody is past that many days.", "Koi reminder nahi bana.", "کوئی یاد دہانی نہیں بنی۔")
    return c.t("{n} reminder(s) drafted: ", "{n} reminders tayyar: ", "{n} یاد دہانیاں تیار: ", n=len(rows)) + _listing(
        c, rows, lambda r: f"{c.customer(r.get('customer_id'))} {rs(r.get('amount_due'))} ({r.get('tier')})")


def _promise(d, c: _Ctx) -> str:
    d = d or {}
    return c.t("Promise logged: {w} pays {a} by {day}.", "Wada darj: {w} {day} tak {a} dega.", "وعدہ درج: {w} {day} تک {a} دے گا۔",
               w=c.customer(d.get("customer_id")), a=rs(d.get("amount")), day=_day(d.get("promised_date")))


FORMATTERS: dict[str, Callable[[Any, _Ctx], str]] = {
    "get_stock": _stock, "get_customer_khata": _khata, "aging_report": _aging, "list_orders": _orders, "get_order": _order,
    "collection_report": _collection, "cashbook": _cashbook, "get_digest": _digest, "slow_stock": _slow, "top_customers": _top,
    "sales_report": _sales, "profit_summary": _profit, "stock_valuation": _valuation, "stock_ledger": _stock_ledger,
    "payables_report": _payables, "supplier_khata": _supplier_khata, "list_suppliers": _suppliers, "broken_promises": _broken,
    "suggest_dispatch": _suggest, "list_stops": _stops, "get_plan": _plan, "find_customer": _find_customer, "find_supplier": _find_customer,
    "search_products": _search_products, "list_routes": _routes, "list_vehicles": _vehicles,
    "create_order": _created_order, "update_order": _changed_order, "confirm_order": _status_order, "cancel_order": _status_order, "allocate_order": _allocated,
    "create_dispatch_plan": _plan_made, "approve_dispatch_plan": _plan_made, "adjust_stock": _adjusted, "transfer_stock": _transferred,
    "close_stop": _closed, "record_deposit": _deposit, "record_payment": _payment, "record_expense": _expense, "credit_note": _credit,
    "reverse_ledger_entry": _reversed, "reverse_expense": _reversed, "reverse_purchase": _reversed, "reverse_supplier_entry": _reversed,
    "record_purchase": _purchase, "pay_supplier": _paid_supplier, "draft_reminder": _reminder, "draft_due_reminders": _reminders,
    "send_reminder": _reminder, "log_promise": _promise,
}

_REJECTED = re.compile(r"rejected the tool call for `?(\w+)`?(?: with reason: (.*))?", re.I | re.S)


def render(tool: str, raw: Any, repo, text: str = "", args: dict | None = None) -> str | None:
    """The sentence for one tool result (raw: the JSON text or the parsed value). None if there is none."""
    c = _Ctx(repo, text, args or {})
    try:
        if isinstance(raw, str):
            m = _REJECTED.search(raw)
            if m:
                why = (m.group(2) or "").strip().rstrip(".")
                return c.t("Nothing was done", "Kuch nahi kiya gaya", "کچھ نہیں کیا گیا") + (f" -- {why}." if why else ".")
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                return None
        if isinstance(raw, dict) and raw.get("error"):
            return None
        fn = FORMATTERS.get(tool)
        return fn(raw, c) if fn else None
    except Exception:
        log.warning("no readable reply for %s", tool, exc_info=True)
        return None
