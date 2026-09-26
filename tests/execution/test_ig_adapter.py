"""The IG Markets adapter (Phase 93, D112).

Against a stub client, never a login. What is asserted is the set of
decisions IG's responses do not make for you:

* Demo and live are different hosts with different money in them, so an
  unrecognised `account_type` is refused rather than defaulted.
* The session tokens arrive in HEADERS. A client that reads only the body
  authenticates once and then 401s on everything after, which looks
  exactly like a wrong password.
* A `dealReference` is a receipt for a REQUEST. Acceptance and the dealt
  level are only knowable from the confirm, and a REJECTED deal is a
  definite answer — not something to leave the reconciler waiting on.
* `available`, not `balance`, is cash: `balance` includes margin already
  committed, and sizing against it double-counts money the account cannot
  deploy.
* A position aggregate cannot close anything, because IG closes a dealId.
"""

from decimal import Decimal

import pytest

from apps.api.app.execution.adapters.ig import (
    IG_DEMO_URL,
    IG_LIVE_URL,
    IgAdapter,
    IgError,
    IgOrderNotConfirmedError,
    build_ig_adapter,
)
from apps.api.app.execution.broker import OrderNotConfirmedError, OrderRequest
from apps.api.app.risk.models import Side

D = Decimal

SESSION_HEADERS = {"CST": "cst-token", "X-SECURITY-TOKEN": "xst-token"}

ACCOUNTS = {
    "accounts": [
        {"accountId": "ABC1", "preferred": False, "balance": {"balance": "1", "available": "1"}},
        {
            "accountId": "MAIN",
            "preferred": True,
            # `balance` is bigger than `available` on purpose: margin is
            # already committed, and cash must be the deployable figure.
            "balance": {"balance": "50000.00", "available": "12000.00"},
        },
    ]
}

POSITIONS = {
    "positions": [
        {
            "position": {"dealId": "DIAAAA1", "direction": "BUY", "size": "2", "level": "1.1000"},
            "market": {"epic": "CS.D.EURUSD.MINI.IP"},
        },
        {
            "position": {
                "dealId": "DIAAAA2", "direction": "SELL", "size": "0.5", "level": "1.1050"
            },
            "market": {"epic": "CS.D.EURUSD.MINI.IP"},
        },
    ]
}


class StubClient:
    """Answers per (method, path-prefix) and records what it was sent."""

    def __init__(self, routes):
        self._routes = routes
        self.calls: list[tuple[str, str, dict, dict]] = []

    def request(self, method, path, *, version, body=None, headers=None):
        self.calls.append((method, path, dict(body or {}), dict(headers or {})))
        for (m, prefix), answer in self._routes.items():
            if m == method and path.startswith(prefix):
                return answer() if callable(answer) else answer
        raise AssertionError(f"no stub for {method} {path}")


def routes(over=None):
    """Default answers, with per-test overrides.

    Overrides are passed POSITIONALLY: the keys are (method, prefix)
    tuples, and `**` expansion requires string keys.
    """
    base = {
        ("POST", "/session"): (200, SESSION_HEADERS, {}),
        ("GET", "/accounts"): (200, {}, ACCOUNTS),
        ("GET", "/positions"): (200, {}, POSITIONS),
    }
    base.update(over or {})
    return base


def adapter(client=None, **over) -> IgAdapter:
    kwargs = dict(
        api_key="key",
        username="user",
        password="pw",
        account_type="DEMO",
    )
    kwargs.update(over)
    return IgAdapter(client or StubClient(routes()), **kwargs)  # type: ignore[arg-type]


def buy(**over) -> OrderRequest:
    base = dict(symbol="CS.D.EURUSD.MINI.IP", side=Side.BUY, quantity=D("1"))
    base.update(over)
    return OrderRequest(**base)  # type: ignore[arg-type]


# --- account type -----------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "demo-account", "PAPER", None])
def test_an_unrecognised_account_type_is_refused_never_defaulted(bad):
    # Guessing picks one of two accounts with different money in them.
    with pytest.raises(IgError) as err:
        adapter(account_type=bad)
    assert "no default" in str(err.value)


def test_the_account_type_picks_the_host():
    assert adapter(account_type="demo").base_url == IG_DEMO_URL
    assert adapter(account_type="LIVE").base_url == IG_LIVE_URL


# --- session ----------------------------------------------------------------


def test_the_session_tokens_are_read_from_headers_and_sent_on_every_later_call():
    client = StubClient(routes())
    a = adapter(client)
    _ = a.cash
    authed = [h for m, p, _b, h in client.calls if p.startswith("/accounts")]
    assert authed and authed[0]["CST"] == "cst-token"
    assert authed[0]["X-SECURITY-TOKEN"] == "xst-token"
    assert authed[0]["X-IG-API-KEY"] == "key"


def test_a_login_that_returns_no_tokens_is_an_error_not_an_anonymous_retry():
    client = StubClient(routes({("POST", "/session"): (200, {}, {})}))
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    assert "no CST" in str(err.value)


