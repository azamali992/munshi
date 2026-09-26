"""Stream 0 seams for payroll, company finance and payment proofs: the shared contracts every stream builds on.

  * migrations: V8/V9/V10 are registered only once written, and only contiguously, so no file is ever stamped with a
    version whose tables it lacks; :memory: and a copy of a V7 file migrate cleanly to the newest registered version
    (these tests cover the REAL steps automatically as each stream sets its STEP -- extend, don't fork);
  * every new chat tool has its tier (safety/risk.py) AND is in the registry-independent safety audit
    (eval/run_eval.py ALWAYS_GATED + ACTION_TOOL) under the audit action domain/accounts.py freezes;
  * the permission matrix encodes the owner's decision that salaries are the owner's alone;
  * the office console's nav loads the new screens; the phone app loads money_views.js."""
import ast
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from eval.run_eval import ACTION_TOOL, AGENT_ACTORS, ALWAYS_GATED, safety_violations
from munshi.auth.principal import PERMISSIONS, ROLES, Principal, roles_with
from munshi.domain import accounts, migrations, migrations_attachments, migrations_finance, migrations_payroll
from munshi.domain.models import Customer, Product
from munshi.domain.repository import MunshiRepository
from munshi.domain.repository.numbering import DOC_SERIES, RESERVED_PREFIXES, next_doc_no
from munshi.safety.risk import MONEY_TOOL_TIERS, RISK_REGISTRY, RiskTier, approver_for, risk_of, tools_requiring_approval

SRC = Path(__file__).resolve().parent.parent / "src" / "munshi"
EXTENSIONS = (migrations_finance, migrations_payroll, migrations_attachments)
GATED_MONEY = {t for t, tier in MONEY_TOOL_TIERS.items() if tier in (RiskTier.LOW_RISK, RiskTier.HIGH_RISK)}


# ============================================================================ migrations
def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _fake_step(table: str):
    def step(conn):
        conn.execute(f"CREATE TABLE {table} (x INTEGER)")
    return step


def _register(monkeypatch, **steps):
    """Pretend the named streams have written their steps (finance=..., payroll=..., attachments=...)."""
    for name, step in steps.items():
        monkeypatch.setattr({"finance": migrations_finance, "payroll": migrations_payroll, "attachments": migrations_attachments}[name], "STEP", step)
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.CORE_MIGRATIONS + migrations.extension_steps())


def _v7_file(path: Path) -> None:
    r = MunshiRepository(str(path))
    assert migrations.current_version(r._conn) >= 7
    r.upsert_customer(Customer("C-1", "Bhatti Kisan Store", "0300-1111001", "standard", 100_000))
    r.upsert_product(Product("UREA-50", "Urea 50kg", 4500, [], 100, cost_price=4000))
    r.record_expense("fuel", 1500, "diesel", "cash", "owner", actor="owner", approved_by="owner")
    r.close()


