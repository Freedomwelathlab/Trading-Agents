"""Ingest real OHLCV bars into a DEPLOYED Trading OS by driving its own
admin endpoint (Phase 74 follow-up).

The deployed API is the only thing that can write to the production
database, and it needs the `LONGPORT_*` variables set on the host first
(see the runbook in docs/DEPLOYMENT.md). This script does not touch any
secret store and never sees the vendor credentials: it authenticates as
an existing admin with a password you type at runtime, then POSTs
`/admin/market-data/backfill` for each symbol/interval so the ingestion
runs INSIDE production, against production's own Postgres and its own
vendor credentials.

Usage:

    python scripts/backfill_prod.py --base-url https://<your-api-host>

It prompts for the admin email and password (password via getpass, never
echoed, never an argument). Add --symbols / --intervals / --days to
override the defaults.

Each backfill runs synchronously in the API request, so very large
intraday ranges can be slow; the defaults are chosen to stay well within
a normal request timeout. Re-running is safe — ingestion is an idempotent
upsert keyed on (symbol, interval, ts).
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# Default coverage: enough to make the Markets terminal show real charts
# and session levels for the instruments this project actually works with.
DEFAULT_SYMBOLS = ["TQQQ.US", "QQQ.US"]
DEFAULT_INTERVALS = ["1d", "1h", "15m", "5m"]
# Lookback per interval (calendar days). Daily gets years; the fine
# intraday intervals get a shorter window so one request stays quick.
DEFAULT_DAYS = {"1d": 1095, "1h": 180, "15m": 120, "5m": 45, "30m": 90, "1m": 7}


def _login(base: str, email: str, password: str) -> str:
    data = urllib.parse.urlencode({"username": email, "password": password}).encode()
    req = urllib.request.Request(
        f"{base}/auth/login",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            token = json.load(r).get("access_token")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        raise SystemExit(f"Login failed ({exc.code}): {body}") from exc
    if not token:
        raise SystemExit("Login succeeded but no access_token was returned.")
    return token


def _backfill(base: str, token: str, symbol: str, interval: str, days: int) -> dict:
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    payload = json.dumps(
        {
            "symbol": symbol,
            "bar_interval": interval,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/admin/market-data/backfill",
        data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    # Generous timeout: a paged intraday backfill runs synchronously in the
    # request, and this is the one call that legitimately takes a while.
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        return {"status": f"HTTP_{exc.code}", "error": exc.read().decode(errors="replace")[:200]}
    except Exception as exc:  # noqa: BLE001 - report, do not crash the batch
        return {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Deployed API origin, no trailing slash.")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--intervals", nargs="+", default=DEFAULT_INTERVALS)
    parser.add_argument("--email", help="Admin email (prompted if omitted).")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    email = (args.email or input("Admin email: ")).strip()
    password = getpass.getpass("Admin password (not echoed): ")

    token = _login(base, email, password)
    print(f"Authenticated as {email}.\n")

    print(f"{'symbol':<10}{'interval':<9}{'status':<12}{'bars':>8}  range")
    print("-" * 70)
    for symbol in args.symbols:
        for interval in args.intervals:
            days = DEFAULT_DAYS.get(interval, 90)
            result = _backfill(base, token, symbol, interval, days)
            status = str(result.get("status", "?"))
            bars = result.get("bars_ingested", "")
            span = ""
            if result.get("earliest_bar_date"):
                span = f"{result['earliest_bar_date']} .. {result.get('latest_bar_date')}"
            detail = result.get("error", "")
            print(f"{symbol:<10}{interval:<9}{status:<12}{str(bars):>8}  {span}{detail}")
    print("\nDone. Reload the Markets page — charts and session levels should now render.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
