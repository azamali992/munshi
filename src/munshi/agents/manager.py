"""The Manager: routes each message to one specialist and never acts.

Offline (the stub), routing is keyword rules in Roman Urdu, English and Urdu
script, matched on text.fold()ed text so Arabic-keyboard letters, Urdu digits and
spelling variants land on the same words. Order matters, most specific first:

  1. things no munshi should do -- wipe/delete records, reveal a PIN or code, edit
     master data -- go to the help desk, which refuses plainly (never a tool);
  2. money direction before stock words: 'Fauji ko 5 lakh bank transfer kar do' is a
     supplier payment (khareed), 'X ne 2 lakh transfer kiya' is a receipt (hisaab),
     and only then is 'transfer' a godown move;
  3. the business areas, with the paraphrases real users type ('kitna baqi hai',
     'ka hisaab', 'orders dikhao', 'mera agla stop', Urdu-script keywords);
  4. what's left: approval-bypass requests, greetings, thanks, off-topic and
     unrecognised Urdu script go to the help desk; anything else routes nowhere and
     the platform asks which area it's about.

The help desk ('help') has no tools for any role, so nothing it says can act."""
from __future__ import annotations

import re
from typing import Literal

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from munshi.llm.numbers import UNIT_WORDS, normalize_numbers
from munshi.llm.stub_model import Rule, StubToolCallingModel, contains
from munshi.llm.text import fold
from munshi.safety.auth import MunshiState

Specialist = Literal["order", "godown", "delivery", "hisaab", "khareed", "wasooli", "report", "help"]


@tool
def route_to_order() -> str:
    """Route to the Order Munshi: taking, confirming, cancelling or looking up customer orders."""
    return "ROUTE:order"

@tool
def route_to_godown() -> str:
    """Route to the Godown Munshi: stock levels, allocation, dispatch planning, loading, transfers, adjustments."""
    return "ROUTE:godown"

@tool
def route_to_delivery() -> str:
    """Route to the Delivery Munshi: a driver's stops and closing deliveries with OTP."""
    return "ROUTE:delivery"

@tool
def route_to_hisaab() -> str:
    """Route to the Hisaab Munshi: driver cash deposits, payments received at the office, expenses, cashbook, khata, credit notes, today's digest."""
    return "ROUTE:hisaab"

@tool
def route_to_khareed() -> str:
    """Route to the Khareed Munshi: stock received from suppliers, supplier bills and payments, what we owe."""
    return "ROUTE:khareed"

@tool
def route_to_wasooli() -> str:
    """Route to the Wasooli Munshi: receivables aging, reminders, promises to pay."""
    return "ROUTE:wasooli"

@tool
def route_to_report() -> str:
    """Route to the Report Munshi: sales, profit, margin, collections, stock valuation, slow stock, top customers, a product's movement history."""
    return "ROUTE:report"

@tool
def route_to_help() -> str:
    """Route to the help desk: greetings, thanks, questions about what Munshi can do, and requests no munshi may carry out
    (deleting records, revealing a PIN or delivery code, approving without a human)."""
    return "ROUTE:help"


_ROUTES = [route_to_order, route_to_godown, route_to_delivery, route_to_hisaab, route_to_khareed, route_to_wasooli, route_to_report]
_STUB_ROUTES = _ROUTES + [route_to_help]

CLARIFY = "Is this about an order, the godown, a delivery, cash/khata, a supplier, collections, or a report?"


def _rx(pattern: str):
    """A regex over text.fold()ed text with spoken numbers already turned into digits ('ڈیڑھ سو بوری' -> '150 بوری').
    Urdu-script literals in the pattern are folded the same way."""
    rx = re.compile(re.sub(r"[؀-ۿ]+", lambda m: fold(m.group(0)), pattern))
    return lambda t: bool(rx.search(normalize_numbers(fold(t), UNIT_WORDS)))


def _all(*preds):
    return lambda t: all(p(t) for p in preds)


def _none(*preds):
    return lambda t: not any(p(t) for p in preds)


_MONEY = r"(lakh|lac|hazar|hazaar|crore|karor|\brs\b|rupay|rupees|rupe|روپ|لاکہ|ہزار|payment|paise|\bpay\b|cheque|\bbank\b|\bcash\b|jazz ?cash|easy ?paisa)"
# requests no munshi may carry out
_DESTROY = _rx(r"\b(delete|mita|mitao|mita do|wipe|erase|truncate|drop table|saaf kar)\b|مٹا|ڈیلیٹ|حذف")
_SECRET = _rx(r"\b(pin|password|passcode|pass word)\b|پن کوڈ|پاس ورڈ|\b(otp|code|کوڈ)\b.{0,12}\b(kya|batao|bata|bhejo|dikhao|share|tell|what)\b"
              r"|\b(what|tell|share)\b.{0,12}\b(otp|code)\b|او ٹی پی.{0,12}(کیا|بتاو)")
