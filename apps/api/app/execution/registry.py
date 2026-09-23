"""The broker bridge: one catalogue of providers, what each can do, and
what it needs to be told (Phase 90, D109).

`brokers.provider` has always been a free string. This turns it into a
lookup: a provider name resolves to what that venue trades, which order
types it takes, whether it can short, and which credential fields an
operator has to supply. Two things follow, and they are the whole point of
the file.

**A refusal can be specific and early.** With a capability declaration the
OMS can say "kraken does not trade options" before it builds an order,
instead of forwarding the order and surfacing a vendor error code. The
difference between `BROKER_CANNOT: kraken trades crypto, not option` and
`VENDOR_ERROR: 40004` is the difference between a platform and a wrapper.

**The UI does not have to know any broker.** `credential_fields` describes
the form, so adding a venue is a registry entry rather than a frontend
change.

**A catalogued provider is not a working one.** `factory is None` means
this platform knows what the venue is and has no adapter for it yet. Those
entries exist because an operator choosing a broker needs to see the real
list, and because the capability data is what the credential form and the
pre-flight check are built from — but nothing can trade through one, and
`build_adapter` raises rather than falling back to anything. Declaring a
capability is a description; implementing an adapter is a promise, and
this file keeps the two visibly apart via `adapter_status`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from apps.api.app.execution.broker import OrderRequest
from apps.api.app.risk.models import Side


class AssetClass(str, enum.Enum):  # noqa: UP042 (str mixin matches this codebase)
    """What an instrument IS, for the purpose of deciding whether a venue
    can trade it.

    An ETF is deliberately not a member: to an order path an ETF is an
    equity, and a separate member would invite a broker declaration that
    lists one and not the other while meaning the same thing.
    """

    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"
    FOREX = "forex"
    CRYPTO = "crypto"


class CredentialKind(str, enum.Enum):  # noqa: UP042
    """Whether a field is a secret. Secrets are never returned by any
    route once written; non-secrets (an account id, a paper/live flag)
    are, because an operator needs to confirm which account is wired."""

    SECRET = "secret"
    PUBLIC = "public"


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    kind: CredentialKind
    help: str
    required: bool = True


AdapterFactory = Callable[[dict[str, str]], object]
"""Builds a live adapter from decrypted credentials. Typed loosely on
purpose: the concrete return must satisfy `execution.broker`'s Protocol,
and importing every vendor SDK here to say so would make this module
un-importable without all of them installed."""


@dataclass(frozen=True)
class BrokerProvider:
    provider: str
    display_name: str
    asset_classes: frozenset[AssetClass]
    order_types: frozenset[str]
    supports_short: bool
    supports_extended_hours: bool
    supports_fractional: bool
    supports_cancel: bool
    credential_fields: tuple[CredentialField, ...]
    notes: str
    factory: AdapterFactory | None = field(default=None, compare=False)

    @property
    def adapter_status(self) -> str:
        """`implemented` or `catalogued`. Read this before believing any
        other field is actionable: the capabilities of a catalogued
        provider describe the VENUE, not this platform's ability to reach
        it."""
        return "implemented" if self.factory is not None else "catalogued"


def _api_key_pair(key_label: str, secret_label: str) -> tuple[CredentialField, ...]:
    return (
        CredentialField("api_key", key_label, CredentialKind.PUBLIC, "The key's identifier."),
        CredentialField("api_secret", secret_label, CredentialKind.SECRET, "Never shown again."),
    )


PROVIDERS: dict[str, BrokerProvider] = {
    "paper": BrokerProvider(
        provider="paper",
        display_name="Paper (in-platform simulator)",
        # The simulator prices whatever the caller hands it, so its asset
        # coverage is a fact about our own market data, not about a venue.
        asset_classes=frozenset(AssetClass),
        order_types=frozenset({"market", "limit"}),
        supports_short=True,
        supports_extended_hours=True,
        supports_fractional=True,
        supports_cancel=False,
        credential_fields=(),
        notes=(
            "Deterministic simulator. Fills at the market price the caller supplies, holds no "
            "resting orders (a non-marketable limit is refused rather than parked), and "
            "collateralises shorts at 100% cash because it has no margin model."
        ),
        # Constructed by `execution.persistence`, which needs the account
        # row, so it is not built from credentials and has no factory here.
        factory=None,
    ),
    "longbridge": BrokerProvider(
        provider="longbridge",
        display_name="Longbridge / Longport",
        asset_classes=frozenset({AssetClass.EQUITY, AssetClass.OPTION}),
        order_types=frozenset({"market", "limit"}),
        supports_short=False,
        supports_extended_hours=True,
        supports_fractional=False,
        supports_cancel=True,
        credential_fields=(
            CredentialField("app_key", "App key", CredentialKind.PUBLIC, "LONGPORT_APP_KEY."),
            CredentialField(
                "app_secret", "App secret", CredentialKind.SECRET, "LONGPORT_APP_SECRET."
            ),
            CredentialField(
                "access_token",
                "Access token",
                CredentialKind.SECRET,
                "LONGPORT_ACCESS_TOKEN. Expires; re-issue and re-save when it does.",
            ),
        ),
        notes=(
            "The platform's existing live venue. Equities and options on US and HK markets; "
            "no futures, no spot FX and no spot crypto - its currency endpoint is a "
            "conversion-rate lookup, not a tradeable instrument."
        ),
        factory=None,
    ),
    "ibkr": BrokerProvider(
        provider="ibkr",
        display_name="Interactive Brokers",
        asset_classes=frozenset(
            {AssetClass.EQUITY, AssetClass.OPTION, AssetClass.FUTURE, AssetClass.FOREX}
        ),
        order_types=frozenset({"market", "limit"}),
        supports_short=True,
        supports_extended_hours=True,
        supports_fractional=True,
        supports_cancel=True,
        credential_fields=(
            CredentialField(
                "gateway_url",
                "Client Portal gateway URL",
                CredentialKind.PUBLIC,
                "e.g. https://localhost:5000/v1/api. IBKR's Web API authenticates through a "
                "gateway process you run and re-authenticate daily.",
            ),
            CredentialField(
                "account_id",
                "Account id",
                CredentialKind.PUBLIC,
                "The IBKR account the orders are placed in (Uxxxxxxx).",
            ),
        ),
        notes=(
            "The widest coverage of any venue here. The cost is operational, not technical: "
            "the Client Portal API authenticates through a gateway process whose session "
            "expires daily, so an unattended bot needs that session kept alive. Spot crypto "
            "is routed through Paxos and is deliberately NOT declared here until it is tested."
        ),
        factory=None,
    ),
    "ig": BrokerProvider(
        provider="ig",
        display_name="IG Markets",
        # IG's instruments are CFDs/spread bets over the underlying, not
        # the underlying itself. They are declared under the asset class
        # they TRACK because that is what a strategy reasons about, and the
        # note says plainly what is actually being bought.
        asset_classes=frozenset({AssetClass.FOREX, AssetClass.EQUITY, AssetClass.FUTURE}),
        order_types=frozenset({"market", "limit"}),
        supports_short=True,
        supports_extended_hours=False,
        supports_fractional=True,
        supports_cancel=True,
        credential_fields=(
            CredentialField("api_key", "API key", CredentialKind.PUBLIC, "From My IG > Settings."),
            CredentialField("username", "Username", CredentialKind.PUBLIC, "IG login."),
            CredentialField("password", "Password", CredentialKind.SECRET, "IG password."),
            CredentialField(
                "account_type",
                "Account type",
                CredentialKind.PUBLIC,
                "DEMO or LIVE - they are different hosts, and mixing them silently trades the "
                "wrong account.",
            ),
        ),
        notes=(
            "Everything at IG is a CFD or spread bet over the underlying, NOT the underlying: "
            "positions are leveraged, carry overnight financing, and are never delivered. A "
            "strategy measured on cash equities does not transfer to them unchanged."
        ),
        factory=None,
    ),
    "moomoo": BrokerProvider(
        provider="moomoo",
        display_name="moomoo / Futu",
        asset_classes=frozenset({AssetClass.EQUITY, AssetClass.OPTION, AssetClass.FUTURE}),
        order_types=frozenset({"market", "limit"}),
        supports_short=True,
        supports_extended_hours=True,
        supports_fractional=False,
        supports_cancel=True,
        credential_fields=(
            CredentialField(
                "opend_host",
                "OpenD host",
                CredentialKind.PUBLIC,
                "moomoo's API runs through OpenD, a gateway process on your own machine.",
            ),
            CredentialField("opend_port", "OpenD port", CredentialKind.PUBLIC, "Default 11111."),
            CredentialField(
                "trade_password",
                "Trade unlock password",
                CredentialKind.SECRET,
                "Required to unlock trading in each OpenD session.",
            ),
        ),
        notes=(
            "Like IBKR, reached through a local gateway (OpenD) rather than a public REST "
            "endpoint, so a cloud-hosted platform needs that gateway reachable from where the "
            "API runs. That is a deployment problem, not an adapter problem, and it has to be "
            "solved before this is useful unattended."
        ),
        factory=None,
    ),
    "kraken": BrokerProvider(
        provider="kraken",
        display_name="Kraken",
        asset_classes=frozenset({AssetClass.CRYPTO}),
        order_types=frozenset({"market", "limit"}),
        supports_short=False,
        supports_extended_hours=True,
        supports_fractional=True,
        supports_cancel=True,
        credential_fields=_api_key_pair("API key", "Private key"),
        notes=(
            "Spot crypto, 24/7 - so `supports_extended_hours` is true in the trivial sense "
            "that there are no sessions. Margin shorting exists at Kraken but is not declared "
            "until an adapter has been tested against it."
        ),
        factory=None,
    ),
    "binance": BrokerProvider(
        provider="binance",
        display_name="Binance",
        asset_classes=frozenset({AssetClass.CRYPTO}),
        order_types=frozenset({"market", "limit"}),
        supports_short=False,
        supports_extended_hours=True,
        supports_fractional=True,
        supports_cancel=True,
        credential_fields=_api_key_pair("API key", "Secret key"),
        notes=(
            "Spot crypto, 24/7. Availability and the permitted endpoints differ by "
            "jurisdiction (Binance, Binance.US and others are separate venues with separate "
            "keys); the adapter must be told which host it is talking to rather than assume."
        ),
        factory=None,
    ),
}


class UnknownProviderError(Exception):
    """A broker row names a provider this platform has no entry for."""


class AdapterNotImplementedError(Exception):
    """The provider is catalogued but has no adapter. Raised instead of
    returning something that looks like a broker, because a stand-in would
    accept orders nothing would ever execute."""


class BrokerCannotError(Exception):
    """This venue cannot do what the order asks. A refusal BEFORE the
    order is built, naming the venue and the capability."""


def get_provider(provider: str) -> BrokerProvider:
    entry = PROVIDERS.get(provider)
    if entry is None:
        raise UnknownProviderError(
            f"No broker provider named {provider!r}. Known: {', '.join(sorted(PROVIDERS))}."
        )
    return entry


def build_adapter(provider: str, credentials: dict[str, str]) -> object:
    entry = get_provider(provider)
    if entry.factory is None:
        raise AdapterNotImplementedError(
            f"{entry.display_name} is catalogued but has no adapter in this build. Its "
            f"capabilities describe the venue, not this platform's ability to reach it."
        )
    return entry.factory(credentials)


def check_supported(
    provider: str,
    *,
    asset_class: AssetClass,
    order: OrderRequest | None = None,
    opens_short: bool = False,
    extended_hours: bool = False,
) -> None:
    """Refuse, specifically, before an order is built.

    Raises `BrokerCannotError` naming the venue and the exact capability it
    lacks. Every check here is one the venue would otherwise fail at with a
    numeric error code, at which point the operator has to go and look it
    up; the point of doing it here is that the answer is already known.

    `opens_short` is passed by the caller rather than inferred from
    `Side.SELL`, because selling something you hold is not a short and a
    check that conflated them would block every exit.
    """
    entry = get_provider(provider)
    if asset_class not in entry.asset_classes:
        traded = ", ".join(sorted(a.value for a in entry.asset_classes))
        raise BrokerCannotError(
            f"BROKER_CANNOT: {entry.display_name} trades {traded}, not {asset_class.value}."
        )
    if order is not None and order.order_type not in entry.order_types:
        takes = ", ".join(sorted(entry.order_types))
        raise BrokerCannotError(
            f"BROKER_CANNOT: {entry.display_name} takes {takes} orders, "
            f"not {order.order_type}."
        )
    if opens_short and not entry.supports_short:
        raise BrokerCannotError(
            f"BROKER_CANNOT: {entry.display_name} cannot open a short position here."
        )
    if extended_hours and not entry.supports_extended_hours:
        raise BrokerCannotError(
            f"BROKER_CANNOT: {entry.display_name} does not trade outside the regular session."
        )
    if (
        order is not None
        and not entry.supports_fractional
        and order.quantity != order.quantity.to_integral_value()
    ):
        raise BrokerCannotError(
            f"BROKER_CANNOT: {entry.display_name} does not accept a fractional quantity "
            f"({order.quantity}); round it to a whole number of units first."
        )


def opens_short(side: Side, *, held: Decimal, quantity: Decimal) -> bool:
    """True only when the sell goes PAST what is held. Selling ten of ten
    held is an exit; selling twelve of ten is an exit plus a two-unit
    short."""
    return side is Side.SELL and quantity > max(held, Decimal(0))
