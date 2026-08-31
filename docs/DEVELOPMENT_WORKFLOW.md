# Development Workflow

1. Read root `CLAUDE.md`.
2. Read `docs/PROJECT_CONTEXT.md` only if the task needs project-level
   context, not for every task.
3. Identify the relevant module via `docs/MODULE_MAP.md`.
4. Read `docs/CODE_MAP.md` for that module.
5. Read only the source files the task touches.
6. Plan (for anything non-trivial — a short plan, not a doc).
7. Implement.
8. Run targeted tests (`pytest tests/test_X.py`, not the full suite, for a
   small change).
9. Fix.
10. Run the full suite (`pytest`) plus `ruff check .` and `mypy apps` before
    considering a change done.
11. Update `docs/IMPLEMENTATION_STATUS.md` if something meaningful shipped.
12. Update `docs/ARCHITECTURE.md`, `docs/CODE_MAP.md`, or `docs/DECISIONS.md`
    if the architecture or a significant decision changed. Don't touch them
    for unrelated changes.
13. Move to the next highest-priority task from `docs/IMPLEMENTATION_STATUS.md`.

Don't reread the entire project at every turn — this workflow exists so that
doesn't have to happen.

## What CI actually runs (`.github/workflows/ci.yml`, D052)

Two independent jobs, both on GitHub-hosted runners, no external service
and no repository secret:

| Job | Runs | Needs |
| --- | --- | --- |
| `test` | `bash scripts/secret_scan.sh`, `ruff check .`, `mypy apps`, `alembic upgrade head`, the `downgrade base` → `upgrade head` round-trip, `pytest -v` | Postgres + Redis service containers |
| `web` | `npm ci`, `npm run build`, `npm test` in `apps/web` | Node 22 only |

Step 10 above (`pytest` + `ruff check .` + `mypy apps`) covers the `test`
job. If you touched `apps/web`, also run `npm run build && npm test` there
before calling the change done — that is now a blocking check.

Two things worth knowing before you fight CI:

- **The secret scan lives in `scripts/secret_scan.sh`, not inline in the
  workflow.** Run it locally the same way CI does. If it flags a line that
  is a deliberate fixture rather than a real credential, append a
  `pragma: allowlist secret` comment to *that line*. Don't exclude the
  file, and don't widen the pattern.
- **Tests must not read secrets out of the ambient environment.** The
  `test` job exports `JWT_SECRET_KEY` for every step, so a test asserting
  fail-closed-when-unset has to `monkeypatch.delenv` it; `_env_file=None`
  alone is not enough.

The Playwright e2e suite below is **not** run by CI (D052) — it is still a
manual, local-only gate.

## Running the e2e suite (`apps/web`, Playwright — D045)

`apps/web` has two independent test suites:

| Suite | Command | Needs |
| --- | --- | --- |
| Component (Vitest + RTL) | `npm test` | nothing but `node_modules` |
| End-to-end (Playwright) | `npm run test:e2e` | Docker, a migrated Postgres, a running API, a browser |

They are separate on purpose. `npm test` mocks `fetch` and asserts
rendering; `npm run test:e2e` mocks nothing at all — that is the only
reason it earns its cost. `vitest.config.ts` excludes `e2e/**` so the two
never collide.

One-time setup, inside `apps/web`:

```
npm install                        # picks up @playwright/test
npx playwright install chromium    # one engine is enough
```

Full local bring-up (from the repo root; adjust ports if they collide
with a stack you already have running — the examples below use the base
`docker-compose.yml` defaults):

```
# 1. Real Postgres + Redis
docker compose up -d postgres redis

# 2. Real schema
alembic upgrade head

# 3. Real API (needs DATABASE_URL/REDIS_URL/JWT_SECRET_KEY in your .env)
uvicorn apps.api.app.main:app --port 8000

# 4. Real fixtures — users, roles, brokers, grants (direct SQL, D013)
python scripts/seed_e2e.py

# 5. The suite. It starts `next dev` itself; do not start one yourself.
cd apps/web && npm run test:e2e
```

Step 4 is also run automatically by Playwright's `globalSetup` before
every run, because several specs submit real trades that permanently
change real broker rows. Environment knobs:

- `E2E_API_BASE_URL` — backend base URL (default `http://localhost:8000`).
  Passed to `next dev` as `API_BASE_URL`, so CI and local runs can target
  different backends.
- `E2E_WEB_PORT` — port for the dev server Playwright starts (default 3100).
- `E2E_SEED_COMMAND` — override the seed command (e.g. to name a venv's
  interpreter). `E2E_SKIP_SEED=1` skips seeding entirely.

Notes that will save you an hour:

- The suite addresses the dev server as `localhost`, never `127.0.0.1`.
  Next 16 returns 403 for asset requests from a bare IP that isn't in
  `allowedDevOrigins`, which leaves the page rendered but never hydrated —
  and an unhydrated click fires the browser's native form submit instead
  of the React handler.
- Specs wait for hydration (`waitForHydration()` in `e2e/helpers.ts`)
  before interacting, for the same reason.
- The suite is single-worker, serial, and never retries: it mutates real
  broker state, so a retried trade would run against a different book.
- `scripts/seed_e2e.py` deletes and recreates the rows it owns. Never
  point it at a database you care about.
