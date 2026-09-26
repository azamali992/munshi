"""What the offline munshis say when they don't act: a refusal (can't, and why in a
phrase), a "didn't understand" that names the closest things it could have meant,
or ONE clarifying question about the most blocking missing field.

English for Roman/English messages; Urdu when the message was typed in Urdu script.
NATIVE-SPEAKER CHECK WANTED: the Urdu strings below were written carefully but not by
a native speaker. They are kept short and plain on purpose; please review the ones
marked (?) first -- they use trade loanwords (آرڈر، اسٹاک، او ٹی پی) whose usual
spelling varies between businesses."""
from __future__ import annotations

from munshi.llm.resolve import Resolution


class Ask(str):
    """A clarifying question that asks for ONE missing piece (`slot`): customer, supplier, amount, date, items,
    order, product, otp or stock_kind (a purchase or a correction?). `candidates` are the [{"id", "name"}] it
    offered, best first as shown. The platform remembers an Ask as the thread's open question, so the next
    message can simply answer it ("haji sons", "80000", "doosra"). A plain str with two attributes: anything
    that concatenates it just gets the text."""

    slot: str
    candidates: list[dict]

    def __new__(cls, text: str, slot: str, candidates: list[dict] | None = None):
        s = super().__new__(cls, text)
        s.slot, s.candidates = slot, list(candidates or [])
        return s


# the slot each question key asks for
SLOT_OF = {"which_customer": "customer", "no_customer": "customer", "which_supplier": "supplier", "no_supplier": "supplier",
           "how_much": "amount", "how_much_from": "amount", "which_date": "date", "by_when": "date", "no_items": "items",
           "which_product": "product", "which_otp": "otp", "stock_kind": "stock_kind", "which_order": "order"}

UR = {
    # refusals
    "refuse_delete": "میں کوئی ریکارڈ مٹا نہیں سکتا۔ منشی میں کچھ ڈیلیٹ نہیں ہوتا؛ غلط اندراج اس کی آئی ڈی سے ریورس ہوتا ہے، اور ریورس مالک منظور کرتا ہے۔",
    "refuse_secret": "میں پن، پاس ورڈ یا کوڈ نہیں بتا سکتا۔ ڈیلیوری کا کوڈ صرف گاہک کے پاس ہوتا ہے۔",   # (?)
    "refuse_bypass": "میں منظوری کے بغیر کچھ نہیں کر سکتا۔ ہر کارڈ کو صحیح شخص خود منظور یا رد کرتا ہے۔",
    "refuse_edit": "میں چیٹ سے گاہک یا مال کی تفصیل نہیں بدل سکتا۔ یہ سیٹ اپ میں مالک بدلتا ہے۔",
    # small talk
    "greet": "وعلیکم السلام! آرڈر، اسٹاک، کھاتہ، ادائیگی یا رپورٹ — بتائیں کیا کرنا ہے۔",   # (?)
    "thanks": "شکریہ! کچھ اور ہو تو بتائیں۔",
    "ack": "ٹھیک ہے۔",
    "bye": "اللہ حافظ۔",
    "offtopic": "یہ میرے کام سے باہر ہے۔ میں آرڈر، گودام، ڈیلیوری، کھاتہ، خریداری، وصولی اور رپورٹ میں مدد کرتا ہوں۔",
    "didnt": "معاف کیجیے، بات سمجھ نہیں آئی۔ ایسے لکھیں: «چوہدری فارمز کو 20 یوریا بھیج دو» یا «یوریا کا اسٹاک کتنا ہے»۔",   # (?)
    # clarifying questions
    "which_customer": "کون سا گاہک؟ {options}",
    "no_customer": "یہ کس گاہک کے لیے ہے؟ مثال: «چوہدری فارمز کو 20 یوریا»۔",
    "which_supplier": "کون سا سپلائر؟ {options}",
    "no_supplier": "کس سپلائر کا؟ نام لکھیں۔",
    "no_items": "کون سا مال اور کتنا؟ مثال: «20 یوریا اور 5 ڈی اے پی»۔",
    "unknown_item": "«{what}» ہمارے مال کی فہرست میں نہیں ملا۔ صحیح نام لکھیں۔",
    "two_customers": "ایک پیغام میں دو گاہک ہیں ({a} اور {b})۔ ہر گاہک کا آرڈر الگ بھیجیں۔",
    "fix_qty": "مقدار واضح نہیں: {why} پورا آرڈر صحیح مقدار کے ساتھ دوبارہ لکھیں۔",   # (?) {why} is still English
    "how_much": "کتنی رقم؟",
    "bounce": "باؤنس چیک ادائیگی نہیں ہوتا۔ اصل رسید کو اس کی آئی ڈی سے ریورس کریں، مثلاً «RCP-… چیک باؤنس، ریورس کرو» — ریورس مالک منظور کرتا ہے۔",   # (?)
}

