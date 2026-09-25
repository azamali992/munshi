"""Memory across conversations: the pure language side (the repository stores it -- domain/repository/memory.py; the
platform decides when something was confirmed -- platform.py; the resolver applies it -- resolve.with_memory).

  LEARNED NAMES  `phrase_of()` turns the words that named someone in a message into the phrase worth remembering
                 ('Bhatti sahab ko 10 urea' -> 'Bhatti sahab'): the resolver's own evidence words plus the honorific
                 after them, only when they are contiguous, contain a real name word, and are not simply the entity's
                 full name (a full name needs no memory).

  COMMANDS       Said in chat, by the owner or a clerk (the platform checks the role):
                   teach / re-point   'Bhatti sahab matlab Bhatti Traders hai', 'bhatti sahab = C-013', 'FFC means Fauji'
                   forget             'Bhatti sahab bhool jao', 'forget bhatti sahab'
                   list               'kya kya yaad hai', 'learned names', 'what do you remember'
                 `command_of()` only recognises the shapes; `target_of()` reads the right-hand side by NAME ONLY (never
                 by another learned name) and must be sure of exactly one customer, supplier or product.

  HABITS         `repeat_plan()`: the lines of a customer's last confirmed order, for "wahi order dobara" / "same as last
                 time" / "pichla order repeat karo". Only a DELIVERED last order is repeated; one still on its way is a
                 double-order risk, so the munshi asks instead.

Nothing here writes, and nothing here decides that an action is safe: a learned name only ever leads to a read or to
an approval card that says, in plain words, which memory it used."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from munshi.llm.resolve import GENERIC, STOPWORDS, Resolution, resolve_customer, resolve_supplier
from munshi.llm.stub_model import contains
from munshi.llm.text import fold, is_honorific, phrase_key, words

MAX_PHRASE_WORDS = 4
TEACHERS = ("owner", "clerk")          # roles whose confirmations teach, and who may re-point or forget


# ------------------------------------------------------------------ phrases
def valid_phrase(phrase: str) -> bool:
    """1-4 words, at least one of them a real name word (not an honorific, command word, generic word or number)."""
    ws = words(fold(phrase))
    if not ws or len(ws) > MAX_PHRASE_WORDS or len(phrase.strip()) > 40:
        return False
    return any(not is_honorific(w) and w not in STOPWORDS and w not in GENERIC and not w[:1].isdigit()
               and (len(w) >= 3 or "؀" <= w[:1] <= "ۿ") for w in ws)


def _display(text: str, folded_words: list[str]) -> str:
    """The phrase as the user typed it (their casing and script), else the folded words."""
    pat = r"\s+".join(re.escape(w) for w in folded_words)
    m = re.search(rf"(?<!\w){pat}(?!\w)", fold(text))
    if m:
        orig = re.search(rf"(?i)(?<!\w){pat}(?!\w)", text)
        if orig:
            return orig.group(0)
    return " ".join(folded_words)


def phrase_of(text: str, tokens: list[str], evidence, name: str = "") -> str | None:
    """The phrase that named someone: the evidence positions (contiguous) plus up to two honorifics right after them
    ('bhatti sahab', 'malik wale'). None when there is nothing worth remembering."""
    ev = sorted(set(evidence or ()))
    if not ev or ev[-1] - ev[0] + 1 != len(ev):
        return None
    lo, hi = ev[0], ev[-1]
    while hi + 1 < len(tokens) and hi - ev[-1] < 2 and is_honorific(tokens[hi + 1]):
        hi += 1
    ws = [t for t in tokens[lo:hi + 1]]
    if not all(re.fullmatch(r"[a-z]+|[؀-ۿ]+", w) for w in ws):
        return None
    phrase = _display(text, ws)
    if not valid_phrase(phrase):
        return None
    if name and phrase_key(phrase) == phrase_key(name):
        return None                                   # the full name itself: nothing to remember
    return phrase


def learn_link(kind: str, entity_id: str, phrase: str, source: str, taught_by: str = "") -> dict:
    return {"link": "learn", "kind": kind, "entity_id": entity_id, "phrase": phrase, "phrase_norm": phrase_key(phrase),
            "source": source, "taught_by": taught_by}


def used_link(kind: str, alias: dict) -> dict:
    return {"link": "used", "kind": kind, "entity_id": alias["id"], "phrase": alias["phrase"], "phrase_norm": alias["phrase_norm"],
            "alias_id": alias["alias_id"], "previous_entity_id": alias.get("previous_id"), "source": "memory"}


def note_text(phrase: str, name: str, previous: str | None = None) -> str:
    """What a card says when a learned name decided who it is for."""
    return f"{phrase} = {name} (last time you meant {previous})" if previous else f"{phrase} = {name} (remembered)"


# ------------------------------------------------------------------ commands
@dataclass
class Command:
    kind: str                   # teach | forget | list
    phrase: str = ""
    target: str = ""


_TEACH = re.compile(r"^\s*[\"'“]?(?P<phrase>[^\"'”=?؟]{2,40}?)[\"'”]?\s*(?:=|\bka matlab\b|\bki matlab\b|\bmatlab\b|\bmatlb\b|\bmeans\b|\bse murad\b"
                    r"|\byani\b|کا مطلب|مطلب|سے مراد|یعنی)\s*(?P<target>[^?؟]{2,60}?)\s*(?:\bhai\b|\bhe\b|\bhain\b|\bhy\b|ہے|ہیں)?\s*[.!۔]*\s*$", re.I)
_FORGET_PRE = re.compile(r"^\s*(?:please\s+|plz\s+)?(?:forget|bhool jao|bhul jao|bhula do|bhula dein)\s+[\"'“]?(?P<phrase>[^\"'”?؟]{2,40}?)[\"'”]?\s*[.!۔]*\s*$", re.I)
_FORGET_POST = re.compile(r"^\s*[\"'“]?(?P<phrase>[^\"'”?؟]{2,40}?)[\"'”]?\s+(?:ko\s+|کو\s+)?(?:bhool jao|bhul jao|bhool ja|bhul ja|bhula do|bhula dein|bhool jayen"
                          r"|forget it|forget|yaad mat rakho|بھول جاؤ|بھول جائیں|بھلا دو)\s*[.!۔]*\s*$", re.I)
_LIST = contains("kya kya yaad hai", "kya kya yaad he", "kya yaad hai", "kya yaad he", "kia kia yaad hai", "learned names", "learnt names",
                 "what have you learned", "what have you learnt", "what do you remember", "remembered names", "yaad kiye hue naam",
                 "کیا کیا یاد ہے", "کیا یاد ہے")


def command_of(text: str) -> Command | None:
    """A memory command's shape, or None. The platform still checks the role, the phrase and the target."""
    t = str(text or "").strip()
    if not t or len(t) > 120 or "\n" in t:
        return None
    if _LIST(t) and len(words(fold(t))) <= 6:
        return Command("list")
    m = _FORGET_PRE.match(t) or _FORGET_POST.match(t)
    if m:
        return Command("forget", m.group("phrase").strip())
    m = _TEACH.match(t)
    if m and not re.search(r"(?i)\b(kya|kia|what|kaun|kon|which)\b|کیا|کون", m.group("target")):
        return Command("teach", m.group("phrase").strip(), m.group("target").strip())
    return None


