"""The minute tick (digest + outbox) and the channel adapter with a fake provider."""
from datetime import datetime

from munshi.channels import NullChannel, deliver_outbox
from munshi.channels.whatsapp import WhatsAppCloudChannel
from munshi.domain.seed import seeded_repository
from munshi.tenancy.hub import DEMO_BUSINESS_ID, TenantHub
from munshi.web.app import scheduler_tick


class FakeChannel:
    name = "fake"

    def __init__(self, fail_for=()):
        self.sent, self.fail_for = [], set(fail_for)

    def send(self, to, text):
        if to in self.fail_for: raise RuntimeError("provider down")
        self.sent.append((to, text)); return "wamid.1"


def test_digest_is_sent_once_per_day_at_the_business_time():
    hub = TenantHub(in_memory=True); hub.ensure_demo()
    repo = hub.platform(DEMO_BUSINESS_ID).repo
    repo.set_setting("digest_time", "20:00")
    sent = {}
    assert scheduler_tick(hub, sent, datetime(2026, 9, 9, 19, 59)) == []
    assert scheduler_tick(hub, sent, datetime(2026, 9, 9, 20, 0)) == [DEMO_BUSINESS_ID]
    assert scheduler_tick(hub, sent, datetime(2026, 9, 9, 20, 0)) == []          # not twice
    n = repo.notifications("owner")
    assert n[0]["kind"] == "digest" and "receivables" in n[0]["text"]
    assert repo.outbox("queued")[0]["to_phone"] == "0300-0000001" and repo.outbox("queued")[0]["ref"] == "digest"
    assert scheduler_tick(hub, sent, datetime(2026, 9, 10, 20, 0)) == [DEMO_BUSINESS_ID]   # next day again


def test_outbox_delivery_marks_sent_and_keeps_failures():
    repo = seeded_repository()
    repo.queue_message("whatsapp", "0300-1", "hello", "r1"); repo.queue_message("whatsapp", "0300-2", "hi", "r2")
    assert deliver_outbox(repo, NullChannel()) == {"sent": 0, "failed": 0, "queued": 2}
    ch = FakeChannel(fail_for={"0300-2"})
    r = deliver_outbox(repo, ch)
    assert r == {"sent": 1, "failed": 1, "queued": 0} and ch.sent == [("0300-1", "hello")]
    assert repo.outbox("sent")[0]["to_phone"] == "0300-1" and repo.outbox("failed")[0]["error"] == "provider down"


def test_whatsapp_number_formatting():
    assert WhatsAppCloudChannel.e164("0300-1234567") == "923001234567"
    assert WhatsAppCloudChannel.e164("+92 300 1234567") == "923001234567"


def test_template_payload_when_configured():
    ch = WhatsAppCloudChannel("t", "p", template="munshi_notice", template_lang="en")
    body = ch.payload("0300-1234567", "hello")
    assert body["type"] == "template" and body["template"]["name"] == "munshi_notice" and body["template"]["components"][0]["parameters"][0]["text"] == "hello"
    assert WhatsAppCloudChannel("t", "p").payload("0300-1234567", "hi")["type"] == "text"


def test_nightly_backup_writes_a_readable_copy(tmp_path, monkeypatch):
    import sqlite3

    from munshi.web.app import nightly_backup
    hub = TenantHub(str(tmp_path)); hub.ensure_demo()
    files = nightly_backup(hub)
    assert len(files) == 1 and files[0].startswith("demo-")
    conn = sqlite3.connect(str(tmp_path / "backups" / files[0]))
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 10
    monkeypatch.setenv("MUNSHI_BACKUP_TIME", "02:30"); sent = {}
    assert scheduler_tick(hub, sent, datetime(2026, 9, 10, 2, 30)) == [] and sent["__backup__"] == "2026-09-10"
    hub.close()


def test_webhook_channel_is_chosen_without_whatsapp(monkeypatch):
    from munshi.channels import WebhookChannel, build_channel
    monkeypatch.delenv("WHATSAPP_TOKEN", raising=False); monkeypatch.setenv("MESSAGE_WEBHOOK_URL", "https://example.invalid/hook")
    ch = build_channel()
    assert isinstance(ch, WebhookChannel) and ch.name == "webhook"
    monkeypatch.setenv("WHATSAPP_TOKEN", "t"); monkeypatch.setenv("WHATSAPP_PHONE_ID", "p")
    assert build_channel().name == "whatsapp"
