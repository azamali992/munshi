"""Catalogue-aware extraction for the offline stub, and the deterministic layer any
model path should reuse: 'Chaudhry Farms ko 20 urea aur 5 dap bhej do' -> a
customer resolution, order lines, and the list of reasons NOT to act.

Design rules, in priority order:
  1. Never invent. A product is only ever a whole-token match of a SKU, a name, an
     alias or a known spelling variant of one (or one typo away from a long one) --
     never a substring, so the stop id STP-89DAC339 can't turn into 'imidacloprid'.
     IDs, OTPs, phone numbers, dates, times, weights ('50kg') and prices are masked
     before anything is read as a quantity.
  2. Every quantity-like number must be accounted for. A number no product claims
     ('10 glyphosate', '5 se zyada') is a reason to ask, not something to drop: a
     partial order that looks complete is a wrong card.
  3. Anything unsure is a `problem`, and a problem means ask: ambiguous or unknown
     customer, two customers in one message, a negated line with a quantity
     ('50 urea nahi 30'), ranges, negative / fractional / implausible quantities,
     units without a pack size (carton, peti, ton), the same product twice.
  4. Numbers are read here, in code ('dedh sau' -> 150, '1,08,250' -> 108250).

`OrderParse.ready` is the only green light for proposing an order."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from munshi.domain.repository import MunshiRepository
from munshi.llm import numbers as N
from munshi.llm.resolve import Resolution, resolve_customer, resolve_supplier
from munshi.llm.text import edit_distance, fold, is_urdu, words

# ------------------------------------------------------------------ vocabulary (all folded before use)
# Spelling / script variants people type for common agri products, keyed by a word in the product's
# SKU, name or aliases. The tenant's own aliases always apply as well.
_VARIANTS = {
    "urea": ["uriya", "yuria", "yooria", "uria", "ureaa", "you rea", "yu ria", "یوریا", "یوریہ"],
    "dap": ["d a p", "d.a.p", "ڈی اے پی", "ڈیپ"],
    "npk": ["n p k", "این پی کے"],
    "potash": ["potaash", "پوٹاش"],
    "sop": ["s o p", "ایس او پی"],
    "zinc": ["zink", "زنک"],
    "makai": ["makki", "maki", "makkai", "مکئی", "مکی"],
    "maize": ["makai", "makki"],
    "gandum": ["gundum", "گندم"],
    "wheat": ["gandum"],
    "cyper": ["saiper", "سائپر"],
    "drip": ["ڈرپ"],
}
_NEGATION = {fold(w) for w in "nahi nahin nai na mat not no without bina baghair نہیں نہ مت بغیر".split()}
_CAP = [fold(p) for p in ("se zyada", "se ziada", "se zaida", "se kam", "zyada se zyada", "kam se kam", "at most", "at least", "maximum",
                          "max", "upto", "up to", "سے زیادہ", "سے کم")]
_RETURN = {fold(w) for w in "wapis wapas wapsi returned return returns واپس".split()}
_DELIVER = {fold(w) for w in "delivered deliver diya de utara utar diye دیا ڈیلیور".split()}
_ALL = {fold(w) for w in "all sab saara sara poora pura complete full سب سارا پورا".split()}
_CASH = {fold(w) for w in "cash nakad naqd naqad paise paisay raqam rs rupay rupees نقد پیسے رقم روپے".split()}
_BOUNCE = re.compile(r"\b(bounce|bounced|bouncing|dishono(u)?r(ed)?)\b|باونس|چیک واپس|(cheque|check|chq) (wapi?s|wapas|return)")

_ID_RE = re.compile(r"(?<![a-z0-9-])(?:ord|dsp|stp|rem|rcp|crn|opb|rev|inv|exp|pur|spy|bil|prm|trf|prn|dep|pay|c|s|v|wh|r)-[a-z0-9]+(?:-[a-z0-9]+)*(?![a-z0-9])"
                    r"|(?<![a-z0-9-])[a-z]{1,4}-\d+[a-z0-9]*(?![a-z0-9])")
_OTP_RE = re.compile(r"(?:otp|o\.t\.p|code|pin|او ٹی پی|اوٹی پی|کوڈ)\s*(?:hai|is|:|#|-|=)?\s*(\d{4,6})(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?92|0)\d{2,3}[- ]?\d{7}(?!\d)")
_DATE_RE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})(?!\d)")
_TIME_RE = re.compile(r"\[?\b\d{1,2}:\d{2}(?:\s?(?:am|pm))?\]?")
_WEIGHT_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?(?:kg|kgs|kilo|kilos|ml|gm|gms|g|gram|grams)\b")
_PRICE_RE = re.compile(r"(?:@|\bat\b|\brate\b|\bprice\b|\bqeemat\b|\bbhao\b)\s*(?:rs\.?\s*)?(\d+(?:\.\d+)?)"
                       r"|(\d+(?:\.\d+)?)\s*(?:rs\.?\s*)?(?:rate|per bag|per bori|ka rate|ki rate|fi bori)\b")
_RANGE_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*(?:-|to|ya|or|یا)\s*\d+(?:\.\d+)?(?![\w.])")
_NEG_RE = re.compile(r"(?:(?<=\s)|^)-\s?\d+(?:\.\d+)?")
_TOKEN_RE = re.compile(r"x[a-z]+\d*x|[a-z]+|[؀-ۿ]+|\d+(?:\.\d+)?|[,.;:?!()%]")

MASKS = {"xidx", "xotpx", "xmaskx", "xdatex", "xpricex", "xrangex", "xnegx"}
MIN_PLAUSIBLE_LIMIT = 2000        # a line above max(this, 3 x stock on hand) is asked about, never carded
_FILLER = {"of", "x"}
_CONJ = {",", ".", "aur", "and", "&", "اور"}


def _is_num(t: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", t))


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


# ------------------------------------------------------------------ product index
@dataclass
class Catalogue:
    phrases: dict[tuple[str, ...], str]       # token phrase -> SKU (phrases shared by two products are left out)
    singles: dict[str, str]                   # single-token keys long enough for a one-typo match
    units: dict[str, str]
    nouns: frozenset[str]
    skus: list[str]                           # index -> SKU, for the xskuNx placeholders
    sku_folded: list[str]
    vocab: frozenset[str] = frozenset()       # every word of every product phrase ('seed' in 'makai seed')


def catalogue(repo: MunshiRepository) -> Catalogue:
    raw: dict[tuple[str, ...], set[str]] = {}
    units: dict[str, str] = {}
    skus = [p.sku for p in repo.list_products()]
    for p in repo.list_products():
        units[p.sku] = p.unit or "bag"
        keys = {fold(p.name), *(fold(a) for a in p.aliases), fold(p.sku).split("-")[0]}
        toks = {t for k in keys for t in words(k)}
        for base, variants in _VARIANTS.items():
            if base in toks:
                keys.update(fold(v) for v in variants)
        for k in keys:
            ws = tuple(t for t in words(k) if not t.isdigit() and t not in ("kg", "ml", "l", "m", "g"))
            if ws:
                raw.setdefault(ws, set()).add(p.sku)
    phrases = {k: next(iter(v)) for k, v in raw.items() if len(v) == 1}
    singles = {k[0]: v for k, v in phrases.items() if len(k) == 1 and len(k[0]) >= 4 and k[0].isascii()}
    nouns = frozenset(k[0] for k in phrases)
    vocab = frozenset(t for k in raw for t in k) | {"beej", fold("بیج"), "khad", fold("کھاد")}
    return Catalogue(phrases, singles, units, nouns, skus, [fold(s) for s in skus], vocab)


# ------------------------------------------------------------------ masking + tokens
@dataclass
class Prepared:
    folded: str                                # fold(text): IDs still in place, for ID lookups
    text: str                                  # masked and number-normalised
    tokens: list[str]
    prices: list[float] = field(default_factory=list)
    otp: str = ""


def prepare(text: str, cat: Catalogue) -> Prepared:
    f = fold(text)
    s = f
    for i in sorted(range(len(cat.skus)), key=lambda i: -len(cat.sku_folded[i])):
        s = re.sub(rf"(?<![a-z0-9-]){re.escape(cat.sku_folded[i])}(?![a-z0-9-])", f" xsku{i}x ", s)
    otp_m = _OTP_RE.search(s)
    s = _OTP_RE.sub(" xotpx ", s)
    s = _ID_RE.sub(" xidx ", s)
    s = _PHONE_RE.sub(" xmaskx ", s)
    s = _DATE_RE.sub(" xdatex ", s)
    s = _TIME_RE.sub(" xmaskx ", s)
    s = N.normalize_numbers(s, cat.nouns | N.UNIT_WORDS)
    s = _WEIGHT_RE.sub(" xmaskx ", s)
    prices = [float(m.group(1) or m.group(2)) for m in _PRICE_RE.finditer(s)]
    s = _PRICE_RE.sub(" xpricex ", s)
    s = _RANGE_RE.sub(" xrangex ", s)
    s = _NEG_RE.sub(" xnegx ", s)
    return Prepared(f, s, _TOKEN_RE.findall(s), prices, otp_m.group(1) if otp_m else "")


# ------------------------------------------------------------------ product mentions and lines
@dataclass
class Mention:
    sku: str
    start: int
    end: int                                   # exclusive


@dataclass
class Line:
    sku: str
    qty: int
    unit: str | None
    mention: Mention
    num_pos: int

    @property
    def lo(self) -> int:
        return min(self.mention.start, self.num_pos)

    @property
    def hi(self) -> int:
        return max(self.mention.end - 1, self.num_pos)


def mentions(tokens: list[str], cat: Catalogue) -> list[Mention]:
    out: list[Mention] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        m = re.fullmatch(r"xsku(\d+)x", t)
        if m:
            out.append(Mention(cat.skus[int(m.group(1))], i, i + 1))
            i += 1
            continue
        hit = None
        for n in (4, 3, 2, 1):
            key = tuple(tokens[i:i + n])
            if len(key) == n and key in cat.phrases:
                hit = Mention(cat.phrases[key], i, i + n)
                break
        if hit is None and len(t) >= 5 and t.isascii() and t.isalpha() and t not in N.UNIT_WORDS:
            near = {s for k, s in cat.singles.items() if k[:2] == t[:2] and edit_distance(t, k, 1) <= 1}
            if len(near) == 1:
                hit = Mention(near.pop(), i, i + 1)
        if hit:
            out.append(hit)
            i = hit.end
        else:
            i += 1
    return out


def _unit_check(unit: str | None, product_unit: str) -> tuple[bool, int]:
    """(acceptable for this product, multiplier)"""
    if unit is None:
        return True, 1
    if unit in N.BAG_UNITS:
        return product_unit == "bag", 1
    if unit in N.DOZEN_UNITS:
        return True, 12
    if unit in N.PC_UNITS:
        return product_unit == "pc", 1
    if unit in N.LTR_UNITS:
        bottle = unit in ("bottle", "bottles", "botal")
        return product_unit == "ltr" or (bottle and product_unit == "pc"), 1
    return False, 1                                  # carton / peti / box / ton: no pack-size table, so ask


def _num_before(tokens, m: Mention) -> tuple[int, str | None] | None:
    unit, j, skipped = None, m.start - 1, 0
    while j >= 0 and skipped <= 2:
        t = tokens[j]
        if _is_num(t):
            return j, unit
        if t in N.UNIT_WORDS:
            unit = unit or t
        elif t not in _FILLER:
            return None
        j -= 1
        skipped += 1
    return None


def _num_after(tokens, m: Mention) -> tuple[int, str | None] | None:
    j = m.end
    if j < len(tokens) and tokens[j] in (":", "x", "=", "ke", "ki", "ka", "of"):      # 'drip line ke 1000' = 1000 drip line
        j += 1
    if j < len(tokens) and _is_num(tokens[j]):
        nxt = tokens[j + 1] if j + 1 < len(tokens) else ""
        if nxt in ("%", "percent", "fisad"):             # '10 urea 20 % discount': a percentage is never a quantity
            return None
        return j, (nxt if nxt in N.UNIT_WORDS else None)
    return None


@dataclass
class Lines:
    lines: list[Line] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    orphans: list[int] = field(default_factory=list)    # numbers no product claimed
    dropped: list[str] = field(default_factory=list)    # negated products left out ('dap nahi')
    used: set[int] = field(default_factory=set)          # token positions that belong to lines


def read_lines(tokens: list[str], cat: Catalogue, repo: MunshiRepository, ignore: set[int] | None = None, check_stock: bool = True,
               allow_repeats: bool = False) -> Lines:
    out = Lines()
    ignore = ignore or set()
    ms = mentions(tokens, cat)
    for m in ms:                                        # 'makai seed', 'gandum beej': trailing product words belong to the product
        j = m.end
        while j < len(tokens) and tokens[j] in cat.vocab and not any(x.start == j for x in ms):
            out.used.add(j)
            j += 1
    befores = {id(m): _num_before(tokens, m) for m in ms}
    afters = {id(m): _num_after(tokens, m) for m in ms}
    label = {id(m): " ".join(tokens[m.start:m.end]) for m in ms}

    # negation: 'dap nahi' / 'nahi dap' / 'bina dap' leaves DAP out; a negated line WITH a number is a correction -> ask
    keep = []
    for m in ms:
        nxt = tokens[m.end:m.end + 2]
        neg_after = bool(nxt) and (nxt[0] in _NEGATION or (nxt[0] in ("bhi", "بہی") and len(nxt) > 1 and nxt[1] in _NEGATION))
        neg_before = m.start > 0 and tokens[m.start - 1] in _NEGATION
        if neg_after or neg_before:
            out.used.update(range(m.start, m.end))
            if befores[id(m)] or (neg_before and afters[id(m)]):
                out.problems.append(f"'{label[id(m)]}' has a quantity and a 'nahi' -- which quantity is final?")
                out.used.add((befores[id(m)] or afters[id(m)])[0])
            else:
                out.dropped.append(m.sku)
            continue
        keep.append(m)
    ms = keep
    for i, t in enumerate(tokens[:-1]):
        if _is_num(t) and tokens[i + 1] in _NEGATION:
            out.problems.append(f"'{t} {tokens[i + 1]}' -- which quantity is final?")
            out.used.add(i)

    def assign(side):
        got, used = {}, set()
        for m in ms:
            c = (befores if side == "before" else afters)[id(m)]
            if c is None or c[0] in used:
                return None
            got[id(m)] = c
            used.add(c[0])
        return got
    a, b = assign("before"), assign("after")
    if a is not None and b is not None and {k: v[0] for k, v in a.items()} != {k: v[0] for k, v in b.items()}:
        out.problems.append("the numbers could belong to either neighbouring product -- please write '20 urea, 5 dap'")
        chosen = a
    elif a is not None or b is not None:
        chosen = a if a is not None else b
    else:                                               # mixed styles: each product takes an adjacent number nobody else took
        chosen, taken = {}, set()
        for m in ms:
            for c in (befores[id(m)], afters[id(m)]):
                if c and c[0] not in taken:
                    chosen[id(m)] = c
                    taken.add(c[0])
                    break

    seen: set[str] = set()
    for m in ms:
        out.used.update(range(m.start, m.end))
        c = chosen.get(id(m))
        if not c:
            out.problems.append(f"how many {label[id(m)]}?")
            continue
        pos, unit = c
        out.used.add(pos)
        for k in range(min(pos, m.start), max(pos, m.end - 1) + 2):
            if k < len(tokens) and tokens[k] in N.UNIT_WORDS | _FILLER:
                out.used.add(k)
        punit = cat.units.get(m.sku, "bag")
        ok, mult = _unit_check(unit, punit)
        if not ok:
            out.problems.append(f"{label[id(m)]} is sold by the {punit}, not by the {unit} -- how many {punit}s?")
            continue
        qty = float(tokens[pos]) * mult
        if not qty.is_integer():
            out.problems.append(f"{_fmt(qty)} {label[id(m)]}: only whole {punit}s -- how many?")
            continue
        if check_stock:
            try:
                on_hand = sum(s.on_hand for s in repo.stock_by_sku(m.sku))
            except Exception:
                on_hand = 0
            if qty > max(MIN_PLAUSIBLE_LIMIT, 3 * on_hand):
                out.problems.append(f"{_fmt(qty)} {label[id(m)]} is far more than we ever hold -- please confirm the number")
                continue
        if qty <= 0:
            out.problems.append(f"{_fmt(qty)} {label[id(m)]} is not a quantity")
            continue
        if m.sku in seen and not allow_repeats:
            out.problems.append(f"{label[id(m)]} appears twice -- which quantity is final?")
            continue
        seen.add(m.sku)
        out.lines.append(Line(m.sku, int(qty), unit, m, pos))

    for i, t in enumerate(tokens):
        if i in ignore or i in out.used:
            continue
        if _is_num(t):
            out.orphans.append(i)
        elif t == "xrangex":
            out.problems.append("a range -- which exact quantity?")
        elif t == "xnegx":
            out.problems.append("a negative quantity")
    joined = " ".join(tokens)
    if any(re.search(rf"(?<!\w){re.escape(c)}(?!\w)", joined) for c in _CAP):
        out.problems.append("a limit ('se zyada' / 'se kam') -- which exact quantity?")
    return out


def orphan_note(tokens: list[str], i: int) -> tuple[str, str]:
    """('10 glyphosate', 'glyphosate') -- the orphan number and the word after it, if any."""
    from munshi.llm.resolve import STOPWORDS
    nxt = [w for w in tokens[i + 1:i + 3] if w not in N.UNIT_WORDS and w not in _CONJ]
    what = nxt[0] if nxt and nxt[0].isalpha() and nxt[0] not in MASKS and nxt[0] not in _NEGATION and nxt[0] not in STOPWORDS else ""
    return (f"{tokens[i]} {what}".strip(), what)


# ------------------------------------------------------------------ orders
@dataclass
class OrderParse:
    customer: Resolution
    items: list[dict]
    problems: list[str]
    unknown: list[str]
    dropped: list[str]
    urdu: bool = False

    @property
    def ready(self) -> bool:
        return self.customer.ok and bool(self.items) and not self.problems


def _exclusions(pr: Prepared, ln: Lines) -> set[int]:
    return set(ln.used) | {i for i, t in enumerate(pr.tokens) if t in N.UNIT_WORDS or _is_num(t) or t in MASKS or t.startswith("xsku")}


def analyse_order(text: str, repo: MunshiRepository) -> OrderParse:
    cat = catalogue(repo)
    pr = prepare(text, cat)
    ln = read_lines(pr.tokens, cat, repo)
    cust = resolve_customer(pr.folded, repo, exclude=_exclusions(pr, ln), tokens=pr.tokens)
    problems = list(ln.problems)
    unknown = []
    for i in ln.orphans:
        note, what = orphan_note(pr.tokens, i)
        if pr.tokens[i + 1:i + 2] == ["%"] or what in ("percent", "discount", "off"):
            problems.append(f"a discount ('{pr.tokens[i]}%') -- discounts come from the customer's terms in Setup, not from the order message")
        elif what:
            unknown.append(what)
            problems.append(f"'{note}' -- that isn't in the catalogue")
        else:
            problems.append(f"'{note}' -- what is this number for?")
    if cust.other is not None:
        problems.append(f"two customers ({cust.name or cust.candidates[0].name} and {cust.other.name}) -- one order per customer, please send them separately")
    return OrderParse(cust, [{"sku": x.sku, "qty": x.qty} for x in ln.lines], problems, unknown, ln.dropped, is_urdu(text))


# ------------------------------------------------------------------ delivery close
@dataclass
class CloseParse:
    stop_id: str
    delivered: list[dict] | None               # None = everything that was loaded for the stop
    returned: list[dict]
    cash: float
    otp: str
    problems: list[str]


def analyse_close(text: str, repo: MunshiRepository) -> CloseParse:
    stop = (ids_in(text, "STP") or [""])[0]
    cat = catalogue(repo)
    pr = prepare(text, cat)
    toks = pr.tokens
    cash_pos: set[int] = set()
    for i, t in enumerate(toks):
        if t in _CASH:
            for j in (i + 1, i - 1, i + 2):
                if 0 <= j < len(toks) and _is_num(toks[j]):
                    cash_pos.add(j)
                    break
    ln = read_lines(toks, cat, repo, ignore=cash_pos, check_stock=False, allow_repeats=True)   # '8 npk ... wapis 2 npk'
    problems = list(ln.problems)
    orphans = list(ln.orphans)
    if not cash_pos and len(orphans) == 1 and float(toks[orphans[0]]) >= 100:
        cash_pos.add(orphans.pop())                   # '40 hazar liye': the one money-sized number left is the cash
    if orphans:
        problems.append("a number I can't place: " + ", ".join(orphan_note(toks, i)[0] for i in orphans))
    cash_vals = {float(toks[i]) for i in cash_pos}
    if len(cash_vals) > 1:
        problems.append("two different cash amounts")

    # which lines came back: a return word right before a line, or right after it (and not before another line),
    # or the last delivered/returned word before a line that has none of its own
    starts = {ln_.lo: ln_ for ln_ in ln.lines}
    ends = {ln_.hi: ln_ for ln_ in ln.lines}
    own: dict[int, str] = {}
    marks = sorted((i, "ret" if t in _RETURN else "del") for i, t in enumerate(toks) if t in _RETURN or t in _DELIVER)
    for i, kind in marks:
        j = i + 1
        while j < len(toks) and toks[j] in _CONJ:
            j += 1
        k = i - 1
        while k >= 0 and toks[k] in N.UNIT_WORDS:
            k -= 1
        if j in starts:
            own.setdefault(id(starts[j]), kind)
        elif k in ends:
            own.setdefault(id(ends[k]), kind)
    delivered, returned = [], []
    for line in sorted(ln.lines, key=lambda x: x.lo):
        kind = own.get(id(line)) or next((kd for i, kd in reversed(marks) if i < line.lo), "del")
        (returned if kind == "ret" else delivered).append({"sku": line.sku, "qty": line.qty})
    says_all = any(t in _ALL for t in toks)
    deliver: list[dict] | None
    if delivered:
        deliver = delivered
    elif says_all or not returned:
        deliver = None                                 # 'delivered all' / 'sab de diya' / 'deliver ho gaya'
    else:
        deliver = []
        problems.append("what was delivered? (say 'delivered all' or list the items)")
    if not pr.otp:
        problems.append("the customer's OTP code")
    return CloseParse(stop, deliver, returned, next(iter(cash_vals)) if len(cash_vals) == 1 else 0.0, pr.otp, problems)


# ------------------------------------------------------------------ money
@dataclass
class AmountParse:
    amount: float | None
    problem: str = ""


def amount_in(text: str) -> AmountParse:
    s = _OTP_RE.sub(" ", fold(text))
    s = _ID_RE.sub(" ", s)
    s = _PHONE_RE.sub(" ", s)
    s = _DATE_RE.sub(" ", s)
    s = _TIME_RE.sub(" ", s)
    s = N.normalize_numbers(s)
    if _RANGE_RE.search(s):
        return AmountParse(None, "a range -- which exact amount?")
    vals = N.numbers_in(s)
    if not vals:
        return AmountParse(None, "how much?")
    big = sorted({v for v in vals if v >= 100})
    if len(set(vals)) == 1:
        v = vals[0]
    elif len(big) == 1:
        v = big[0]
    else:
        return AmountParse(None, "which amount? I see " + ", ".join(_fmt(x) for x in sorted(set(vals))))
    if v <= 0:
        return AmountParse(None, "the amount must be above zero")
    return AmountParse(v)


def numbers_said(text: str, repo: MunshiRepository | None = None) -> set[float]:
    """Every number the message states -- spoken forms read ('dedh sau' -> 150, '50 hazar' -> 50000), lakh grouping
    handled -- with IDs, OTPs, phone numbers, dates and times masked out. A quantity or amount a model passes must
    be one of these: numbers are read from the message, never computed or recalled."""
    s = _OTP_RE.sub(" ", fold(text))
    s = _ID_RE.sub(" ", s)
    s = _PHONE_RE.sub(" ", s)
    s = _DATE_RE.sub(" ", s)
    s = _TIME_RE.sub(" ", s)
    nouns = catalogue(repo).nouns if repo is not None else frozenset()
    return set(N.numbers_in(N.normalize_numbers(s, nouns | N.UNIT_WORDS)))


def otp_in(text: str) -> str:
    """The delivery code the message gives after 'otp' / 'code' / 'pin' ('' if none)."""
    m = _OTP_RE.search(fold(text))
    return m.group(1) if m else ""


def is_bounce(text: str) -> bool:
    return bool(_BOUNCE.search(fold(text)))


def method_in(text: str) -> str:
    t = fold(text)
    if re.search(r"jazz ?cash|جاز", t):
        return "jazzcash"
    if re.search(r"easy ?paisa|ایزی", t):
        return "easypaisa"
    if re.search(r"\b(cheque|check|chq|chek)\b|چیک", t):
        return "cheque"
    if re.search(r"\b(bank|online|ibft|transfer)\b|بینک", t):
        return "bank"
    return "cash"


# ------------------------------------------------------------------ dates
_WEEKDAYS = {0: "monday somwar peer pir", 1: "tuesday mangal", 2: "wednesday budh", 3: "thursday jumerat jumeraat",
             4: "friday jumma juma jummah jumah جمعہ", 5: "saturday", 6: "sunday itwar itwaar اتوار"}
_MONTHS = {m: i for i, ms in enumerate(["jan january", "feb february", "mar march", "apr april", "may", "jun june", "jul july",
                                        "aug august", "sep sept september", "oct october", "nov november", "dec december"], 1) for m in ms.split()}


def date_in(text: str, today: date | None = None) -> str | None:
    """An ISO date, '2 october', or a weekday / 'kal' / 'parson' said as a deadline. None if there's none."""
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if m:
        return m.group(1)
    from munshi.domain.models import business_today
    today = today or business_today()
    t = fold(text)
    for m in re.finditer(r"[a-z]+", t):                 # '2 october', 'october 2', '2nd oct'
        if m.group(0) not in _MONTHS:
            continue
        before = re.search(r"(?<!\d)(\d{1,2})\s*(?:st|nd|rd|th)?\s+$", t[:m.start()])
        after = re.match(r"\s+(\d{1,2})(?!\d)", t[m.end():])
        day = int((before or after).group(1)) if (before or after) else 0
        if 1 <= day <= 31:
            try:
                d = date(today.year, _MONTHS[m.group(0)], day)
            except ValueError:
                return None
            return (d if d >= today else date(today.year + 1, d.month, d.day)).isoformat()
    toks = set(words(t))
    for wd, names in _WEEKDAYS.items():
        if toks & {fold(n) for n in names.split()}:
            ahead = (wd - today.weekday()) % 7 or 7
            return (today + timedelta(days=ahead)).isoformat()
    if toks & {"parson", "parso", fold("پرسوں")}:
        return (today + timedelta(days=2)).isoformat()
    if toks & {"kal", "tomorrow", fold("کل")}:
        return (today + timedelta(days=1)).isoformat()
    return None


