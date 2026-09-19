# Deployment & Production Runbook

Production is two services:

- **API** — Railway service `Trading-Agents`, project `virtuous-friendship`,
  auto-deploys on every push to `main` and runs `alembic upgrade head` as
  its pre-deploy step. Origin: `https://trading-agents-production-a6aa.up.railway.app`.
- **Web** — Vercel project `web`, root directory `apps/web`, framework
  Next.js. Origin: `https://web-iota-umber-64epcerqre.vercel.app`. Its one
  required variable is `API_BASE_URL`, pointing at the Railway origin above.

Everything below is an operator action. The assistant is deliberately
prevented by its harness from reading or writing any secret store, so the
credential steps are yours to run — they are laid out exactly.

---

## 1. Rotate `JWT_SECRET_KEY` (do this once, now)

`.env.example` ships a placeholder secret, and the repository is public.
If production's `JWT_SECRET_KEY` was ever copied from that file, anyone can
forge auth tokens. Rotate it to a fresh random value — this is safe to do
regardless (it only invalidates existing login sessions):

1. Generate a fresh secret locally:

   ```bash
   python -c "import secrets; print(secrets.token_hex(48))"
   ```

2. Railway → project `virtuous-friendship` → service `Trading-Agents` →
   **Variables** → edit `JWT_SECRET_KEY` → paste the value → save.

Railway redeploys automatically. Everyone (including you) signs in again.

---

## 2. Enable live market data (`LONGPORT_*`)

The deployed API has no market-data vendor until these three variables are
set, so the Markets page shows `NOT_CONFIGURED` / `DATA_UNAVAILABLE`. Copy
the values from your **local** `trading-os/.env` (never commit them):

Railway → `Trading-Agents` → **Variables** → add three variables:

| Variable | Source (local `.env`) |
|---|---|
| `LONGPORT_APP_KEY` | line `LONGPORT_APP_KEY=` |
| `LONGPORT_APP_SECRET` | line `LONGPORT_APP_SECRET=` |
| `LONGPORT_ACCESS_TOKEN` | line `LONGPORT_ACCESS_TOKEN=` |

After the redeploy, the startup banner reports
`market_data_vendor: longbridge`, and **live quotes appear immediately** on
the Markets page. Note the access token expires periodically — when it
does, quotes 503 in production until you paste a fresh token here, exactly
as locally.

> Chart candles and session levels need bars ingested into the production
> database — that is step 4. Quotes are live without it.

---

## 3. Make your account the owner (existing account)

If you already sign in but see `Missing required permission` errors, the
account exists with a narrow role. Set ONE more variable on Railway →
`Trading-Agents` → **Variables**:

    OWNER_BOOTSTRAP_EMAIL=you@example.com

On the next deploy the API gives that account every permission and every
broker, and removes `admin:manage` from every other account (D097). It
never creates an account and is safe to leave set. Sign out and back in.

The same thing from a shell: `python scripts/grant_owner.py --email
you@example.com --demote-others` with `DATABASE_URL` pointed at the
Railway Postgres.

## 3b. Create the first production admin (empty database)

Production has no public registration. On a fresh production database,
insert the first admin directly. `DATABASE_URL` is on Railway → service
`Postgres` → **Variables** (use the public/proxy URL for a connection from
your machine):

```bash
export DATABASE_URL='postgresql://...'   # from Railway Postgres → Variables
python scripts/create_admin.py
```

It prompts for email and password (never echoed) and writes only the hash.
See `docs/API.md` § "Creating the first user".

---

## 4. Load real bars into production

Once step 2 is live and step 3 has given you an admin, ingest bars by
driving the deployed API's own admin endpoint. This never exposes the
vendor credentials — it authenticates as your admin and the ingestion runs
inside production:

```bash
python scripts/backfill_prod.py --base-url https://trading-agents-production-a6aa.up.railway.app
```

It prompts for the admin email/password and backfills TQQQ.US and QQQ.US
across daily/1h/15m/5m by default (override with `--symbols` / `--intervals`).
Re-running is safe: ingestion is an idempotent upsert. Reload the Markets
page afterward and charts + session levels render.

---

## Safety posture in production (unchanged by any of the above)

`TRADING_MODE=research` and `LIVE_TRADING_ENABLED=false` on Railway. None
of the steps here touch those. Live execution stays off until it is armed
deliberately, per `docs/TRADING_SAFETY.md`.