EN = {
    "refuse_delete": "I can't delete or wipe records -- nothing in Munshi is ever deleted. A wrong entry is reversed by its ID (e.g. 'reverse RCP-...'), and the owner approves reversals.",
    "refuse_secret": "I can't share PINs, passwords or delivery codes. The delivery code is only with the customer -- ask them for it at the door.",
    "refuse_bypass": "I can't act without approval, and I can't approve anything myself -- every card is approved or rejected by the right person in Approvals.",
    "refuse_edit": "I can't change customer or product details from chat -- the owner does that in Setup.",
    "greet": "Wa alaikum assalam! Tell me an order, a stock or khata question, a payment, or ask for a report.",
    "thanks": "Shukriya! Anything else?",
    "ack": "Theek hai.",
    "bye": "Allah Hafiz.",
    "offtopic": "That's outside what I do. I can help with orders, the godown, deliveries, khata and payments, purchases, collections and reports.",
    "didnt": "Sorry, I didn't understand that. Did you mean {closest}? For example: 'Chaudhry Farms ko 20 urea bhej do' or 'urea ka stock kitna hai'.",
    "which_customer": "Which customer -- {options}?",
    "no_customer": "Which customer is this for? e.g. 'Chaudhry Farms ko 20 urea aur 5 dap'.",
    "which_supplier": "Which supplier -- {options}?",
    "no_supplier": "Which supplier? Say their name, e.g. 'received 100 urea from Fauji at 3600'.",
    "no_items": "Which items, and how many? e.g. 'Chaudhry Farms ko 20 urea aur 5 dap'.",
    "unknown_item": "I couldn't find '{what}' in the catalogue, so I haven't made the order. Which product is it?",
    "two_customers": "That's two customers ({a} and {b}). Send one order per customer so each gets its own card.",
    "fix_qty": "I haven't made a card: {why} Please send the order again with the exact quantities.",
    "how_much": "How much was it? Please send the amount.",
    "bounce": ("A bounced cheque isn't a payment, so I haven't recorded anything. Reverse the original receipt by its ID "
               "(e.g. 'RCP-... cheque bounced, reverse it'); the owner approves reversals."),
    # a real model's proposal that the message doesn't back up (agents/guard.py): nothing is carded, one question
    "confirm_items": "I read the items as {items} -- nothing was done yet. Please send the order again exactly as it should be.",
    "confirm_amount": "I read the amount as {amount} -- nothing was done yet. Please send it again with the exact amount.",
    "which_method": "How was it paid -- cash, bank, JazzCash, Easypaisa or cheque?",
    "which_date": "By when? Please give the date, e.g. '2 October' or 'jumma tak'.",
    "which_product": "Which product? Please name it, e.g. 'urea' or 'DAP'.",
    "which_godown": "Which godown? Please name it.",
    "which_ref": "Which {what}? Say it the way you know it, e.g. {example}; nothing was done yet.",
    "which_otp": "To close the stop I need the delivery code the customer has, e.g. 'Chaudhry Farms pe sab de diya, cash 50000, code 1234'.",
    "not_backed": "I couldn't match that to what you wrote ({why}), so nothing was done. Please send it again with the details.",
}

UR |= {
    "which_method": "ادائیگی کیسے ہوئی — نقد، بینک، جاز کیش، ایزی پیسہ یا چیک؟",   # (?)
    "which_date": "کب تک؟ تاریخ لکھیں۔",
    "which_product": "کون سا مال؟ نام لکھیں۔",
    "which_godown": "کون سا گودام؟",
}

