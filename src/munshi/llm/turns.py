"""Messages that are about MORE than one request, or about a request already made. Pure functions over text and
master data; the platform decides what to do with what they read (and every write still goes through a card).

  correction()       'galti, 40 kar do' / 'nahi 25' / '15000 nahi 12000 the' -- the new arguments for the card the
                     user just asked for, or None when the message isn't a correction of it
  batch_of()         'sab confirm kar do' / 'confirm ORD-1 ORD-2' -- which action, over which kind of record
  split_customers()  'punjab seed mart 20 dap aur green valley 30 urea' -- one order text per customer, only when
                     every part is a complete order on its own (else None: the order desk asks)
  split_reads()      'haji sons ka khata aur urea ka stock dono batao' -- two questions asked at once
  asks_pending()     'koi approval pending he?' -- a question about the cards waiting, not a request to approve

Nothing here resolves an id from memory or computes a number: quantities and amounts are the ones the message says."""
from __future__ import annotations

import re

from munshi.llm import numbers as N
from munshi.llm.parse import _DATE_RE, _ID_RE, _OTP_RE, _PHONE_RE, analyse_order, customer_resolution, ids_in, sku_in
from munshi.llm.stub_model import contains
from munshi.llm.text import fold, words

ORDER_TOOLS = ("create_order", "update_order")
AMOUNT_TOOLS = ("record_payment",)

_CUE = contains("galti", "ghalti", "galat", "ghalat", "sorry", "correction", "mistake", "wrong", "nahi", "nahin", "nai", "no", "not", "na",
                "غلطی", "غلط", "نہیں")
_BARE = re.compile(r"^\W*(?:ji\s+|haan\s+|acha\s+)?(\d+(?:\.\d+)?)\s*(?:bori|bags?|katte|kar do|karo|kardo|kr do|kar dein|kar den|kar dijiye|chahiye|hai|he|"
                   r"tha|the|thi|rakho|likho|likh do|hona chahiye)?\W*$")
_OLD_NEW = re.compile(r"(\d+(?:\.\d+)?)\s*(?:bori|bags?|katte|rs|rupay)?\s*(?:nahi|nahin|nai|na|not)\b[\s,.-]*(?:balkay|balke|but|bulkay)?\s*(\d+(?:\.\d+)?)")
# the right figure first, the wrong one after with 'nahi': '40 urea chahiye 50 nahi'
_NEW_OLD = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[a-z]+\s+){0,3}(\d+(?:\.\d+)?)\s*(?:bori|bags?|katte|rs|rupay)?\s*(?:nahi|nahin|nai)\b\s*\W*$")


def _plain(text: str) -> str:
    s = _OTP_RE.sub(" ", fold(text))
    s = _ID_RE.sub(" ", s)
    s = _PHONE_RE.sub(" ", s)
    s = _DATE_RE.sub(" ", s)
    return N.normalize_numbers(s, N.UNIT_WORDS)


def correction(text: str, tool: str, args: dict, repo) -> dict | None:
    """The corrected arguments for the user's own `tool(args)` card, when `text` corrects it; None otherwise.
    Orders: one line's quantity ('40 kar do' when the card has one line; 'npk 15' names the line; '20 nahi 25' picks the
    line that had 20). Payments: the amount ('15000 nahi 12000'). A message naming someone else is never a correction."""
    s = _plain(text)
    nums = N.numbers_in(s)
    if not nums:
        return None
    if tool == "record_purchase":
        # 'rate 2700 tha' / '2700 ke rate pe': the unit cost of a one-line purchase card
        r = re.search(r"(?:\brate\b|@|\bprice\b|\bqeemat\b)\s*(?:rs\.?\s*)?(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:rs\.?\s*)?(?:ka|ke|ki)?\s*rate\b", s)
        items = [dict(i) for i in args.get("items") or [] if isinstance(i, dict)]
        if not r or len(items) != 1 or customer_resolution(text, repo).ok:
            return None
        items[0]["unit_cost"] = float(r.group(1) or r.group(2))
        return dict(args) | {"items": items}
    m = _OLD_NEW.search(s)
    m2 = None if m else _NEW_OLD.search(s)
    bare = _BARE.match(s)
    if not (m or m2 or bare or (_CUE(text) and len(set(nums)) == 1)):
        return None
    old, new = (float(m.group(1)), float(m.group(2))) if m else (float(m2.group(2)), float(m2.group(1))) if m2 else (
        None, nums[0] if len(set(nums)) == 1 else None)
    if new is None or new <= 0:
        return None
    c = customer_resolution(text, repo)
    if c.status == "ambiguous" or (c.ok and args.get("customer_id") and c.id != args.get("customer_id")):
        return None
    if tool in ORDER_TOOLS:
        if not float(new).is_integer():
            return None
        items = [dict(i) for i in args.get("items") or [] if isinstance(i, dict)]
        sku = sku_in(text, repo)
        if sku:
            hit = [i for i in items if i.get("sku") == sku]
            if not hit:
                return None
        elif old is not None:
            hit = [i for i in items if float(i.get("qty") or 0) == old]
        else:
            hit = items if len(items) == 1 else []
        if len(hit) != 1:
            return None
        hit[0]["qty"] = int(new)
        return dict(args) | {"items": items}
    if tool in AMOUNT_TOOLS:
        if old is not None and abs(old - float(args.get("amount") or 0)) > 0.01:
            return None
        if sku_in(text, repo):
            return None
        return dict(args) | {"amount": new}
    return None


