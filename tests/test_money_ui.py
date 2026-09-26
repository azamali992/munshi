"""Stream C (console + phone screens for payroll, finance and payment proofs): the static contract.

The browser run lives in tests/ui_money_check.py (it needs a server and Chromium). This file pins what can be checked
without one: the fixtures the screens were built against have the exact table shape of accounts.table(); every word
the new screens use exists in English and Urdu; the service worker ships the new phone module; and the one-time PIN
has no path into storage, the console or a second request.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from munshi.domain.accounts import TABLE_KINDS, col, table
from tests.fixtures import money_ui as fx

STATIC = Path(__file__).resolve().parent.parent / "src" / "munshi" / "web" / "static"
NEW_JS = ["office/employees.js", "office/payroll.js", "office/accounts.js", "office/finance.js", "office/close.js", "office/lib.js", "money_views.js"]
TABLE_KEYS = set(table("x", [col("a", "A")], []).keys())


def _tables(obj):
    """Every table payload inside a fixture response."""
    if isinstance(obj, dict):
        if {"columns", "rows"} <= obj.keys():
            yield obj
        for v in obj.values():
            yield from _tables(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _tables(v)


def _all_fixture_responses():
    for method, rx, handler in fx.routes():
        path = rx.strip("^$").replace("/?", "")
        path = re.sub(r"\(\\d\{4\}-\\d\{2\}\)", fx.PERIOD, path)
        path = re.sub(r"\((EMP|ADJ|PAY|PSL|ADV|CC)-\[[^)]*\)", lambda m: {"EMP": "EMP-BILAL001", "ADJ": "ADJ-9X3N5T2R", "PAY": "PAY-2026-000001",
                                                                        "PSL": "PSL-2026-000002", "ADV": "ADV-2026-000001", "CC": "CC-7Q2M4K1P"}[m.group(1)], path)
        path = re.sub(r"\(\[A-Z0-9-\]\+\)", "ACC-HBL", path).replace(r"(\d+)", "2")
        path = re.sub(r"\(([a-z/|-]+)\)", lambda m: m.group(1).split("|")[0], path)
        hit = fx.match(method, path)
        if not hit:
            continue
        h, m = hit
        for role in ("owner", "clerk"):
            status, body = h(m, {"kind": "eobi", "status": "active", "by": "product"}, {"payments": [], "fingerprint": fx.preview()["fingerprint"]}, role, {})
            yield f"{method} {path} as {role}", status, body


def test_every_fixture_table_has_the_exact_accounts_table_shape():
    seen = 0
    for name, _status, body in _all_fixture_responses():
        for tb in _tables(body):
            seen += 1
            assert set(tb) == TABLE_KEYS, (name, set(tb) ^ TABLE_KEYS)
            for c in tb["columns"]:
                assert c["kind"] in TABLE_KINDS, (name, c)
                assert c["align"] == ("right" if c["kind"] in ("money", "qty", "days", "pct") else "left"), (name, c)
            keys = {c["key"] for c in tb["columns"]}
            for r in tb["rows"]:
                assert set(r) <= keys | {"_em"}, (name, set(r) - keys)
    assert seen >= 25


def test_the_payroll_fixture_is_the_plans_worked_month():
    """Plan §10.2: the figures the screens were checked with are the worked September, to the paisa."""
    reg = fx.register()["totals"]
    assert reg["gross"] == 308690.80 and reg["total_deductions"] == 12256.92 and reg["net_pay"] == 296433.88 and reg["eobi_er"] == 7400.0
    tb = fx.trial_balance()
    assert tb["balanced"] and tb["table"]["totals"]["debit"] == tb["table"]["totals"]["credit"]


def test_a_clerk_is_never_given_a_pay_field_by_the_fixtures():
    """The fixtures mirror decision 2 (salaries are the owner's): the screens were checked against a clerk response
    with no pay key at all, so nothing client-side can have come to depend on one."""
    pay = {"pay_basis", "basic", "daily_rate", "pay_method", "payee_ref", "cash_allowed", "components"}
    for e in fx.employees("all", pay=False)["employees"]:
        assert not pay & set(e)
    assert all(r["line"] != "  Salaries" for r in fx.pnl(clerk=True)["table"]["rows"])
    assert "Staff payments (owner only)" in str(fx.account_book("ACC-HBL", redact=True))


def _i18n_blocks():
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    en, ur = src.split("\n  ur: {", 1)
    keys = lambda block: set(re.findall(r"""(?m)^\s*['"]?([\w.]+)['"]?\s*:""", block))  # noqa: E731
    return keys(en), keys(ur)