# follow-ups, whole-business answers and help (Roman Urdu in RU; Urdu script in UR)
EN |= {
    "how_much_from": "How much did {name} pay? Send just the amount, e.g. '50000'.",
    "by_when": "By when will {name} pay? e.g. '25 october' or 'jumma tak'.",
    "which_order": "No order was named, so nothing was done. Which order should I {verb}?{where}",
    "stock_kind": ("{qty} {product} at {godown} -- did this come from a supplier (which one, and at what price?) or is it a stock correction? "
                   "Reply e.g. 'Fauji se, {price} rate' or 'correction'."),
    "stock_in_what": "Which product and how many came in, and at which godown? e.g. '100 urea Multan godown mein aaye'.",
    "from_chat": "for {name} (from our conversation)",
    "sku": ("An SKU is just the short code for a product in your catalogue (e.g. UREA-50 = Urea 50kg). You never need to type it: "
            "write the product's name and I find it. Things you can ask: 'aaj ka stock', 'udhaar list', 'Haji Sons ka khata', "
            "'Haji Sons ne 50000 diye', 'aaj kis kis ne payment ki', 'Chaudhry Farms ko 20 urea bhej do'."),
    "help_more": " For example: 'aaj ka stock', 'udhaar list', 'Haji Sons ka khata', 'aaj kis kis ne payment ki'.",
}
RU = {
    "how_much_from": "{name} ne kitne paise diye? Sirf raqam likhein, maslan '50000'.",
    "by_when": "{name} kab tak dega? Maslan '25 october' ya 'jumma tak'.",
    "stock_kind": ("{qty} {product} {godown} mein -- ye kisi supplier se aaya hai (kaun sa, kis rate par?) ya stock ki correction hai? "
                   "Jawab dein, maslan 'Fauji se, {price} rate' ya 'correction'."),
    "stock_in_what": "Kaun sa maal, kitna, aur kis godown mein aaya? Maslan '100 urea Multan godown mein aaye'.",
    "sku": ("SKU sirf maal ka chhota code hai (maslan UREA-50 = Urea 50kg). Aap ko ye likhna nahi parta: maal ka naam likhein, main dhoond leta hoon. "
            "Aap ye pooch sakte hain: 'aaj ka stock', 'udhaar list', 'Haji Sons ka khata', 'Haji Sons ne 50000 diye', 'aaj kis kis ne payment ki', "
            "'Chaudhry Farms ko 20 urea bhej do'."),
}
UR |= {
    "how_much_from": "{name} نے کتنی رقم دی؟ صرف رقم لکھیں، مثلاً «50000»۔",
    "by_when": "{name} کب تک دے گا؟ مثلاً «25 اکتوبر» یا «جمعہ تک»۔",   # (?)
    "stock_kind": "{qty} {product} ({godown}) — یہ کسی سپلائر سے آیا ہے (کون سا، کس ریٹ پر؟) یا اسٹاک کی درستی ہے؟ مثلاً «فوجی سے، {price} ریٹ» یا «correction»۔ (supplier / correction)",   # (?)
    "stock_in_what": "کون سا مال، کتنا، اور کس گودام میں آیا؟",
    "sku": ("SKU مال کا مختصر کوڈ ہے (مثلاً UREA-50 = یوریا 50 کلو)۔ آپ کو یہ لکھنے کی ضرورت نہیں: مال کا نام لکھیں۔ "
            "آپ یہ پوچھ سکتے ہیں: «آج کا اسٹاک»، «ادھار لسٹ»، «حاجی سنز کا کھاتہ»، «آج کس کس نے ادائیگی کی»۔"),   # (?)
    "help_more": " مثلاً: «آج کا اسٹاک»، «ادھار لسٹ»، «حاجی سنز کا کھاتہ»۔",
}

HELP_BY_ROLE = {
    "owner": "I can take orders, check stock and khata, record payments and expenses, receive purchases, pay suppliers, chase collections and give reports.",
    "clerk": "I can take orders, check stock and khata, allocate and plan dispatch, record payments and expenses, receive purchases and chase collections.",
    "salesman": "I can book orders (the office confirms them), show a customer's khata and check stock, and log promises to pay.",
    "driver": "Ask 'aaj kahan kahan jana hai' for today's stops, and close one with the customer's code: 'Chaudhry Farms pe sab de diya, cash 50000, code 1234'.",
}


def t(key: str, urdu: bool, roman: bool = False, candidates: list[dict] | None = None, **kw) -> str:
    """The reply for `key`: Urdu script for an Urdu-script message, Roman Urdu where one is written and the message
    was Roman Urdu, else English. A question that asks for one missing piece comes back as an Ask."""
    table = UR if urdu and key in UR else RU if roman and key in RU else EN
    if "why" in kw:                                   # end the reason with exactly one mark
        w = str(kw["why"]).rstrip(" .")
        kw["why"] = w if w.endswith("?") else w + "."
    text = table[key].format(**kw)
    return Ask(text, SLOT_OF[key], candidates) if key in SLOT_OF else text


