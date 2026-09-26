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
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
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
_EDIT = _rx(r"\b(naam|name|phone|number|address|rate|price)\b.{0,20}\b(badal|badlo|change|rename|update|edit)\b|\brename\b"
            r"|\b(rate|price|qeemat|keemat|bhao)\b\s*(?:rs\.?\s*)?\d+\s*(kar do|kardo|karo|kar dein|kr do|set)\b")
_BYPASS = _rx(r"\b(approve|approval|manzoor)\w*\b.{0,30}\b(bina|without|khud|sab|all|every|pending)\b|\bbina (pooch|puch)|"
              r"\b(sab|all|every)\b.{0,20}\b(approve|approvals|manzoor)\b|ignore (all |the )?(previous|above|earlier) instructions|approvals? (off|disabled|band)")
_GREET = _rx(r"^\W*(assalam|asalam|salam|slam|aoa|a\.o\.a|hello|hi|hey|salaam|adaab|good (morning|evening|afternoon)|subah? ba?khair|subha ba?khair|"
             r"shab ba?khair)\b|السلام|^\W*سلام|صبح بخیر")
_THANKS = _rx(r"\b(shukriya|shukria|thanks|thank you|thx|jazakallah|meherbani)\b|شکریہ")
_BYE = _rx(r"\b(khuda hafiz|allah hafiz|bye|baad mein|kal baat)\b|خدا حافظ|اللہ حافظ")
_ACK = _rx(r"^\W*(ok|okay|theek|thik|haan|han|ji|jee|acha|achha|done|👍|✅)\W*(hai|he|bhai|g)?\W*$")
_HELP = _rx(r"^\W*(help|madad)\W*[?]?\W*$|\b(kya kar sakte|kya kya kar|what can you do|what do you do|how (do|to) (i )?use|kaise (use|istemal))\b|مدد"
            r"|\bsku\b.{0,12}\b(kya|kia|what|matlab|mean|means|hota|hai|he)\b|\bwhat('?s| is) (an? |the )?sku\b")
_ARRIVED = _rx(r"\b(aaye|aaya|aayi|aye|aya|ayi|ayein|aayein|aaein|aain|aa gaye|aa gaya|aa gayi|arrived|came in|pohanch gaya|pohnch gaya)\b|آئے|آیا|آئی|پہنچ")
_INCREASE = _rx(r"\b(increase|brha|barha|bara do|badha|barhao|brhao|barhado|brhado|add|adjust|plus)\b|بڑھا")
# whole-business questions: nobody named, the answer is a list across the business
_PAID_TODAY = _all(_rx(r"\b(aaj|aj|today|todays|today's)\b|آج|اج"),
                   _rx(r"\b(payment|payments|paid|paise|paisay|collection|collections|wasooli|wusooli|jama|received|recovery|bank|online|jazz ?cash|"
                       r"easy ?paisa|cheque)\b|ادائیگی|جمع|وصول|پیسے|بینک"),
                   _none(_rx(r"\d{3,}")))
_RECEIVABLES = _all(_rx(r"\b(kis|kin|kaun|kon|who|which|sab|sabhi|saare|sare|list|kaunse|konse|kinse)\b|کس|کون|سب|لسٹ"),
                    _rx(r"\b(paise|paisay|dena|dene|lene|lena|udhaar|udhar|owe|owes|owing|baqi|baaki|receivable|receivables|outstanding)\b|پیسے|ادھار|ادہار|باقی"),
                    _none(_rx(r"\d{3,}")), _none(_rx(r"\b(supplier|suppliers|payable|payables|stock|stocks|maal)\b|سپلائر|اسٹاک")))
_ALL_PRODUCTS = _rx(r"\b(all|sab|saare|sare|tamam|har|every)\b.{0,20}\b(products?|items?|maal|cheez\w*|stock|stocks)\b|سب مال|تمام مال")
_STOCK_WORD = _rx(r"\b(stock|stocks|inventory|godown)\b|اسٹاک|سٹاک|گودام")
_OFFTOPIC = _rx(r"\b(mausam|weather|cricket|news|khabar|joke|lateefa|gana|song|movie|film)\b")
_PERSONAL = _rx(r"\b(mera|meri|my)\b.{0,12}\b(tankha|tankhwah|salary|pay)\b")
_URDU_SCRIPT = _rx(r"[؀-ۿ]")