def old_new(text: str) -> tuple[float | None, float | None]:
    """('15000 nahi 12000') -> (15000, 12000); a single number -> (None, n); else (None, None)."""
    s = _plain(text)
    m = _OLD_NEW.search(s)
    if m:
        return float(m.group(1)), float(m.group(2))
    nums = set(N.numbers_in(s))
    return (None, next(iter(nums))) if len(nums) == 1 else (None, None)


_ACTUAL = re.compile(r"\b(?:asal|asli|actual|actually|darasal|sahi|correct|should be|hona chahiye)\w*\s*(?:mei|me|mein|mai|main|to|mein to|is|was|amount|raqam)?\s*"
                     r"(?:rs\.?\s*)?(\d+(?:\.\d+)?)")


def wrong_right(text: str) -> tuple[float | None, float | None]:
    """(the wrong figure, the right one) of a correction: '20000 nahi 12000 thi', '20000 wali galat thi, asal mei 12000 thi'.
    (None, None) when the message isn't shaped like one."""
    old, new = old_new(text)
    if old is not None:
        return old, new
    s = _plain(text)
    m = _ACTUAL.search(s)
    if not m:
        return None, None
    right = float(m.group(1))
    others = [n for n in N.numbers_in(s[:m.start()] + " " + s[m.end():]) if n != right and n >= 100]
    return (others[0], right) if len(set(others)) == 1 else (None, None)


# undoing a payment already recorded -- never a new one
_REV_WORD = contains("reverse", "reversal", "reverse karo", "undo", "cancel", "radd", "mansookh", "galat", "ghalat", "galti", "ghalti", "wrong",
                     "mistake", "bounce", "bounced", "واپس", "منسوخ", "غلط", "باؤنس")
_REV_BACK = re.compile(r"\b(wapis|wapas)\s+(karo|kar do|kardo|kar dein|lo|le lo|kar lo|karein)\b")
_RECEIPT = contains("payment", "payments", "raqam", "receipt", "raseed", "rasid", "entry", "cheque", "check", "chq", "jazzcash", "jazz cash", "easypaisa",
                    "easy paisa", "bank", "cash", "paise", "wusooli", "wasooli", "ادائیگی", "رسید", "چیک", "رقم")


def rev_intent(text: str, repo=None) -> bool:
    """'X ki 20000 wali payment reverse karo', 'cheque bounce ho gaya', 'galat entry thi, cancel kar do', 'payment 20000 nahi 12000
    thi': a request to undo a payment already recorded. Not an order ('order cancel karo'), not a promise, and not money paid
    back to us ('Rana ne 20000 wapis kiye' is a payment)."""
    from munshi.llm.parse import is_bounce
    f = fold(text)
    if re.search(r"\b(order|orders|promise|wada|waada|reminder|supplier|suppliers)\b|\b(spy|bil|pur|exp)-|آرڈر|وعدہ|سپلائر", f):
        return False                                  # (a supplier's payment or bill is the khareed munshi's reversal)
    if is_bounce(text):
        return True
    if not _RECEIPT(text):
        return False
    return bool(_REV_WORD(text) or _REV_BACK.search(f)) or (_OLD_NEW.search(_plain(text)) is not None and _CUE(text))


def order_text(customer_id: str, items: list[dict]) -> str:
    """The order request a corrected card is raised from: 'C-001 ko 40 UREA-50' (ids and SKU codes: nothing to re-read)."""
    return f"{customer_id} ko " + " aur ".join(f"{int(i['qty'])} {i['sku']}" for i in items)


