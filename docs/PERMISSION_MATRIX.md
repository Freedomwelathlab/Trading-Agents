# Permission matrix (Phase 68, D086)

The complete, closed set of `Permission` values
(`apps/api/app/auth/permissions.py`) — there are exactly 10, and adding an
11th means adding a member there AND a check that actually enforces it
somewhere; the enum alone does nothing. This table is a human-readable
cross-reference, not the source of truth — `permissions.py`'s own
docstrings are, and every row below was checked against them plus the
actual route source at the time of writing (Phase 68). It is linked from
`docs/TRADING_SAFETY.md`.

Automated enforcement of "every non-public route requires SOME
authentication dependency" (not "the correct one" — see that test's own
docstring for the precise scope) lives in
`tests/auth/test_permission_matrix.py`, added the same phase as this table.

| Permission | One-line description | Routes that require it | ADMIN override? |
| --- | --- | --- | --- |
| `trade:submit:paper` (`SUBMIT_PAPER_TRADE`) | Submit a trade against a broker the caller holds a `BrokerGrant` for — the base trading capability, checked together with broker-grant ownership by `require_broker_access`. | `POST /brokers/{broker_id}/trades` (paper branch), `POST /brokers/{broker_id}/agent-trades` | No — no permission in this codebase carries one except `admin:manage` itself; see `ADMIN` row. |
| `trade:submit:live` (`SUBMIT_LIVE_TRADE`) | Required IN ADDITION to `trade:submit:paper` to submit a trade against a `kind=LIVE` broker — checked in-handler by `_authorize_live_trade`, last of that function's three gates (confirmation, then live-path configuration, then this permission). **Docstring was stale as of Phase 67** ("reserved, not enforced anywhere yet") — fixed this phase; it has been enforced since Phase 43 (D058). | `POST /brokers/{broker_id}/trades` (live branch only, in-handler — not visible to `tests/auth/test_permission_matrix.py`'s dependency-tree walk, which is why this permission also appears via source grep in that test's third check) | No. |
| `portfolio:view` (`VIEW_PORTFOLIO`) | View a broker's positions/P&L/order and fill history — a strictly weaker capability than trading in it, combined with the same per-broker `BrokerGrant` check `require_broker_access` applies to every permission it gates. | `GET /brokers/{broker_id}/portfolio`, `POST /brokers/{broker_id}/portfolio/snapshots`, `GET /brokers/{broker_id}/portfolio/history`, `GET /brokers/{broker_id}/orders`, `GET /brokers/{broker_id}/orders/{order_id}`, `GET /brokers/{broker_id}/fills` | No. |
| `admin:manage` (`ADMIN`) | One coarse permission gating every route under `/admin` — user/role/broker-grant/broker CRUD, and the emergency-stop WRITE actions (activate/deactivate). Deliberately not split into per-resource permissions (D013). | Every route in `apps/api/app/api/routes/admin.py` (`/admin/users`, `/admin/roles`, `/admin/broker-grants`, `/admin/brokers`, `/admin/brokers/{id}/mode`), plus `POST /admin/emergency-stop` and `POST /admin/emergency-stop/deactivate` (`emergency_stop.py`'s `router`, not its `status_router`) | N/A — this IS the admin override; nothing sits above it. |
| `strategy:manage` (`STRATEGY_MANAGE`) | Create, read, edit, fork a version, validate a version of, and get an AI-drafted proposal for a strategy — ONLY the caller's own (`owner_user_id` checked per route). No ADMIN override on purpose (D071): admin:manage is operational, not a license to read every user's strategy research. | Every route under `/strategies` in `apps/api/app/api/routes/strategies.py` (`POST /strategies`, `GET /strategies`, `GET/PATCH /strategies/{id}`, `GET/PATCH /strategies/{id}/versions/{id}`, `POST /strategies/{id}/versions` [fork], `POST /strategies/{id}/versions/{id}/validate`, `POST /strategies/research/propose`), plus `GET /strategies/leaderboard` (`leaderboard.py`) | No. |
| `strategy:backtest` (`STRATEGY_BACKTEST`) | Run and read backtests, Monte Carlo resamples, walk-forward runs, robustness (parameter-sensitivity) runs, and universe scans over the caller's OWN strategies — deliberately separate from `strategy:manage` (an evaluation-only role can hold this without authoring strategies, and vice versa). No ADMIN override. | `strategy_backtests.py`'s `router`/`runs_router`, `monte_carlo.py`'s `router`/`runs_router`, `walk_forward.py`'s `router`/`runs_router`, `robustness.py`'s `router`/`runs_router`, `universe_scan.py`'s `router`/`scans_router` | No. |
| `strategy:signal` (`STRATEGY_SIGNAL`) | Evaluate and read a strategy version's CURRENT signal — present-tense, "what should happen now," one step from an order (the input Phase 63's runner acts on) rather than a historical what-if. Deliberately separate from `strategy:backtest` in both directions. No ADMIN override. | `signals.py`'s `router`/`evaluations_router` (`POST/GET /strategies/{id}/versions/{id}/signals`, `GET /signal-evaluations/{id}`) | No. |
| `strategy:deploy` (`STRATEGY_DEPLOY`) | Create, list, read, pause, resume, and stop a `StrategyDeployment` — put a validated version on the scheduled paper-trading runner. Does NOT include approval (see below). No ADMIN override. | `deployments.py`'s `router` (create/list under `/strategies`) and `deployments_router` (`GET/POST /deployments/{id}`, `.../pause`, `.../resume`, `.../stop`, `.../runs`, `.../signals`, `.../monitoring`, `.../drift-checks`) | No. |
| `strategy:approve_deployment` (`STRATEGY_APPROVE_DEPLOYMENT`) | Gates ONLY the action moving a deployment `pending_approval → active` — the mandatory human gate spec §25/§52 requires before a strategy trades, even in paper mode. Deliberately separate from `strategy:deploy` so one role can propose and a different role can approve. No ADMIN override — an operational admin is not automatically a trading approver. | `POST /deployments/{id}/approve` (`_approve_router`) — paper-mode deployments; a `live`-mode deployment additionally needs the permission below | No. |
| `strategy:approve_live_deployment` (`STRATEGY_APPROVE_LIVE_DEPLOYMENT`) | Required IN ADDITION to `strategy:approve_deployment` to approve a `mode='live'` deployment — checked in-handler (the deployment's mode isn't known until it's loaded), not at the router-dependency level. Holding it does not let anything actually trade live: the runner refuses every live cycle unconditionally regardless (D082). No ADMIN override. | `POST /deployments/{id}/approve`, live-mode branch only (in-handler — not visible to the dependency-tree walk, same reason as `SUBMIT_LIVE_TRADE` above; this permission's presence is confirmed by the same source-grep check) | No. |

## What is intentionally NOT in this table

Ownership checks (`_load_owned_strategy`, `_load_owned_deployment`,
`require_broker_access`'s `BrokerGrant` lookup) are a SECOND, separate
authorization layer on top of every `strategy:*` and broker-scoped
permission above (D071) — this table lists the permission, not the
per-resource ownership rule layered on top of it. `GET /auth/session`,
`GET /watchlists/*`, and `GET /market-data/*` require only
`get_current_user` (any authenticated active user) and no `Permission` at
all, so they do not appear here — see
`apps/api/app/auth/dependencies.py`'s own docstring for that distinction,
and `tests/auth/test_permission_matrix.py` for the automated proof that
every non-public route requires at least that much.