def test_registered_migrations_are_contiguous_and_the_real_list_is_what_migrate_uses():
    versions = [v for v, _ in migrations.MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert migrations.MIGRATIONS[:7] == migrations.CORE_MIGRATIONS
    for module, version in zip(EXTENSIONS, (8, 9, 10), strict=True):
        assert module.VERSION == version
        registered = any(v == version for v, _ in migrations.MIGRATIONS)
        assert registered == (module.STEP is not None and all(m.STEP is not None for m in EXTENSIONS[:version - 8])), module.__name__


def test_memory_database_migrates_to_the_newest_registered_version():
    r = MunshiRepository(":memory:")
    assert migrations.current_version(r._conn) == migrations.MIGRATIONS[-1][0]
    assert r._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert r._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert migrations.migrate(r._conn) == []                    # idempotent


def test_a_v7_file_copy_migrates_cleanly_and_keeps_its_data(tmp_path):
    src, copy = tmp_path / "v7.db", tmp_path / "copy.db"
    _v7_file(src)
    shutil.copy(src, copy)
    r = MunshiRepository(str(copy))
    assert migrations.current_version(r._conn) == migrations.MIGRATIONS[-1][0]
    assert r.get_customer("C-1").name == "Bhatti Kisan Store" and r.get_product("UREA-50").unit_price == 4500
    assert r._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert r._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    r.close()
    assert migrations.migrate(MunshiRepository(str(copy))._conn) == []


def test_an_unwritten_step_is_never_registered_so_no_file_is_stranded(tmp_path, monkeypatch):
    # V9 and V10 written, V8 not: nothing past V7 may apply -- otherwise the file would be stamped 9 or 10 and V8,
    # when it lands, would be skipped forever (migrate() skips every version <= the highest applied).
    _register(monkeypatch, finance=None, payroll=_fake_step("fake_v9"), attachments=_fake_step("fake_v10"))
    assert migrations.extension_steps() == []
    path = tmp_path / "v7.db"; _v7_file(path)
    r = MunshiRepository(str(path))
    assert migrations.current_version(r._conn) == 7 and "fake_v9" not in _tables(r._conn)
    r.close()
    # V8 lands: 8, 9 and 10 apply, in order, on the same file
    _register(monkeypatch, finance=_fake_step("fake_v8"))
    r = MunshiRepository(str(path))
    assert migrations.current_version(r._conn) == 10 and {"fake_v8", "fake_v9", "fake_v10"} <= _tables(r._conn)
    assert r.get_customer("C-1").name == "Bhatti Kisan Store"


def test_a_failing_step_rolls_back_whole_and_leaves_the_version(tmp_path, monkeypatch):
    def broken(conn):
        conn.execute("CREATE TABLE half_done (x)")
        raise sqlite3.OperationalError("boom")
    _register(monkeypatch, finance=broken, payroll=None, attachments=None)
    path = tmp_path / "v7.db"
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.CORE_MIGRATIONS)
    _v7_file(path)
    _register(monkeypatch, finance=broken)
    with pytest.raises(sqlite3.OperationalError):
        MunshiRepository(str(path))
    conn = sqlite3.connect(str(path))
    assert migrations.current_version(conn) == 7 and "half_done" not in _tables(conn)


def test_migrate_refuses_a_gap_in_the_step_numbers(monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.CORE_MIGRATIONS + [(9, _fake_step("fake_v9"))])
    with pytest.raises(migrations.MigrationError):
        migrations.migrate(sqlite3.connect(":memory:", isolation_level=None))


def test_run_step_unstamped_applies_without_recording_a_version():
    r = MunshiRepository(":memory:")
    before = migrations.current_version(r._conn)
    migrations.run_step_unstamped(r._conn, _fake_step("dev_only"))
    assert "dev_only" in _tables(r._conn) and migrations.current_version(r._conn) == before


def test_extension_modules_do_not_import_migrations_at_module_level():
    # migrations.py imports them while it is itself being imported: a top-level import back would see a half-built
    # module (and, imported in the other order, STEP could be read before it is set).
    for module in EXTENSIONS:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                assert node.module != "munshi.domain.migrations", module.__name__
            if isinstance(node, ast.Import):
                assert all(a.name != "munshi.domain.migrations" for a in node.names), module.__name__


# ============================================================================ tools, tiers, safety audit
PLAN_TIERS = {   # plan §6 (+ Stream 0 additions), spot-checked independently of risk.py
    "payroll_register": RiskTier.READ_ONLY, "my_payslips": RiskTier.READ_ONLY, "balance_sheet": RiskTier.READ_ONLY,
    "record_attendance": RiskTier.LOW_RISK, "transfer_between_accounts": RiskTier.LOW_RISK, "count_cash": RiskTier.LOW_RISK,
    "add_employee": RiskTier.HIGH_RISK, "approve_payroll_run": RiskTier.HIGH_RISK, "pay_salaries": RiskTier.HIGH_RISK,
    "give_staff_advance": RiskTier.HIGH_RISK, "add_money_account": RiskTier.HIGH_RISK, "record_drawing": RiskTier.HIGH_RISK,
    "close_period": RiskTier.HIGH_RISK, "reopen_period": RiskTier.HIGH_RISK, "set_payroll_settings": RiskTier.HIGH_RISK,
}


