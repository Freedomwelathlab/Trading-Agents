"""The paper option book: open, close, settle and mark option structures on a
PAPER broker (Phase 102, D122).

Every opening order is priced from the live DELAYED chain
(`options/paper_orders.py`), gated by the deterministic Risk Engine
(`options/risk_gate.py`), and only then written: an `option_orders` row
(filled or refused - always a row once the Risk Engine has been asked), one
`option_fills` row per leg labelled with the quote's source and timestamp,
an `option_structures` row, and the cash movement on the broker's
`broker_accounts` row, which is locked `FOR UPDATE` exactly as the equity
paper broker locks it (D014), so an option fill and an equity fill on the
same broker serialise instead of double-spending.

Refusals that happen BEFORE the Risk Engine is consulted - a live broker, a
malformed or naked structure, a leg with no two-sided market, no chain
provider - raise `OptionOrderError` with a coded reason and write nothing:
there was no decision to record, only an input that could not be priced.

**Paper only.** A `kind=live` broker is refused here as well as at the
route. There is no live option order path in this system; building one is
a separate, separately-gated piece of work (D122).

**Settlement.** An open structure whose expiry has passed (or whose expiry
session has closed) settles at intrinsic from the underlying's stored
daily close for the expiry session - cash-settled, which real equity
options are not (they are physically settled; D119 made the same
simplification for the backtest). No close, no settlement: the structure
stays open and is reported as pending rather than settled at a guessed
price.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.app.core.config import Settings
from apps.api.app.db.models import (
    Broker,
    BrokerAccount,
    BrokerKind,
    BrokerPosition,
    OptionFill,
    OptionOrder,
    OptionOrderStatus,
    OptionStructure,
    OptionStructureStatus,
)
from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionChainProvider
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.store import MarketDataStore
from apps.api.app.options.paper_orders import (
    MULTIPLIER,
    LegSide,
    LegSpec,
    OptionOrderError,
    OptionStructureType,
    PricedLeg,
    StructureEconomics,
    mid_value,
    price_close,
    price_open,
    realized_pnl,
    settlement_value,
    structure_key,
    validate_structure,
)
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options.risk_gate import (
    evaluate_option_close,
    evaluate_option_open,
    option_limits,
)
from apps.api.app.risk.limits import build_risk_limits
from apps.api.app.risk.models import AccountState, RecentOrder, RiskDecision, Side
from apps.api.app.safety.emergency_stop import is_emergency_stop_active

NEW_YORK = ZoneInfo("America/New_York")
SETTLEMENT_FINAL_AFTER_ET = time(16, 15)
"""An expiry-day structure settles only once the expiry session is over.
16:15 ET is after the latest-trading ETF options stop; the close used is the
underlying's stored daily close, which is final after 16:00."""

_ZERO = Decimal(0)
_MARK_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "1d")


def new_york_date(ts: datetime) -> date:
    return ts.astimezone(NEW_YORK).date()


def is_delayed(source: str) -> bool:
    return "delayed" in source


# --- inputs / outputs ----------------------------------------------------------


@dataclass(frozen=True)
class OpenOrderRequest:
    underlying: str
    expiry: date
    structure_type: OptionStructureType
    legs: tuple[LegSpec, ...]
    quantity: int


@dataclass
class OptionOrderOutcome:
    order: OptionOrder
    decision: RiskDecision
    structure: OptionStructure | None = None
    fills: list[OptionFill] = field(default_factory=list)
    economics: StructureEconomics | None = None

    @property
    def filled(self) -> bool:
        return self.order.status is OptionOrderStatus.FILLED


@dataclass
class SettlementResult:
    structure: OptionStructure
    settled: bool
    detail: str


# --- shared helpers ------------------------------------------------------------


def _require_paper(broker: Broker) -> None:
    if broker.kind is not BrokerKind.PAPER:
        raise OptionOrderError(
            "LIVE_NOT_SUPPORTED: option orders are PAPER ONLY in this system. This broker is "
            "a live broker, and no live option order path exists - nothing was sent anywhere "
            "(docs/DECISIONS.md D122)."
        )


async def fetch_chain(
    provider: OptionChainProvider | None, underlying: str, expiry: date
) -> OptionChain:
    if provider is None:
        raise OptionOrderError(
            "NOT_CONFIGURED: no option chain provider is wired, so there is no quote to "
            "price an option order from."
        )
    try:
        return await provider.get_chain(underlying, expiry)
    except DataUnavailableError as exc:
        raise OptionOrderError(f"DATA_UNAVAILABLE: {exc}") from None
    except VendorError as exc:
        raise OptionOrderError(f"VENDOR_ERROR: {exc}") from None