_EDIT = _rx(r"\b(naam|name|phone|number|address|rate|price)\b.{0,20}\b(badal|badlo|change|rename|update|edit)\b|\brename\b")
_BYPASS = _rx(r"\b(approve|approval|manzoor)\w*\b.{0,30}\b(bina|without|khud|sab|all|every|pending)\b|\bbina (pooch|puch)|"
              r"\b(sab|all|every)\b.{0,20}\b(approve|approvals|manzoor)\b|ignore (all |the )?(previous|above|earlier) instructions|approvals? (off|disabled|band)")
_GREET = _rx(r"^\W*(assalam|asalam|salam|slam|aoa|a\.o\.a|hello|hi|hey|salaam|adaab|good (morning|evening))\b|السلام|^\W*سلام")
_THANKS = _rx(r"\b(shukriya|shukria|thanks|thank you|thx|jazakallah|meherbani)\b|شکریہ")
_BYE = _rx(r"\b(khuda hafiz|allah hafiz|bye|baad mein|kal baat)\b|خدا حافظ|اللہ حافظ")
_ACK = _rx(r"^\W*(ok|okay|theek|thik|haan|han|ji|jee|acha|achha|done|👍|✅)\W*(hai|he|bhai|g)?\W*$")
_HELP = _rx(r"^\W*(help|madad)\W*$|\b(kya kar sakte|what can you do)\b|مدد")
_OFFTOPIC = _rx(r"\b(mausam|weather|cricket|news|khabar|joke|lateefa|gana|song|movie|film)\b")
_PERSONAL = _rx(r"\b(mera|meri|my)\b.{0,12}\b(tankha|tankhwah|salary|pay)\b")
_URDU_SCRIPT = _rx(r"[؀-ۿ]")