def update_text(order_id: str, items: list[dict]) -> str:
    return f"{order_id} order mei " + " aur ".join(f"{i['sku']} {int(i['qty'])}" for i in items) + " kar do"


def purchase_text(args: dict) -> str:
    """'S-003 se 50 DRIP-100 aaye 2700 rate bill X WH-VEHARI' -- the corrected purchase, re-read by the same rules."""
    it = args["items"][0]
    cost = it.get("unit_cost")
    return (f"{args['supplier_id']} se {int(it['qty'])} {it['sku']} aaye" + (f" {cost:g} rate" if cost else "")
            + (f" bill {args['invoice_ref']}" if args.get("invoice_ref") else "") + (f" {args['warehouse_id']}" if args.get("warehouse_id") else ""))


def payment_text(customer_id: str, amount: float, method: str) -> str:
    return f"{customer_id} ne {int(amount) if float(amount).is_integer() else amount} {method or 'cash'} diye"


# ------------------------------------------------------------------ batches
_ALL = contains("sab", "sabhi", "saare", "sare", "sary", "saray", "all", "tamam", "tamaam", "every", "har ek", "sab ke sab", "sabko", "sab ko",
                "سب", "تمام")
_VERBS = (("confirm", contains("confirm", "pakka", "کنفرم", "پکا"), "order", ("draft",)),
          ("cancel", contains("cancel", "mansookh", "منسوخ"), "order", ("draft", "confirmed", "allocated")),
          ("allocate", contains("allocate", "reserve"), "godown", ("confirmed",)))


def batch_of(text: str) -> tuple[str, str, tuple, list[str]] | None:
    """(verb, specialist, statuses, explicit order ids) for 'sab confirm kar do' / 'confirm ORD-1 ORD-2'; None if the
    message is about one record (or no record at all)."""
    ids = ids_in(text, "ORD")
    for verb, cue, spec, statuses in _VERBS:
        if cue(text) and (len(ids) >= 2 or (_ALL(text) and not ids)):
            return verb, spec, statuses, ids
    return None


def batch_step(verb: str, order_id: str, text: str) -> str:
    """The one-record request each card of a batch is raised from."""
    if verb == "cancel":
        return f"cancel {order_id} -- {text[:60]}"
    return f"{verb} {order_id}"


# ------------------------------------------------------------------ one message, several requests
_SPLIT = re.compile(r"\s*(?:,|;|\n|\baur\b|\band\b|\bor\b|\bphir\b|اور)\s*", re.I)


def _chunks(text: str) -> list[str]:
    return [c.strip() for c in _SPLIT.split(text) if c and c.strip()]


def split_customers(text: str, repo) -> list[str] | None:
    """Order texts, one per customer, when the message orders for two or more customers and EVERY part is a complete
    order (customer + lines, nothing unaccounted for). Anything less -> None (the order desk then asks)."""
    chunks = _chunks(text)
    if len(chunks) < 2:
        return None
    segs: list[list[str]] = []
    who: list[str] = []
    for ch in chunks:
        c = customer_resolution(ch, repo)
        if c.status == "ambiguous" or (c.ok and c.other is not None):
            return None
        if c.ok and (not who or who[-1] != c.id):
            segs.append([ch]); who.append(c.id or "")
        elif segs:
            segs[-1].append(ch)
        else:
            return None
    if len(set(who)) < 2 or len(who) != len(set(who)):
        return None
    out = [" aur ".join(s) for s in segs]
    for o in out:
        op = analyse_order(o, repo)
        if not op.ready or op.customer.other is not None:
            return None
    return out


def split_customers_named(text: str, repo) -> list[str]:
    """Every customer the message names confidently, in order ('malik agro aur green valley dono ka khata' -> [C-001, C-003]);
    an ambiguous name adds nobody."""
    out: list[str] = []
    for ch in [text] + _chunks(text):
        c = customer_resolution(ch, repo)
        for r in ([c, c.other] if c.ok else []):
            if r is not None and getattr(r, "id", None) and r.id not in out:
                out.append(r.id)
    return out


_PAYWORD = contains("diye", "diya", "di", "dia", "jama", "paid", "payment", "bheje", "bheja", "transfer", "wusool", "wasool", "received", "mile", "mila",
                    "دیے", "دیا", "جمع")