def test_the_login_happens_once_and_is_reused():
    client = StubClient(routes())
    a = adapter(client)
    _ = a.cash
    _ = a.positions
    assert sum(1 for m, p, _b, _h in client.calls if p.startswith("/session")) == 1


def test_an_error_code_in_a_200_body_is_still_a_refusal():
    client = StubClient(
        routes({("POST", "/session"): (200, {}, {"errorCode": "error.security.api-key-invalid"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    assert "api-key-invalid" in str(err.value)


def test_a_rejected_login_says_the_key_was_accepted_and_the_password_was_not():
    """`invalid-details` is NOT a bad key. Measured against IG: a fake key
    gets 403 `api-key-invalid` on both hosts. Telling an operator to check
    "their credentials" sends them to re-paste a key that was fine."""
    client = StubClient(
        routes({("POST", "/session"): (401, {}, {"errorCode": "error.security.invalid-details"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    message = str(err.value)
    assert "error.security.invalid-details" in message  # IG's own code survives
    assert "ACCEPTED the API key" in message
    assert "DEMO" in message  # names the host it was talking to
    assert "not an email" in message


def test_a_rejected_demo_login_points_at_the_web_api_demo_login_not_2fa():
    """The operator's single 2FA-protected web login opens demo in the
    browser and is still the wrong pair for the demo API."""
    client = StubClient(
        routes({("POST", "/session"): (401, {}, {"errorCode": "error.security.invalid-details"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    message = str(err.value)
    assert "Web API demo login details" in message
    assert "2FA is not what is blocking it" in message


def test_a_rejected_live_login_does_not_give_demo_advice():
    client = StubClient(
        routes({("POST", "/session"): (401, {}, {"errorCode": "error.security.invalid-details"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client, account_type="LIVE").cash
    assert "Web API demo login details" not in str(err.value)
    assert "LIVE host" in str(err.value)


def test_an_unrecognised_key_names_the_host_it_was_refused_by():
    client = StubClient(
        routes({("POST", "/session"): (403, {}, {"errorCode": "error.security.api-key-invalid"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client, account_type="LIVE").cash
    assert "LIVE host" in str(err.value)
    assert "per environment" in str(err.value)


def test_an_unmapped_login_error_still_carries_the_venues_own_code():
    client = StubClient(
        routes({("POST", "/session"): (403, {}, {"errorCode": "error.security.account-suspended"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    assert "account-suspended" in str(err.value)


# --- account ----------------------------------------------------------------


def test_cash_is_the_available_balance_of_the_preferred_account():
    a = adapter()
    # 12,000 available, not the 50,000 headline balance, and from the
    # PREFERRED account rather than the first one listed.
    assert a.cash == D("12000.00")


def test_an_account_with_no_available_figure_is_an_error_not_zero_cash():
    client = StubClient(
        routes(
            {
                ("GET", "/accounts"): (
                    200,
                    {},
                    {"accounts": [{"accountId": "X", "preferred": True, "balance": {}}]},
                )
            }
        )
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    assert "absent figure, not zero cash" in str(err.value)


def test_positions_net_by_epic_and_sign_by_direction():
    a = adapter()
    # A 2-lot buy and a 0.5-lot sell on the same epic net to +1.5.
    assert a.positions == {"CS.D.EURUSD.MINI.IP": D("1.5")}


def test_open_deals_keeps_the_deal_ids_the_aggregate_cannot_carry():
    a = adapter()
    ids = sorted(d["deal_id"] for d in a.open_deals())
    assert ids == ["DIAAAA1", "DIAAAA2"]
    # Two deals, one netted position - which is exactly why closing needs
    # this list and not `positions`.
    assert len(a.positions) == 1


def test_valuing_the_account_needs_a_mark_for_every_open_epic():
    a = adapter()
    with pytest.raises(IgError):
        a.get_account_state(marks={})
    state = a.get_account_state(marks={"CS.D.EURUSD.MINI.IP": D("1.1")})
    assert state.cash == D("12000.00")
    assert state.current_exposure == D("1.65")


# --- dealing ----------------------------------------------------------------


def accepted_confirm(**over):
    base = {"dealStatus": "ACCEPTED", "dealId": "DEAL9", "level": "1.1042", "size": "1"}
    base.update(over)
    return (200, {}, base)


def test_an_accepted_deal_reports_the_level_and_size_ig_confirmed():
    client = StubClient(
        routes(
            {
                ("POST", "/positions/otc"): (200, {}, {"dealReference": "REF1"}),
                ("GET", "/confirms/"): accepted_confirm(size="0.75"),
            }
        )
    )
    fill = adapter(client).submit_order(buy(quantity=D("1")), market_price=D("1.10"))
    # The DEALT size, not the requested one.
    assert fill.quantity == D("0.75")
    assert fill.fill_price == D("1.1042")


def test_a_rejected_deal_is_an_error_not_something_the_reconciler_waits_on():
    client = StubClient(
        routes(
            {
                ("POST", "/positions/otc"): (200, {}, {"dealReference": "REF2"}),
                ("GET", "/confirms/"): (
                    200,
                    {},
                    {"dealStatus": "REJECTED", "reason": "INSUFFICIENT_FUNDS"},
                ),
            }
        )
    )
    with pytest.raises(IgError) as err:
        adapter(client).submit_order(buy(), market_price=D("1.10"))
    assert "REJECTED" in str(err.value) and "INSUFFICIENT_FUNDS" in str(err.value)
    assert not isinstance(err.value, OrderNotConfirmedError)


def test_an_unconfirmed_deal_raises_the_ports_type_carrying_the_reference():
    client = StubClient(
        routes(
            {
                ("POST", "/positions/otc"): (200, {}, {"dealReference": "REF3"}),
                ("GET", "/confirms/"): (200, {}, {"dealStatus": "PENDING"}),
            }
        )
    )
    with pytest.raises(IgOrderNotConfirmedError) as err:
        adapter(client).submit_order(buy(), market_price=D("1.10"))
    assert isinstance(err.value, OrderNotConfirmedError)
    assert err.value.order_id == "REF3"


def test_an_accepted_deal_with_no_level_is_unconfirmed_rather_than_a_zero_fill():
    client = StubClient(
        routes(
            {
                ("POST", "/positions/otc"): (200, {}, {"dealReference": "REF4"}),
                ("GET", "/confirms/"): accepted_confirm(level=None),
            }
        )
    )
    with pytest.raises(IgOrderNotConfirmedError):
        adapter(client).submit_order(buy(), market_price=D("1.10"))


def test_the_epic_passes_through_verbatim_and_the_reference_price_is_not_sent():
    client = StubClient(
        routes(
            {
                ("POST", "/positions/otc"): (200, {}, {"dealReference": "REF5"}),
                ("GET", "/confirms/"): accepted_confirm(),
            }
        )
    )
    adapter(client).submit_order(
        buy(symbol="IX.D.FTSE.DAILY.IP", quantity=D("1")), market_price=D("7700")
    )
    sent = next(b for m, p, b, _h in client.calls if p == "/positions/otc")
    assert sent["epic"] == "IX.D.FTSE.DAILY.IP"
    assert sent["direction"] == "BUY" and sent["orderType"] == "MARKET"
    # IG fills at its own level; the caller's reference price must not
    # appear anywhere in the deal.
    assert "7700" not in str(sent)


def test_a_deal_accepted_with_no_reference_is_an_error():
    client = StubClient(routes({("POST", "/positions/otc"): (200, {}, {})}))
    with pytest.raises(IgError) as err:
        adapter(client).submit_order(buy(), market_price=D("1.10"))
    assert "no dealReference" in str(err.value)


def test_a_non_positive_size_never_reaches_the_venue():
    client = StubClient(routes())
    with pytest.raises(IgError):
        adapter(client).submit_order(buy(quantity=D("0")), market_price=D("1.10"))
    assert not any(p == "/positions/otc" for _m, p, _b, _h in client.calls)


# --- closing ----------------------------------------------------------------


def test_closing_takes_a_deal_id_and_an_explicit_direction():
    client = StubClient(routes({("POST", "/positions/otc"): (200, {}, {"dealReference": "C1"})}))
    a = adapter(client)
    assert a.close_deal("DIAAAA1", direction="sell", size=D("2")) == "C1"
    sent = next(b for m, p, b, _h in client.calls if p == "/positions/otc")
    assert sent["dealId"] == "DIAAAA1" and sent["direction"] == "SELL"
    # IG tunnels the delete through POST; a real DELETE with a body is
    # rejected by the gateway.
    hdr = next(h for m, p, _b, h in client.calls if p == "/positions/otc")
    assert hdr["_method"] == "DELETE"


@pytest.mark.parametrize("bad", ["", "long", "CLOSE"])
def test_an_unclear_close_direction_is_refused(bad):
    # Inferring it from a stale local view is how a close becomes a
    # doubling.
    with pytest.raises(IgError):
        adapter().close_deal("D1", direction=bad, size=D("1"))


# --- factory ----------------------------------------------------------------


def test_the_factory_refuses_a_half_filled_credential_set():
    with pytest.raises(IgError) as err:
        build_ig_adapter({"api_key": "k", "username": "u"})
    assert "NOT_CONFIGURED" in str(err.value)
    assert "password" in str(err.value) and "account_type" in str(err.value)


def test_the_factory_wires_the_host_the_account_type_names():
    built = build_ig_adapter(
        {"api_key": "k", "username": "u", "password": "p", "account_type": "live"}
    )
    assert built.base_url == IG_LIVE_URL
    assert built.account_type == "LIVE"


def test_a_suspended_client_says_so_and_says_to_stop_retrying():
    """Measured 2026-09-25: after repeated invalid-details refusals IG
    answered `error.security.client-suspended` while the web login kept
    working in the browser."""
    from apps.api.app.execution.broker import VenueLoginRefusedError

    client = StubClient(
        routes({("POST", "/session"): (401, {}, {"errorCode": "error.security.client-suspended"})})
    )
    with pytest.raises(IgError) as err:
        _ = adapter(client).cash
    assert isinstance(err.value, VenueLoginRefusedError)
    message = str(err.value)
    assert "SUSPENDED this API client" in message
    assert "not your web login" in message
    assert "NEW API key" in message
