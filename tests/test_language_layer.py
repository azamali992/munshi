"""Unit tests for the deterministic language layer (llm/text.py, numbers.py, resolve.py,
parse.py): normalisation, spoken numbers, entity resolution with its confidence rule, and
the order/close/amount extractors. No platform, no model -- pure functions over seed data."""
from __future__ import annotations

from datetime import date

import pytest

from munshi.domain.models import Customer
from munshi.domain.seed import seeded_repository
from munshi.llm import parse as P
from munshi.llm.numbers import normalize_numbers
from munshi.llm.text import fold, is_urdu, phonetic, sounds_like


@pytest.fixture(scope="module")
def repo():
    r = seeded_repository()
    r.upsert_customer(Customer("C-011", "Chaudhry Traders", "0300-1111011", "standard", 300_000, "R-VEHARI"))
    r.upsert_customer(Customer("C-012", "Malik Seeds", "0300-1111012", "standard", 300_000, "R-VEHARI"))
    return r


# ---------------------------------------------------------------- normalisation
@pytest.mark.parametrize("a,b", [
    ("يوريا", "یوریا"),                   # Arabic yeh -> Urdu yeh
    ("كھاتہ", "کھاتہ"),                   # Arabic kaf -> Urdu kaf
    ("چوهدري", "چوہدری"),                 # heh / heh goal / yeh variants
    ("کھاتا", "کہاتا"),                   # do-chashmi heh folds for matching
    ("۲۰", "20"), ("٢٠", "20"),           # Urdu-Indic and Arabic-Indic digits
    ("ـیوریاـ", "یوریا"),                 # tatweel
])
def test_fold_unifies_script_variants(a, b):
    assert fold(a) == fold(b)


def test_is_urdu():
    assert is_urdu("چوہدری فارمز کو 20 یوریا") and not is_urdu("Chaudhry Farms ko 20 urea")


@pytest.mark.parametrize("w", ["Chaudhry", "Chaudhary", "Chowdhry", "Choudhry", "Chaudry", "chaudhri"])
def test_one_sound_key_for_chaudhry(w):
    assert sounds_like(fold(w), "chaudhry"), phonetic(w)


def test_sound_key_is_not_a_substring_match():
    assert not sounds_like("purana", "rana") and not sounds_like("malik", "malika" + "x")


# ---------------------------------------------------------------- numbers
@pytest.mark.parametrize("text,want", [
    ("dedh sau", "150"), ("dhai sau", "250"), ("sawa sau", "125"), ("paune do sau", "175"), ("saade teen sau", "350"),
    ("50 hazar", "50000"), ("dedh lakh", "150000"), ("1.5 lakh", "150000"), ("5 lakh", "500000"), ("ek lakh bees hazar", "120000"),
    ("1,000", "1000"), ("1,08,250", "108250"), ("25,000", "25000"), ("1k", "1000"), ("2.5k", "2500"),
    ("bees bori", "20 bori"), ("pachas bag", "50 bag"), ("ڈیڑھ سو", "150"), ("پچاس ہزار", "50000"), ("۲۰", "20"),
])
def test_spoken_numbers(text, want):
    assert normalize_numbers(fold(text)) == want


@pytest.mark.parametrize("text", ["bhej do", "kar do", "de do", "بھیج دو", "20 25"])
def test_verbs_and_adjacent_numbers_are_left_alone(text):
    assert normalize_numbers(fold(text)) == fold(text)


def test_do_is_two_before_a_unit_or_multiplier():
    assert normalize_numbers(fold("do bori")) == "2 bori" and normalize_numbers(fold("do sau")) == "200"


# ---------------------------------------------------------------- resolution
@pytest.mark.parametrize("text,want", [
    ("Chaudhry Farms ko 20 urea", "C-002"), ("Chaudhry Traders ko 10 urea", "C-011"), ("chaudhary farm ko 20 urea", "C-002"),
    ("Chaudhry Frams ko 20 urea", "C-002"), ("Malik Agro ko 5 dap", "C-001"), ("Malik Seeds ko 5 gandum", "C-012"),
    ("purana balance batao Bhatti Kisan ka", "C-007"), ("C-004 ko 5 urea", "C-004"), ("0300-1111009 wale ko 5 urea", "C-009"),
    ("ملک ایگرو اسٹور کے لیے 15 بوری یوریا", "C-001"), ("حاجی سنز کا کھاتہ", "C-009"),
])
def test_confident_customer(repo, text, want):
    assert P.parse_customer(text, repo) == want


