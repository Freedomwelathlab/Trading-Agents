"""The `StrategyDeployment` lifecycle state machine (Phase 63).

Every transition is an explicit, separately-authorized action - there is no
code path that produces an `ACTIVE` deployment without a human calling
`approve_deployment`. That is the mandatory human gate spec §25/§52 and
docs/TRADING_SAFETY.md require before a strategy trades, even in paper mode,
expressed as a state machine rather than a comment.

These functions take a session and DO NOT commit - the caller (the route)
owns the transaction, matching `safety/emergency_stop.py::set_emergency_stop`
and every other write-composition in this codebase.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.db.models import (
    Broker,
    BrokerKind,
    StrategyDeployment,
    StrategyDeploymentStatus,
    StrategyVersion,
    StrategyVersionStatus,
)

MAX_DEPLOYMENT_SYMBOLS = 50
"""The same order of magnitude as a universe scan's cap and for the same
reason: a runner cycle does one indexed bar read plus an in-memory
evaluation per symbol, and possibly one paper order. It stays synchronous
per cycle; a far larger universe is a different design."""


class DeploymentError(Exception):
    """A lifecycle transition that is not allowed from the current state, or
    a request the runner could never satisfy. The route turns this into a
    409/422 with the message verbatim - it is always a plain, safe string.
    """


def _normalize_symbols(raw: Sequence[str]) -> list[str]:
    """Trim, upper-case, drop blanks, de-dupe preserving first-seen order -
    `universe_scan.normalize_symbols`' exact rule. The persisted list is
    what the runner hands the bar store, so `"aapl"` and `"AAPL "` must not
    become two rows for one market."""
    seen: dict[str, None] = {}
    for item in raw:
        token = item.strip().upper()
        if token and token not in seen:
            seen[token] = None
    return list(seen)


async def create_deployment(
    session: AsyncSession,
    *,
    strategy_version_id: uuid.UUID,
    broker_id: uuid.UUID,
    symbols: Sequence[str],
    bar_interval: str,
    mode: str,
    requested_by_user_id: uuid.UUID | None,
) -> StrategyDeployment:
    """Create a deployment in `PENDING_APPROVAL`. Never `ACTIVE` - approval
    is a separate action.

    Rejects, before any row is written:
    - a mode that is neither `paper` nor `live` (Phase 64, D082);
    - a strategy version that is not `VALIDATED` (the rules must be frozen
      and structurally sound before anything trades them);
    - a broker id that does not exist, or one whose kind does not match the
      requested mode (a `paper` deployment needs a `PAPER` broker, a `live`
      deployment needs a `LIVE` broker - never mixed);
    - an empty or over-cap symbol list.

    A `live` mode row is real and approvable, but the runner never places a
    live order for it (`StrategyDeploymentRunStatus.
    SKIPPED_LIVE_TRADING_DISABLED`) - this function's job is only to keep
    the data honest (a live deployment names a live broker), not to gate
    execution; that gate lives in the runner, unconditionally.
    """
    if mode not in ("paper", "live"):
        raise DeploymentError(
            f"UNSUPPORTED_MODE: mode must be 'paper' or 'live', got {mode!r}."
        )

    version = (
        await session.execute(
            select(StrategyVersion).where(StrategyVersion.id == strategy_version_id)
        )
    ).scalar_one_or_none()
    if version is None:
        raise DeploymentError(f"NO_SUCH_VERSION: no strategy version {strategy_version_id}.")
    if version.status is not StrategyVersionStatus.VALIDATED:
        raise DeploymentError(
            f"VERSION_NOT_VALIDATED: version {strategy_version_id} is "
            f"{version.status.value}; only a validated version can be deployed."
        )

    broker = (
        await session.execute(select(Broker).where(Broker.id == broker_id))
    ).scalar_one_or_none()
    if broker is None:
        raise DeploymentError(f"NO_SUCH_BROKER: no broker {broker_id}.")
    required_kind = BrokerKind.PAPER if mode == "paper" else BrokerKind.LIVE
    if broker.kind is not required_kind:
        error_code = "NOT_A_PAPER_BROKER" if mode == "paper" else "NOT_A_LIVE_BROKER"
        raise DeploymentError(
            f"{error_code}: broker {broker_id} is kind '{broker.kind.value}'. A "
            f"{mode} deployment must trade a {required_kind.value} broker (spec §51 "
            f"keeps the two apart)."
        )

    normalized = _normalize_symbols(symbols)
    if not normalized:
        raise DeploymentError("EMPTY_SYMBOL_LIST: a deployment must name at least one symbol.")
    if len(normalized) > MAX_DEPLOYMENT_SYMBOLS:
        raise DeploymentError(
            f"TOO_MANY_SYMBOLS: {len(normalized)} symbols, limit is {MAX_DEPLOYMENT_SYMBOLS}."
        )

    deployment = StrategyDeployment(
        id=uuid.uuid4(),
        strategy_version_id=strategy_version_id,
        broker_id=broker_id,
        mode=mode,
        status=StrategyDeploymentStatus.PENDING_APPROVAL,
        symbols=normalized,
        bar_interval=bar_interval,
        requested_by_user_id=requested_by_user_id,
    )
    session.add(deployment)
    await session.flush()
    await session.refresh(deployment)
    return deployment


def _touch(deployment: StrategyDeployment) -> None:
    deployment.updated_at = datetime.now(UTC)


async def approve_deployment(
    deployment: StrategyDeployment, *, approved_by_user_id: uuid.UUID | None
) -> StrategyDeployment:
    """The mandatory human gate. `PENDING_APPROVAL` -> `ACTIVE`, recording
    who approved and when. Any other starting state is a 409.

    This function does not itself distinguish `paper` from `live` - the
    route layer requires the caller to also hold
    `STRATEGY_APPROVE_LIVE_DEPLOYMENT` before calling this for a `live`
    deployment (Phase 64, D082)."""
    if deployment.status is not StrategyDeploymentStatus.PENDING_APPROVAL:
        raise DeploymentError(
            f"NOT_PENDING_APPROVAL: deployment is {deployment.status.value}; only a "
            f"pending-approval deployment can be approved."
        )
    deployment.status = StrategyDeploymentStatus.ACTIVE
    deployment.approved_by_user_id = approved_by_user_id
    deployment.approved_at = datetime.now(UTC)
    _touch(deployment)
    return deployment


async def pause_deployment(
    deployment: StrategyDeployment, *, reason: str | None
) -> StrategyDeployment:
    """`ACTIVE` -> `PAUSED`. The runner then skips it (writing a
    `SKIPPED_NOT_ACTIVE` run row) until it is resumed."""
    if deployment.status is not StrategyDeploymentStatus.ACTIVE:
        raise DeploymentError(
            f"NOT_ACTIVE: deployment is {deployment.status.value}; only an active "
            f"deployment can be paused."
        )
    deployment.status = StrategyDeploymentStatus.PAUSED
    deployment.paused_reason = reason
    _touch(deployment)
    return deployment


async def resume_deployment(deployment: StrategyDeployment) -> StrategyDeployment:
    """`PAUSED` -> `ACTIVE`. Does not re-run approval: the deployment was
    already approved once and pausing is an operational step, not a
    withdrawal of that approval."""
    if deployment.status is not StrategyDeploymentStatus.PAUSED:
        raise DeploymentError(
            f"NOT_PAUSED: deployment is {deployment.status.value}; only a paused "
            f"deployment can be resumed."
        )
    deployment.status = StrategyDeploymentStatus.ACTIVE
    deployment.paused_reason = None
    _touch(deployment)
    return deployment


async def stop_deployment(deployment: StrategyDeployment) -> StrategyDeployment:
    """Any non-terminal state -> `STOPPED` (terminal). Re-running the
    strategy means a new deployment through approval again. Deliberately does
    NOT close open positions - a stopped deployment's paper positions stay in
    the broker account exactly as they are; unwinding them is a separate,
    deliberate act, not a side effect of stopping the automation."""
    if deployment.status is StrategyDeploymentStatus.STOPPED:
        raise DeploymentError("ALREADY_STOPPED: deployment is already stopped.")
    deployment.status = StrategyDeploymentStatus.STOPPED
    deployment.stopped_at = datetime.now(UTC)
    _touch(deployment)
    return deployment