def _stub(repo=None) -> StubToolCallingModel:
    R = lambda pred, route: Rule(pred, route, lambda t: {})   # noqa: E731

    def _product(t: str) -> bool:
        """The message names a product in this business's catalogue (only with a repository)."""
        if repo is None:
            return False
        from munshi.llm.parse import catalogue, mentions, prepare
        cat = catalogue(repo)
        return bool(mentions(prepare(t, cat).tokens, cat))

    def _nobody(t: str) -> bool:
        """The message names no customer or supplier, even ambiguously (only checkable with a repository)."""
        if repo is None:
            return True
        from munshi.llm.parse import customer_resolution, supplier_resolution
        return customer_resolution(t, repo).status == "none" and supplier_resolution(t, repo).status == "none"

    def _supplier(t: str) -> bool:
        """The message names one supplier confidently, and no customer."""
        if repo is None:
            return False
        from munshi.llm.parse import customer_resolution, supplier_resolution
        return supplier_resolution(t, repo).ok and customer_resolution(t, repo).status == "none"

    def _customer(t: str) -> bool:
        if repo is None:
            return False
        from munshi.llm.parse import customer_resolution
        r = customer_resolution(t, repo)
        return r.ok and r.other is None

    def _ambiguous(t: str) -> bool:
        if repo is None:
            return False
        from munshi.llm.parse import customer_resolution
        return customer_resolution(t, repo).status == "ambiguous"

    def _rev_intent(t: str) -> bool:
        from munshi.llm.turns import rev_intent
        return rev_intent(t)

    def _godowns(n: int):
        def pred(t: str) -> bool:
            if repo is None:
                return False
            from munshi.llm.parse import warehouses_in
            return len(warehouses_in(t, repo)) == n
        return pred

    return StubToolCallingModel(rules=[
        # 1. never business: refused by the help desk
        R(_DESTROY, "route_to_help"), R(_SECRET, "route_to_help"), R(_EDIT, "route_to_help"), R(_PERSONAL, "route_to_help"),
        # deciding cards from chat ('malik wala reject kar do, baqi sab approve kar do'): decisions are taken on the cards
        R(_all(_BYPASS, _rx(r"\b(approve|reject|manzoor|radd)\w*\b")), "route_to_help"),
        # reversals and credit notes (kept first: 'damaged' / 'transfer' must not pull them elsewhere) -- 'chhoot' is a discount given
        R(contains("credit note", "refund", "waive", "maaf", "chhoot", "chhot", "riayat", "riyayat", "رعایت", "چھوٹ"), "route_to_hisaab"),
        # undoing a customer's payment ('X ki 20000 wali entry galat thi, cancel kar do'): hisaab, never the order desk's 'cancel'
        R(_all(_rev_intent, lambda t: not _supplier(t)), "route_to_hisaab"),
        # a customer's credit limit is on their account (the order desk shows it), not a stock question
        R(_rx(r"\bcredit limit\b|\blimit (kitni|kitna|kya)\b"), "route_to_order"),
        # the day's cash: expenses, the driver's hand-in ('kharche kitne hue aaj', 'driver ka cash pura aya?')
        R(_rx(r"\b(kharche|kharchay|kharchey|kharchon)\b|\bdriver (ka|ki|ke)? ?cash\b|\bcash (pura|poora|short|kam)\b|\bkitna short\b|خرچے"), "route_to_hisaab"),
        # a promise to pay mentions money too, but it is a collections matter ('ne ... 50000 ka wada kiya', 'kaha 30 tareekh tak de dega')
        R(_rx(r"\b(wada|waada|promise|promised|de dega|de denge|dega|denge|degi|dengay)\b|وعدہ"), "route_to_wasooli"),
        R(contains("collections report", "collection report", "recovery report"), "route_to_report"),
        # who owes the MOST ('sab se zyada kis ka he'): the receivables, largest first -- not a sales ranking
        R(_rx(r"sab se (zyada|ziada|zaida)\s+(kis|kiss|kin)\s*(ka|ki|ke|k)\b|\b(who|which customer) owes (us )?(the )?most\b"), "route_to_wasooli"),
        # whole-business questions (nobody named): who paid today, who owes us -- lists, never 'which customer?'
        R(_all(_PAID_TODAY, _nobody), "route_to_report"),
        R(_all(_RECEIVABLES, _nobody), "route_to_wasooli"),
        # 'which of my customers are over their credit terms / overdue, and our exposure': the aging list
        R(_all(_rx(r"\b(over (their |the )?(credit )?terms|overdue|past due|exposure)\b"), _nobody), "route_to_wasooli"),
        # 2. money direction before 'transfer'
        # money paid to a named SUPPLIER ('ali akbar ko 35000 cash de diye', 'fauji ko 5 lakh ka cheque de diya'): a supplier payment
        R(_all(_supplier, _rx(r"\bko\b|کو"), _rx(r"\d"),
               _rx(r"\b(de diya|de diye|de di|diye|diya|payment ki|pay ki|pay kiye|ada ki|ada kiye|bhej diye|bheje)\b|دیے|دیا")), "route_to_khareed"),
        R(_all(_rx(r"\bko\b"), _rx(_MONEY), _rx(r"\b(de do|dedo|de dein|de den|dena hai|pay kar|pay karo|ada kar|transfer kar|bhej de)\b")),
          "route_to_khareed"),
        R(_all(_rx(_MONEY), _rx(r"\b(ne|se)\b"), _rx(r"\b(transfer|diye|di|diya|kiya|kiye|bheje|bheja|jama|aaya|aaye|aayi|dale|daale|mile|mila|received|wasool|wusool|vasool)\b")),
          "route_to_hisaab"),
        # Urdu script: 'رانا برادرز نے ۲۵۰۰۰ نقد دیے' (a sum a customer paid)
        R(_all(_rx(r"نے"), _rx(r"\d{3,}"), _rx(r"دیے|دیا|دی|جمع|بھیجے|ادا"), lambda t: not _product(t)), "route_to_hisaab"),
        # 'Rana Brothers ne 20000 diye': a sum paid by someone, with no money word
        R(_all(_rx(r"\bne\b"), _rx(r"(?<![\w-])\d{3,}(?![\w-])"), _rx(r"\b(diye|diya|dia|di|jama|bheje|bheja|transfer|kiye|kiya|karwaye|karwa diye)\b"),
               _none(_rx(r"\b(ord|stp|dsp)-")), lambda t: not _product(t)), "route_to_hisaab"),
        # a supplier the conversation is about (the platform adds its ID to the message: 'unko 200000 de do S-001')
        R(_rx(r"(?<![\w-])s-\d+\b"), "route_to_khareed"),        # ('S-001' reads 's-1' once numbers are normalised)
        # a supplier's goods arriving ('fauji ka truck aya he 200 bori urea 3600 rate bill no FF-4410'): a purchase
        R(_all(_supplier, _rx(r"\d"), lambda t: _product(t) or _STOCK_WORD(t),
               _rx(r"\b(aaye|aaya|aayi|aye|aya|ayi|ayein|aayein|aaein|aain|aa gaye|aa gaya|aa gayi|arrived|received|truck|bill|rate|pohanch\w*|pohnch\w*)\b")),
          "route_to_khareed"),
        # stock written off ('urea ki 6 bori damage ho gayi', '6 bori phati hui thi damage likh do'): the godown, nobody named
        R(_all(_product, _rx(r"\d"), _nobody, _rx(r"\b(damage|damaged|phat\w*|kharab|toot\w*|tuti|nikal do|nikaal do|write off|gal gayi|bheeg gayi|chori)\b|خراب")),
          "route_to_godown"),
        # stock moved between two godowns ('multan se 30 bori vehari bhej do')
        R(_all(_godowns(2), _product, _rx(r"\d"), _nobody, _rx(r"\b(bhej|bhejo|bhej do|transfer|move|shift|le jao|pohncha do|bhijwa do)\b")), "route_to_godown"),
        # stock coming in / stock count going up: a godown write, never a stock read ('drip line ke 1000 aur stock ayein')
        R(_all(_rx(r"\d"), lambda t: _product(t) or _STOCK_WORD(t), lambda t: _ARRIVED(t) or _INCREASE(t), _none(_rx(r"\bse\b|سے")), _none(_rx(_MONEY)),
               _none(_rx(r"\b(order|orders)\b"))), "route_to_godown"),
        # a product at a named godown, no quantity ('vehari mei urea?'): a stock question
        R(_all(_product, _nobody, _godowns(1), _none(_rx(r"\d"))), "route_to_godown"),
        # what a product's SKU code is ('sku kya he drip ka?'): the catalogue
        R(_all(_product, _rx(r"\bsku\b")), "route_to_order"),
        # is a product running low ('cypermethrin kam he kya'), or what does it cost ('What is the price of SOP?')
        R(_all(_product, _nobody, _rx(r"\b(kam|khatam|khatm|low|short|bacha|bachi|bache|finish)\b"), _none(_rx(r"\d{2,}"))), "route_to_godown"),
        R(_all(_product, _nobody, _rx(r"\b(price|rate|qeemat|keemat|bhao|bhav)\b"), _none(_rx(r"\d{2,}"))), "route_to_order"),
        # a customer's account as a statement ('Shalimar ka ledger nikalo'): the khata, not a product's stock ledger
        R(_all(_customer, _rx(r"\b(ledger|statement)\b")), "route_to_order"),
        R(contains("restock", "write off", "write-off", "damaged", "transfer", "allocate", "dispatch", "approve dsp", "shift"), "route_to_godown"),
        R(_all(_rx(r"\bdsp-"), _rx(r"\b(approve|load|loading|manzoor)\b")), "route_to_godown"),
        # a new customer ('party naya he, usman traders khanewal, 0301-...'): the order desk says how one is added
        R(_rx(r"\b(naya|nayi|naye|new)\b.{0,20}\b(party|customer|gahak|grahak|dukan|client)\b|\b(party|customer|gahak|client)\b.{0,15}\b(naya|nayi|new)\b"),
          "route_to_order"),
        # 3. business areas
        R(contains("sales report", "profit", "munafa", "margin", "valuation", "slow stock", "dead stock", "top customers", "collection report",
                   "revenue", "bikri", "sale", "sales", "ledger", "movement", "value", "nahi bik", "bik nahi", "not selling", "kamaya", "kamai", "kamaaya",
                   "best seller", "bikne wali", "bikta", "zyada bika", "منافع", "بکری", "سیل", "رپورٹ"), "route_to_report"),
        R(_rx(r"sab se (zyada|ziada|zaida) kaun (khareed|kharid|leta)|sab se (zyada|ziada|zaida)\b.{0,20}\b(kis ne|kisne|kaun|kon)\b"
              r"|sab se (zyada|ziada|zaida)\b.{0,30}\b(kis|kaun|kon)\b.{0,25}\b(ne|khareed\w*|kharid\w*|liya|lia)\b"), "route_to_report"),
        R(contains("supplier", "suppliers", "payable", "payables", "purchase", "bought", "khareed", "fauji", "engro", "arrived", "from",
                   "فوجی", "اینگرو", "خرید", "سپلائر"), "route_to_khareed"),
        R(_all(_rx(r"\bse\b"), _rx(r"\b(aaya|aayi|aaye|aai|aya|ayi|aa gaya|aa gayi)\b"), _none(_rx(_MONEY))), "route_to_khareed"),
        R(_rx(r"سے .*(آیا|آئی|آئے)"), "route_to_khareed"),
        R(contains("remind", "reminder", "overdue", "aging", "collections", "promise", "wasooli", "who owes", "broken", "yaad", "yaad dehani",
                   "wada", "waada", "toda", "de denge", "denge", "وصولی", "یاد دہانی", "وعدہ"), "route_to_wasooli"),
        R(_rx(r"\b(kis kis|kaun kaun|kon kon|sab se (zyada|ziada) (udhaar|udhar|baqi))\b|کس کس|کون کون|سب سے زیادہ (ادھار|باقی)"), "route_to_wasooli"),
        R(contains("deposit", "handed", "counted", "credit note", "refund", "digest", "close the day", "summary", "reconcile", "expense", "kharcha",
                   "diesel", "fuel", "salary", "cashbook", "cash book", "paid", "payment", "jazzcash", "easypaisa", "cheque", "received", "jama",
                   "bijli", "mazdoor", "petrol", "rent", "marammat", "chai", "chai pani", "wasool", "wusool", "vasool", "raqam",
                   "کیش بک", "خرچہ", "جمع", "ادائیگی", "چیک", "مرمت", "وصول"),
          "route_to_hisaab"),
        R(_rx(r"\b(aaj|today|din)\b.{0,12}\b(hisaab|hisab)\b"), "route_to_hisaab"),
        R(_rx(r"\b(mera|meri|my|aaj ka)\b.{0,10}\bplan\b"), "route_to_delivery"),
        # stock questions ('stocks kitne baqi hein', 'for all the products?', 'kitna maal pada hai') before the khata words
        R(_all(_rx(r"\b(stocks|inventory|stock count)\b"), _none(_rx(_MONEY))), "route_to_godown"),
        R(_all(_ALL_PRODUCTS, _none(_rx(r"\b(order|orders|bhej|bhejo|chahiye|sale|sales|bikri|otp|code|band|close|delivered|de diya|utar)\b|\b(stp|dsp|ord)-"))),
          "route_to_godown"),
        R(contains("stop", "stops", "delivered", "otp", "driver", "agla stop", "next stop", "kahan jana", "kahan kahan", "de diya", "utar diya",
                   "اسٹاپ", "ڈیلیور"), "route_to_delivery"),
        R(contains("stock", "allocate", "dispatch", "plan", "load", "godown", "restock", "write off", "damaged", "approve dsp", "vehicle", "route",
                   "transfer", "available", "اسٹاک", "سٹاک", "گودام"), "route_to_godown"),
        # today's run ('aaj ki gaari bana do', 'gaari tayyar karo'): dispatch planning
        R(_rx(r"\b(gaari|gari|gaadi|gadi|گاڑی)\b.{0,20}\b(bana|banao|bana do|tayyar|tayar|ready|nikal\w*|bhej\w*|plan|laga\w*)\b"), "route_to_godown"),
        # a customer's balance asked loosely ('New Kisan Dost wale kitne ke hain abhi tak'): their khata
        R(_all(_customer, _none(_product), _rx(r"\b(kitne|kitna|kitni|paise|paisay|scene|haal)\b"), _none(_rx(r"\d"))), "route_to_order"),
        R(_all(_rx(r"\b(kitni|kitna|kitne|how much|how many|کتنی|کتنا)\b"), _rx(r"\b(hai|he|pari|padi|bachi|baqi hai|پڑی|ہے)\b"),
               _none(_rx(r"\b(baqi|baaki|udhaar|udhar|dena|dene|owe|paise|hisaab|hisab|balance|khata)\b|باقی|ادہار|حساب|کہاتہ|بیلنس"))),
          "route_to_godown"),
        R(contains("order", "orders", "confirm", "cancel", "khata", "khaata", "account", "balance", "bhej", "chahiye", "want", "bags", "bori", "urea", "dap",
                   "baqi", "baaki", "udhaar", "udhar", "hisaab", "hisab", "outstanding", "owe", "owes", "pakka", "mansookh", "dikhao",
                   "بھیج", "بھیجو", "آرڈر", "کھاتہ", "حساب", "بیلنس", "باقی", "ادھار", "چاہیے", "منسوخ"), "route_to_order"),
        R(_rx(r"\b(dena|dene) hai|kitne paise"), "route_to_order"),
        # the same order again ('same as last time', 'wahi dobara'): the order desk repeats a customer's last order (or asks)
        R(contains("same as last time", "same again", "wahi dobara", "wohi dobara", "wahi phir se", "وہی دوبارہ"), "route_to_order"),
        R(_rx(r"\bko\s+\d|\d+\s*(bori|bag|bags|katte|katta|carton|peti|dozen)\b|\d+\s*(بوری|کٹے)"), "route_to_order"),
        R(_rx(r"^\W*(aur|and|or)\b.*\b(ka|ki|ke)\W*$"), "route_to_order"),                  # 'aur Haji Sons ka?'
        R(_rx(r"\bord-[a-z0-9]+"), "route_to_order"),                                      # 'ORD-... ka status kya hai'
        R(_rx(r"^\W*dsp-[a-z0-9]+\W*$"), "route_to_delivery"),
        R(contains("need", "needs", "chahiye", "mangta", "mangwana"), "route_to_order"),
        # a customer (even an ambiguous one: the order desk asks which), a product and a quantity, however it is worded
        # ('Punjab Seed Mart ka maal: gandum beej pandrah', 'Malik ka maal: makai aath')
        R(_all(lambda t: _customer(t) or _ambiguous(t), _product, _rx(r"\d")), "route_to_order"),
        # a quantity followed by a word, with no other cue ('New Kisan Dost 6 drip line'): the order desk reads it
        # and asks if it can't account for every number -- it never guesses
        R(_rx(r"(?<![\w-])\d+\s*(x\s*)?[a-z؀-ۿ]{2,}"), "route_to_order"),
        # 4. the rest: refusals of approval bypass, small talk, off-topic, and Urdu script nobody recognised
        R(_BYPASS, "route_to_help"), R(_GREET, "route_to_help"), R(_THANKS, "route_to_help"), R(_BYE, "route_to_help"),
        R(_ACK, "route_to_help"), R(_HELP, "route_to_help"), R(_OFFTOPIC, "route_to_help"), R(_URDU_SCRIPT, "route_to_help"),
    ], fallback_text=CLARIFY)