@pytest.mark.parametrize("text,ids", [
    ("Chaudhry ko 20 urea", {"C-002", "C-011"}), ("Malik sahab ko 10 dap", {"C-001", "C-012"}), ("kisan ko 5 urea", {"C-007", "C-010"}),
])
def test_ambiguous_customer_returns_candidates_not_a_guess(repo, text, ids):
    r = P.customer_resolution(text, repo)
    assert r.status == "ambiguous" and r.id is None and {c.id for c in r.candidates} == ids


def test_a_name_made_of_command_words_never_matches(repo):
    from munshi.llm.resolve import resolve
    ents = [("C-900", "Bhej Do Traders", "0300-0000000"), ("C-002", "Chaudhry Farms", "0300-1111002")]
    assert resolve(fold("Chaudhry Farms ko 20 urea bhej do"), ents, "C").id == "C-002"
    assert resolve(fold("20 urea bhej do"), ents, "C").status == "none"


def test_a_product_word_is_not_a_second_customer(repo):
    op = P.analyse_order("bhatti kisan ko 10 makai seed", repo)
    assert op.ready and op.customer.id == "C-007" and op.items == [{"sku": "SEED-MAIZE", "qty": 10}]


def test_supplier_resolution(repo):
    assert P.parse_supplier("Fauji ko 5 lakh bank transfer kar do", repo) == "S-001"
    assert P.parse_supplier("Ali Akbar walon se 20 cyper aaya", repo) == "S-003"
    assert P.parse_supplier("20 urea Multan se Vehari shift kar do", repo) is None     # a city in a supplier's name is not the supplier


# ---------------------------------------------------------------- order lines
@pytest.mark.parametrize("text,items", [
    ("Chaudhry Farms ko 20 urea aur 5 dap bhej do", [("DAP-50", 5), ("UREA-50", 20)]),
    ("Chaudhry Farms 20 yuria 5 dap send kr do", [("DAP-50", 5), ("UREA-50", 20)]),
    ("Chaudhry Farms ko 20 you rea aur 5 d a p bhej do", [("DAP-50", 5), ("UREA-50", 20)]),
    ("urea 30 bori Chaudhry Farms", [("UREA-50", 30)]),
    ("Please send 30 bags of urea to Al-Barakah Traders", [("UREA-50", 30)]),
    ("Chaudhry Farms ko 20 bori urea 50kg wali", [("UREA-50", 20)]),
    ("C-002 ko 20 UREA-50", [("UREA-50", 20)]),
    ("Haji Sons ko 5 dozen imida", [("IMIDA-250", 60)]),
    ("Chaudhry Farms ko 20 urea bhej do lekin dap nahi", [("UREA-50", 20)]),
])
def test_order_lines(repo, text, items):
    op = P.analyse_order(text, repo)
    assert op.ready, op.problems
    assert sorted((i["sku"], i["qty"]) for i in op.items) == items


@pytest.mark.parametrize("text,why", [
    ("Chaudhry Farms ko 20 urea aur 10 glyphosate", "catalogue"),
    ("Chaudhry Farms ko 5 carton imida", "sold by"),
    ("Chaudhry Farms ko 2 ton urea", "sold by"),
    ("Chaudhry Farms ko 20-25 bag urea", "range"),
    ("Chaudhry Farms ko -20 urea", "negative"),
    ("Chaudhry Farms ko 1.5 bori urea", "whole"),
    ("Chaudhry Farms ko 999999 urea", "far more"),
    ("Chaudhry Farms ko 20 urea aur 30 urea", "twice"),
    ("Chaudhry Farms ko 50 urea nahi 30 urea", "nahi"),
    ("Chaudhry Farms ko 20 urea, 5 se zyada dap nahi", "limit"),
    ("Rana Brothers ko 10 urea 20 % discount pe", "discount"),
    ("Rana Brothers ko 10 urea 20% discount pe bhej do", "discount"),
    ("Chaudhry Farms ko 20 urea aur Rana Brothers ko 5 dap", "two customers"),
])
def test_order_problems_mean_ask(repo, text, why):
    op = P.analyse_order(text, repo)
    assert not op.ready and any(why in p for p in op.problems), op.problems


