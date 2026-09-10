"""TenantHub: one file per business, approvals that survive a restart, demo lifecycle, migrations."""
import sqlite3

from munshi.domain.migrations import MIGRATIONS, V1, current_version, migrate
from munshi.domain.repository import MunshiRepository
from munshi.tenancy.hub import DEMO_BUSINESS_ID, TenantHub


def test_signup_creates_isolated_files(tmp_path):
    hub = TenantHub(str(tmp_path))
    a = hub.signup("A Traders", "Lahore", "Ali", "0300-1111111", "2580")
    b = hub.signup("B Traders", "Karachi", "Bilal", "0300-2222222", "4826", sample_data=True)
    assert (tmp_path / "tenants" / f"{a['business']['business_id']}.db").exists()
    assert (tmp_path / "tenants" / f"{b['business']['business_id']}.db").exists()
    pa, pb = hub.platform(a["business"]["business_id"]), hub.platform(b["business"]["business_id"])
    assert pa.repo.list_customers() == [] and len(pb.repo.list_customers()) == 10
    assert pa.repo.business_name == "A Traders" and pb.repo.business_name == "B Traders"
    assert pa.repo.default_warehouse_id() == "WH-MAIN"
    hub.close()


def test_pending_approval_survives_a_restart(tmp_path):
    hub = TenantHub(str(tmp_path)); hub.ensure_demo()
    p = hub.platform(DEMO_BUSINESS_ID)
    r = p.handle_message("t", "clerk", "Chaudhry Farms ko 20 urea bhej do", user="Bilal")
    aid = r.pending.approval_id
    n = len(p.repo.list_orders())
    hub.close()                                   # "server restart"
    hub2 = TenantHub(str(tmp_path)); hub2.ensure_demo()
    p2 = hub2.platform(DEMO_BUSINESS_ID)
    assert [x["approval_id"] for x in p2.list_pending()] == [aid]
    out = p2.resolve(aid, True, "clerk", user="Bilal")
    assert "ORD-" in out.text and len(p2.repo.list_orders()) == n + 1
    assert p2.list_pending() == []
    hub2.close()


def test_demo_is_idempotent_and_resettable(tmp_path):
    hub = TenantHub(str(tmp_path)); hub.ensure_demo(); hub.ensure_demo()
    assert len(hub.registry.list_businesses()) == 1
    p = hub.platform(DEMO_BUSINESS_ID)
    p.repo.record_expense("misc", 10, "x", "cash", "t", "test")
    hub.reset_demo()
    assert hub.platform(DEMO_BUSINESS_ID).repo.expenses_between("2000-01-01", "2999-01-01")[-1].note != "x"
    hub.close()


def test_platform_cache_evicts_oldest(tmp_path):
    hub = TenantHub(str(tmp_path), max_open=2)
    ids = [hub.signup(f"T{i}", "", f"O{i}", f"0300-000000{i}", "2580")["business"]["business_id"] for i in range(3)]
    for i in ids: hub.platform(i)
    assert len(hub._platforms) == 2 and ids[0] not in hub._platforms
    hub.close()


def test_migration_from_v1_file(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path); conn.executescript(V1)
    conn.execute("INSERT INTO customers VALUES ('C-001','Old Shop','0300','standard',1000,NULL,'ur-en')")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)"); conn.execute("INSERT INTO schema_version VALUES (1, 'x')"); conn.commit(); conn.close()
    repo = MunshiRepository(path)
    assert current_version(repo._conn) == MIGRATIONS[-1][0]
    c = repo.get_customer("C-001")
    assert c.active is True and c.credit_days == 30 and c.discount_pct == 0
    assert migrate(repo._conn) == []                  # idempotent
    repo.close()