# ------------------------------------------------------------------ helpers used by the specialists
_ID_ANY = re.compile(r"(?<![A-Za-z0-9-])([A-Za-z]{1,4}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)(?![A-Za-z0-9])")


def ids_in(text: str, prefix: str) -> list[str]:
    """IDs with this prefix, case-insensitive, returned upper-case ('stp-89dac339' -> 'STP-89DAC339')."""
    p = prefix.upper() + "-"
    return list(dict.fromkeys(i.upper() for i in _ID_ANY.findall(fold(text)) if i.upper().startswith(p)))


def _prepared(text: str, repo: MunshiRepository, check_stock: bool = False):
    cat = catalogue(repo)
    pr = prepare(text, cat)
    return cat, pr, read_lines(pr.tokens, cat, repo, check_stock=check_stock)


def parse_items(text: str, repo: MunshiRepository) -> list[dict]:
    """Order lines, only when every line is clean and every number is accounted for; [] otherwise."""
    _, _, ln = _prepared(text, repo, check_stock=True)
    return [] if (ln.problems or ln.orphans) else [{"sku": x.sku, "qty": x.qty} for x in ln.lines]


def customer_resolution(text: str, repo: MunshiRepository) -> Resolution:
    _, pr, ln = _prepared(text, repo)
    return resolve_customer(pr.folded, repo, exclude=_exclusions(pr, ln), tokens=pr.tokens)