def _used_keys():
    used, prefixes = set(), set()
    for f in NEW_JS:
        s = (STATIC / f).read_text(encoding="utf-8")
        used |= {k for k in re.findall(r"\bt\('((?:pr|fi|cl|mv|st|adj|adjh|rh|role|prov|m|ev|rk|fa|ln|jk)\.[a-z_0-9]+)'", s) if not k.endswith("_")}
        prefixes |= set(re.findall(r"\bt\('((?:pr|fi|mv)\.[a-z_]*_)' \+", s))
    return used, prefixes


def test_every_new_word_exists_in_english_and_urdu():
    en, ur = _i18n_blocks()
    used, prefixes = _used_keys()
    assert len(used) > 400
    assert not sorted(used - en), "missing English"
    assert not sorted(used - ur), "missing Urdu"
    for p in prefixes:                                  # t('pr.s_' + step) and friends: at least one key each
        assert any(k.startswith(p) for k in en) and any(k.startswith(p) for k in ur), p
    ours = {k for k in en if re.match(r"(pr|fi|cl|mv|st|adj|adjh|rh|role|prov|m|ev|rk|fa|ln|jk)\.", k)}
    assert ours == {k for k in ur if re.match(r"(pr|fi|cl|mv|st|adj|adjh|rh|role|prov|m|ev|rk|fa|ln|jk)\.", k)}


def test_urdu_keeps_every_placeholder():
    src = (STATIC / "i18n.js").read_text(encoding="utf-8")
    en_block, ur_block = src.split("\n  ur: {", 1)
    pairs = lambda b: dict(re.findall(r"""^\s*'((?:pr|fi|cl|mv|st)\.[\w]+)': '((?:[^'\\]|\\.)*)',""", b, re.M))  # noqa: E731
    en, ur = pairs(en_block), pairs(ur_block)
    holes = lambda s: sorted(set(re.findall(r"\{(\w+)\}", s)))  # noqa: E731
    for k, v in en.items():
        assert holes(ur[k]) == holes(v), k


def test_service_worker_ships_the_money_module_and_a_new_shell():
    sw = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "'/static/money_views.js'" in sw
    assert "munshi-shell-v6'" not in sw and re.search(r"munshi-shell-v(\d+)", sw) and int(re.search(r"munshi-shell-v(\d+)", sw).group(1)) >= 7


@pytest.mark.parametrize("f", NEW_JS)
def test_no_new_screen_logs_or_stores_anything_a_pin_could_be_in(f):
    s = (STATIC / f).read_text(encoding="utf-8")
    assert not re.search(r"console\.\w+\(", s), f
    assert not re.search(r"sessionStorage\.\w+\(", s), f
    for m in re.finditer(r"(store\.set|localStorage\.setItem)\(([^)]*)\)", s):
        assert "pin" not in m.group(2).lower(), (f, m.group(0))


def test_a_generated_pin_only_reaches_the_one_time_dialog():
    """pin_once is read in exactly the places that hand it to pinOnce(); the WhatsApp share is a wa.me link built in
    the browser, and the dialog wipes it on close."""
    emp = (STATIC / "office" / "employees.js").read_text(encoding="utf-8")
    for line in [ln for ln in emp.splitlines() if "pin_once" in ln]:
        assert "pinOnce(" in line or line.strip().startswith(("/*", "//", "*")) or "!r.login?.pin_once" in line or "if (lg.pin_once)" in line, line
    lib = (STATIC / "office" / "lib.js").read_text(encoding="utf-8")
    body = lib[lib.index("export function pinOnce"):lib.index("// ---------------------------------------------------------------- the statutory boundary line")]
    assert "https://wa.me/" in body and "api(" not in body and "post(" not in body and "fetch(" not in body
    assert "pin = ''" in body                                 # wiped on close


def test_the_chat_hook_only_touches_the_chat_post_and_the_pin_answer():
    mv = (STATIC / "money_views.js").read_text(encoding="utf-8")
    hook = mv[mv.index("window.fetch = (input, init) =>"):mv.index("// ================================================================ payment proof composer")]
    assert "url === '/api/chat'" in hook and "armedIds = null" in hook
    assert "pin_change_required" in hook
    assert "localStorage" not in hook and "console" not in hook


def test_the_views_and_core_files_are_untouched_by_the_money_module():
    """money_views.js extends V/M from the outside; views.js keeps only Stream 0's import line for it."""
    views = (STATIC / "views.js").read_text(encoding="utf-8")
    assert views.count("money_views") == 1
    assert "payslips" not in (STATIC / "core.js").read_text(encoding="utf-8")
