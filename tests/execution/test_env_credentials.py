"""Credentials supplied as environment variables (Phase 94, D113).

The tests that matter here are about the two ways this can be quietly
wrong: a half-set credential set that looks configured on a dashboard, and
a name whose case does not match. Both were real — the deployment this was
built for had six broker variables set, every one under a name nothing
read.
"""

import pytest

from apps.api.app.execution.credentials import ResolvedCredentials
from apps.api.app.execution.env_credentials import (
    configured_providers,
    env_credential_status,
    env_var_name,
    env_var_names,
    load_env_credentials,
    partial_providers,
)

KRAKEN = {
    "KRAKEN_API_KEY": "kkey",
    "KRAKEN_API_SECRET": "ksecret",
}


def test_the_variable_name_is_one_rule_over_the_registrys_own_fields():
    assert env_var_name("kraken", "api_key") == "KRAKEN_API_KEY"
    assert env_var_name("ig", "account_type") == "IG_ACCOUNT_TYPE"

    # Derived from the provider, never a hand-kept table: every declared
    # field has a name, optional ones included, so a provider that gains a
    # field gains its variable in the same commit.
    ig = env_var_names("ig")
    assert ig["password"] == "IG_PASSWORD"
    assert ig["deal_currency"] == "IG_DEAL_CURRENCY"
    assert set(ig) == {"api_key", "username", "password", "account_type", "deal_currency"}


def test_a_complete_set_loads_and_optional_fields_are_optional():
    assert load_env_credentials("kraken", KRAKEN) == {"api_key": "kkey", "api_secret": "ksecret"}

    status = env_credential_status("kraken", KRAKEN)
    assert status.complete is True
    assert status.missing_required == ()
    # `quote_currency` and `live_orders` are declared but not required; an
    # absent optional never makes a set incomplete.
    assert set(status.present) == {"api_key", "api_secret"}

    with_optional = load_env_credentials(
        "kraken", {**KRAKEN, "KRAKEN_QUOTE_CURRENCY": "EUR"}
    )
    assert with_optional is not None
    assert with_optional["quote_currency"] == "EUR"


def test_a_half_set_credential_returns_nothing_and_names_what_is_missing():
    """The failure this whole module exists for: it LOOKS configured."""
    half = {"IG_API_KEY": "k", "IG_USERNAME": "u"}

    assert load_env_credentials("ig", half) is None

    status = env_credential_status("ig", half)
    assert status.complete is False
    assert status.any_present is True
    # Variable names, not field names - this is read by someone about to
    # go and set them.
    assert status.missing_required == ("IG_PASSWORD", "IG_ACCOUNT_TYPE")

    # And it is reported apart from the venues nobody has touched.
    assert partial_providers(half) == {"ig": ("IG_PASSWORD", "IG_ACCOUNT_TYPE")}
    assert "ig" not in configured_providers(half)