# What each munshi's "did you mean" names, with one example the user can copy (EN/Roman Urdu share the example).
_CLOSEST = {
    "order": ("an order or a customer's khata", "ek order ya kisi customer ka khata", "کوئی آرڈر یا کسی گاہک کا کھاتہ", "Haji Sons ko 10 urea"),
    "godown": ("stock or a dispatch plan", "stock ya dispatch plan", "اسٹاک یا ڈسپیچ پلان", "urea ka stock kitna hai"),
    "delivery": ("today's stops or closing a delivery", "aaj ke stops ya delivery band karna", "آج کے اسٹاپ یا ڈیلیوری", "aaj kahan kahan jana hai"),
    "hisaab": ("a payment, an expense or the cashbook", "payment, kharcha ya cashbook", "ادائیگی، خرچہ یا کیش بک", "Rana Brothers ne 20000 cash diye"),
    "khareed": ("a supplier: goods received or what we owe", "supplier: maal aaya ya kitna dena hai", "سپلائر: مال آیا یا کتنا دینا ہے", "Fauji ko kitna dena hai"),
    "wasooli": ("collections: who owes, reminders, promises", "wasooli: kis ke paise baqi, reminder, wada", "وصولی: کس کے پیسے باقی، یاد دہانی، وعدہ", "udhaar list"),
    "report": ("a report: sales, profit, collections", "report: sale, munafa, wasooli", "رپورٹ: سیل، منافع، وصولی", "is mahine ka munafa"),
}


def didnt(specialists: list, urdu: bool = False, roman: bool = False) -> str:
    """'Sorry, I didn't understand that -- did you mean X or Y? e.g. ...': the one or two closest things the message
    could have meant (the munshis the engines routed it to), never a guess at the answer."""
    picks = [s for s in dict.fromkeys(specialists) if s in _CLOSEST][:2] or ["order", "godown"]
    col = 2 if urdu else 1 if roman else 0
    what = (" یا " if urdu else " ya " if roman else " or ").join(_CLOSEST[s][col] for s in picks)
    eg = " / ".join(f"«{_CLOSEST[s][3]}»" if urdu else f"'{_CLOSEST[s][3]}'" for s in picks)
    if urdu:
        return f"معاف کیجیے، بات سمجھ نہیں آئی۔ کیا آپ کا مطلب {what} ہے؟ مثلاً {eg}۔"
    if roman:
        return f"Maaf kijiye, baat samajh nahi aayi. Kya aap ka matlab {what} hai? Maslan {eg}."
    return f"Sorry, I didn't understand that. Did you mean {what}? For example {eg}."


def _ordered(res: Resolution) -> list:
    """The options as a question lists them: by ID, or -- when the resolver ranked them for the asking user (the
    customers they order for most, llm.resolve._rank) -- in that order. Ranking only orders; it never picks."""
    return list(res.candidates)[:3] if getattr(res, "ranked", False) else sorted(res.candidates, key=lambda c: c.id)[:3]


def options(res: Resolution, urdu: bool = False) -> str:
    """'Chaudhry Farms or Chaudhry Traders' -- names as stored in master data, at most three. No record codes: a person
    answers with a name or 'pehla' / 'doosra' (the open-question memory keeps the ids)."""
    names = [c.name for c in _ordered(res)]
    word, comma = (" یا ", "، ") if urdu else (" or ", ", ")
    return word.join(names) if len(names) <= 2 else comma.join(names[:-1]) + word + names[-1]


def _cands(res: Resolution) -> list[dict]:
    """The candidates in the order the question lists them ('pehla' = the first shown)."""
    return [{"id": c.id, "name": c.name} for c in _ordered(res)]


def ask_customer(res: Resolution, urdu: bool = False) -> str:
    if res.status == "ambiguous":
        return t("which_customer", urdu, candidates=_cands(res), options=options(res, urdu))
    return t("no_customer", urdu)


def ask_supplier(res: Resolution, urdu: bool = False) -> str:
    if res.status == "ambiguous":
        return t("which_supplier", urdu, candidates=_cands(res), options=options(res, urdu))
    return t("no_supplier", urdu)


def ask_order(op, urdu: bool = False) -> str:
    """One question, most blocking first: which customer, unknown product, two customers, quantities."""
    if op.customer.status == "ambiguous":
        return ask_customer(op.customer, urdu)
    if op.unknown:
        return t("unknown_item", urdu, what=op.unknown[0])
    two = next((p for p in op.problems if p.startswith("two customers")), None)
    if two and op.customer.other is not None:
        return t("two_customers", urdu, a=op.customer.name or op.customer.candidates[0].name, b=op.customer.other.name)
    if op.problems:
        return t("fix_qty", urdu, why=op.problems[0])
    if op.customer.status != "ok":
        return ask_customer(op.customer, urdu)
    return t("no_items", urdu)