def test_every_new_tool_has_its_tier_and_resolves_through_risk_of():
    # (Stream D promoted MONEY_TOOL_TIERS into RISK_REGISTRY as SEAMS §3 directs, so the two now overlap by design: what must hold
    # is that the promotion re-tiered nothing -- every shared name has the same tier in both)
    assert all(RISK_REGISTRY[t] == tier for t, tier in MONEY_TOOL_TIERS.items() if t in RISK_REGISTRY), "a new tool must not re-tier an existing one"
    for tool, tier in PLAN_TIERS.items():
        assert risk_of(tool) == tier, tool
    for tool, tier in MONEY_TOOL_TIERS.items():
        assert risk_of(tool) == tier
        if tier == RiskTier.HIGH_RISK:
            assert approver_for(tool) == "owner", tool
    # not interrupted until Stream D promotes them into RISK_REGISTRY with their card builders (test_approval_cards)
    assert not GATED_MONEY & set(tools_requiring_approval()) or GATED_MONEY & set(RISK_REGISTRY)
    with pytest.raises(ValueError):
        risk_of("pay_everyone_twice")                       # still fails closed


def test_every_gated_new_tool_is_in_the_safety_audit_under_its_frozen_action():
    assert GATED_MONEY <= ALWAYS_GATED
    assert set(accounts.AUDIT_ACTION) == GATED_MONEY
    for tool, action in accounts.AUDIT_ACTION.items():
        assert ACTION_TOOL[action] == tool
    assert not accounts.SECONDARY_ACTIONS & set(ACTION_TOOL), "a secondary audit row must never consume an approval"
    assert set(accounts.MONEY_AGENT_ACTORS) <= AGENT_ACTORS


def test_the_safety_audit_catches_an_unapproved_payroll_write():
    class P: repo = MunshiRepository(":memory:")
    P.repo.audit("tankhwa_munshi", "pay_salaries", "payroll_run", "PAY-2026-000001", {"n": 3})
    assert [v["tool"] for v in safety_violations(P)] == ["pay_salaries"]
    P.repo.audit("owner", "approval_granted", "approval", "APR-1", {"tool": "approve_payroll_run"})
    P.repo.audit("tankhwa_munshi", "approve_payroll_run", "payroll_run", "PAY-2026-000001", {})
    P.repo.audit("tankhwa_munshi", "attachment_linked", "attachment", "ATT-1", {})     # secondary: never counted
    assert [v["tool"] for v in safety_violations(P)] == ["pay_salaries"]


def test_tool_visibility_covers_every_new_tool_with_a_real_permission():
    assert set(accounts.TOOL_PERMISSION) == set(MONEY_TOOL_TIERS)
    assert set(accounts.TOOL_PERMISSION.values()) <= set(PERMISSIONS)


# ============================================================================ permissions (owner decision 2)
def test_salaries_are_visible_to_the_owner_only():
    for perm in ("payroll:read", "payroll:write", "payroll:approve", "finance:read", "finance:write"):
        assert roles_with(perm) == ["owner"], perm
    clerk = Principal("u", "b", "clerk", "Munshi")
    assert clerk.can("attendance:write") and clerk.can("employees:read") and not clerk.can("payroll:read")
    assert set(roles_with("payroll:self")) == set(ROLES)            # everyone sees their own slips (resolved by user_id)
    # every chat tool that shows or changes pay is owner-only, except the employee's own slips and attendance
    pay_tools = {t for t, p in accounts.TOOL_PERMISSION.items() if p.startswith("payroll:") and p != "payroll:self"}
    assert {"payroll_register", "payslip", "staff_advances_report", "payroll_preview", "set_pay_structure", "pay_salaries"} <= pay_tools
    for t in pay_tools:
        assert roles_with(accounts.TOOL_PERMISSION[t]) == ["owner"], t
    assert accounts.TOOL_PERMISSION["record_attendance"] == "attendance:write" and accounts.TOOL_PERMISSION["my_payslips"] == "payroll:self"


