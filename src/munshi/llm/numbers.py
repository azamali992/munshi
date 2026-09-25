"""Quantities and amounts as people say them, rewritten as plain digits.

    1,000 / 1,08,250 / Rs. 25,000     -> 1000 / 108250 / 25000   (western and lakh grouping)
    1k / 2.5k                         -> 1000 / 2500
    50 hazar / 5 lakh / 1.5 lakh      -> 50000 / 500000 / 150000
    dedh sau / dhai sau / dedh lakh   -> 150 / 250 / 150000
    sawa sau / paune do sau           -> 125 / 175
    ek lakh bees hazar / ek sau bees  -> 120000 / 120
    bees bori / pachas bag / das dap  -> 20 bori / 50 bag / 10 dap
    ڈیڑھ سو / پچاس ہزار / ۲۰              -> 150 / 50000 / 20         (after text.fold)

Words that are also everyday verbs or pronouns are numbers only where a number
must be: 'do' (2, and 'give' in 'bhej do'), 'ek', 'nau', and their Urdu forms count
only before a multiplier (sau, hazar, lakh...), a unit (bori, bag...), a product
word the caller passes in, or after sawa/paune/saade. Words that are too
ambiguous to ever read as numbers ('tera' = your, 'bara' = big, 'saath' = with,
'chay' = tea) are not in the table at all.

The arithmetic is done here, in code, never by a model."""
from __future__ import annotations

import re

from munshi.llm.text import fold

_WORD = {
    "ek": 1, "aik": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "three": 3, "char": 4, "chaar": 4, "four": 4,
    "panch": 5, "paanch": 5, "five": 5, "chhe": 6, "six": 6, "saat": 7, "seven": 7, "aath": 8, "eight": 8,
    "nau": 9, "nine": 9, "das": 10, "dus": 10, "ten": 10, "gyarah": 11, "gyara": 11, "eleven": 11, "barah": 12, "twelve": 12,
    "terah": 13, "chaudah": 14, "pandrah": 15, "pandra": 15, "fifteen": 15, "solah": 16, "satrah": 17, "atharah": 18,
    "unees": 19, "bees": 20, "twenty": 20, "pachees": 25, "pachis": 25, "tees": 30, "thirty": 30, "chalees": 40, "chalis": 40,
    "forty": 40, "pachas": 50, "pachaas": 50, "fifty": 50, "sattar": 70, "assi": 80, "nabbe": 90,
    # Urdu script (folded forms: ھ->ہ, ے->ی)
    "ایک": 1, "دو": 2, "تین": 3, "چار": 4, "پانچ": 5, "چہ": 6, "سات": 7, "اٹہ": 8, "نو": 9, "دس": 10, "گیارہ": 11,
    "بارہ": 12, "تیرہ": 13, "چودہ": 14, "پندرہ": 15, "سولہ": 16, "سترہ": 17, "اٹہارہ": 18, "انیس": 19, "بیس": 20,
    "پچیس": 25, "تیس": 30, "چالیس": 40, "پچاس": 50, "ساٹہ": 60, "ستر": 70, "نوی": 90,
}
_GUARDED = {"do", "ek", "aik", "nau", "one", "دو", "ایک", "نو"}
_MULT = {"sau": 100, "hundred": 100, "hazar": 1000, "hazaar": 1000, "hajar": 1000, "thousand": 1000, "lakh": 100_000, "lac": 100_000,
         "lakhs": 100_000, "laakh": 100_000, "lacs": 100_000, "million": 1_000_000, "crore": 10_000_000, "karor": 10_000_000, "karod": 10_000_000,
         "سو": 100, "ہزار": 1000, "لاکہ": 100_000, "لاک": 100_000, "کروڑ": 10_000_000}
_FRACTION = {"adha": 0.5, "aadha": 0.5, "dedh": 1.5, "derh": 1.5, "dairh": 1.5, "deedh": 1.5, "dhai": 2.5, "dhaai": 2.5, "dhayi": 2.5,
             "adhai": 2.5, "arhai": 2.5, "ڈیڑہ": 1.5, "ڈیڑ": 1.5, "ڈہائی": 2.5, "ادہا": 0.5}
_MODIFIER = {"sawa": 0.25, "paune": -0.25, "pone": -0.25, "paunay": -0.25, "saade": 0.5, "sade": 0.5, "saarhe": 0.5, "sarhe": 0.5,
             "سوا": 0.25, "پونی": -0.25, "ساڑہی": 0.5}
_WORD = {fold(k): v for k, v in _WORD.items()}
_GUARDED = {fold(k) for k in _GUARDED}
_MULT = {fold(k): v for k, v in _MULT.items()}
_FRACTION = {fold(k): v for k, v in _FRACTION.items()}
_MODIFIER = {fold(k): v for k, v in _MODIFIER.items()}