def supplier_resolution(text: str, repo: MunshiRepository) -> Resolution:
    _, pr, ln = _prepared(text, repo)
    return resolve_supplier(pr.folded, repo, exclude=_exclusions(pr, ln), tokens=pr.tokens)


def parse_customer(text: str, repo: MunshiRepository) -> str | None:
    """The customer ID only when resolution is confident; None when ambiguous or absent."""
    r = customer_resolution(text, repo)
    return r.id if r.ok else None


def parse_supplier(text: str, repo: MunshiRepository) -> str | None:
    r = supplier_resolution(text, repo)
    return r.id if r.ok else None


def sku_in(text: str, repo: MunshiRepository) -> str:
    """The one product a question is about ('' if none, or several different ones)."""
    cat = catalogue(repo)
    found = list(dict.fromkeys(m.sku for m in mentions(prepare(text, cat).tokens, cat)))
    return found[0] if len(found) == 1 else ""


def money_in(text: str) -> float:
    return amount_in(text).amount or 0.0


def int_in(text: str) -> int:
    """The first signed whole number once IDs, SKU codes and OTPs are masked (0 if none)."""
    s = _OTP_RE.sub(" ", fold(text))
    s = _ID_RE.sub(" ", s)
    s = re.sub(r"(?<![a-z0-9-])[a-z]+-\d+[a-z0-9-]*", " ", s)
    s = N.normalize_numbers(s)
    m = re.search(r"(?:(?<=\s)|^)[-+]?\d+", s)
    return int(m.group(0)) if m else 0


def warehouses_in(text: str, repo: MunshiRepository) -> list[str]:
    """Godowns named in the text -- by ID or by a distinctive word of their name ('Multan') -- in the order written."""
    t = fold(text)
    found: list[tuple[int, str]] = []
    for w in repo.list_warehouses():
        m = re.search(rf"(?<![a-z0-9-]){re.escape(fold(w.warehouse_id))}(?![a-z0-9-])", t)
        if not m:
            for tok in words(fold(w.name)):
                if tok in ("godown", "warehouse", "store", "main") or len(tok) < 4:
                    continue
                m = re.search(rf"(?<![\w-]){re.escape(tok)}(?![\w-])", t)
                if m:
                    break
        if m:
            found.append((m.start(), w.warehouse_id))
    return [w for _, w in sorted(found)]
