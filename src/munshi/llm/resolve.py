"""Customer and supplier resolution: the words a user typed -> a master-data ID, in
code, with a confidence rule. A model (or the stub) supplies text; it never
supplies an ID.

Cascade, first hit wins:
  1. an explicit ID in the text (C-002, S-001) that exists;
  2. the entity's phone number (10+ digits);
  3. name tokens, matched token by token against the message -- never as raw
     substrings ('purana' does not contain the customer 'Rana'), each token by
     exact form, singular form ('farms'/'farm'), Roman-Urdu sound key
     (Chaudhry/Chaudhary/Chowdhry/Chaudry) or one edit on that key for longer
     words (Frams/Farms); Urdu-script tokens via a small dictionary of trade-name
     words, else a consonant skeleton of their transliteration.

Name tokens that are function or command words ('bhej', 'do', 'ko', 'karo',
'order', 'khata'...) are dropped before matching, so a customer called
"Bhej Do Traders" cannot capture every "... bhej do" order. Generic
business words ('Traders', 'Store', 'Brothers', 'Sons'...) count, but can
never be the only evidence for a name.

Confidence rule (accept only when it holds, otherwise return the candidates and ask):
  * eligible: at least one distinctive name token is present, and either half of
    the distinctive tokens are present or the first one (the head: 'Fauji',
    'Rana', 'Chaudhry') is;
  * clear lead: no OTHER eligible entity is supported by the same message words
    or a superset of them. 'Chaudhry ko ...' is equally evidence for Chaudhry
    Farms and Chaudhry Traders -> ambiguous, ask which. 'Chaudhry Traders ko ...'
    has 'traders', which Chaudhry Farms can't explain -> accept Chaudhry Traders.
  * a second eligible entity supported by entirely different words is reported
    as `other`: the message may be about two customers (never merge them).

Tuned on eval/gold_corpus.jsonl: on the act messages that name a customer or
supplier, every accepted resolution was the gold entity (see eval/run_gold.py
entity_match and the WRONG-CARD rate)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from munshi.llm.text import fold, has_urdu_word, romanize, skeleton, sounds_like, words

_STOP = """
ko ka ki ke kay ne se sy par pe pr mein main mai me mn aur or and the a an of for to from by at in on with bhi hai hain hy he tha thi ho hua hui
huwa gaya gayi gaye kar karo kardo krdo kr krna karna karein karen kijiye kren do de dein den dena diya diye di dijiye dedo bhej bhejo bhejdo
bhejna bhejein bhejen bhejden bhjo send please plz pls bhai bhaijan sahab sahib saab sb ji jee wale walay walon wala wali walo waly order orders
khata balance hisaab hisab baqi baaki baki udhaar udhar payment paid pay cash bank cheque check chq jazzcash easypaisa stock kitna kitni kitne kya
kia kaun kon kis abhi ab aaj kal sab all yeh ye woh wo is us isko usko iska uska uski iski nahi nahin nai na mat not no lekin magar but ok okay
haan han yes dikhao dikha batao bata karwaye karwa chahiye chahye want need needs reminder promise wada new customer customers party total rs
rupay rupees rupe pkr hazar lakh sau per rate bill liye lie tak jaldi foran confirm cancel delivered deliver close otp code driver godown stop
plan report mujhe hum humein hamein aap ap unko inko unhon unhone ek from ms mr mrs m s bhejden kal
کو کا کی کے نے سے پر میں اور بہی ہے ہیں تہا تہی ہو ہوا گیا کر کرو کردو کریں دو دیں دی دیا بہیج بہیجو بہیجدو صاحب جی والی والوں والا حساب کہاتہ کہاتا
بیلنس باقی ادہار ادائیگی کیش بینک چیک اسٹاک سٹاک کتنا کتنی کتنی کیا کون کس اج کل سب یہ وہ نہیں نہ مت لیکن ارڈر بتاو بتائیں دکہاو دکہائیں چاہیی روپی
ہزار لاکہ سو لیی لئی تک
"""
STOPWORDS = frozenset(fold(w) for w in _STOP.split())
GENERIC = frozenset(fold(w) for w in """traders trader store stores brothers brother bros sons son co company centre center mart enterprises
enterprise agency agencies group depot corporation corp ltd limited pvt private industries shop dukan house & and""".split())


def _sing(t: str) -> str:
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


@dataclass
class Candidate:
    id: str
    name: str
    score: float
    evidence: frozenset[int] = frozenset()
    via: str = "name"                 # id | phone | head (the name's first distinctive word was present) | name


@dataclass
class Resolution:
    status: str                      # ok | ambiguous | none
    id: str | None = None
    name: str = ""
    candidates: list[Candidate] = field(default_factory=list)   # best first, at most 3
    other: Candidate | None = None   # a second, independent entity in the same message
    evidence: frozenset[int] = frozenset()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _name_tokens(name: str) -> tuple[list[str], list[str]]:
    toks = [_sing(t) for t in words(fold(name)) if len(t) >= 2 and not t.isdigit()]
    distinctive = [t for t in toks if t not in STOPWORDS and t not in GENERIC and len(t) >= 3]
    generic = [t for t in toks if t in GENERIC]
    return distinctive, generic


def _token_matches(msg_tok: str, name_tok: str) -> bool:
    if "؀" <= msg_tok[:1] <= "ۿ":
        r = romanize(msg_tok)
        if has_urdu_word(msg_tok):
            return _sing(r) == name_tok or sounds_like(_sing(r), name_tok)
        sk = skeleton(r)
        return len(sk) >= 3 and sk == skeleton(name_tok)
    m = _sing(msg_tok)
    return m == name_tok or sounds_like(m, name_tok) or (msg_tok != m and sounds_like(msg_tok, name_tok + "s"))


def message_tokens(folded: str) -> list[str]:
    return words(folded)


def resolve(folded: str, entities: list[tuple[str, str, str]], id_prefix: str, exclude: set[int] | frozenset[int] = frozenset(),
            tokens: list[str] | None = None) -> Resolution:
    """`entities` are (id, name, phone). `exclude` are message-token positions that belong
    to something else (product words, units, numbers) and can't be name evidence."""
    ids = {e[0].upper(): e for e in entities}
    for m in re.finditer(rf"(?<![a-z0-9]){id_prefix.lower()}-(\d{{3,}})(?![a-z0-9])", folded):
        e = ids.get(f"{id_prefix}-{m.group(1)}".upper())
        if e:
            c = Candidate(e[0], e[1], 1.0, via="id")
            return Resolution("ok", e[0], e[1], [c])
    digits = re.sub(r"\D", "", folded)
    if len(digits) >= 10:
        for e in entities:
            ph = re.sub(r"\D", "", e[2] or "")
            if len(ph) >= 10 and ph in digits:
                return Resolution("ok", e[0], e[1], [Candidate(e[0], e[1], 1.0, via="phone")])

    toks = tokens if tokens is not None else message_tokens(folded)
    usable = [(i, t) for i, t in enumerate(toks) if i not in exclude and t not in STOPWORDS and not t[:1].isdigit()]
    eligible: list[Candidate] = []
    for eid, name, _ in entities:
        distinctive, generic = _name_tokens(name)
        if not distinctive:
            continue                  # a name made only of command words / generic words is never matched by name
        ev: set[int] = set()
        hit_d = 0
        for nt in distinctive:
            pos = next((i for i, t in usable if i not in ev and _token_matches(t, nt)), None)
            if pos is not None:
                ev.add(pos)
                hit_d += 1
        if hit_d == 0:
            continue
        head_hit = any(_token_matches(t, distinctive[0]) for i, t in usable)
        if not (hit_d / len(distinctive) >= 0.5 or head_hit):
            continue
        hit_g = 0
        for nt in generic:
            pos = next((i for i, t in usable if i not in ev and _token_matches(t, nt)), None)
            if pos is not None:
                ev.add(pos)
                hit_g += 1
        score = (hit_d + 0.5 * hit_g) / (len(distinctive) + 0.5 * len(generic))
        eligible.append(Candidate(eid, name, round(score, 3), frozenset(ev), via="head" if head_hit else "name"))
    if not eligible:
        return Resolution("none")
    eligible.sort(key=lambda c: (-len(c.evidence), -c.score, c.id))
    top = eligible[0]
    rivals = [c for c in eligible[1:] if c.evidence >= top.evidence]
    # a second customer in the same message must be named by its head word or by two words ('Rana Brothers'),
    # not by one trailing word like the 'seed' in 'makai seed'
    others = [c for c in eligible[1:] if not (c.evidence & top.evidence) and (c.via == "head" or len(c.evidence) >= 2)]
    other = others[0] if others else None
    if rivals:
        return Resolution("ambiguous", None, "", [top, *rivals][:3], other, top.evidence)
    return Resolution("ok", top.id, top.name, [top, *[c for c in eligible[1:] if c not in others]][:3], other, top.evidence)


def resolve_customer(folded: str, repo, exclude=frozenset(), tokens=None) -> Resolution:
    return resolve(folded, [(c.customer_id, c.name, c.phone) for c in repo.list_customers()], "C", exclude, tokens)


def resolve_supplier(folded: str, repo, exclude=frozenset(), tokens=None) -> Resolution:
    return resolve(folded, [(s.supplier_id, s.name, s.phone) for s in repo.list_suppliers()], "S", exclude, tokens)