# Unit words (folded). Kept here because the guarded number words need them.
BAG_UNITS = {"bag", "bags", "bori", "boriyan", "boriya", "borian", "boriyon", "bora", "katta", "katte", "kattay", "katay", "kata", "katty",
             "بوری", "بوریاں", "بوریان", "کٹا", "کٹی", "کٹہ", "بیگ"}
PACK_UNITS = {"carton", "cartons", "peti", "petiyan", "petti", "box", "boxes", "dabba", "dabbe", "packet", "packets", "pack", "packs",
              "کارٹن", "پیٹی", "ڈبہ", "ڈبی"}
DOZEN_UNITS = {"dozen", "dozens", "darjan", "درجن"}
WEIGHT_UNITS = {"ton", "tons", "tonne", "tonnes", "ٹن", "mann", "maund", "من"}
LTR_UNITS = {"ltr", "ltrs", "litre", "litres", "liter", "liters", "lt", "لیٹر", "bottle", "bottles", "botal"}
PC_UNITS = {"pc", "pcs", "piece", "pieces", "adad", "nag", "عدد"}
BAG_UNITS, PACK_UNITS, DOZEN_UNITS, WEIGHT_UNITS, LTR_UNITS, PC_UNITS = (
    frozenset(fold(u) for u in s) for s in (BAG_UNITS, PACK_UNITS, DOZEN_UNITS, WEIGHT_UNITS, LTR_UNITS, PC_UNITS))
UNIT_WORDS = BAG_UNITS | PACK_UNITS | DOZEN_UNITS | WEIGHT_UNITS | LTR_UNITS | PC_UNITS

_TOKEN = re.compile(r"\d+(?:\.\d+)?|[a-z]+|[؀-ۿ]+|\s+|.", re.S)
_GROUPED = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{2})*,\d{3}|\d{1,3}(?:,\d{3})+)(?![\d,])")
_KILO = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)k\b")


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(round(v, 4))


def _num(tok: str) -> float | None:
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    return None


def normalize_numbers(folded: str, nouns: set[str] | frozenset[str] = frozenset()) -> str:
    """`folded` is text.fold() output. `nouns` are extra words (product names) that make a
    guarded number word like 'do' count as a number when it comes right before them."""
    s = _GROUPED.sub(lambda m: m.group(1).replace(",", ""), folded)
    s = _KILO.sub(lambda m: _fmt(float(m.group(1)) * 1000), s)
    toks = [m.group(0) for m in _TOKEN.finditer(s)]
    sig = [i for i, t in enumerate(toks) if not t.isspace()]      # indexes of non-space tokens
    out: list[str] = []
    k = 0            # position in sig
    last = -1        # last toks index already emitted
    triggers = UNIT_WORDS | set(_MULT) | set(nouns)
    while k < len(sig):
        parsed = _phrase(toks, sig, k, triggers)
        if parsed is None:
            k += 1
            continue
        value, k_end = parsed
        start, end = sig[k], sig[k_end - 1]
        out.append("".join(toks[last + 1:start]))
        out.append(_fmt(value))
        last = end
        k = k_end
    out.append("".join(toks[last + 1:]))
    return "".join(out)


def _base(toks, sig, k, triggers) -> tuple[float, int] | None:
    """One number without its multiplier: digits, a number word, a fraction word, or sawa/paune/saade + number."""
    if k >= len(sig):
        return None
    t = toks[sig[k]]
    nxt = toks[sig[k + 1]] if k + 1 < len(sig) else ""
    if (v := _num(t)) is not None:
        return v, k + 1
    if t in _FRACTION:
        return _FRACTION[t], k + 1
    if t in _MODIFIER:
        if nxt in _MULT:                                   # sawa sau = 1.25 x 100
            return 1 + _MODIFIER[t], k + 1
        inner = _num(nxt) if nxt else None
        if inner is None and nxt in _WORD:
            inner = float(_WORD[nxt])
        if inner is not None:
            return inner + _MODIFIER[t], k + 2
        return None
    if t in _WORD:
        if t in _GUARDED and nxt not in triggers:
            return None
        return float(_WORD[t]), k + 1
    return None


def _phrase(toks, sig, k, triggers) -> tuple[float, int] | None:
    total = 0.0
    prev_mult = None
    got = False
    while True:
        b = _base(toks, sig, k, triggers)
        if b is None:
            break
        base, k2 = b
        mult = 1
        if k2 < len(sig) and toks[sig[k2]] in _MULT:
            mult = _MULT[toks[sig[k2]]]
            k2 += 1
            # "do sau hazar" is not a thing people say; "ek sau" then "bees" is handled by the next group
        if got and (prev_mult is None or prev_mult <= 1 or mult >= prev_mult):
            break                                           # "20 25" are two numbers, not 45
        total += base * mult
        got, prev_mult, k = True, mult, k2
        if mult == 1:
            break
    return (total, k) if got else None


def numbers_in(text_normalized: str) -> list[float]:
    return [float(x) for x in re.findall(r"(?<![\w.-])\d+(?:\.\d+)?(?![\w.])", text_normalized)]
