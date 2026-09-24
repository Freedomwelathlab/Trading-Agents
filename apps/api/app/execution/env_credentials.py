"""Broker credentials supplied as environment variables (Phase 94, D113).

Phase 90 built one way to give this platform a broker credential: an
operator types it into the admin UI and it is stored, Fernet-encrypted, in
`broker_credentials`. That is the right default — it is per-broker-row,
per-user, and survives nothing being in the environment.

It is also the only way, and that turns out to exclude the deployment this
platform actually runs on. On Railway the operator has env vars and no
shell, no console and no second machine; the encrypted store additionally
needs `BROKER_CREDENTIAL_ENCRYPTION_KEY` set before it will hold anything
at all. So an operator who has put a Kraken key in front of the platform
still cannot reach a venue with it. This module is the second source, and
it is the same shape as the `LONGPORT_*` and `LLM_PROVIDER_*` trios this
codebase has read from the environment since Phase 1.

**One naming rule, derived from the registry, never hand-maintained.**
A field's variable is `{PROVIDER}_{FIELD}` upper-cased — `KRAKEN_API_KEY`,
`IG_PASSWORD`, `BINANCE_API_SECRET`. The field names come from
`PROVIDERS[...].credential_fields`, so a provider that gains a field gains
its variable in the same commit, and there is no second list to fall out
of date. `env_var_names()` exists so the UI and `/health` can TELL the
operator the exact string rather than leave them to infer it.

**Lookup is case-insensitive.** A dashboard lets you type
`Kraken_API_KEY`, POSIX env vars are case-sensitive, and the resulting
failure is invisible: the variable is plainly there in the dashboard and
the platform reports it absent. Matching case-insensitively costs a
lowercased index and removes a whole class of unreproducible support
question. Two variables differing only in case is pathological; if it
happens, the exact-case match wins and the other is ignored.

**A credential set is used whole or not at all** (D015, and the same rule
`save_credentials` enforces for the stored path). If a required field's
variable is absent, this module reports the set incomplete and hands back
nothing — it does not return a partial dict for an adapter to fail on
later with a vendor error code, and it does not fill the gap from the
other source. A set assembled half from the environment and half from the
database was never entered as a whole by anybody, and nobody could then
say which credential a rejected order actually used.

Nothing here decrypts, stores or logs a value. `env_credential_status()`
reports PRESENCE and variable NAMES only, which is what makes it safe for
`/health` and for an API response.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from apps.api.app.execution.registry import PROVIDERS, get_provider


def env_var_name(provider: str, field: str) -> str:
    """The one rule. See the module docstring for why it is not a table.

    `provider` here is really a PREFIX: a provider's own name, or one of
    its `env_aliases` (Phase 97, D116) - `LONGPORT` for Longbridge."""
    return f"{provider}_{field}".upper()


def _families(provider: str) -> tuple[str, ...]:
    """The provider's own prefix first, then its aliases, in order."""
    return (provider, *get_provider(provider).env_aliases)


def env_var_names(provider: str) -> dict[str, str]:
    """`{field_name: ENV_VAR_NAME}` for every field a provider declares,
    required or not — the operator needs the optional ones' names too."""
    entry = get_provider(provider)
    return {f.name: env_var_name(provider, f.name) for f in entry.credential_fields}


@dataclass(frozen=True)
class EnvCredentialStatus:
    """What the environment holds for one provider, in names only."""

    provider: str
    present: tuple[str, ...]
    """Field names whose variable is set and non-empty."""
    missing_required: tuple[str, ...]
    """VARIABLE names, not field names — this is read by an operator who
    is about to go and set them, and the field name alone would make them
    guess at the prefix and the case."""
    complete: bool
    """Every REQUIRED field present. An optional field's absence never
    makes a set incomplete; the adapter's own default covers it."""
    prefix: str = ""
    """The variable family the set was read from (`KRAKEN`, `LONGPORT`...),
    upper-cased, so an operator can see WHICH variables answered."""

    @property
    def any_present(self) -> bool:
        """True when the environment holds SOMETHING for this provider.

        Distinguishes "the operator has not configured this venue" from
        "the operator tried and one variable is missing or misspelled" —
        the second deserves to be reported and the first does not.
        """
        return bool(self.present)


def _index(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Lowercased view of the environment. Exact case wins a collision."""
    source = os.environ if environ is None else environ
    folded: dict[str, str] = {}
    for key, value in source.items():
        lowered = key.lower()
        if lowered not in folded or key == lowered.upper():
            folded[lowered] = value
    return folded


def _family_status(
    provider: str, prefix: str, folded: Mapping[str, str]
) -> EnvCredentialStatus:
    entry = get_provider(provider)
    present: list[str] = []
    missing: list[str] = []
    for field in entry.credential_fields:
        variable = env_var_name(prefix, field.name)
        value = folded.get(variable.lower())
        if value is not None and value.strip():
            present.append(field.name)
        elif field.required:
            missing.append(variable)
    return EnvCredentialStatus(
        provider=provider,
        present=tuple(present),
        missing_required=tuple(missing),
        complete=not missing,
        prefix=prefix.upper(),
    )


def env_credential_status(
    provider: str, environ: Mapping[str, str] | None = None
) -> EnvCredentialStatus:
    """Presence only. Never returns or logs a value.

    With aliases, the FIRST family holding anything decides, complete or
    not. A half-set `LONGBRIDGE_*` is reported as half-set rather than
    quietly bypassed for a complete `LONGPORT_*`: the operator started
    typing the first family, and silently using the other one would leave
    them believing their new values are live when they are not.
    """
    folded = _index(environ)
    statuses = [_family_status(provider, prefix, folded) for prefix in _families(provider)]
    for status in statuses:
        if status.any_present:
            return status
    return statuses[0]


def load_env_credentials(
    provider: str, environ: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    """The credential set from the environment, or None.

    None means "not usable from here", covering both nothing-set and
    partially-set. The caller cannot tell those apart from this return
    value and should not: neither can build an adapter. Use
    `env_credential_status()` when the difference has to be REPORTED.
    """
    status = env_credential_status(provider, environ)
    if not status.complete:
        return None

    entry = get_provider(provider)
    folded = _index(environ)
    credentials: dict[str, str] = {}
    for field in entry.credential_fields:
        value = folded.get(env_var_name(status.prefix, field.name).lower())
        if value is not None and value.strip():
            credentials[field.name] = value.strip()
    return credentials


def configured_providers(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Providers the environment genuinely configures.

    A provider that requires NOTHING (the paper broker) is excluded rather
    than reported complete. Its set is trivially satisfied by an empty
    environment, so listing it would tell an operator that a venue is
    wired up when nobody has supplied anything — the exact false
    reassurance this module exists to remove. `env_credential_status` still
    answers `complete=True` for it, which is the accurate answer to the
    narrower question it is asked.
    """
    return tuple(
        name
        for name in sorted(PROVIDERS)
        if any(f.required for f in get_provider(name).credential_fields)
        and env_credential_status(name, environ).complete
    )


def partial_providers(environ: Mapping[str, str] | None = None) -> dict[str, tuple[str, ...]]:
    """Providers the operator has STARTED and not finished, mapped to the
    variable names still missing.

    Reported separately from the unconfigured majority because this is the
    state that looks configured from a dashboard and is not.
    """
    started: dict[str, tuple[str, ...]] = {}
    for name in sorted(PROVIDERS):
        status = env_credential_status(name, environ)
        if status.any_present and not status.complete:
            started[name] = status.missing_required
    return started
