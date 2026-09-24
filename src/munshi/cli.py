"""Operator command line: python -m munshi.cli <command>

  serve                          run the API + app (uvicorn) on $PORT (default 8000)
  businesses                     list businesses in the registry
  create-business NAME OWNER PHONE PIN [--city C] [--sample]
  add-user BUSINESS_ID NAME PHONE ROLE PIN
  reset-pin PHONE PIN            reset a user's PIN (every business they belong to)
  backup BUSINESS_ID [--out DIR] consistent copy of a business's SQLite file
  restore BUSINESS_ID FILE       replace a business's file with a backup (the current file is kept as .before-restore)
  migrate                        apply pending schema migrations to every business file
  demo-reset                     wipe and reseed the Sultan Traders demo
  deliver-outbox                 push queued customer messages through the configured channel
  check                          startup sanity: secret, data dir, channel, model

Data directory: $MUNSHI_DATA_DIR (default ./data).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from munshi.domain.models import business_today


def _hub():
    from munshi.tenancy.hub import TenantHub
    return TenantHub(os.environ.get("MUNSHI_DATA_DIR", "data"))


def cmd_serve(a):
    import uvicorn
    # X-Forwarded-For / -Proto are only honoured from the reverse proxies named in
    # MUNSHI_TRUSTED_PROXY_IPS (comma-separated IPs/CIDRs, e.g. "127.0.0.1" for a
    # Caddy on the same host). Unset: nobody is trusted and the socket peer is used,
    # so a client cannot pick its own address to dodge the sign-in rate limit.
    trusted = ",".join(p.strip() for p in os.environ.get("MUNSHI_TRUSTED_PROXY_IPS", "").split(",") if p.strip())
    uvicorn.run("munshi.web.app:app", host=a.host, port=int(os.environ.get("PORT", a.port)), workers=1,
                proxy_headers=bool(trusted), forwarded_allow_ips=trusted or "127.0.0.1")


def cmd_businesses(a):
    hub = _hub()
    for b in hub.registry.list_businesses():
        users = hub.registry.list_users(b["business_id"], include_inactive=False)
        print(f"{b['business_id']:12} {b['name']:30} {b['city']:14} plan={b['plan']:6} users={len(users)} active={bool(b['active'])}")


def cmd_create_business(a):
    hub = _hub()
    r = hub.signup(a.name, a.city or "", a.owner, a.phone, a.pin, sample_data=a.sample)
    print(f"created {r['business']['business_id']} ({a.name}); owner {a.owner} can sign in with {r['user']['phone']}")


def cmd_add_user(a):
    hub = _hub()
    u = hub.registry.create_user(a.business_id, a.name, a.phone, a.role, a.pin)
    print(f"added {u['user_id']} {u['name']} ({u['role']}) to {a.business_id}")


def cmd_reset_pin(a):
    hub = _hub()
    from munshi.auth.registry import normalize_phone
    phone = normalize_phone(a.phone); n = 0
    for b in hub.registry.list_businesses():
        for u in hub.registry.list_users(b["business_id"]):
            if u["phone"] == phone:
                hub.registry.set_pin(u["user_id"], a.pin); n += 1
                print(f"reset PIN for {u['name']} at {b['name']} (sessions ended)")
    if not n: print("no user with that phone"); sys.exit(1)


def cmd_backup(a):
    hub = _hub(); hub.registry.get_business(a.business_id)
    out = Path(a.out or (hub.data_dir / "backups")); out.mkdir(parents=True, exist_ok=True)
    target = out / f"{a.business_id}-{business_today().isoformat()}.db"
    if target.exists(): target.unlink()
    repo = hub.platform(a.business_id).repo
    with repo._lock:
        repo._conn.execute("VACUUM INTO ?", (str(target),))
    print(f"backup written: {target} ({target.stat().st_size // 1024} KB)")


def cmd_restore(a):
    import shutil
    import sqlite3
    hub = _hub(); hub.registry.get_business(a.business_id)
    src = Path(a.file)
    if not src.exists(): raise ValueError(f"no such file: {src}")
    sqlite3.connect(str(src)).execute("SELECT COUNT(*) FROM customers")      # it must at least be a Munshi file
    hub.close()
    target = Path(hub.db_path(a.business_id))
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(target) + suffix)
        if f.exists(): f.rename(str(target) + suffix + ".before-restore") if not suffix else f.unlink()
    shutil.copyfile(src, target)
    print(f"restored {a.business_id} from {src.name}; previous file kept as {target.name}.before-restore. Restart the server.")


def cmd_migrate(a):
    hub = _hub()
    from munshi.domain.migrations import current_version
    for b in hub.registry.list_businesses():
        repo = hub.platform(b["business_id"]).repo       # opening applies migrations
        print(f"{b['business_id']:12} schema v{current_version(repo._conn)}")


def cmd_demo_reset(a):
    hub = _hub(); hub.ensure_demo(); hub.reset_demo(); print("demo reseeded")


def cmd_deliver_outbox(a):
    hub = _hub()
    for b in hub.registry.list_businesses():
        print(b["business_id"], hub.platform(b["business_id"]).deliver_messages())


def cmd_check(a):
    prod = os.environ.get("MUNSHI_ENV", "development") == "production"
    secret = os.environ.get("MUNSHI_SECRET", "")
    print(f"env:        {'production' if prod else 'development'}")
    print(f"secret:     {'set' if secret and secret != 'change-me' else 'MISSING (required in production)'}")
    print(f"data dir:   {os.environ.get('MUNSHI_DATA_DIR', 'data')} ({'exists' if Path(os.environ.get('MUNSHI_DATA_DIR', 'data')).exists() else 'will be created'})")
    print(f"model:      {os.environ.get('LLM_PROVIDER', 'stub')}{' (GROQ_API_KEY set)' if os.environ.get('GROQ_API_KEY') else ''}")
    from munshi.channels import build_channel
    ch = build_channel().name
    print(f"channel:    {ch}{' (messages queue with wa.me links; set WHATSAPP_* or MESSAGE_WEBHOOK_URL to send automatically)' if ch == 'outbox' else ''}")
    print(f"sign-up:    {os.environ.get('MUNSHI_SIGNUP', 'open' if not prod else 'closed')}")
    print(f"demo:       {os.environ.get('MUNSHI_DEMO', '1')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="munshi", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", default="8000"); s.set_defaults(fn=cmd_serve)
    sub.add_parser("businesses").set_defaults(fn=cmd_businesses)
    s = sub.add_parser("create-business"); s.add_argument("name"); s.add_argument("owner"); s.add_argument("phone"); s.add_argument("pin"); s.add_argument("--city"); s.add_argument("--sample", action="store_true"); s.set_defaults(fn=cmd_create_business)
    s = sub.add_parser("add-user"); s.add_argument("business_id"); s.add_argument("name"); s.add_argument("phone"); s.add_argument("role", choices=["owner", "clerk", "salesman", "driver"]); s.add_argument("pin"); s.set_defaults(fn=cmd_add_user)
    s = sub.add_parser("reset-pin"); s.add_argument("phone"); s.add_argument("pin"); s.set_defaults(fn=cmd_reset_pin)
    s = sub.add_parser("backup"); s.add_argument("business_id"); s.add_argument("--out"); s.set_defaults(fn=cmd_backup)
    s = sub.add_parser("restore"); s.add_argument("business_id"); s.add_argument("file"); s.set_defaults(fn=cmd_restore)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("demo-reset").set_defaults(fn=cmd_demo_reset)
    sub.add_parser("deliver-outbox").set_defaults(fn=cmd_deliver_outbox)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ValueError, Exception) as e:      # noqa: BLE001 - operator-facing: one clear line, exit 1
        if isinstance(e, KeyboardInterrupt): raise
        print(f"error: {e}", file=sys.stderr); sys.exit(1)


if __name__ == "__main__":
    main()