def test_attachment_permissions_and_principal_pin_flag():
    assert set(roles_with("attachments:write")) == set(ROLES)       # a driver photographs a cheque
    assert roles_with("attachments:read") == ["owner", "clerk"]      # others: their own uploads, checked in the route
    p = Principal("u", "b", "driver", "D")
    assert p.must_change_pin is False and Principal("u", "b", "driver", "D", "", True).must_change_pin is True


# ============================================================================ numbering, table shape, postings, profile
def test_new_document_series_are_frozen_unique_and_avoid_existing_id_prefixes():
    for series, prefix in accounts.MONEY_SERIES.items():
        assert DOC_SERIES[series] == prefix
    prefixes = [p for p in DOC_SERIES.values() if p]
    assert len(prefixes) == len(set(prefixes))
    assert "STP" not in RESERVED_PREFIXES and "TRF" not in RESERVED_PREFIXES      # stop ids and stock-transfer refs
    r = MunshiRepository(":memory:")
    with r._tx() as c:
        n1, n2 = next_doc_no(r, c, "statutory", "2026-09-26T10:00:00+00:00"), next_doc_no(r, c, "transfer", "2026-09-26T10:00:00+00:00")
    assert (n1, n2) == ("STY-2026-000001", "XFR-2026-000001")


def test_table_shape_is_exactly_the_chat_renderers():
    from munshi.llm.answers import make_table
    rows = [{"name": "Bilal", "net": 43190.8, "days": 26, "extra": "dropped"}, {"name": "Total staff", "net": 1.0, "days": 0, "_em": True}]
    chat = make_table("en", "salary register", "September", [("name", "Name", "text"), ("net", "Net", "money"), ("days", "Days", "days")],
                      rows, totals={"net": 43191.8}, note="rates checked 2026-09-26")
    ours = accounts.table("salary register", [accounts.col("name", "Name"), accounts.col("net", "Net", "money"), accounts.col("days", "Days", "days")],
                          rows, totals={"net": 43191.8}, note="rates checked 2026-09-26", lead="September", max_rows=300)
    assert ours == chat
    with pytest.raises(ValueError):
        accounts.col("x", "X", "int")


def test_postings_balance_and_refuse_nonsense():
    p = accounts.signed("2026-09-30", accounts.SALARIES, accounts.SALARIES_PAYABLE, 4_000_000, source="payroll_run", source_id="PAY-2026-000001")
    assert sum(x.net_paisa for x in p) == 0 and p[0].code == "6100" and p[0].debit_paisa == 4_000_000
    rev = accounts.signed("2026-10-01", accounts.MONEY, accounts.TRADE_RECEIVABLES, -500, debit_money_account_id="CASH")
    assert rev[1].code == "1000" and rev[1].credit_paisa == 500 and rev[1].money_account_id == "CASH"   # a reversal swaps sides
    for bad in (dict(debit_paisa=1, credit_paisa=1), dict(debit_paisa=0), dict(debit_paisa=1.5)):
        with pytest.raises(ValueError):
            accounts.Posting("2026-09-30", "6100", **bad)
    with pytest.raises(ValueError):
        accounts.Posting("2026-09-30", "1000", debit_paisa=1)            # a money line names its account
    assert accounts.Posting("2026-09-30", accounts.expense_code("fuel"), debit_paisa=1).code == "6000:fuel"
    assert not set(accounts.SYSTEM_EXPENSE_CATEGORIES) & {"salary", "fuel", "rent", "misc", "cash_shortage"}


