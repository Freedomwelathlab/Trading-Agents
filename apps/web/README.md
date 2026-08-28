# Trading OS — Web Frontend

Phase 17 first frontend slice: login, backend health status, quote
lookup, and paper-trade submission. Next.js 16 (App Router), TypeScript,
Tailwind v4. See `docs/DECISIONS.md` D020 and `docs/CODE_MAP.md`'s
"Frontend" section in the repo root for the full design rationale.

## Setup

```bash
npm install
cp .env.example .env.local   # then edit API_BASE_URL if needed
```

## Environment variables

| Variable       | Where used                    | Default                 |
|----------------|--------------------------------|--------------------------|
| `API_BASE_URL` | server-side only (route handlers in `app/api/**`) | `http://localhost:8000` |

`API_BASE_URL` is never sent to the browser — every backend call is
proxied through a Next.js route handler, so only server-side code needs
to know where the API lives.

## Run the backend (from the repo root, not `apps/web/`)

```bash
docker compose up -d      # postgres, redis, api on :8000 by default
# if a sibling worktree already has 8000/5432/6379 in use, remap ports in
# your local docker-compose override and point API_BASE_URL at the
# remapped API port instead
docker compose down        # when finished
```

## Develop

```bash
npm run dev
# http://localhost:3000
```

`/` redirects to `/dashboard` if you already have a session cookie, or
`/login` otherwise. `/dashboard` is gated by `proxy.ts` (Next.js 16's
replacement for `middleware.ts`) on the presence of that cookie.

## Build

```bash
npm run build
```

Must succeed with zero TypeScript errors — this is a hard requirement in
this repo, not a suggestion. `npm run start` serves the production build.

## Test

```bash
npm run test
```

Vitest + React Testing Library (jsdom environment). Covers the quote
lookup and trade submission components' success and error-rendering
paths, including the exact `NOT_CONFIGURED:`/`NO_DATA_AVAILABLE:` sentinel
strings and a rejected trade's `block_reason` — never a generic "error
occurred" message. See D020 for why Vitest/RTL was chosen over Playwright
for this phase.

## Auth model

The JWT returned by `POST /auth/login` is stored in an httpOnly cookie
set by `/api/auth/login`, a Next.js route handler — never in
`localStorage` or any other browser-readable store. Every other
authenticated call (`/api/quote/[symbol]`, `/api/trades/[brokerId]`) is
also a route handler that reads the cookie server-side and attaches
`Authorization: Bearer <token>` before calling the backend. See D020 for
the full tradeoff against a localStorage-based v1.
