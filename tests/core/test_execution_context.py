from uuid import uuid4

import pytest

from apps.api.app.core.config import Settings, TradingMode
from apps.api.app.core.execution_context import (
    LiveContext,
    PaperContext,
    ResearchContext,
    build_execution_context,
)


def test_research_mode_needs_no_broker_id():
    settings = Settings(_env_file=None, trading_mode=TradingMode.RESEARCH)
    context = build_execution_context(settings)
    assert isinstance(context, ResearchContext)


def test_paper_mode_requires_a_broker_id():
    settings = Settings(_env_file=None, trading_mode=TradingMode.PAPER)
    with pytest.raises(ValueError, match="requires a broker_id"):
        build_execution_context(settings)


def test_paper_mode_with_broker_id_builds_paper_context():
    settings = Settings(_env_file=None, trading_mode=TradingMode.PAPER)
    broker_id = uuid4()
    context = build_execution_context(settings, broker_id=broker_id)
    assert isinstance(context, PaperContext)
    assert context.broker_id == broker_id


def test_live_context_builds_only_when_explicitly_enabled():
    settings = Settings(
        _env_file=None, trading_mode=TradingMode.LIVE, live_trading_enabled=True
    )
    context = build_execution_context(settings, broker_id=uuid4())
    assert isinstance(context, LiveContext)


def test_live_mode_without_enable_flag_cannot_even_construct_settings():
    # Settings itself fails closed before execution_context is ever reached -
    # this documents that double-enforcement rather than assuming it.
    with pytest.raises(ValueError, match="LIVE_TRADING_ENABLED"):
        Settings(_env_file=None, trading_mode=TradingMode.LIVE, live_trading_enabled=False)