def test_ids_otps_and_phones_never_become_quantities_or_products(repo):
    assert P.parse_items("close STP-89DAC339 delivered 8 npk otp 3519", repo) == [{"sku": "NPK-25", "qty": 8}]
    assert P.parse_items("ORD-5080A0AC ke liye 3 zinc, call 0300-1234567", repo) == [{"sku": "ZINC-10", "qty": 3}]


# ---------------------------------------------------------------- delivery close
@pytest.mark.parametrize("text,delivered,returned", [
    ("close STP-1 delivered 8 npk aur 5 sop, wapis 2 npk, cash 60000, otp 3519", [("NPK-25", 8), ("SOP-50", 5)], [("NPK-25", 2)]),
    ("close STP-1 delivered 8 npk 5 sop returned 2 npk cash 60000 otp 3519", [("NPK-25", 8), ("SOP-50", 5)], [("NPK-25", 2)]),
    ("close STP-1 npk 8 sop 5 return npk 2 cash 60000 otp 3519", [("NPK-25", 8), ("SOP-50", 5)], [("NPK-25", 2)]),
    ("close STP-1: delivered 15 urea 5 dap, 5 urea wapis, cash 40000 otp 3519", [("DAP-50", 5), ("UREA-50", 15)], [("UREA-50", 5)]),
])
def test_close_lines(repo, text, delivered, returned):
    c = P.analyse_close(text, repo)
    assert not c.problems, c.problems
    assert sorted((i["sku"], i["qty"]) for i in c.delivered) == delivered
    assert sorted((i["sku"], i["qty"]) for i in c.returned) == returned
    assert c.otp == "3519"


@pytest.mark.parametrize("text,cash", [("STP-1 pe sab de diya, 50 hazar cash liya, otp 8355", 50000),
                                       ("STP-1 ڈیلیور ہو گیا، پچاس ہزار نقد، او ٹی پی 4378", 50000),
                                       ("close STP-1 delivered all cash 1,08,250 otp 1111", 108250)])
def test_close_all_with_spoken_cash(repo, text, cash):
    c = P.analyse_close(text, repo)
    assert not c.problems and c.delivered is None and c.cash == cash


def test_close_without_otp_asks(repo):
    assert "the customer's OTP code" in P.analyse_close("STP-1 deliver ho gaya otp nahi mila", repo).problems


# ---------------------------------------------------------------- money and dates
@pytest.mark.parametrize("text,amount", [("Chaudhry Farms ne 50 hazar jazzcash kiye", 50000), ("Rana Brothers se dedh lakh ka cheque aaya", 150000),
                                         ("Haji Sons ne Rs. 25,000 cash jama karwaye", 25000), ("ملک ایگرو اسٹور نے ۵۰۰۰۰ روپے بینک میں جمع کروائے", 50000),
                                         ("DSP-35BEF02E driver ne 45000 jama karwaye", 45000)])
def test_amounts(text, amount):
    assert P.amount_in(text).amount == amount


def test_amount_problems():
    assert P.amount_in("Chaudhry Farms ne payment kar di").amount is None
    assert P.amount_in("20000 ya 25000 diye").amount is None
    assert P.amount_in("5000 aur 7000 diye").amount is None


@pytest.mark.parametrize("text,method", [("bank transfer", "bank"), ("jazz cash", "jazzcash"), ("cheque se", "cheque"), ("naqd", "cash")])
def test_method(text, method):
    assert P.method_in(text) == method


def test_bounce_detected():
    assert P.is_bounce("Malik Agro Store ka 20000 ka cheque bounce ho gaya") and not P.is_bounce("Malik Agro ne 20000 cheque diya")


def test_dates():
    thu = date(2026, 9, 24)
    assert P.date_in("jumma tak 50 hazar", thu) == "2026-09-25"
    assert P.date_in("Haji Sons ne 50000 ka wada kiya hai 2 october tak", thu) == "2026-10-02"
    assert P.date_in("kal tak", thu) == "2026-09-25"
    assert P.date_in("by 2026-10-02", thu) == "2026-10-02"
    assert P.date_in("abhi", thu) is None
