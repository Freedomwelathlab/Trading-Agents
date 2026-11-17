"""Get today's Zerodha (Kite Connect) access token (Phase 110, D137).

Kite invalidates the access token at 06:00 IST every day and a new one
needs a browser login by the account holder - there is no way round that,
and automating the login is against Zerodha's terms. This script does the
part that can be done for you: it shows the login link, then trades the
one-time `request_token` Kite hands back for the day's `access_token`.

    python -m scripts.zerodha_login

1. Open the printed link, log in to Kite.
2. Kite redirects to your app's redirect URL with `?request_token=...`.
   Paste that whole URL (or just the token) here.
3. The access token is printed ONCE. Paste it into the Zerodha broker's
   credentials in the admin screen (field "Access token").

The API secret is read with `getpass` (not echoed) and is never printed,
logged or written anywhere. The access token is shown on this terminal
only, because you have to carry it to the credentials screen; it stops
working tomorrow morning.

This script places no order and reads no account.
"""

from __future__ import annotations

import getpass
import sys
import urllib.parse

from apps.api.app.execution.adapters.zerodha import (
    KITE_LOGIN_URL,
    KITE_VERSION,
    ZerodhaError,
    exchange_request_token,
)


def request_token_from(pasted: str) -> str:
    """Accepts the bare token or the whole redirect URL it arrived in."""
    pasted = pasted.strip()
    if "request_token=" in pasted:
        query = urllib.parse.urlparse(pasted).query or pasted
        values = urllib.parse.parse_qs(query).get("request_token", [])
        return values[0] if values else ""
    return pasted


def main() -> int:
    api_key = input("Kite Connect API key: ").strip()
    if not api_key:
        print("No API key given.")
        return 2
    api_secret = getpass.getpass("Kite Connect API secret (not shown): ").strip()
    if not api_secret:
        print("No API secret given.")
        return 2

    link = f"{KITE_LOGIN_URL}?{urllib.parse.urlencode({'v': KITE_VERSION, 'api_key': api_key})}"
    print(f"\nOpen this link and log in:\n  {link}\n")
    token = request_token_from(input("Paste the redirect URL (or the request_token): "))
    if not token:
        print("No request_token found in what was pasted.")
        return 2

    try:
        access_token = exchange_request_token(
            api_key=api_key, api_secret=api_secret, request_token=token
        )
    except ZerodhaError as exc:
        print(f"Kite refused the exchange: {exc}")
        return 1

    print("\nAccess token (valid until 06:00 IST tomorrow):")
    print(f"  {access_token}")
    print("\nPaste it into the Zerodha broker's credentials. It is not stored here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