def test_an_empty_string_is_absent_not_present():
    """A dashboard variable cleared to "" must not read as configured."""
    status = env_credential_status("kraken", {"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "   "})
    assert status.complete is False
    assert status.missing_required == ("KRAKEN_API_SECRET",)


def test_lookup_is_case_insensitive_because_dashboards_let_you_type_anything():
    typed = {"Kraken_API_KEY": "kkey", "kraken_api_secret": "ksecret"}
    assert load_env_credentials("kraken", typed) == {"api_key": "kkey", "api_secret": "ksecret"}


def test_exact_case_wins_a_collision():
    both = {"KRAKEN_API_KEY": "canonical", "Kraken_API_Key": "other", "KRAKEN_API_SECRET": "s"}
    loaded = load_env_credentials("kraken", both)
    assert loaded is not None
    assert loaded["api_key"] == "canonical"


def test_nothing_set_is_reported_as_nothing_rather_than_as_a_problem():
    assert configured_providers({}) == ()
    assert partial_providers({}) == {}
    assert env_credential_status("binance", {}).any_present is False


def test_configured_providers_lists_only_the_complete_ones():
    environ = {
        **KRAKEN,
        "BINANCE_API_KEY": "b",  # secret missing - incomplete
        "IG_API_KEY": "k",
        "IG_USERNAME": "u",
        "IG_PASSWORD": "p",
        "IG_ACCOUNT_TYPE": "DEMO",
    }
    assert configured_providers(environ) == ("ig", "kraken")
    assert set(partial_providers(environ)) == {"binance"}


def test_paper_needs_nothing_and_is_not_reported_as_env_configured():
    """A provider requiring nothing is trivially "complete" — so listing
    it as configured would claim a venue is wired that nobody supplied
    anything for."""
    assert env_credential_status("paper", {}).complete is True
    assert "paper" not in configured_providers({})


@pytest.mark.parametrize("provider", ["kraken", "ig", "binance", "longbridge", "ibkr", "moomoo"])
def test_every_catalogued_provider_can_be_asked_for_its_variable_names(provider):
    """The catalogue and this module never disagree about a field."""
    names = env_var_names(provider)
    assert names
    assert all(v.isupper() and v.startswith(provider.upper()) for v in names.values())


def test_resolved_credentials_carries_its_source():
    """An operator debugging a rejected order has to be able to ask which
    key it used."""
    resolved = ResolvedCredentials(credentials={"api_key": "k"}, source="environment")
    assert resolved.source == "environment"
    assert resolved.credentials == {"api_key": "k"}


@pytest.mark.parametrize(
    ("provider", "environ"),
    [
        ("kraken", {"KRAKEN_API_KEY": "k", "KRAKEN_API_SECRET": "czNjcmV0"}),
        (
            "ig",
            {
                "IG_API_KEY": "k",
                "IG_USERNAME": "u",
                "IG_PASSWORD": "p",
                "IG_ACCOUNT_TYPE": "DEMO",
            },
        ),
    ],
)
def test_env_credentials_build_the_real_adapter(provider, environ):
    """The two layers agree on field NAMES, end to end.

    A unit test with a fake provider would pass while the real factory
    raised `NOT_CONFIGURED` over a renamed field — the one failure that
    only shows up against a live venue. Construction opens no connection,
    so this costs nothing and catches exactly that.
    """
    from apps.api.app.execution.registry import build_adapter

    credentials = load_env_credentials(provider, environ)
    assert credentials is not None
    adapter = build_adapter(provider, credentials)
    assert adapter is not None


LONGPORT = {
    "LONGPORT_APP_KEY": "a",
    "LONGPORT_APP_SECRET": "b",
    "LONGPORT_ACCESS_TOKEN": "c",
}


def test_longbridge_falls_back_to_the_longport_trio_already_on_railway():
    """Phase 97 (D116): the same three values were already set for market
    data; asking for them again under LONGBRIDGE_* invites a typo."""
    status = env_credential_status("longbridge", LONGPORT)
    assert status.complete and status.prefix == "LONGPORT"
    assert load_env_credentials("longbridge", LONGPORT) == {
        "app_key": "a",
        "app_secret": "b",
        "access_token": "c",
    }
    assert "longbridge" in configured_providers(LONGPORT)


def test_the_providers_own_prefix_wins_and_is_never_blended_with_the_alias():
    own = {
        "LONGBRIDGE_APP_KEY": "x",
        "LONGBRIDGE_APP_SECRET": "y",
        "LONGBRIDGE_ACCESS_TOKEN": "z",
    }
    assert load_env_credentials("longbridge", {**LONGPORT, **own}) == {
        "app_key": "x",
        "app_secret": "y",
        "access_token": "z",
    }
    # A HALF-set own family is reported half-set, not bypassed for the
    # complete alias: the operator started typing LONGBRIDGE_* and would
    # otherwise believe values are live that are not.
    half = {**LONGPORT, "LONGBRIDGE_APP_KEY": "x"}
    status = env_credential_status("longbridge", half)
    assert status.prefix == "LONGBRIDGE" and not status.complete
    assert load_env_credentials("longbridge", half) is None


def test_the_variable_names_shown_to_an_operator_stay_the_providers_own():
    assert env_var_names("longbridge")["app_key"] == "LONGBRIDGE_APP_KEY"