_KHATA_Q = contains("khata", "balance", "baqi", "baaki", "udhaar", "udhar", "outstanding", "hisaab", "hisab", "owe", "owes", "کھاتہ", "حساب", "باقی")


def _kind(part: str, repo) -> str | None:
    """What one piece of a message asks for, if it is complete on its own: 'payment' (a customer paid a stated sum),
    'order' (a customer's order, every line read), 'khata' (a customer's balance); None otherwise."""
    from munshi.llm.parse import amount_in
    c = customer_resolution(part, repo)
    if not c.ok or c.other is not None:
        return None
    op = analyse_order(part, repo)
    if op.ready:
        return "order"
    if _PAYWORD(part) and not op.items and amount_in(part).amount is not None and not rev_intent(part):
        return "payment"
    if _KHATA_Q(part) and not op.items and not re.search(r"\d", part):
        return "khata"
    return None


def split_intents(text: str, repo) -> list[tuple[str, str]] | None:
    """[(kind, request)] when one message carries two DIFFERENT requests for one customer, each complete on its own
    ('chaudhry farms ne 20000 diye aur 10 urea bhi chahiye unko' -> a payment and an order). The customer named first is
    carried into a later part that names nobody. None when it is one request (or anything is unclear)."""
    parts = [p.strip(" ,.") for p in re.split(r"\s+(?:aur|and|phir|,)\s+|,\s*(?:aur|and)?\s*", text) if p and p.strip(" ,.")]
    if len(parts) < 2 or len(parts) > 3:
        return None
    first = customer_resolution(parts[0], repo)
    if not first.ok or first.other is not None:
        return None
    out: list[tuple[str, str]] = []
    for p in parts:
        c = customer_resolution(p, repo)
        if c.status == "none":
            p = f"{p} {first.id}"
        elif not c.ok or c.id != first.id:
            return None
        k = _kind(p, repo)
        if k is None:
            return None
        out.append((k, p))
    kinds = [k for k, _ in out]
    return out if len(set(kinds)) == len(kinds) else None


_BOTH = contains("dono", "donon", "both", "دونوں")


_ASKS = re.compile(r"\b(kya|kia|kitna|kitni|kitne|kaun|kon|kaunse|konse|kahan|kab|which|what|how many|how much|who)\b|[?؟]|کیا|کتنا|کتنے|کون")


def split_reads(text: str) -> list[str] | None:
    """Two questions asked together ('X ka khata aur Y ka stock dono batao', 'kaun se orders nahi gaye aur kaun se stop khule
    hein'): the two parts, each keeping the ask word."""
    halves = re.split(r"\s+(?:aur|and|اور)\s+", text, maxsplit=1)
    if not _BOTH(text) and not (len(halves) == 2 and all(_ASKS.search(fold(h)) and len(words(fold(h))) >= 3 for h in halves)):
        return None
    parts = [p for p in re.split(r"\s+(?:aur|and|اور)\s+", text, maxsplit=1)]
    if len(parts) != 2 or min(len(words(fold(p))) for p in parts) < 2:
        return None
    tail = re.sub(r"\b(dono|donon|both)\b", "", parts[1], flags=re.I).strip()
    ask = re.search(r"\b(batao|bata do|bataen|dikhao|dikha do|tell me|show)\b", tail, re.I)
    first = parts[0] + (f" {ask.group(1)}" if ask else "")
    return [first.strip(), tail]


# ------------------------------------------------------------------ the approvals queue
_PENDING_Q = re.compile(r"\b(approvals?|approve|manzoori|manzoor|pending)\b")
_PENDING_CUE = re.compile(r"\b(koi|kya|kia|kaun|kon|konse|kaunse|kitne|kitni|dikhao|dikha|list|show|which|any|what|how many|mere|pas|paas|baqi|abhi)\b|[?؟]")
_ACT = re.compile(r"\b(kar do|karo|kardo|kr do|de do|dedo|bina|without|khud|sab|all|every|ignore)\b|\b(ord|dsp|rem|rcp)-")


def asks_pending(text: str) -> bool:
    """'koi approval pending he?', 'approvals dikhao', 'kaun se approve karne hein' -- a question about the queue. An
    instruction to approve anything ('sab approve kar do', 'bina pooche approve') is not one (the help desk refuses it)."""
    f = fold(text)
    if not _PENDING_Q.search(f) or _ACT.search(f) or re.search(r"\border", f):
        return False
    return bool(_PENDING_CUE.search(f)) or bool(re.fullmatch(r"\W*(approvals?|pending)\W*", f))
