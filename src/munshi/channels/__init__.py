"""Channels: how Munshi reaches customers and the owner. Every message is
written to the business's outbox first; a channel then delivers it (or
not). With no channel configured, messages stay in the outbox where the
app shows them with a one-tap WhatsApp link — the clerk sends them by hand."""
from munshi.channels.whatsapp import Channel, NullChannel, WebhookChannel, WhatsAppCloudChannel, build_channel, deliver_outbox

__all__ = ["Channel", "NullChannel", "WebhookChannel", "WhatsAppCloudChannel", "build_channel", "deliver_outbox"]