@dataclass
class Target:
    kind: str = ""              # customer | supplier | product ('' when not sure)
    id: str = ""
    name: str = ""
    problem: str = ""           # why not: 'unknown' | 'ambiguous' | 'several'
    options: list[str] = field(default_factory=list)


def _clean(res: Resolution, text: str) -> bool:
    """A confident resolution that explains the whole target ('Bhatti Traders', 'C-013', 'Fauji'), not one word of it."""
    if not res.ok or res.other is not None:
        return False
    from munshi.llm.followup import _is_name_word, _leftover
    return not _leftover(text, lambda w: _is_name_word(w, res.name))


def target_of(text: str, repo) -> Target:
    """Who or what the right-hand side of a teach command names -- by name or ID only, never by a learned name."""
    from munshi.llm.parse import catalogue, mentions, prepare
    f = fold(text)
    c = resolve_customer(f, repo, memory=False)
    s = resolve_supplier(f, repo, memory=False)
    cat = catalogue(repo)
    skus = list(dict.fromkeys(m.sku for m in mentions(prepare(text, cat).tokens, cat)))
    found = []
    if _clean(c, text):
        found.append(Target("customer", c.id or "", c.name))
    if _clean(s, text):
        found.append(Target("supplier", s.id or "", s.name))
    if len(skus) == 1 and len(words(f)) <= 4:
        try:
            found.append(Target("product", skus[0], repo.get_product(skus[0]).name))
        except Exception:
            pass
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        return Target(problem="several", options=[f"{x.name} ({x.id})" for x in found])
    amb = c if c.status == "ambiguous" else s if s.status == "ambiguous" else None
    if amb is not None:
        return Target(problem="ambiguous", options=[f"{x.name} ({x.id})" for x in amb.candidates])
    return Target(problem="unknown")


