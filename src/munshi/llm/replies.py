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
}

HELP_BY_ROLE = {
    "owner": "I can take orders, check stock and khata, record payments and expenses, receive purchases, pay suppliers, chase collections and give reports.",
    "clerk": "I can take orders, check stock and khata, allocate and plan dispatch, record payments and expenses, receive purchases and chase collections.",
    "salesman": "I can book orders (the office confirms them), show a customer's khata and check stock, and log promises to pay.",
    "driver": "Ask 'mera agla stop kaun sa hai' for today's stops, and close a stop with 'close STP-... delivered all, cash 50000, otp 1234'.",
}


def t(key: str, urdu: bool, **kw) -> str:
    table = UR if urdu and key in UR else EN
    if "why" in kw:                                   # end the reason with exactly one mark
        w = str(kw["why"]).rstrip(" .")
        kw["why"] = w if w.endswith("?") else w + "."
    return table[key].format(**kw)


def options(res: Resolution, urdu: bool = False) -> str:
    """'Chaudhry Farms (C-002) or Chaudhry Traders (C-011)' -- names as stored in master data, with IDs, at most three."""
    names = [f"{c.name} ({c.id})" for c in sorted(res.candidates, key=lambda c: c.id)[:3]]
    word, comma = (" یا ", "، ") if urdu else (" or ", ", ")
    return word.join(names) if len(names) <= 2 else comma.join(names[:-1]) + word + names[-1]


def ask_customer(res: Resolution, urdu: bool = False) -> str:
    if res.status == "ambiguous":
        return t("which_customer", urdu, options=options(res, urdu))
    return t("no_customer", urdu)


def ask_supplier(res: Resolution, urdu: bool = False) -> str:
    if res.status == "ambiguous":
        return t("which_supplier", urdu, options=options(res, urdu))
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
