"""WhatsApp Business Cloud API adapter.

Configure with (see .env.example):
    WHATSAPP_TOKEN        permanent system-user token from Meta Business Manager
    WHATSAPP_PHONE_ID     the sender phone-number id from the WhatsApp Cloud API dashboard
    WHATSAPP_TEMPLATE     optional: an approved template name with one {{1}} body parameter.
                          Meta only delivers business-initiated messages outside a 24-hour
                          customer window as templates, so set this for reminders/codes.
Without token + phone id, `build_channel()` returns NullChannel and every message
stays queued in the outbox, where the app renders it as a wa.me link instead."""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Protocol

log = logging.getLogger("munshi.channels")


class Channel(Protocol):
    name: str
    def send(self, to_phone: str, text: str) -> str: ...     # returns provider message id


class NullChannel:
    name = "outbox"

    def send(self, to_phone: str, text: str) -> str:
        raise RuntimeError("no channel configured")


class WhatsAppCloudChannel:
    name = "whatsapp"

    def __init__(self, token: str, phone_id: str, api_version: str = "v21.0", template: str = "", template_lang: str = "en") -> None:
        self.token, self.phone_id, self.api_version, self.template, self.template_lang = token, phone_id, api_version, template, template_lang

    @staticmethod
    def e164(phone: str) -> str:
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits.startswith("0") and len(digits) == 11: digits = "92" + digits[1:]
        return digits

    def payload(self, to_phone: str, text: str) -> dict:
        if self.template:
            return {"messaging_product": "whatsapp", "to": self.e164(to_phone), "type": "template",
                    "template": {"name": self.template, "language": {"code": self.template_lang},
                                 "components": [{"type": "body", "parameters": [{"type": "text", "text": text}]}]}}
        return {"messaging_product": "whatsapp", "to": self.e164(to_phone), "type": "text", "text": {"body": text}}

    def send(self, to_phone: str, text: str) -> str:
        body = json.dumps(self.payload(to_phone, text)).encode()
        req = urllib.request.Request(f"https://graph.facebook.com/{self.api_version}/{self.phone_id}/messages", data=body,
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get("messages", [{}])[0].get("id", "")


class WebhookChannel:
    """POST {"to": "...", "text": "..."} to any URL: an n8n flow, an SMS-gateway app on a
    spare Android phone, a Twilio wrapper. Set MESSAGE_WEBHOOK_URL (+ optional MESSAGE_WEBHOOK_TOKEN)."""
    name = "webhook"

    def __init__(self, url: str, token: str = "") -> None:
        self.url, self.token = url, token

    def send(self, to_phone: str, text: str) -> str:
        body = json.dumps({"to": to_phone, "text": text}).encode()
        headers = {"Content-Type": "application/json"}
        if self.token: headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return str(resp.status)


def build_channel() -> Channel:
    token, phone_id = os.environ.get("WHATSAPP_TOKEN"), os.environ.get("WHATSAPP_PHONE_ID")
    if token and phone_id:
        return WhatsAppCloudChannel(token, phone_id, template=os.environ.get("WHATSAPP_TEMPLATE", ""), template_lang=os.environ.get("WHATSAPP_TEMPLATE_LANG", "en"))
    if os.environ.get("MESSAGE_WEBHOOK_URL"):
        return WebhookChannel(os.environ["MESSAGE_WEBHOOK_URL"], os.environ.get("MESSAGE_WEBHOOK_TOKEN", ""))
    return NullChannel()


def deliver_outbox(repo, channel: Channel, limit: int = 50) -> dict:
    """Push queued outbox messages through the channel. Safe to call often."""
    sent = failed = 0
    if isinstance(channel, NullChannel):
        return {"sent": 0, "failed": 0, "queued": len(repo.outbox("queued"))}
    for m in reversed(repo.outbox("queued", limit)):
        try:
            channel.send(m["to_phone"], m["text"]); repo.mark_message(m["msg_id"], "sent"); sent += 1
        except Exception as e:      # keep the message; the next pass retries
            log.warning("outbox send failed for %s: %s", m["msg_id"], e)
            repo.mark_message(m["msg_id"], "failed", str(e)[:200]); failed += 1
    return {"sent": sent, "failed": failed, "queued": len(repo.outbox("queued"))}
