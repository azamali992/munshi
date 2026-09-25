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
entity_match and the WRONG-CARD rate).

Learned names (memory across conversations, `with_memory`): after the name
reading, the business's learned phrases ('Bhatti sahab' -> C-007, taught only by a
human's confirmation -- see platform.py) are matched as whole words (llm.text.token_key:
spelling and script folded, honorifics unified). A learned phrase decides with
confidence only where names don't: a fuller name of someone else in the message, or
the phrase's own words naming someone else, win (the latter asks). The resolution
carries `alias` so every card that relied on memory says so. resolve_customer and
resolve_supplier are the ONE door every reader uses -- the offline rules, the topic
memory, the model guard's WHO check -- so all of them agree on every learned name."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from munshi.llm.text import fold, has_urdu_word, romanize, skeleton, sounds_like, token_key, words

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
    alias: dict | None = None        # the learned name that decided it (see with_memory), when memory decided it
    ranked: bool = False             # candidates are in the asking user's order (most used first), not by id

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


# ------------------------------------------------------------------ learned names (memory across conversations)
def learned(repo, kind: str) -> list[dict]:
    """The business's active learned names of this kind (repository cache); none for a repository without memory."""
    fn = getattr(repo, "learned_aliases", None)
    if fn is None:
        return []
    try:
        return fn(kind)
    except Exception:                    # a memory problem must never stop a message being read by name
        return []


def alias_hits(toks: list[str], exclude, aliases: list[dict]) -> list[tuple[frozenset[int], dict]]:
    """Learned phrases present in the message as whole words, in order, on positions that are not something else
    (product words, numbers): [(positions, alias row)], longest phrase first, never overlapping."""
    keys = [None if (i in exclude or not t[:1].isalpha()) else token_key(t) for i, t in enumerate(toks)]
    out: list[tuple[frozenset[int], dict]] = []
    taken: set[int] = set()
    for a in sorted(aliases, key=lambda a: (-len(a["phrase_norm"].split()), a["alias_id"])):
        pk = a["phrase_norm"].split()
        for i in range(len(keys) - len(pk) + 1):
            span = range(i, i + len(pk))
            if keys[i:i + len(pk)] == pk and not taken.intersection(span):
                out.append((frozenset(span), a))
                taken.update(span)
                break
    return sorted(out, key=lambda h: min(h[0]))


def with_memory(base: Resolution, folded: str, entities: list[tuple[str, str, str]], id_prefix: str, exclude, toks: list[str],
                aliases: list[dict], name_of=None) -> Resolution:
    """The name reading (`base`) with the business's learned names applied. Precedence, first that applies:

      * an explicit ID or phone number, or no learned phrase in the message: the name reading stands;
      * the name reading already gives the learned entity: it stands (memory wasn't needed);
      * the learned phrase is part of a FULLER name of someone else ('Malik' learned, 'Malik Agro Store' written):
        that name stands -- the words were the name, not the nickname;
      * the phrase's own words name someone else confidently (a customer added later called 'Zamindar Traders' for a
        learned 'zamindar sahab'): the name wins and it ASKS, offering both;
      * otherwise the learned name decides, with confidence ('Bhatti sahab' when there are two Bhattis), and the
        resolution says so (`alias`) so the card can name the memory it relied on. Another customer named elsewhere
        in the message is reported as `other` (two customers: ask), exactly as for names."""
    if not aliases or (base.ok and base.candidates and base.candidates[0].via in ("id", "phone")):
        return base
    ids = {e[0]: e for e in entities}
    hits = [h for h in alias_hits(toks, exclude, [a for a in aliases if a["entity_id"] in ids])]
    if not hits:
        return base
    pos, a = hits[0]
    tid, tname = a["entity_id"], ids[a["entity_id"]][1]
    if base.ok and base.id == tid:
        return base
    if base.ok and base.id != tid and base.evidence > pos:
        return base
    mine = Candidate(tid, tname, 1.0, pos, via="alias")
    allpos = frozenset(range(len(toks)))
    own = resolve(folded, entities, id_prefix, frozenset(exclude) | (allpos - pos), toks)
    if own.ok and own.id != tid:
        return Resolution("ambiguous", None, "", [own.candidates[0], mine], None, pos)
    if own.status == "ambiguous" and tid not in {c.id for c in own.candidates}:
        return Resolution("ambiguous", None, "", [mine, *own.candidates][:3], None, pos)
    other = None
    second = next((h for h in hits[1:] if h[1]["entity_id"] != tid), None)
    if second is not None:
        other = Candidate(second[1]["entity_id"], ids[second[1]["entity_id"]][1], 1.0, second[0], via="alias")
    else:
        rest = resolve(folded, entities, id_prefix, frozenset(exclude) | pos, toks)
        if rest.status in ("ok", "ambiguous") and rest.candidates:
            top = rest.candidates[0]
            if tid not in {c.id for c in rest.candidates} and (top.via == "head" or len(top.evidence) >= 2):
                other = top
    prev = a.get("previous_entity_id")
    use = {"alias_id": a["alias_id"], "phrase": a["phrase"], "phrase_norm": a["phrase_norm"], "id": tid, "name": tname,
           "previous_id": prev if prev and not a.get("uses") else None,
           "previous_name": ((name_of(prev) if name_of else "") or prev) if prev and not a.get("uses") else None}
    return Resolution("ok", tid, tname, [mine], other, pos, alias=use)


def _rank(res: Resolution, repo) -> Resolution:
    """A "which one?" question lists the options this user orders for most often first (never picks one)."""
    if res.status != "ambiguous" or len(res.candidates) < 2:
        return res
    try:
        usage = repo.customer_usage(repo._current_user())
    except Exception:
        return res
    if not usage or not any(usage.get(c.id) for c in res.candidates):
        return res
    res.candidates = sorted(res.candidates, key=lambda c: (-usage.get(c.id, 0), c.id))
    res.ranked = True
    return res


def resolve_customer(folded: str, repo, exclude=frozenset(), tokens=None, memory: bool = True) -> Resolution:
    ents = [(c.customer_id, c.name, c.phone) for c in repo.list_customers()]
    toks = tokens if tokens is not None else message_tokens(folded)
    res = resolve(folded, ents, "C", exclude, toks)
    if memory:
        res = with_memory(res, folded, ents, "C", exclude, toks, learned(repo, "customer"), lambda i: _name(repo, "customer", i))
        res = _rank(res, repo)
    return res


def resolve_supplier(folded: str, repo, exclude=frozenset(), tokens=None, memory: bool = True) -> Resolution:
    ents = [(s.supplier_id, s.name, s.phone) for s in repo.list_suppliers()]
    toks = tokens if tokens is not None else message_tokens(folded)
    res = resolve(folded, ents, "S", exclude, toks)
    return with_memory(res, folded, ents, "S", exclude, toks, learned(repo, "supplier"), lambda i: _name(repo, "supplier", i)) if memory else res


def _name(repo, kind: str, rid: str) -> str:
    """The name of what a learned phrase used to mean (for "last time you meant ..."); '' if it is gone."""
    try:
        return (repo.get_customer(rid) if kind == "customer" else repo.get_supplier(rid)).name
    except Exception:
        return ""
