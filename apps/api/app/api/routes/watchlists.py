"""Watchlists - the research half of the research-to-trade dashboard
(Phase 50).

**Scoping.** A watchlist belongs to exactly one user and is reachable only
by that user. This is the first resource in this codebase that is scoped
to the caller's identity alone: every route under `/brokers/{id}/...`
additionally requires a `BrokerGrant` (D012), and D034's broker discovery
is grant-scoped too. A watchlist has nothing to do with brokers - it is a
list of symbols someone is reading about - so gating it on a grant would
mean a user with no broker access could not research anything, which is
backwards.

Authentication only (`get_current_user`), no `Permission` check, for the
same reason D034 gives for `GET /brokers`: the result is already scoped to
the caller's own rows, so there is nothing a coarser permission would
additionally protect. `VIEW_PORTFOLIO` and `SUBMIT_PAPER_TRADE` would both
be wrong here - a user with neither still has a legitimate reason to keep
a list of symbols.

**Not-yours is 403, not-there is 404.** Ownership is checked in exactly
one place (`_owned_watchlist`) and reproduces the order and the two status
codes `require_broker_access` uses: look the row up first (404 if there is
no such id anywhere), then check the caller owns it (403 if they do not).
That is the existing convention in this repo for "the resource exists but
isn't yours", and using a blanket 404 here instead would make this the
only resource in the codebase that answers a different way.

**Explicit creation, no lazy default.** There is no auto-created "My
watchlist". `GET /watchlists` on a fresh account returns an empty list -
a true statement - and the frontend offers a create form. The rejected
alternative was creating a default row on first read, which would make a
GET write to the database and would litter the table with empty
watchlists belonging to users who only ever glanced at the page.

**Quotes never fabricate.** `GET /watchlists/{id}/quotes` resolves every
symbol through `apps.api.app.marketdata.resolution.resolve_quote` - the
same path, and as of this phase literally the same function,
`GET /market-data/{symbol}/quote` uses. A symbol whose quote cannot be
fetched keeps its row and carries a `DATA_UNAVAILABLE:` sentinel; it is
never dropped from the response and never given a stand-in price. With no
vendor configured at all, every row comes back unavailable and
`market_data_configured` is false - the endpoint still answers 200,
because "here is your list, none of it can be priced right now" is the
honest answer and a 503 would additionally hide the user's own symbols
from them.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from apps.api.app.api.dependencies import get_market_data_router
from apps.api.app.api.schemas_watchlists import (
    AddWatchlistItemRequest,
    CreateWatchlistRequest,
    ListWatchlistsResponse,
    WatchlistQuote,
    WatchlistQuotesResponse,
    WatchlistResponse,
    normalize_symbol,
)
from apps.api.app.auth.dependencies import get_current_user
from apps.api.app.db.base import get_session
from apps.api.app.db.models import User, Watchlist, WatchlistItem
from apps.api.app.marketdata.resolution import resolve_quote, row_sentinel
from apps.api.app.marketdata.router import MarketDataRouter

router = APIRouter(prefix="/watchlists", tags=["watchlists"])

DEFAULT_LIST_LIMIT = 50
"""Same default page size as D034's broker discovery, D031's admin
listings and D027's portfolio history."""
MAX_LIST_LIMIT = 500
"""Same hard ceiling, same reasoning: a caller wanting more pages rather
than the server ever building an unbounded response."""

MAX_ITEMS_PER_WATCHLIST = 200
"""A watchlist's length is the number of vendor calls
`GET /watchlists/{id}/quotes` will make, so it cannot be unbounded - an
unbounded list is a way for one authenticated user to turn one HTTP
request into arbitrarily many upstream ones. 200 is generous for actual
research use and still a bounded fan-out."""


async def _owned_watchlist(
    watchlist_id: uuid.UUID, user: User, session: AsyncSession
) -> Watchlist:
    """404 when no watchlist has this id, 403 when one does but the caller
    does not own it - the same order and codes `require_broker_access`
    uses. Checking existence first is what keeps a nonexistent id from
    403ing merely because nobody can own a row that isn't there."""
    watchlist = (
        await session.execute(select(Watchlist).where(Watchlist.id == watchlist_id))
    ).scalar_one_or_none()
    if watchlist is None:
        raise HTTPException(status_code=404, detail=f"No watchlist with id {watchlist_id}.")
    if watchlist.user_id != user.id:
        raise HTTPException(status_code=403, detail=f"Watchlist {watchlist_id} is not yours.")
    return watchlist


async def _symbols(session: AsyncSession, watchlist_id: uuid.UUID) -> list[str]:
    return list(
        (
            await session.execute(
                select(WatchlistItem.symbol)
                .where(WatchlistItem.watchlist_id == watchlist_id)
                .order_by(WatchlistItem.symbol.asc())
            )
        )
        .scalars()
        .all()
    )


@router.post("", response_model=WatchlistResponse, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    payload: CreateWatchlistRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistResponse:
    """Names are not unique - two watchlists may share one, deliberately.
    Uniqueness would be a constraint on how a person organizes their own
    reading, and the id is what every other route addresses anyway."""
    watchlist = Watchlist(id=uuid.uuid4(), user_id=current_user.id, name=payload.name)
    session.add(watchlist)
    await session.commit()
    await session.refresh(watchlist)
    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        created_at=watchlist.created_at,
        symbols=[],
    )