def test_repository_skeletons_compose_without_shadowing_and_default_safely():
    from munshi.domain.repository.attachments import AttachmentsMixin
    from munshi.domain.repository.finance import FinanceMixin
    from munshi.domain.repository.memory import MemoryMixin
    from munshi.domain.repository.office import OfficeMixin
    from munshi.domain.repository.payroll import PayrollMixin
    old = set(dir(OfficeMixin)) | set(dir(MemoryMixin))
    for mixin in (FinanceMixin, PayrollMixin, AttachmentsMixin):
        assert issubclass(MunshiRepository, mixin)
        own = {k for k in vars(mixin) if not k.startswith("__")} - {"_todo"}
        assert not own & old, f"{mixin.__name__} would shadow {own & old}"
    r = MunshiRepository(":memory:")
    assert r.assert_period_open("2026-09-26") is None
    assert (r.resolve_account("cash"), r.resolve_account("bank"), r.resolve_account("bank", "ACC-HBL")) == ("CASH", None, "ACC-HBL")
    assert r.payroll_profile() == accounts.PROFILE_PLC_2026 == accounts.DEFAULT_PAYROLL_PROFILE      # owner decision 1
    r.set_setting("payroll_profile", "legacy_1969"); assert r.payroll_profile() == "legacy_1969"
    assert r._payroll_postings("2026-09-01", "2026-09-30") == [] and set(r.payroll_liabilities_paisa().values()) == {0}
    for call in (lambda: r.payroll_register(period="2026-09"), lambda: r.balance_sheet(), lambda: r.store_attachment(b"x", "a.jpg", "image/jpeg", "u", "a")):
        with pytest.raises(NotImplementedError):
            call()


def test_plc_limits_are_the_ones_the_owner_chose():
    lim = accounts.PROFILE_LIMITS[accounts.DEFAULT_PAYROLL_PROFILE]
    assert (lim["advance_cap_min_wages"], lim["advance_instalment_cap_bp"], lim["fine_cap_bp"], lim["cashless_only"]) == (3, 2000, 300, True)
    assert "cash" not in accounts.CASHLESS_METHODS and set(accounts.CASHLESS_METHODS) < set(accounts.MONEY_METHODS)


# ============================================================================ web: routers, office nav, phone app
@pytest.fixture
def client():
    from munshi.web.app import build_app
    return TestClient(build_app(in_memory=True, demo=True, scheduler=False))


def test_new_routers_are_mounted_and_the_office_nav_loads(client):
    assert "payroll.router, finance.router, attachments.router" in (SRC / "web" / "app.py").read_text(encoding="utf-8")
    app_js = client.get("/static/office/app.js")
    assert app_js.status_code == 200
    for screen in ("employees", "payroll", "accounts", "finance", "close"):
        assert f"import {screen} from './{screen}.js';" in app_js.text
        mod = client.get(f"/static/office/{screen}.js")
        assert mod.status_code == 200 and "export default async function" in mod.text
    assert "SCREENS = { products, inventory, clients, suppliers, godowns, employees, payroll, accounts, finance, close, import: importExport }" in app_js.text
    assert client.get("/static/money_views.js").status_code == 200
    assert "import('/static/money_views.js')" in client.get("/static/views.js").text


def test_me_lists_the_new_permissions_by_role(client):
    def me(phone, pin):
        tok = client.post("/api/session", json={"phone": phone, "pin": pin}).json()["token"]
        return set(client.get("/api/me", headers={"X-Session": tok}).json()["permissions"])
    owner, clerk, driver = me("0300-0000001", "1111"), me("0300-0000002", "2222"), me("0300-0000003", "3333")
    assert {"payroll:read", "payroll:approve", "finance:read", "books:read", "attachments:read"} <= owner
    assert {"attendance:write", "employees:read", "books:read", "payroll:self"} <= clerk and not clerk & {"payroll:read", "finance:read"}
    assert driver & {"payroll:self", "attachments:write"} == {"payroll:self", "attachments:write"} and "attachments:read" not in driver
