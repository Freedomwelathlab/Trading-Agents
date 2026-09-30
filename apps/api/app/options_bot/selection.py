"""How the options bot turns its rules into legs on a real chain (Phase 102,
D122). Pure: no I/O.

- **Expiry**: the NEAREST listed expiry whose calendar DTE (New York date)
  is inside the bot's band. None in the band -> no entry, said why.
- **Anchor strike**: the contract whose VENDOR delta magnitude is closest to
  the bot's target - the bought leg of a long option or debit spread, the
  sold leg(s) of a credit spread or iron condor. A quote with no delta or
  no two-sided market is not a candidate; Greeks are never computed here to
  fill a gap (the D108 rule).
- **Wing**: the two-sided strike nearest `anchor +/- spread_width` on the
  far side of the anchor.
- **Liquidity**: every leg must pass the playbook's own filter
  (`options/selection.py::check_liquidity`: a two-sided market, open
  interest >= 100, spread within 10% of mid for one leg / 5% per leg for a
  multi-leg structure). A contract that fails is not traded - the reason is
  returned instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionQuote
from apps.api.app.options.paper_orders import (
    LegSide,
    LegSpec,
    OptionStructureType,
    two_sided,
)
from apps.api.app.options.pricing import OptionRight
from apps.api.app.options.selection import check_liquidity


class SelectionRefused(Exception):
    """The chain cannot express the bot's rules right now. Not an error in
    the bot - a reason it did not trade, recorded on the run row."""


@dataclass(frozen=True)
class Candidate:
    legs: tuple[LegSpec, ...]
    anchor_delta: Decimal | None


def pick_expiry(expiries: Sequence[date], *, today: date, dte_min: int, dte_max: int) -> date:
    inside = sorted(e for e in expiries if dte_min <= (e - today).days <= dte_max)
    if not inside:
        raise SelectionRefused(
            f"no listed expiry within {dte_min}-{dte_max} DTE "
            f"(listed: {', '.join(e.isoformat() for e in sorted(expiries)[:6]) or 'none'})"
        )
    return inside[0]


def _tradeable(chain: OptionChain, right: OptionRight) -> list[OptionQuote]:
    return [q for q in chain.by_right(right) if two_sided(q) is not None]


def _anchor(chain: OptionChain, right: OptionRight, target: Decimal) -> OptionQuote:
    candidates = [q for q in _tradeable(chain, right) if q.delta is not None]
    if not candidates:
        raise SelectionRefused(
            f"no two-sided {right.value} with a vendor delta on {chain.expiry.isoformat()}"
        )
    return min(candidates, key=lambda q: (abs(abs(q.delta) - target), q.strike))  # type: ignore[arg-type]


def _wing(chain: OptionChain, right: OptionRight, anchor: Decimal, width: Decimal,
          *, above: bool) -> OptionQuote:
    target = anchor + width if above else anchor - width
    beyond = [
        q for q in _tradeable(chain, right)
        if (q.strike > anchor if above else q.strike < anchor)
    ]
    if not beyond:
        raise SelectionRefused(
            f"no two-sided {right.value} {'above' if above else 'below'} {anchor} for the wing"
        )
    return min(beyond, key=lambda q: (abs(q.strike - target), q.strike))


def _liquid(quotes: Sequence[OptionQuote]) -> None:
    multi = len(quotes) > 1
    for q in quotes:
        assert q.bid is not None and q.ask is not None  # two-sided by construction
        check = check_liquidity(
            bid=q.bid, ask=q.ask, open_interest=q.open_interest or 0, is_multi_leg=multi
        )
        if not check.passed:
            raise SelectionRefused(
                f"{q.contract_symbol} fails the liquidity filter: {check.reason}"
            )


def build_candidate(
    structure: OptionStructureType,
    chain: OptionChain,
    *,
    target_delta: Decimal,
    spread_width: Decimal | None,
) -> Candidate:
    call, put = OptionRight.CALL, OptionRight.PUT
    buy, sell = LegSide.BUY, LegSide.SELL
    width = spread_width or Decimal(0)
    s = structure
    if s is OptionStructureType.LONG_CALL:
        a = _anchor(chain, call, target_delta)
        quotes, legs = [a], [LegSpec(call, a.strike, buy)]
    elif s is OptionStructureType.LONG_PUT:
        a = _anchor(chain, put, target_delta)
        quotes, legs = [a], [LegSpec(put, a.strike, buy)]
    elif s is OptionStructureType.BULL_CALL:
        a = _anchor(chain, call, target_delta)
        w = _wing(chain, call, a.strike, width, above=True)
        quotes, legs = [a, w], [LegSpec(call, a.strike, buy), LegSpec(call, w.strike, sell)]
    elif s is OptionStructureType.BEAR_PUT:
        a = _anchor(chain, put, target_delta)
        w = _wing(chain, put, a.strike, width, above=False)
        quotes, legs = [a, w], [LegSpec(put, a.strike, buy), LegSpec(put, w.strike, sell)]
    elif s is OptionStructureType.BULL_PUT:
        a = _anchor(chain, put, target_delta)
        w = _wing(chain, put, a.strike, width, above=False)
        quotes, legs = [a, w], [LegSpec(put, a.strike, sell), LegSpec(put, w.strike, buy)]
    elif s is OptionStructureType.BEAR_CALL:
        a = _anchor(chain, call, target_delta)
        w = _wing(chain, call, a.strike, width, above=True)
        quotes, legs = [a, w], [LegSpec(call, a.strike, sell), LegSpec(call, w.strike, buy)]
    elif s is OptionStructureType.IRON_CONDOR:
        sp = _anchor(chain, put, target_delta)
        sc = _anchor(chain, call, target_delta)
        if not sp.strike < sc.strike:
            raise SelectionRefused(
                f"the {target_delta} delta put ({sp.strike}) is not below the call ({sc.strike})"
            )
        lp = _wing(chain, put, sp.strike, width, above=False)
        lc = _wing(chain, call, sc.strike, width, above=True)
        quotes = [lp, sp, sc, lc]
        legs = [
            LegSpec(put, lp.strike, buy), LegSpec(put, sp.strike, sell),
            LegSpec(call, sc.strike, sell), LegSpec(call, lc.strike, buy),
        ]
        a = sp
    else:
        raise SelectionRefused(f"{s.value} is not a bot structure")
    _liquid(quotes)
    return Candidate(legs=tuple(legs), anchor_delta=a.delta)