@router.get("", response_model=ListWatchlistsResponse)
async def list_watchlists(
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ListWatchlistsResponse:
    """The caller's own watchlists, ordered by `(created_at, id)`. The id
    is the tiebreaker that makes the sort total and `offset` paging
    deterministic when two rows share a timestamp - same reasoning as
    D034's `(name, id)`.

    `selectinload` fetches every page's items in one additional query
    rather than one per watchlist; this listing is the one place an N+1
    would otherwise appear."""
    rows = (
        (
            await session.execute(
                select(Watchlist)
                .options(selectinload(Watchlist.items))
                .where(Watchlist.user_id == current_user.id)
                .order_by(Watchlist.created_at.asc(), Watchlist.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListWatchlistsResponse(
        watchlists=[
            WatchlistResponse(
                id=row.id,
                name=row.name,
                created_at=row.created_at,
                symbols=[item.symbol for item in row.items],
            )
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.delete("/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(
    watchlist_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Owner-only. The items go with it via
    `watchlist_items.watchlist_id ON DELETE CASCADE` (migration 0014), in
    the database rather than in a loop here that could half-finish.

    This is a real delete, not a soft one. Nothing in this table is an
    audit trail - it is a user's own working research state, and the
    append-only rules that govern `orders`/`fills` exist for records of
    money moving, which a watchlist is not."""
    await _owned_watchlist(watchlist_id, current_user, session)
    await session.execute(delete(Watchlist).where(Watchlist.id == watchlist_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{watchlist_id}/items",
    response_model=WatchlistResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_watchlist_item(
    watchlist_id: uuid.UUID,
    payload: AddWatchlistItemRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistResponse:
    """409 when the symbol is already on this list.

    The duplicate check is the database's unique constraint, caught here -
    not a SELECT-then-INSERT, which two concurrent requests can both pass
    before either inserts. The pre-check that IS here exists only to
    return the 409 without burning a transaction in the common case; the
    constraint is what actually guarantees it.

    No vendor validation: a symbol is accepted whether or not any wired
    provider knows it. Refusing unknown symbols would make the endpoint
    fail closed on a research artifact (and would silently change
    behaviour the moment a vendor was configured). An unpriceable symbol
    surfaces honestly at `GET /watchlists/{id}/quotes` instead.
    """
    await _owned_watchlist(watchlist_id, current_user, session)
    symbol = normalize_symbol(payload.symbol)

    count = len(await _symbols(session, watchlist_id))
    if count >= MAX_ITEMS_PER_WATCHLIST:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Watchlist {watchlist_id} already holds {count} symbols "
                f"(maximum {MAX_ITEMS_PER_WATCHLIST})."
            ),
        )

    session.add(WatchlistItem(id=uuid.uuid4(), watchlist_id=watchlist_id, symbol=symbol))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Symbol {symbol} is already on watchlist {watchlist_id}.",
        ) from None

    watchlist = await _owned_watchlist(watchlist_id, current_user, session)
    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        created_at=watchlist.created_at,
        symbols=await _symbols(session, watchlist_id),
    )


@router.delete("/{watchlist_id}/items/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_watchlist_item(
    watchlist_id: uuid.UUID,
    symbol: str,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """404 when the symbol is not on this (owned) watchlist - the same
    normalization is applied to the path segment as on the way in, so
    `DELETE .../items/aapl` removes the `AAPL` that `POST` with `"aapl"`
    stored."""
    await _owned_watchlist(watchlist_id, current_user, session)
    normalized = normalize_symbol(symbol)

    # `RETURNING id` rather than inspecting a driver rowcount: it is one
    # statement (so two concurrent deletes cannot both report success) and
    # it is a typed result rather than a DBAPI attribute.
    deleted = (
        (
            await session.execute(
                delete(WatchlistItem)
                .where(
                    WatchlistItem.watchlist_id == watchlist_id,
                    WatchlistItem.symbol == normalized,
                )
                .returning(WatchlistItem.id)
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Symbol {normalized} is not on watchlist {watchlist_id}.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{watchlist_id}/quotes", response_model=WatchlistQuotesResponse)
async def get_watchlist_quotes(
    watchlist_id: uuid.UUID,
    market_data_router: MarketDataRouter | None = Depends(get_market_data_router),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WatchlistQuotesResponse:
    """Every symbol on the watchlist with its live quote, resolved through
    the same `resolve_quote` path as `GET /market-data/{symbol}/quote`.

    Symbols are resolved sequentially rather than with `asyncio.gather`.
    That is a deliberate choice about upstream load, not an oversight: the
    vendors behind `MarketDataRouter` are rate-limited third parties, and
    fanning a 200-symbol list out into 200 simultaneous requests is how
    one user's page refresh becomes everyone's rate-limit rejection. The
    per-list cap (`MAX_ITEMS_PER_WATCHLIST`) bounds the total either way.

    A row is never dropped and never guessed. `unavailable` carries the
    real reason - the router's `NO_DATA_AVAILABLE:` message, or the
    `NOT_CONFIGURED:` string when no vendor is wired - behind a
    `DATA_UNAVAILABLE:` prefix.
    """
    watchlist = await _owned_watchlist(watchlist_id, current_user, session)
    symbols = await _symbols(session, watchlist_id)

    quotes: list[WatchlistQuote] = []
    for symbol in symbols:
        resolved = await resolve_quote(market_data_router, symbol)
        if resolved.snapshot is None:
            quotes.append(WatchlistQuote(symbol=symbol, unavailable=row_sentinel(resolved)))
            continue
        snapshot = resolved.snapshot
        quotes.append(
            WatchlistQuote(
                symbol=symbol,
                price=snapshot.price,
                as_of=snapshot.as_of,
                source=snapshot.source,
            )
        )

    return WatchlistQuotesResponse(
        watchlist_id=watchlist.id,
        name=watchlist.name,
        market_data_configured=market_data_router is not None,
        quotes=quotes,
    )