def _stub() -> StubToolCallingModel:
    R = lambda pred, route: Rule(pred, route, lambda t: {})   # noqa: E731
    return StubToolCallingModel(rules=[
        # 1. never business: refused by the help desk
        R(_DESTROY, "route_to_help"), R(_SECRET, "route_to_help"), R(_EDIT, "route_to_help"), R(_PERSONAL, "route_to_help"),
        # reversals and credit notes (kept first: 'damaged' / 'transfer' must not pull them elsewhere)
        R(contains("credit note", "refund", "waive", "maaf"), "route_to_hisaab"),
        # a promise to pay mentions money too, but it is a collections matter ('ne ... 50000 ka wada kiya')
        R(_rx(r"\b(wada|waada|promise|promised)\b|وعدہ"), "route_to_wasooli"),
        R(contains("collections report", "collection report", "recovery report"), "route_to_report"),
        # 2. money direction before 'transfer'
        R(_all(_rx(r"\bko\b"), _rx(_MONEY), _rx(r"\b(de do|dedo|de dein|de den|dena hai|pay kar|pay karo|ada kar|transfer kar|bhej de)\b")),
          "route_to_khareed"),
        R(_all(_rx(_MONEY), _rx(r"\b(ne|se)\b"), _rx(r"\b(transfer|diye|di|diya|kiya|kiye|bheje|bheja|jama|aaya|aaye|aayi|dale|daale|mile|mila|received|wasool|wusool|vasool)\b")),
          "route_to_hisaab"),
        R(contains("restock", "write off", "write-off", "damaged", "transfer", "allocate", "dispatch", "approve dsp", "shift"), "route_to_godown"),
        R(_all(_rx(r"\bdsp-"), _rx(r"\b(approve|load|loading|manzoor)\b")), "route_to_godown"),
        # 3. business areas
        R(contains("sales report", "profit", "munafa", "margin", "valuation", "slow stock", "dead stock", "top customers", "collection report",
                   "revenue", "bikri", "sale", "sales", "ledger", "movement", "value", "nahi bik", "bik nahi", "not selling",
                   "منافع", "بکری", "سیل", "رپورٹ"), "route_to_report"),
        R(_rx(r"sab se (zyada|ziada|zaida) kaun (khareed|kharid|leta)"), "route_to_report"),
        R(contains("supplier", "suppliers", "payable", "payables", "purchase", "bought", "khareed", "fauji", "engro", "arrived", "from",
                   "فوجی", "اینگرو", "خرید", "سپلائر"), "route_to_khareed"),
        R(_all(_rx(r"\bse\b"), _rx(r"\b(aaya|aayi|aaye|aai|aya|ayi|aa gaya|aa gayi)\b"), _none(_rx(_MONEY))), "route_to_khareed"),
        R(_rx(r"سے .*(آیا|آئی|آئے)"), "route_to_khareed"),
        R(contains("remind", "reminder", "overdue", "aging", "collections", "promise", "wasooli", "who owes", "broken", "yaad", "yaad dehani",
                   "wada", "waada", "toda", "de denge", "denge", "وصولی", "یاد دہانی", "وعدہ"), "route_to_wasooli"),
        R(_rx(r"\b(kis kis|kaun kaun|kon kon|sab se (zyada|ziada) (udhaar|udhar|baqi))\b|کس کس|کون کون|سب سے زیادہ (ادھار|باقی)"), "route_to_wasooli"),
        R(contains("deposit", "handed", "counted", "credit note", "refund", "digest", "close the day", "summary", "reconcile", "expense", "kharcha",
                   "diesel", "fuel", "salary", "cashbook", "cash book", "paid", "payment", "jazzcash", "easypaisa", "cheque", "received", "jama",
                   "bijli", "mazdoor", "petrol", "rent", "marammat", "chai", "chai pani", "wasool", "کیش بک", "خرچہ", "جمع", "ادائیگی", "چیک", "مرمت"),
          "route_to_hisaab"),
        R(_rx(r"\b(aaj|today|din)\b.{0,12}\b(hisaab|hisab)\b"), "route_to_hisaab"),
        R(_rx(r"\b(mera|meri|my|aaj ka)\b.{0,10}\bplan\b"), "route_to_delivery"),
        R(contains("stop", "stops", "delivered", "otp", "driver", "agla stop", "next stop", "kahan jana", "kahan kahan", "de diya", "utar diya",
                   "اسٹاپ", "ڈیلیور"), "route_to_delivery"),
        R(contains("stock", "allocate", "dispatch", "plan", "load", "godown", "restock", "write off", "damaged", "approve dsp", "vehicle", "route",
                   "transfer", "available", "اسٹاک", "سٹاک", "گودام"), "route_to_godown"),
        R(_all(_rx(r"\b(kitni|kitna|kitne|how much|how many|کتنی|کتنا)\b"), _rx(r"\b(hai|he|pari|padi|bachi|baqi hai|پڑی|ہے)\b"),
               _none(_rx(r"\b(baqi|baaki|udhaar|udhar|dena|dene|owe|paise|hisaab|hisab|balance|khata)\b|باقی|ادہار|حساب|کہاتہ|بیلنس"))),
          "route_to_godown"),
        R(contains("order", "orders", "confirm", "cancel", "khata", "balance", "bhej", "chahiye", "want", "bags", "bori", "urea", "dap",
                   "baqi", "baaki", "udhaar", "udhar", "hisaab", "hisab", "outstanding", "owe", "owes", "pakka", "mansookh", "dikhao",
                   "بھیج", "بھیجو", "آرڈر", "کھاتہ", "حساب", "بیلنس", "باقی", "ادھار", "چاہیے", "منسوخ"), "route_to_order"),
        R(_rx(r"\b(dena|dene) hai|kitne paise"), "route_to_order"),
        R(_rx(r"\bko\s+\d|\d+\s*(bori|bag|bags|katte|katta|carton|peti|dozen)\b|\d+\s*(بوری|کٹے)"), "route_to_order"),
        R(_rx(r"^\W*(aur|and|or)\b.*\b(ka|ki|ke)\W*$"), "route_to_order"),                  # 'aur Haji Sons ka?'
        R(_rx(r"\bord-[a-z0-9]+"), "route_to_order"),                                      # 'ORD-... ka status kya hai'
        R(_rx(r"^\W*dsp-[a-z0-9]+\W*$"), "route_to_delivery"),
        R(contains("need", "needs", "chahiye", "mangta", "mangwana"), "route_to_order"),
        # a quantity followed by a word, with no other cue ('New Kisan Dost 6 drip line'): the order desk reads it
        # and asks if it can't account for every number -- it never guesses
        R(_rx(r"(?<![\w-])\d+\s*(x\s*)?[a-z؀-ۿ]{2,}"), "route_to_order"),
        # 4. the rest: refusals of approval bypass, small talk, off-topic, and Urdu script nobody recognised
        R(_BYPASS, "route_to_help"), R(_GREET, "route_to_help"), R(_THANKS, "route_to_help"), R(_BYE, "route_to_help"),
        R(_ACK, "route_to_help"), R(_HELP, "route_to_help"), R(_OFFTOPIC, "route_to_help"), R(_URDU_SCRIPT, "route_to_help"),
    ], fallback_text=CLARIFY)


def build_manager(model: BaseChatModel | None = None):
    # The stub can also send to the help desk; a real model gets the seven business routes and,
    # when it routes nowhere, the platform asks which area the message is about.
    if model is None:
        return create_agent(_stub(), tools=_STUB_ROUTES, state_schema=MunshiState,
                            system_prompt="You are the Manager at a distribution business. Read the message and call exactly one route_to_* tool for the munshi who handles it. Never do the work yourself.")
    return create_agent(model, tools=_ROUTES, state_schema=MunshiState,
                        system_prompt="You are the Manager at a distribution business. Read the message and call exactly one route_to_* tool for the munshi who handles it. Never do the work yourself.")


def classify(manager, text: str, role: str = "clerk") -> Specialist | None:
    result = manager.invoke({"messages": [HumanMessage(text)], "role": role})
    for m in result["messages"]:
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith("ROUTE:"):
            return m.content.split(":", 1)[1]  # type: ignore[return-value]
    return None