_MODEL_MANAGER_PROMPT = ("You are the Manager at a distribution business in Pakistan. Messages come in Urdu script, Roman Urdu or English, "
                         "often with customer, supplier and product names. Read the message and call exactly one route_to_* tool for the munshi "
                         "who handles it. Never do the work yourself. If it is small talk or not about the business, call no tool. "
                         "Customers buy from us; suppliers sell to us (a note may say which a name is -- trust it). A customer's account, khata or "
                         "balance, or money a customer paid us, is hisaab (or order for an order); an order for goods is order; what we owe or pay a "
                         "supplier, or goods a supplier delivered, is khareed; reminders, overdue lists and promises to pay are wasooli; totals, sales, "
                         "profit and rankings over a period are report. Money RECEIVED from a customer ('wusool hui', 'wasool ho gaye', 'raqam mil gayi', "
                         "'de gaya') is hisaab -- wasooli is only reminders, overdue lists and promises, never recording a payment.")


class _ModelRouter:
    """A real model's routing in ONE request: bind the route tools, read the first route it calls. (An agent loop
    would spend a second request just to acknowledge the route.) Same invoke/result shape as the agent."""

    def __init__(self, model: BaseChatModel) -> None:
        self.model = model.bind_tools(_ROUTES)
        self.routes = {t.name: t for t in _ROUTES}

    def invoke(self, inputs: dict, config: dict | None = None) -> dict:
        msg = self.model.invoke([SystemMessage(_MODEL_MANAGER_PROMPT), *inputs["messages"]], config=config)
        out = [*inputs["messages"], msg]
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc["name"] in self.routes:
                out.append(ToolMessage(self.routes[tc["name"]].invoke({}), tool_call_id=tc["id"] or tc["name"], name=tc["name"]))
                break
        return {"messages": out}


def build_manager(model: BaseChatModel | None = None, repo=None):
    # The stub can also send to the help desk; a real model gets the seven business routes and,
    # when it routes nowhere, the platform asks which area the message is about.
    if model is None:
        return create_agent(_stub(repo), tools=_STUB_ROUTES, state_schema=MunshiState,
                            system_prompt="You are the Manager at a distribution business. Read the message and call exactly one route_to_* tool for the munshi who handles it. Never do the work yourself.")
    return _ModelRouter(model)


def classify(manager, text: str, role: str = "clerk", config: dict | None = None, context: list | None = None) -> Specialist | None:
    result = manager.invoke({"messages": [*(context or []), HumanMessage(text)], "role": role}, config=config)
    for m in result["messages"]:
        if isinstance(m, ToolMessage) and isinstance(m.content, str) and m.content.startswith("ROUTE:"):
            return m.content.split(":", 1)[1]  # type: ignore[return-value]
    return None