def phrase_owner(phrase: str, kind: str, repo) -> Target | None:
    """Someone else the phrase already NAMES (by name, not memory) -- then it can't be taught as a nickname for another."""
    f = fold(phrase)
    if kind == "customer":
        r = resolve_customer(f, repo, memory=False)
    elif kind == "supplier":
        r = resolve_supplier(f, repo, memory=False)
    else:
        from munshi.llm.parse import catalogue, mentions, prepare
        cat = catalogue(repo)
        hit = list(dict.fromkeys(m.sku for m in mentions(prepare(phrase, cat).tokens, cat)))
        return Target("product", hit[0], repo.get_product(hit[0]).name) if hit else None
    return Target(kind, r.id or "", r.name) if r.ok else None


# ------------------------------------------------------------------ replies (English for English, Roman Urdu for Roman Urdu, Urdu for Urdu script)
REPLIES = {
    "learned": {"en": "OK -- from now on '{phrase}' means {name}.", "ru": "Theek hai -- ab se '{phrase}' ka matlab {name} hai.",
                "ur": "ٹھیک ہے — اب سے «{phrase}» کا مطلب {name} ہے۔"},
    "repointed": {"en": "OK -- from now on '{phrase}' means {name} (it meant {previous} before).",
                  "ru": "Theek hai -- ab se '{phrase}' ka matlab {name} hai (pehle {previous} tha).",
                  "ur": "ٹھیک ہے — اب سے «{phrase}» کا مطلب {name} ہے (پہلے {previous} تھا)۔"},
    "same": {"en": "I already remember that: '{phrase}' = {name}.", "ru": "Ye pehle se yaad hai: '{phrase}' = {name}.",
             "ur": "یہ پہلے سے یاد ہے: «{phrase}» = {name}۔"},
    "forgot": {"en": "OK -- I've forgotten '{phrase}' (it meant {name}).", "ru": "Theek hai -- '{phrase}' bhula diya (wo {name} tha).",
               "ur": "ٹھیک ہے — «{phrase}» بھلا دیا (وہ {name} تھا)۔"},
    "not_known": {"en": "I don't have '{phrase}' remembered, so there's nothing to forget.", "ru": "'{phrase}' mujhe yaad hi nahi, is liye bhoolne ko kuch nahi.",
                  "ur": "«{phrase}» مجھے یاد ہی نہیں۔"},
    "no_target": {"en": "I don't know who '{target}' is -- say the full name as it is in the books, or its ID (e.g. C-013).",
                  "ru": "'{target}' kaun hai, mujhe nahi pata -- poora naam likhein jaisa khaate mein hai, ya ID (maslan C-013).",
                  "ur": "«{target}» کون ہے، معلوم نہیں — پورا نام یا آئی ڈی لکھیں (مثلاً C-013)۔"},
    "which_target": {"en": "'{target}' could be {options} -- say which one, by its ID.", "ru": "'{target}' se murad {options} ho sakta hai -- ID se batayein.",
                     "ur": "«{target}» سے مراد {options} ہو سکتا ہے — آئی ڈی سے بتائیں۔"},
    "bad_phrase": {"en": "I can only remember a short name (1-4 words, with a real name word in it), like 'Bhatti sahab'.",
                   "ru": "Main sirf chhota naam yaad rakh sakta hoon (1-4 lafz), maslan 'Bhatti sahab'.",
                   "ur": "میں صرف مختصر نام یاد رکھ سکتا ہوں، جیسے «بھٹی صاحب»۔"},
    "taken": {"en": "'{phrase}' is already how the books name {owner}, so I won't point it at {name}.",
              "ru": "'{phrase}' to khaate mein {owner} ka naam hai, is liye isay {name} nahi bana sakta.",
              "ur": "«{phrase}» کھاتے میں {owner} کا نام ہے، اسے {name} نہیں بنا سکتا۔"},
    "not_allowed": {"en": "Only the owner or a clerk can change what I remember.", "ru": "Yaad-dasht sirf owner ya clerk badal sakte hain.",
                    "ur": "یادداشت صرف مالک یا کلرک بدل سکتے ہیں۔"},
    "list_empty": {"en": "I haven't learned any names yet. When you pick someone from a 'which one?' question, or approve a card, I remember what you called them.",
                   "ru": "Abhi koi naam yaad nahi kiya. Jab aap 'kaun sa?' ka jawab dete hain ya card approve karte hain, main naam yaad rakh leta hoon.",
                   "ur": "ابھی کوئی نام یاد نہیں۔"},
    "list_head": {"en": "Names I remember ({n}):", "ru": "Ye naam yaad hain ({n}):", "ur": "یہ نام یاد ہیں ({n}):"},
    "list_row": {"en": "- {phrase} = {name} ({kind}) -- taught by {who} on {day}, used {uses} time(s){prev}",
                 "ru": "- {phrase} = {name} ({kind}) -- {who} ne {day} ko sikhaya, {uses} dafa istemal{prev}",
                 "ur": "- {phrase} = {name} ({kind}) — {who}، {day}، {uses} بار{prev}"},
    "list_prev": {"en": "; before that it meant {previous}", "ru": "; pehle {previous} tha", "ur": "؛ پہلے {previous}"},
}


