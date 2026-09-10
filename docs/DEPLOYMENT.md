# Deployment

Three ways to run Munshi, from a PC in the office to a hosted server for many businesses.

## 1. On a PC in the office (LAN)

The simplest real deployment: one machine that stays on, phones on the same Wi-Fi.

```bash
git clone https://github.com/azamali992/munshi && cd munshi
cp .env.example .env                 # MUNSHI_DEMO=0 for a real business
docker compose up -d                 # or: pip install -r requirements.txt && PYTHONPATH=src python3 -m munshi.cli serve
```

Find the PC's address (`ipconfig` / `ip a`), open `http://192.168.x.x:8000` on each phone,
*Add to Home Screen* — or install the APK and enter that address. Create the business from
the sign-in screen, or from the command line:

```bash
PYTHONPATH=src python3 -m munshi.cli create-business "Sultan Traders" "Sultan Ahmed" 0300-1234567 2580 --city Multan
PYTHONPATH=src python3 -m munshi.cli add-user B-XXXXXXXX "Bilal" 0300-2345678 clerk 4826
```

Data lives in `./data`. Back it up with `cli backup <business_id>` (or the owner's
*Download backup* in Settings) and copy the file somewhere else.

Without HTTPS, browsers will not register the service worker on a LAN address, so the
app still needs the network to open (the driver's offline queue works either way once the
page is open). For offline-first behaviour, put a TLS proxy in front (below) or use the APK.

## 2. One VPS, HTTPS, several businesses

A 1-vCPU VPS handles a few hundred phones. Caddy gives you TLS with two lines.

```
# Caddyfile
munshi.example.pk {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
# .env
MUNSHI_ENV=production
MUNSHI_SECRET=<python3 -c "import secrets;print(secrets.token_hex(32))">
MUNSHI_DEMO=0
MUNSHI_SIGNUP=1                      # or 0, and create businesses from the CLI
MUNSHI_PUBLIC_URL=https://munshi.example.pk
LLM_PROVIDER=groq
GROQ_API_KEY=...
WHATSAPP_TOKEN=... WHATSAPP_PHONE_ID=...
```

```bash
docker compose up -d && caddy run
python3 -m munshi.cli check          # prints what the server thinks its configuration is
```

Production mode enforces a real secret, closes sign-up unless `MUNSHI_SIGNUP=1`, enables
HSTS and hides the API explorer. The scheduler thread delivers the outbox every minute and
sends each owner their digest at the business's `digest_time`.

Backups: the scheduler writes every business to `data/backups` at `MUNSHI_BACKUP_TIME` (02:30) and keeps 14 days; copy that directory off the machine nightly (or run `python3 -m munshi.cli backup <id>` yourself, or copy the
whole `data/` directory while the app is running — WAL mode makes the files consistent
enough for a restore, `VACUUM INTO` makes them exact).

Upgrades: `git pull && docker compose up -d --build`. Schema migrations apply on the first
open of each business file; `cli migrate` applies them all up front.

## 3. Air-gapped / no LLM

Leave `LLM_PROVIDER=stub`. Every screen, form and report works; chat understands the
catalogue-aware keyword grammar (`Chaudhry Farms ko 20 urea bhej do`, `remind everyone
over 30 days`, `received 100 urea from Fauji at 3600`). No data leaves the machine.

## Environment variables

See `.env.example` — every variable is documented there, with where to get each key.

## Health and observability

- `GET /healthz` → `{"ok": true, "version": ..., "businesses": n}` (used by Docker's healthcheck)
- Logs are JSON lines on stdout with a request id, user id and business id per API call.
- `MUNSHI_TRACING=1` records every agent turn (agent, role, tool, approval, latency) to
  MLflow under `MLFLOW_TRACKING_DIR`; `mlflow ui --backend-store-uri file:data/mlruns`.

## Sizing

Measured on one core with the stub model (`scripts/loadtest.py --users 12 --seconds 90`):
~40 requests/s, p95 under 60 ms, zero errors. With Groq the chat turn latency becomes the
model's (~1–3 s) but the rest is unchanged. Each business file grows roughly 1 MB per
10,000 ledger rows.