async def lock_account(
    session: AsyncSession, broker_id: uuid.UUID, *, default_starting_cash: Decimal
) -> BrokerAccount:
    """The broker's cash row, locked for the rest of the transaction and
    lazily created exactly as `execution/persistence.load_paper_broker`
    creates it."""
    account = (
        await session.execute(
            select(BrokerAccount).where(BrokerAccount.broker_id == broker_id).with_for_update()
        )
    ).scalar_one_or_none()
    if account is None:
        account = BrokerAccount(broker_id=broker_id, cash=default_starting_cash)
        session.add(account)
        await session.flush()
    return account


async def open_structures(session: AsyncSession, broker_id: uuid.UUID) -> list[OptionStructure]:
    return list(
        (
            await session.execute(
                select(OptionStructure)
                .where(
                    OptionStructure.broker_id == broker_id,
                    OptionStructure.status == OptionStructureStatus.OPEN,
                )
                .order_by(OptionStructure.opened_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def _latest_mark(store: MarketDataStore, symbol: str) -> Decimal | None:
    best = None
    for interval in _MARK_INTERVALS:
        bars = await store.get_latest_bars(symbol, bar_interval=interval, count=1)
        if bars and (best is None or bars[-1].ts > best.ts):
            best = bars[-1]
    return best.close if best is not None else None


async def account_state(
    session: AsyncSession, broker_id: uuid.UUID, account: BrokerAccount
) -> AccountState:
    """Cash, plus capital reserved by open option structures (at cost - the
    structures are not re-marked here), plus any equity positions on the same
    broker marked at their latest stored bar. An equity position with no
    stored bar makes the account unvaluable: refused, not guessed."""
    reserved = sum((s.capital_reserved for s in await open_structures(session, broker_id)), _ZERO)
    positions = (
        (await session.execute(select(BrokerPosition).where(BrokerPosition.broker_id == broker_id)))
        .scalars()
        .all()
    )
    store = MarketDataStore(session)
    value = _ZERO
    exposure = _ZERO
    for pos in positions:
        if pos.quantity == 0:
            continue
        mark = await _latest_mark(store, pos.symbol)
        if mark is None:
            raise OptionOrderError(
                f"DATA_UNAVAILABLE: this broker also holds {pos.symbol}, which has no stored "
                "bar to value it at, so the account's equity cannot be computed and no price "
                "was fabricated."
            )
        value += pos.quantity * mark
        exposure += abs(pos.quantity) * mark
    equity = account.cash + reserved + value
    if equity <= 0:
        raise OptionOrderError("DATA_UNAVAILABLE: the paper account has no positive equity.")
    return AccountState(
        equity=equity, cash=max(account.cash, _ZERO), current_exposure=reserved + exposure
    )


async def _recent_filled_opens(
    session: AsyncSession, broker_id: uuid.UUID, *, now: datetime, window_seconds: int
) -> list[RecentOrder]:
    rows = (
        (
            await session.execute(
                select(OptionOrder).where(
                    OptionOrder.broker_id == broker_id,
                    OptionOrder.action == "open",
                    OptionOrder.status == OptionOrderStatus.FILLED,
                    OptionOrder.created_at >= now - timedelta(seconds=window_seconds),
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        RecentOrder(
            symbol=r.structure_key,
            side=Side.BUY,
            quantity=Decimal(r.quantity),
            estimated_price=r.max_loss_per_contract,
            submitted_at=r.created_at,
        )
        for r in rows
        if r.max_loss_per_contract is not None and r.max_loss_per_contract > 0
    ]


def _leg_json(leg: PricedLeg) -> dict[str, Any]:
    return {
        "contract_symbol": leg.contract_symbol,
        "right": leg.right.value,
        "strike": str(leg.strike),
        "side": leg.side.value,
        "entry_fill": str(leg.fill_price),
        "entry_delta": str(leg.delta) if leg.delta is not None else None,
        "exit_fill": None,
    }


def _fill_rows(
    order_id: uuid.UUID,
    legs: list[PricedLeg],
    *,
    expiry: date,
    quantity: int,
    chain: OptionChain,
    now: datetime,
) -> list[OptionFill]:
    return [
        OptionFill(
            id=uuid.uuid4(),
            order_id=order_id,
            contract_symbol=leg.contract_symbol,
            right=leg.right.value,
            strike=leg.strike,
            expiry=expiry,
            side=leg.side.value,
            quantity=quantity,
            bid=leg.bid,
            ask=leg.ask,
            mid=leg.mid,
            fill_price=leg.fill_price,
            quote_source=chain.source,
            quote_as_of=chain.as_of,
            filled_at=now,
        )
        for leg in legs
    ]


# --- open ----------------------------------------------------------------------


async def submit_open(
    session: AsyncSession,
    *,
    broker: Broker,
    request: OpenOrderRequest,
    provider: OptionChainProvider | None,
    settings: Settings,
    now: datetime,
    submitted_by_user_id: uuid.UUID | None,
    options_bot_id: uuid.UUID | None = None,
    options_bot_run_id: uuid.UUID | None = None,
    chain: OptionChain | None = None,
) -> OptionOrderOutcome:
    """Price, gate and (if approved) fill one opening option order. The
    caller owns the transaction and commits it."""
    _require_paper(broker)
    if request.quantity < 1:
        raise OptionOrderError("BAD_QUANTITY: an option order needs at least one contract.")
    validate_structure(request.structure_type, request.legs)
    if request.expiry < new_york_date(now):
        raise OptionOrderError(
            f"DATA_UNAVAILABLE: {request.underlying} {request.expiry.isoformat()} has already "
            "expired."
        )
    if chain is None:
        chain = await fetch_chain(provider, request.underlying, request.expiry)
    k = settings.options_paper_fill_haircut_k
    econ = price_open(request.structure_type, request.legs, chain, k=k)

    account = await lock_account(
        session, broker.id, default_starting_cash=settings.paper_broker_starting_cash
    )
    state = await account_state(session, broker.id, account)
    limits = option_limits(
        build_risk_limits(settings, live=False),
        max_quote_age_seconds=settings.options_max_quote_age_seconds,
    )
    key = structure_key(request.underlying, request.structure_type, request.expiry, request.legs)
    decision = evaluate_option_open(
        structure_key=key,
        quantity=request.quantity,
        max_loss_per_contract=econ.max_loss_per_contract,
        account=state,
        limits=limits,
        emergency_stop_active=await is_emergency_stop_active(
            session, settings_default=settings.emergency_stop_active
        ),
        now=now,
        quote_as_of=chain.as_of,
        recent_orders=await _recent_filled_opens(
            session,
            broker.id,
            now=now,
            window_seconds=settings.risk_duplicate_order_window_seconds,
        ),
    )

    capital = econ.capital_per_contract * request.quantity
    order = OptionOrder(
        id=uuid.uuid4(),
        broker_id=broker.id,
        options_bot_run_id=options_bot_run_id,
        submitted_by_user_id=submitted_by_user_id,
        action="open",
        structure_type=request.structure_type.value,
        structure_key=key,
        underlying=request.underlying,
        expiry=request.expiry,
        quantity=request.quantity,
        status=OptionOrderStatus.FILLED if decision.approved else OptionOrderStatus.REJECTED,
        block_reason=decision.reason.value if decision.reason else None,
        detail=decision.detail,
        net_price=econ.net_price,
        cash_change=-capital if decision.approved else None,
        max_loss_per_contract=econ.max_loss_per_contract,
        quote_source=chain.source,
        quote_as_of=chain.as_of,
        fill_haircut_k=k,
        created_at=now,
    )
    outcome = OptionOrderOutcome(order=order, decision=decision, economics=econ)
    if not decision.approved:
        session.add(order)
        await session.flush()
        return outcome

    structure = OptionStructure(
        id=uuid.uuid4(),
        broker_id=broker.id,
        options_bot_id=options_bot_id,
        structure_type=request.structure_type.value,
        underlying=request.underlying,
        expiry=request.expiry,
        quantity=request.quantity,
        legs=[_leg_json(leg) for leg in econ.legs],
        status=OptionStructureStatus.OPEN,
        entry_net_price=econ.net_price,
        max_loss=capital,
        max_profit=(
            econ.max_profit_per_contract * request.quantity
            if econ.max_profit_per_contract is not None
            else None
        ),
        capital_reserved=capital,
        quote_source=chain.source,
        quote_as_of=chain.as_of,
        opened_at=now,
    )
    session.add(structure)
    await session.flush()
    order.structure_id = structure.id
    session.add(order)
    await session.flush()
    fills = _fill_rows(
        order.id, list(econ.legs), expiry=request.expiry, quantity=request.quantity,
        chain=chain, now=now,
    )
    session.add_all(fills)
    account.cash = account.cash - capital
    await session.flush()
    outcome.structure = structure
    outcome.fills = fills
    return outcome


# --- close ---------------------------------------------------------------------


async def submit_close(
    session: AsyncSession,
    *,
    broker: Broker,
    structure_id: uuid.UUID,
    provider: OptionChainProvider | None,
    settings: Settings,
    now: datetime,
    submitted_by_user_id: uuid.UUID | None,
    reason: str = "operator",
    options_bot_run_id: uuid.UUID | None = None,
    chain: OptionChain | None = None,
) -> OptionOrderOutcome:
    """Close a whole open structure at the delayed quote (each leg on the
    opposite side of its entry, same haircut). Closing reduces risk, so
    only the emergency stop and the data-freshness gate apply."""
    _require_paper(broker)
    structure = await session.get(OptionStructure, structure_id, with_for_update=True)
    if structure is None or structure.broker_id != broker.id:
        raise OptionOrderError(f"NO_SUCH_STRUCTURE: no option position {structure_id} here.")
    if structure.status is not OptionStructureStatus.OPEN:
        raise OptionOrderError(
            f"NOT_OPEN: option position {structure_id} is {structure.status.value}."
        )
    if structure.expiry < new_york_date(now):
        raise OptionOrderError(
            "EXPIRED: this position's expiry has passed; it settles at intrinsic from the "
            "underlying's close instead of trading (POST .../option-positions/settle)."
        )
    if chain is None:
        chain = await fetch_chain(provider, structure.underlying, structure.expiry)
    k = settings.options_paper_fill_haircut_k
    priced, exit_value = price_close(structure.legs, chain, k=k)

    account = await lock_account(
        session, broker.id, default_starting_cash=settings.paper_broker_starting_cash
    )
    limits = option_limits(
        build_risk_limits(settings, live=False),
        max_quote_age_seconds=settings.options_max_quote_age_seconds,
    )
    decision = evaluate_option_close(
        limits=limits,
        emergency_stop_active=await is_emergency_stop_active(
            session, settings_default=settings.emergency_stop_active
        ),
        now=now,
        quote_as_of=chain.as_of,
    )
    pnl = realized_pnl(
        entry_net_price=structure.entry_net_price, exit_value=exit_value,
        quantity=structure.quantity,
    )
    cash_change = structure.capital_reserved + pnl
    legs = [
        LegSpec(right=OptionRight(raw["right"]), strike=Decimal(str(raw["strike"])),
                side=LegSide(raw["side"]))
        for raw in structure.legs
    ]
    order = OptionOrder(
        id=uuid.uuid4(),
        broker_id=broker.id,
        structure_id=structure.id,
        options_bot_run_id=options_bot_run_id,
        submitted_by_user_id=submitted_by_user_id,
        action="close",
        structure_type=structure.structure_type,
        structure_key=structure_key(
            structure.underlying, structure.structure_type, structure.expiry, legs
        ),
        underlying=structure.underlying,
        expiry=structure.expiry,
        quantity=structure.quantity,
        status=OptionOrderStatus.FILLED if decision.approved else OptionOrderStatus.REJECTED,
        block_reason=decision.reason.value if decision.reason else None,
        detail=decision.detail,
        net_price=-exit_value,
        cash_change=cash_change if decision.approved else None,
        max_loss_per_contract=None,
        quote_source=chain.source,
        quote_as_of=chain.as_of,
        fill_haircut_k=k,
        created_at=now,
    )
    session.add(order)
    await session.flush()
    outcome = OptionOrderOutcome(order=order, decision=decision, structure=structure)
    if not decision.approved:
        return outcome

    fills = _fill_rows(
        order.id, priced, expiry=structure.expiry, quantity=structure.quantity,
        chain=chain, now=now,
    )
    session.add_all(fills)
    new_legs = []
    for raw, leg in zip(structure.legs, priced, strict=True):
        updated = dict(raw)
        updated["exit_fill"] = str(leg.fill_price)
        new_legs.append(updated)
    structure.legs = new_legs
    structure.status = OptionStructureStatus.CLOSED
    structure.closed_at = now
    structure.exit_net_price = exit_value
    structure.realized_pnl = pnl
    structure.close_reason = reason
    account.cash = account.cash + cash_change
    await session.flush()
    outcome.fills = fills
    return outcome


# --- settlement ----------------------------------------------------------------


def settlement_due(expiry: date, now: datetime) -> bool:
    local = now.astimezone(NEW_YORK)
    if local.date() > expiry:
        return True
    return local.date() == expiry and local.time() >= SETTLEMENT_FINAL_AFTER_ET


async def underlying_close_on(
    store: MarketDataStore, underlying: str, day: date, *, bar_router: Any | None = None
) -> Decimal | None:
    """The underlying's stored daily close for the New York session `day`,
    optionally fetched from the bar vendor first when it is missing. None
    when neither has it - never an adjacent day's close, never a quote."""

    async def _stored() -> Decimal | None:
        bars = await store.get_bars(
            underlying, bar_interval="1d", start_date=day - timedelta(days=1),
            end_date=day + timedelta(days=2),
        )
        for bar in bars:
            if new_york_date(bar.ts) == day and bar.close is not None and bar.close > 0:
                return bar.close
        return None

    close = await _stored()
    if close is not None or bar_router is None:
        return close
    try:
        if "longbridge" not in getattr(bar_router, "configured_vendors", ()):
            return None
        fetched = await bar_router.get_bars(
            underlying, bar_interval="1d", start_date=day, end_date=day + timedelta(days=1)
        )
    except Exception:  # noqa: BLE001 - a vendor failure leaves the position pending
        return None
    if fetched:
        await store.upsert_bars(fetched)
        return await _stored()
    return None


async def settle_expired(
    session: AsyncSession,
    *,
    broker_id: uuid.UUID,
    settings: Settings,
    now: datetime,
    bar_router: Any | None = None,
    only_bot_id: uuid.UUID | None = None,
) -> list[SettlementResult]:
    """Settle every open structure on this broker whose expiry session is
    over, at intrinsic from the underlying's close. A structure with no
    close available stays open and is reported pending."""
    due = [s for s in await open_structures(session, broker_id) if settlement_due(s.expiry, now)]
    if only_bot_id is not None:
        due = [s for s in due if s.options_bot_id == only_bot_id]
    if not due:
        return []
    account = await lock_account(
        session, broker_id, default_starting_cash=settings.paper_broker_starting_cash
    )
    store = MarketDataStore(session)
    results: list[SettlementResult] = []
    closes: dict[tuple[str, date], Decimal | None] = {}
    for structure in due:
        key = (structure.underlying, structure.expiry)
        if key not in closes:
            closes[key] = await underlying_close_on(
                store, structure.underlying, structure.expiry, bar_router=bar_router
            )
        close = closes[key]
        if close is None:
            results.append(
                SettlementResult(
                    structure, False,
                    f"SETTLEMENT_PENDING: no stored {structure.underlying} daily close for "
                    f"{structure.expiry.isoformat()}; the position stays open until one is "
                    "ingested - no settlement price was guessed.",
                )
            )
            continue
        value = settlement_value(structure.legs, close)
        pnl = realized_pnl(
            entry_net_price=structure.entry_net_price, exit_value=value,
            quantity=structure.quantity,
        )
        structure.status = OptionStructureStatus.SETTLED
        structure.closed_at = now
        structure.exit_net_price = value
        structure.settlement_underlying_price = close
        structure.realized_pnl = pnl
        structure.close_reason = "expiry"
        account.cash = account.cash + structure.capital_reserved + pnl
        results.append(
            SettlementResult(
                structure, True,
                f"settled at intrinsic from {structure.underlying} close {close} on "
                f"{structure.expiry.isoformat()}: P&L {pnl}",
            )
        )
    await session.flush()
    return results


# --- marks ---------------------------------------------------------------------


@dataclass
class StructureMark:
    structure: OptionStructure
    mid_value: Decimal | None
    """Per share at the quote mids, signed like entry."""
    close_value: Decimal | None
    """Per share if closed now at the modelled haircut fill."""
    unrealized_pnl_mid: Decimal | None
    unrealized_pnl_close: Decimal | None
    quote_source: str | None
    quote_as_of: datetime | None
    note: str | None


@dataclass
class ContractPosition:
    contract_symbol: str
    underlying: str
    expiry: date
    right: str
    strike: Decimal
    quantity: int
    """Signed contracts: + long, - short."""
    avg_entry_price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    mid: Decimal | None
    quote_source: str | None
    quote_as_of: datetime | None


@dataclass
class BookView:
    cash: Decimal | None
    capital_reserved: Decimal
    structures: list[StructureMark]
    positions: list[ContractPosition]


async def book_view(
    session: AsyncSession,
    broker_id: uuid.UUID,
    *,
    provider: OptionChainProvider | None,
    now: datetime,
    k: Decimal,
) -> BookView:
    """Open structures with marks, and the per-contract positions they add
    up to. Read-only. A structure whose chain cannot be read, or one of
    whose legs has no two-sided quote, is shown UNMARKED with the reason."""
    account = (
        await session.execute(select(BrokerAccount).where(BrokerAccount.broker_id == broker_id))
    ).scalar_one_or_none()
    structures = await open_structures(session, broker_id)

    chains: dict[tuple[str, date], OptionChain | str] = {}
    for s in structures:
        key = (s.underlying, s.expiry)
        if key in chains:
            continue
        if s.expiry < new_york_date(now):
            chains[key] = "expired: awaiting settlement at intrinsic from the underlying close"
            continue
        try:
            chains[key] = await fetch_chain(provider, s.underlying, s.expiry)
        except OptionOrderError as exc:
            chains[key] = str(exc)

    marks: list[StructureMark] = []
    agg: dict[str, dict[str, Any]] = defaultdict(dict)
    for s in structures:
        chain = chains[(s.underlying, s.expiry)]
        mv = cv = pnl_mid = pnl_close = None
        note: str | None = None
        src = as_of = None
        if isinstance(chain, str):
            note = chain
        else:
            src, as_of = chain.source, chain.as_of
            mv = mid_value(s.legs, chain)
            try:
                _, cv = price_close(s.legs, chain, k=k)
            except OptionOrderError as exc:
                cv = None
                note = str(exc)
            if mv is None and note is None:
                note = "DATA_UNAVAILABLE: a leg has no two-sided quote; not marked"
            if mv is not None:
                pnl_mid = realized_pnl(
                    entry_net_price=s.entry_net_price, exit_value=mv, quantity=s.quantity
                )
            if cv is not None:
                pnl_close = realized_pnl(
                    entry_net_price=s.entry_net_price, exit_value=cv, quantity=s.quantity
                )
        marks.append(
            StructureMark(
                structure=s, mid_value=mv, close_value=cv, unrealized_pnl_mid=pnl_mid,
                unrealized_pnl_close=pnl_close, quote_source=src, quote_as_of=as_of, note=note,
            )
        )
        for raw in s.legs:
            sym = raw["contract_symbol"]
            sign = 1 if raw["side"] == "buy" else -1
            entry = agg[sym]
            entry.setdefault("underlying", s.underlying)
            entry.setdefault("expiry", s.expiry)
            entry.setdefault("right", raw["right"])
            entry.setdefault("strike", Decimal(str(raw["strike"])))
            entry["qty"] = entry.get("qty", 0) + sign * s.quantity
            entry["cost"] = entry.get("cost", _ZERO) + Decimal(str(raw["entry_fill"])) * s.quantity
            entry["abs_qty"] = entry.get("abs_qty", 0) + s.quantity
            if not isinstance(chain, str):
                entry["chain"] = chain

    positions: list[ContractPosition] = []
    for sym, e in sorted(agg.items()):
        bid = ask = mid = None
        src = as_of = None
        pchain = e.get("chain")
        if isinstance(pchain, OptionChain):
            src, as_of = pchain.source, pchain.as_of
            for q in pchain.quotes:
                if q.contract_symbol == sym or (
                    q.right.value == e["right"] and q.strike == e["strike"]
                ):
                    bid, ask, mid = q.bid, q.ask, q.mid
                    break
        positions.append(
            ContractPosition(
                contract_symbol=sym, underlying=e["underlying"], expiry=e["expiry"],
                right=e["right"], strike=e["strike"], quantity=e["qty"],
                avg_entry_price=(e["cost"] / e["abs_qty"]).quantize(Decimal("0.0001")),
                bid=bid, ask=ask, mid=mid, quote_source=src, quote_as_of=as_of,
            )
        )
    return BookView(
        cash=account.cash if account is not None else None,
        capital_reserved=sum((s.capital_reserved for s in structures), _ZERO),
        structures=marks,
        positions=[p for p in positions if p.quantity != 0],
    )


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "MULTIPLIER",
    "BookView",
    "OpenOrderRequest",
    "OptionOrderError",
    "OptionOrderOutcome",
    "SettlementResult",
    "book_view",
    "fetch_chain",
    "lock_account",
    "settle_expired",
    "submit_close",
    "submit_open",
]
