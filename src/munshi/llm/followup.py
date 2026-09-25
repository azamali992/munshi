"""Follow-ups: what a message means given the conversation so far. Pure functions over text and master data;
the platform stores the state (agents' chat meta) and decides when to use it.

Two pieces of conversation state, both per thread AND per role, both expiring (EXPIRY_S):

  OPEN QUESTION  the munshi asked for one missing piece (llm.replies.Ask: customer / supplier / amount / date /
                 items / order / product / otp / stock_kind) of a request. A next message that is ONLY that
                 piece ("haji sons", "80000", "doosra", "kal", "ORD-...", "correction") completes the original
                 request: `answer()` returns the request's text with the piece added, and it runs exactly as if
                 the user had typed it in full -- same rules, same guard, same approval card. A message that
                 carries anything else is a new message, never an answer.

  TOPIC          the customer / supplier / product the last turns were about. A message that names nobody but
                 is clearly about "them" -- a pronoun or possessive ('their', 'uska', 'اس نے'), or a short
                 follow-up about a customer's money or orders ('aur payment?', 'confirm payment', 'orders?') --
                 gets the remembered one (`augment()` adds its ID to the text, so every rule, the guard and the
                 card see the same explicit record). A whole-business question ('kis kis ne', 'sab', 'aaj ki
                 collection') never does, and a message that names someone else switches the topic.

Nothing here decides that something is SAFE to do: a remembered customer only ever leads to a read or to an
approval card that names the customer, and the platform says "(from our conversation)" on it."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from munshi.llm import numbers as N
from munshi.llm.parse import amount_in, analyse_order, customer_resolution, date_in, ids_in, sku_in, supplier_resolution, warehouses_in
from munshi.llm.resolve import GENERIC, _name_tokens, _token_matches
from munshi.llm.stub_model import contains
from munshi.llm.text import fold, words

EXPIRY_S = 600          # an open question or a remembered entity older than this is forgotten
MAX_CHATTER = 3         # an open question survives this many turns of small talk / help in between

# ------------------------------------------------------------------ cues
# A pronoun or possessive that points back at someone already mentioned. The same test is used by the offline
# rules (agents/specialists.py) and by the model guard (agents/guard.py), so both accept the same follow-ups.
PRONOUN = contains(
    "isko", "isse", "ise", "iska", "iski", "iske", "ye", "yeh", "yehi", "yahi", "wo", "woh", "usko", "use", "uska", "uski", "uske",
    "unka", "unki", "unke", "unko", "unhe", "unhen", "unhein", "unhon", "unhone", "unhon ne", "inka", "inki", "inke", "inko", "inhe", "inhein",
    "inhon", "inhone", "usi", "isi", "usne", "us ne", "is ne", "isne", "is ka", "is ki", "is ke", "us ka", "us ki", "us ke", "is ko", "us ko",
    "unho", "unho ne", "unhoon", "unhoon ne", "inho", "inho ne",
    "this", "it", "that", "same", "same customer", "same party", "wala", "wali", "wale",
    # ('he' is left out on purpose: in Roman Urdu it is usually 'hai' -- "payment ki he?")
    "their", "theirs", "them", "they", "his", "him", "her", "this customer", "that customer", "this party",
    "یہ", "اسے", "اسکو", "اس کا", "اس کی", "اس کے", "اس کو", "اس نے", "وہ", "اسی", "ان کا", "ان کی", "ان کے", "ان کو", "انہوں", "انہیں", "انکا", "انکی", "انہوں نے")

# Pronouns that are also everyday words ('this month', 'ye order', 'wo aaya', 'same rate') lean on the topic only
# together with a customer / supplier matter in the same message; the others ('their', 'uska', 'unko') always do.
def _strong_pronoun(text: str) -> bool:
    if not PRONOUN(text):
        return False
    f = fold(text)
    for w in ("ye", "yeh", "yehi", "yahi", "wo", "woh", "this", "it", "that", "same", "wala", "wali", "wale", fold("یہ"), fold("وہ")):
        f = re.sub(rf"(?<!\w){re.escape(w)}(?!\w)", " ", f)
    return PRONOUN(f)


WHOLE_BUSINESS = contains(
    "kis", "kin", "kaun", "kon", "konse", "kaunse", "who", "which", "sab", "sabhi", "saare", "sare", "sary", "tamam", "all", "everyone", "everybody",
    "list", "total", "top", "report", "har", "aaj", "aj", "today", "todays", "today's", "customers", "costumers", "clients", "parties",
    "suppliers", "companies", "profit", "sales", "month", "week", "mahine", "hafte", "kitne orders", "kitne order", "orders kitne", "how many orders",
    "کس", "کون", "سب", "تمام", "لسٹ", "آج", "اج")

_CUSTOMER_INTENT = contains(
    "payment", "payments", "paid", "pay", "jama", "diye", "diya", "wusool", "wasool", "khata", "balance", "hisaab", "hisab", "baqi", "baaki",
    "udhaar", "udhar", "outstanding", "owe", "owes", "orders", "order", "reminder", "remind", "yaad", "promise", "wada", "waada", "credit note",
    "statement", "dena", "dene", "collection", "bheje", "bheja", "bhejay", "jazzcash", "jazz cash", "easypaisa", "easy paisa", "cheque", "check",
    "raqam", "limit", "credit limit", "address", "کھاتہ", "حساب", "بیلنس", "باقی", "ادھار", "ادائیگی", "جمع", "آرڈر", "وعدہ")
_SUPPLIER_INTENT = contains("pay", "payment", "de do", "dedo", "de dein", "ada", "bill", "khata", "hisaab", "hisab", "balance", "dena", "owe",
                            "purchase", "maal", "ادائیگی", "حساب")
_ELLIPSIS = re.compile(r"^\W*(aur|and|or|what about|how about|اور)\b", re.I)
_STOCKISH = contains("stock", "stocks", "maal", "kitna", "kitni", "available", "pada", "pari", "padi", "bacha", "اسٹاک", "سٹاک", "مال")

# words that can surround an answer without adding meaning ('Haji Sons wala', '80000 cash mein', 'kal tak')
_FILLER = frozenset(fold(w) for w in """
ko ka ki ke kay ne se sy wala wali wale walay walon sahab sahib saab sb ji jee g bhai hai he hain hy hein tha thi the thay ok okay haan han
yes ya yeah yup for to a an of is it its it's was were by via mein main me mai mei pe par rs rupay rupees rupe pkr k hazar hazaar lakh lac
crore sau thousand only sirf bas just please plz pls ye yeh wo woh customer party name naam client costumer the tak till until by on
کو کا کی کے نے سے جی بھائی ہے ہیں تھا تھی تھے روپے ہزار لاکھ صرف وہ والا والی والے یہ تک
""".split())
_MONEY_WORDS = frozenset(fold(w) for w in """cash nakad naqd naqad bank online cheque check chq jazzcash jazz easypaisa easy paisa transfer ibft
paid pay payment diye diya di dia jama received mila mile aaye aaya wusool wasool vasool raqam paise paisay نقد بینک چیک جمع ادائیگی""".split())
_ORDINALS = {1: {"pehla", "pehle", "pehli", "pahla", "pahle", "pehlay", "first", "1", "1st", "one", "ek", "پہلا", "پہلے", "پہلی", "اول"},
             2: {"doosra", "dusra", "doosre", "dusre", "doosri", "dusri", "second", "2", "2nd", "two", "do", "دوسرا", "دوسرے", "دوسری"},
             3: {"teesra", "tisra", "teesre", "teesri", "third", "3", "3rd", "three", "teen", "تیسرا", "تیسرے", "تیسری"}}
_ORD_WORD = {fold(w): n for n, ws in _ORDINALS.items() for w in ws}
_YES = frozenset(fold(w) for w in """haan han haa ha ji jee g yes yeah yep ok okay theek thik thek bilkul sahi done wahi wohi yahi yehi
bana banao kar karo kardo chalo go ہاں جی ٹھیک بالکل وہی""".split())
_YES_FILL = frozenset(fold(w) for w in """he hai hy do dein den dijiye de diya lo please plz pls wala wali wale ye yeh wo woh isi usi ko bhai sahab
is it that one this the kar karo same ہے دو دیں""".split())
_POINT = frozenset(fold(w) for w in "ye yeh wo woh wala wali wale isi usi yahi yehi wahi wohi this that یہ وہ".split())
_CORRECTION = contains("correction", "correct", "adjust", "adjustment", "galti", "ghalti", "sahi karo", "sahi kar do", "theek karo", "theek kar do",
                       "count", "ginti", "درستی", "غلطی")


def now() -> datetime:
    return datetime.now().astimezone()


def fresh(at: str | None, t: datetime | None = None) -> bool:
    try:
        return (t or now()) - datetime.fromisoformat(str(at)) <= timedelta(seconds=EXPIRY_S)
    except (TypeError, ValueError):
        return False


def leans_on_history(text: str) -> bool:
    """The message points back at someone named earlier: a pronoun or possessive."""
    return PRONOUN(text)


def _leftover(text: str, drop=lambda t: False) -> list[str]:
    """The words of the message that are not filler and not dropped by `drop`."""
    s = N.normalize_numbers(fold(re.sub(r"(?i)\b[a-z]{1,4}-[a-z0-9]+(?:-[a-z0-9]+)*\b", " ", text or "")))
    return [w for w in words(s) if w not in _FILLER and not re.fullmatch(r"\d+(?:\.\d+)?", w) and not drop(w)]


def _names_words(name: str) -> list[str]:
    d, g = _name_tokens(name)
    return d + g + [t for t in words(fold(name)) if t in GENERIC]


def _is_name_word(w: str, name: str) -> bool:
    return any(_token_matches(w, nt) for nt in _names_words(name))


def is_res_word(w: str, res) -> bool:
    """A word of the name a resolution found -- or of the learned name that decided it ('ffc' for Fauji Fertilizer)."""
    return _is_name_word(w, res.name) or bool(res.alias and w in words(fold(res.alias.get("phrase") or "")))


def ordinal(text: str) -> int | None:
    """1 for 'pehla wala' / 'first' / '1', 2 for 'doosra'... when that is ALL the message says."""
    toks = [w for w in words(fold(text)) if w not in _FILLER or w in _ORD_WORD]
    picks = {_ORD_WORD[w] for w in toks if w in _ORD_WORD}
    rest = [w for w in toks if w not in _ORD_WORD and w not in ("number", "no", "option", "wala", "wali", "wale")]
    return next(iter(picks)) if len(picks) == 1 and not rest else None


# ------------------------------------------------------------------ open questions
def is_yes(text: str) -> bool:
    """'haan', 'ji', 'theek he bana do', 'ok kar do', 'haan wahi', 'ye wala' -- a yes to the one option offered, and nothing else."""
    toks = words(fold(text))
    if not toks or len(toks) > 6 or not all(w in _YES or w in _YES_FILL or w in _FILLER for w in toks):
        return False
    return any(w in _YES or w in _POINT for w in toks)


def by_name_word(text: str, cands: list[dict]) -> int | None:
    """1-based pick when the answer's words fit exactly one offered option's name ('new wala' -> New Kisan Dost),
    even words the resolver ignores ('new'); None if they fit none or several."""
    toks = [w for w in words(fold(text)) if w not in _FILLER and w not in ("wala", "wali", "wale")]
    if not toks or len(toks) > 3:
        return None
    hits = [i for i, c in enumerate(cands, 1) if all(any(_token_matches(t, n) or t == n for n in words(fold(str(c.get("name") or "")))) for t in toks)]
    return hits[0] if len(hits) == 1 else None


_SHOW_ALL = contains("sab dikhao", "sab dikha do", "sari list", "saari list", "puri list", "poori list", "pura dikhao", "poora dikhao", "baqi bhi",
                     "baaki bhi", "aur dikhao", "show all", "all of them", "full list", "the rest", "more", "baqi", "sab", "all", "saare", "sare", "tamam",
                     "باقی بھی", "سب دکھاؤ", "پوری لسٹ")


def answer(ask: dict, text: str, repo) -> tuple[str, str | None] | None:
    """If `text` is only the piece `ask` asked for: (the original request with it added, the specialist to send it
    to or None for the one that asked). None when the message is anything more -- it is then handled as new."""
    slot, base = ask.get("slot"), str(ask.get("text") or "")
    if not slot or not str(text or "").strip():
        return None
    cands = ask.get("candidates") or []
    if slot == "dispatch":                          # a dispatch suggestion: 'theek he, bana do' takes the only one, 'doosra' picks one
        n = ordinal(text) or (1 if len(cands) == 1 and is_yes(text) else None)
        return (str(cands[n - 1]["id"]), ask.get("specialist") or "godown") if n and 0 < n <= len(cands) else None
    if slot == "split":                             # 'haan' to 'one card per customer?'
        if cands and (is_yes(text) or (contains("dono", "donon", "both", "alag", "separate", "sab", "دونوں")(text) and len(words(fold(text))) <= 5
                                       and not customer_resolution(text, repo).ok)):
            return "split", ask.get("specialist") or "order"
        return None
    if not base:
        return None
    if slot == "more":                              # 'sab dikhao' / 'puri list' after a list that was cut short
        if _SHOW_ALL(text) and len(words(fold(text))) <= 5 and not customer_resolution(text, repo).ok:
            return f"{base} sab", None
        return None
    if slot in ("customer", "supplier"):
        n = ordinal(text) or (1 if len(cands) == 1 and is_yes(text) else None) or by_name_word(text, cands)
        if n is not None:
            return (f"{base} {cands[n - 1]['id']}", None) if 0 < n <= len(cands) else None
        res = (customer_resolution if slot == "customer" else supplier_resolution)(text, repo)
        if not res.ok or res.other is not None:
            return None
        rid = res.id or ""
        if _leftover(text, lambda w: is_res_word(w, res)):              # (an ID like C-009 is already out of the words)
            return None
        return f"{base} {rid}", None
    if slot == "amount":
        a = amount_in(text)
        if a.amount is None or _leftover(text, lambda w: w in _MONEY_WORDS):
            return None
        first = amount_in(base)
        if first.amount is None and first.problem.startswith("which amount"):
            # 'which amount? I see 4512, 50000' -> '50000': the other figures in the request were not the amount
            keep = N._fmt(a.amount)
            base = re.sub(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w.])", lambda m: m.group(0) if m.group(0).replace(",", "") == keep else " ", base)
        return f"{base} {text.strip()}", None
    if slot == "date":
        if not date_in(text) or _leftover(text, lambda w: bool(date_in(w)) or w in ("tak", "ko", "pe", "tomorrow", "kal", "parson", "din", "day")):
            return None
        return f"{base} {text.strip()}", None
    if slot == "items":
        op = analyse_order(text, repo)
        if not op.items or op.problems or op.customer.status != "none":
            return None
        return f"{base} {text.strip()}", None
    if slot == "order":
        n = ordinal(text) or (1 if len(cands) == 1 and is_yes(text) else None)
        if n is not None and cands:
            return (f"{base} {cands[n - 1]['id']}", None) if 0 < n <= len(cands) else None
        oid = ids_in(text, "ORD")
        if len(oid) != 1 or _leftover(text, lambda w: w in ("ord", "order", "wala")):
            return None
        return f"{base} {oid[0]}", None
    if slot == "otp":
        m = re.fullmatch(r"\W*(?:(?:sorry|maaf|ji|jee|acha|ok|galti|ghalti|ye lo|yeh lo|ye|yeh|sahi|asal|correct|right|new|naya|dusra|doosra)\W+)*"
                         r"(?:otp|code|pin|او ٹی پی|کوڈ)?\s*(?:hai|he|is|:|-)?\s*(\d{4,6})\s*(?:hai|he|tha|hy)?\s*[.!]?\s*", fold(text))
        if not m:
            return None
        clean = re.sub(r"(?i)\b(?:otp|o\.t\.p|code|pin)\s*(?:hai|is|:|#|-|=)?\s*\d{4,6}\b", " ", base)       # a wrong code tried before is replaced
        return f"{clean.strip()} otp {m.group(1)}", None
    if slot == "product":
        if contains("all", "sab", "sabhi", "saare", "sare", "tamam", "every", "سب", "تمام")(text) and len(words(fold(text))) <= 6:
            return f"{base} {text.strip()}", None
        sku = sku_in(text, repo)
        return (f"{base} {text.strip()}", None) if sku and len(words(fold(text))) <= 5 else None
    if slot == "stock_kind":
        s = supplier_resolution(text, repo)
        if s.ok and s.other is None:
            return f"{base} {text.strip()}", "khareed"
        if _CORRECTION(text) and len(words(fold(text))) <= 6 and not customer_resolution(text, repo).ok:
            return f"{base} adjust", "godown"
        return None
    return None


# ------------------------------------------------------------------ topic memory
def entities(text: str, repo) -> dict:
    """What a message names confidently: {"customer": {...}, "supplier": {...}, "product": {...}} (only those found);
    a kind named ambiguously comes back as None (the topic of that kind is then dropped, never guessed)."""
    out: dict = {}
    c, s = customer_resolution(text, repo), supplier_resolution(text, repo)
    if c.ok and c.other is None:
        out["customer"] = {"id": c.id, "name": c.name}
    elif c.status == "ambiguous" or (c.ok and c.other is not None):
        out["customer"] = None
    if s.ok and s.other is None and (not c.ok or set(s.evidence) - set(c.evidence)):
        out["supplier"] = {"id": s.id, "name": s.name}
    elif s.status == "ambiguous":
        out["supplier"] = None
    sku = sku_in(text, repo)
    if sku:
        try:
            out["product"] = {"sku": sku, "name": repo.get_product(sku).name}
        except Exception:
            pass
    return out


def augment(text: str, topic: dict | None, repo) -> tuple[str, dict | None]:
    """The message with the remembered customer / supplier / product added, when it clearly means them; else the
    message unchanged. Returns (text, the entity used as {"kind", "id", "name"} or None)."""
    if not topic:
        return text, None
    named = entities(text, repo)
    if "customer" in named or "supplier" in named:
        return text, None                                # it names someone (or names someone ambiguously): never override
    strong = _strong_pronoun(text)
    pron = PRONOUN(text)
    short = len(words(fold(text))) <= 7
    kind = topic.get("last")
    # a product follow-up: 'aur dap ka?' after a stock question, or 'iska stock' with a product in the topic
    if topic.get("intent") == "stock" and _ELLIPSIS.search(fold(text)) and named.get("product") and short:
        return f"{text} stock kitna hai", None
    # ... or another godown: 'aur vehari mei?' / 'sirf vehari ka' right after 'multan mei urea kitni he'
    if topic.get("intent") == "stock" and topic.get("product") and not named.get("product") and short and (
            _ELLIPSIS.search(fold(text)) or contains("sirf", "only", "bas", "just")(text)) and warehouses_in(text, repo):
        return f"{text} {topic['product']['sku']} stock kitna hai", {"kind": "product", "id": topic["product"]["sku"], "name": topic["product"]["name"]}
    if topic.get("product") and not named.get("product") and pron and _STOCKISH(text):
        return f"{text} {topic['product']['sku']}", {"kind": "product", **{"id": topic["product"]["sku"], "name": topic["product"]["name"]}}
    if WHOLE_BUSINESS(text) and not strong:
        return text, None
    for k in ([kind] if kind in ("customer", "supplier") else []) + ["customer", "supplier"]:
        ent = topic.get(k)
        if not ent:
            continue
        intent = _CUSTOMER_INTENT(text) if k == "customer" else _SUPPLIER_INTENT(text)
        items = bool(analyse_order(text, repo).items)
        if (strong and (intent or items or short)) or (pron and intent) or (intent and short and not items):
            return f"{text} {ent['id']}", {"kind": k, "id": ent["id"], "name": ent["name"]}
        break
    return text, None


def next_topic(prev: dict | None, run_text: str, repo, role: str, card_args: dict | None = None, whole_business: bool = False,
               intent: str | None = None, t: datetime | None = None) -> dict | None:
    """The topic after a turn: what this turn named (in its text or on its card) replaces what was remembered."""
    at = (t or now()).isoformat()
    topic = dict(prev) if prev and prev.get("role") == role and fresh(prev.get("at"), t) else {"role": role}
    named = entities(run_text, repo)
    for k, idk in (("customer", "customer_id"), ("supplier", "supplier_id")):
        rid = str((card_args or {}).get(idk) or "")
        if rid:
            try:
                rec = repo.get_customer(rid) if k == "customer" else repo.get_supplier(rid)
                named[k] = {"id": rid, "name": rec.name}
            except Exception:
                pass
    touched = False
    for k in ("customer", "supplier", "product"):
        if k in named:
            topic[k] = named[k]
            touched = True
            if named[k] and k != "product":
                topic["last"] = k
    if whole_business and not touched:
        topic.pop("customer", None)
        topic.pop("supplier", None)
        topic.pop("last", None)
    if intent:
        topic["intent"] = intent
    elif touched:
        topic.pop("intent", None)
    if touched or intent:
        topic["at"] = at
    topic["role"] = role
    return topic if any(topic.get(k) for k in ("customer", "supplier", "product")) and topic.get("at") else None
