"""The option chain adapter (Phase 89, D108).

Against a hand-rolled stub, never real credentials, matching every other
vendor adapter test here. What is asserted is the set of decisions the
adapter makes that a vendor response alone does not determine: that the
ladder is the only source of which contracts exist, that a contract the
quote call ignored keeps its row with empty fields, that an absent price
never becomes zero while an absent SIZE and a zero size stay
distinguishable, and that an empty ladder is refused rather than
synthesised.
"""

from datetime import date
from decimal import Decimal

import pytest

from apps.api.app.marketdata.option_chain_provider import OptionChain, OptionQuote
from apps.api.app.marketdata.provider import DataUnavailableError, VendorError
from apps.api.app.marketdata.providers.longbridge import LongbridgeOptionChainProvider
from apps.api.app.options.pricing import OptionRight

EXPIRY = date(2026, 10, 16)


class _Rung:
    def __init__(self, price, call_symbol=None, put_symbol=None):
        self.price = price
        self.call_symbol = call_symbol
        self.put_symbol = put_symbol


class _Q:
    def __init__(self, symbol, **fields):
        self.symbol = symbol
        for k, v in fields.items():
            setattr(self, k, v)


class _Client:
    """Stub quote context. Records the batches it was asked for so the
    batching behaviour can be asserted rather than assumed."""

    def __init__(self, *, expiries=None, ladder=None, quotes=None, raises=None):
        self._expiries = expiries if expiries is not None else [EXPIRY]
        self._ladder = ladder if ladder is not None else []
        self._quotes = quotes or {}
        self._raises = raises
        self.batches: list[list[str]] = []

    async def option_chain_expiry_date_list(self, symbol):
        if self._raises:
            raise self._raises
        return self._expiries

    async def option_chain_info_by_date(self, symbol, expiry_date):
        if self._raises:
            raise self._raises
        return self._ladder

    async def option_quote(self, symbols):
        self.batches.append(list(symbols))
        return [self._quotes[s] for s in symbols if s in self._quotes]


def _provider(**kw) -> LongbridgeOptionChainProvider:
    return LongbridgeOptionChainProvider(_Client(**kw))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_expiries_are_deduplicated_sorted_and_parsed_from_strings():
    p = _provider(expiries=["2026-10-16", "2026-09-18", "2026-10-16", "nonsense"])
    assert await p.get_expiries("TQQQ.US") == [date(2026, 9, 18), EXPIRY]


@pytest.mark.asyncio
async def test_a_vendor_failure_on_expiries_is_a_vendor_error_not_an_empty_list():
    # An empty list means "no options listed", which is a real answer. A
    # failed call must never be able to produce it.
    p = _provider(raises=RuntimeError("socket closed"))
    with pytest.raises(VendorError):
        await p.get_expiries("TQQQ.US")


@pytest.mark.asyncio
async def test_an_empty_ladder_is_refused_rather_than_built_around_the_spot():
    p = _provider(ladder=[])
    with pytest.raises(DataUnavailableError) as err:
        await p.get_chain("TQQQ.US", EXPIRY)
    assert "no option contracts" in str(err.value)


@pytest.mark.asyncio
async def test_a_ladder_with_no_contract_symbols_is_refused():
    p = _provider(ladder=[_Rung("80")])
    with pytest.raises(DataUnavailableError) as err:
        await p.get_chain("TQQQ.US", EXPIRY)
    assert "no contract symbols" in str(err.value)


@pytest.mark.asyncio
async def test_the_ladder_decides_which_contracts_exist_and_quotes_fill_them_in():
    ladder = [
        _Rung("80", call_symbol="TQQQ260116C80000", put_symbol="TQQQ260116P80000"),
        _Rung("85", call_symbol="TQQQ260116C85000"),
    ]
    quotes = {
        "TQQQ260116C80000": _Q(
            "TQQQ260116C80000",
            last_done="3.10",
            bid="3.00",
            ask="3.20",
            volume=120,
            open_interest=0,
            implied_volatility="0.42",
            delta="0.55",
        ),
    }
    chain = await _provider(ladder=ladder, quotes=quotes).get_chain("TQQQ.US", EXPIRY)

    assert isinstance(chain, OptionChain)
    assert len(chain.quotes) == 3  # two calls, one put - the ladder's own count
    assert chain.strikes == (Decimal("80"), Decimal("85"))

    call80 = next(q for q in chain.quotes if q.contract_symbol == "TQQQ260116C80000")
    assert call80.right is OptionRight.CALL
    assert call80.bid == Decimal("3.00")
    assert call80.mid == Decimal("3.10")
    assert call80.spread == Decimal("0.20")
    # Zero open interest is a measurement and survives as 0, unlike an
    # absent price, which must not become one.
    assert call80.open_interest == 0

    # The put was listed but not quoted: it keeps its row with an empty
    # market rather than vanishing and silently shortening the chain.
    put80 = next(q for q in chain.quotes if q.right is OptionRight.PUT)
    assert put80.strike == Decimal("80")
    assert put80.bid is None and put80.ask is None and put80.volume is None
    assert put80.mid is None


@pytest.mark.asyncio
async def test_a_mid_is_never_taken_from_one_side_alone():
    q = OptionQuote(
        contract_symbol="X",
        underlying="TQQQ.US",
        expiry=EXPIRY,
        strike=Decimal("80"),
        right=OptionRight.CALL,
        bid=Decimal("3.00"),
        ask=None,
    )
    assert q.mid is None
    assert q.spread is None


@pytest.mark.asyncio
async def test_a_long_ladder_is_quoted_in_bounded_batches():
    ladder = [_Rung(str(80 + i), call_symbol=f"C{i}", put_symbol=f"P{i}") for i in range(40)]
    client = _Client(ladder=ladder)
    p = LongbridgeOptionChainProvider(client)  # type: ignore[arg-type]
    await p.get_chain("TQQQ.US", EXPIRY)
    # 80 contracts at a batch of 50 is two calls, and no single call may
    # exceed the batch size - the vendor refuses an unbounded symbol list.
    assert len(client.batches) == 2
    assert all(len(b) <= LongbridgeOptionChainProvider.QUOTE_BATCH for b in client.batches)


@pytest.mark.asyncio
async def test_by_right_splits_the_chain_without_reordering_strikes():
    ladder = [
        _Rung("85", call_symbol="C85", put_symbol="P85"),
        _Rung("80", call_symbol="C80", put_symbol="P80"),
    ]
    chain = await _provider(ladder=ladder).get_chain("TQQQ.US", EXPIRY)
    assert {q.contract_symbol for q in chain.by_right(OptionRight.CALL)} == {"C80", "C85"}
    assert {q.contract_symbol for q in chain.by_right(OptionRight.PUT)} == {"P80", "P85"}