def say(key: str, lang: str, **kw) -> str:
    table = REPLIES[key]
    return table.get(lang, table["en"]).format(**kw)


# ------------------------------------------------------------------ habits: repeat the last order
REPEAT = contains("dobara", "dubara", "phir se", "repeat", "same as last time", "same order", "wahi order", "wohi order", "wahi wala order",
                  "pichli dafa wala order", "last order", "دوبارہ", "وہی آرڈر", "پچھلا آرڈر")
_QUESTION = contains("kya", "kia", "kaun", "kon", "konsa", "kaunsa", "what", "which", "show", "dikhao", "dikha", "batao", "bata", "tha", "thi",
                     "list", "کیا", "کون", "دکھاؤ", "بتاؤ")


def asks_repeat(text: str) -> bool:
    """'Haji Sons ko wahi order dobara', 'same as last time', 'pichla order repeat karo' -- a request, not a question
    about the last order ('pichla order kya tha?' is a read)."""
    return REPEAT(text) and not _QUESTION(text) and "?" not in text and "؟" not in text


@dataclass
class RepeatPlan:
    status: str                          # ok | open (last order still on its way) | none (no confirmed order) | unavailable
    order_id: str = ""
    order_status: str = ""
    items: list[dict] = field(default_factory=list)       # [{"sku", "qty"} + "unit_price" for a negotiated line]
    lines: str = ""                                       # '20 x UREA-50, 5 x DAP-50'
    negotiated: list[str] = field(default_factory=list)   # SKUs repeated at their negotiated price
    problem: str = ""


def repeat_plan(repo, customer_id: str) -> RepeatPlan:
    """What "the same again" means for this customer: the lines of their last confirmed order, at today's price list,
    except a line that was priced by hand (negotiated), which keeps that price -- and the card says so."""
    try:
        last = repo.confirmed_orders(customer_id, 1)
    except Exception:
        return RepeatPlan("none")
    if not last:
        return RepeatPlan("none")
    o = last[0]
    lines = ", ".join(f"{i.qty} x {i.sku}" for i in o.items)
    if o.status not in ("delivered", "short"):
        return RepeatPlan("open", o.order_id, o.status, lines=lines)
    try:
        draft = next(iter(repo.list_orders(status="draft", customer_id=customer_id, limit=1)), None)
    except Exception:
        draft = None
    if draft is not None and draft.created_at >= o.created_at:          # a newer order is already waiting: the same risk
        return RepeatPlan("open", draft.order_id, draft.status, lines=", ".join(f"{i.qty} x {i.sku}" for i in draft.items))
    neg = repo.negotiated_prices(o.order_id)
    items, used = [], []
    for i in o.items:
        try:
            p = repo.get_product(i.sku)
        except Exception:
            return RepeatPlan("unavailable", o.order_id, o.status, lines=lines, problem=i.sku)
        if not p.active:
            return RepeatPlan("unavailable", o.order_id, o.status, lines=lines, problem=p.name)
        line = {"sku": i.sku, "qty": int(i.qty)}
        if i.sku in neg:
            line["unit_price"] = neg[i.sku]
            used.append(i.sku)
        items.append(line)
    return RepeatPlan("ok", o.order_id, o.status, items, lines, used)
