"""Bot exit rules (Phase 81, D098). Pure; no database.

Each rule the operator can set is pinned, plus the order of precedence
between them — a bar that touches both the stop and the target must read
as a stop, which is the conservative reading `simulate_bracket` uses.
"""

from decimal import Decimal

from apps.api.app.autotrade.brackets import (
    BracketState,
    ExitRules,
    initial_bracket,
    manage,
)
from apps.api.app.db.models import AutotradeExitReason

D = Decimal


def rules(**over) -> ExitRules:
    base = dict(
        stop_loss_mode="auto", stop_loss_max_pct=None, trailing_stop_pct=None,
        take_profit_mode="auto", take_profit_min_pct=None, trailing_take_profit_pct=None,
    )
    base.update(over)
    return ExitRules(**base)


def test_auto_stop_is_the_structural_stop_and_target_is_two_r():
    b = initial_bracket(entry_price=D("100"), structural_stop=D("98"), rules=rules())
    assert b.stop_price == D("98")
    assert b.take_profit_price == D("104")  # 2R on a 2-point risk


def test_max_stop_only_ever_tightens_the_structural_stop():
    wide = initial_bracket(
        entry_price=D("100"), structural_stop=D("95"),
        rules=rules(stop_loss_mode="max", stop_loss_max_pct=D("2")),
    )
    assert wide.stop_price == D("98.00")  # tightened to 2%
    tight = initial_bracket(
        entry_price=D("100"), structural_stop=D("99"),
        rules=rules(stop_loss_mode="max", stop_loss_max_pct=D("2")),
    )
    assert tight.stop_price == D("99")  # structural stop already tighter; left alone


def test_min_take_profit_is_a_floor_not_a_replacement():
    b = initial_bracket(
        entry_price=D("100"), structural_stop=D("99"),
        rules=rules(take_profit_mode="min", take_profit_min_pct=D("5")),
    )
    assert b.take_profit_price == D("105.00")  # 2R would be 102; the floor wins
    b2 = initial_bracket(
        entry_price=D("100"), structural_stop=D("90"),
        rules=rules(take_profit_mode="min", take_profit_min_pct=D("5")),
    )
    assert b2.take_profit_price == D("120")  # 2R is farther; kept


def test_a_structural_stop_above_entry_falls_back_to_the_cap_or_refuses():
    capped = initial_bracket(
        entry_price=D("100"), structural_stop=D("101"),
        rules=rules(stop_loss_mode="max", stop_loss_max_pct=D("1")),
    )
    assert capped.stop_price == D("99.00")
    refused = initial_bracket(entry_price=D("100"), structural_stop=D("101"), rules=rules())
    assert refused.stop_price == D("100")  # zero-width; the engine refuses to trade it


def state(**over) -> BracketState:
    base = dict(
        entry_price=D("100"), initial_stop_price=D("98"), stop_price=D("98"),
        take_profit_price=D("104"), peak_price=D("100"), take_profit_armed=False,
    )
    base.update(over)
    return BracketState(**base)


def test_stop_touched_on_the_low_exits_as_stop_loss():
    d = manage(state(), bar_high=D("101"), bar_low=D("97.9"), bar_close=D("99"),
               rules=rules(), session_ending=False)
    assert d.exit_reason is AutotradeExitReason.STOP_LOSS


def test_a_bar_touching_both_stop_and_target_reads_as_a_stop():
    d = manage(state(), bar_high=D("105"), bar_low=D("97"), bar_close=D("104"),
               rules=rules(), session_ending=False)
    assert d.exit_reason is AutotradeExitReason.STOP_LOSS


def test_target_touched_exits_as_take_profit_without_a_trail():
    d = manage(state(), bar_high=D("104.2"), bar_low=D("101"), bar_close=D("103"),
               rules=rules(), session_ending=False)
    assert d.exit_reason is AutotradeExitReason.TAKE_PROFIT


def test_trailing_take_profit_arms_at_target_then_exits_on_the_giveback():
    r = rules(trailing_take_profit_pct=D("1"))
    armed = manage(state(), bar_high=D("104.5"), bar_low=D("102"), bar_close=D("104"),
                   rules=r, session_ending=False)
    assert armed.exit_reason is None
    assert armed.state.take_profit_armed and armed.state.peak_price == D("104.5")
    # Runs further: peak moves up, still no exit.
    running = manage(armed.state, bar_high=D("108"), bar_low=D("105"), bar_close=D("107.5"),
                     rules=r, session_ending=False)
    assert running.exit_reason is None and running.state.peak_price == D("108")
    # Gives back 1% from the 108 peak on the close -> exit.
    gave_back = manage(running.state, bar_high=D("107.6"), bar_low=D("106.5"),
                       bar_close=D("106.9"), rules=r, session_ending=False)
    assert gave_back.exit_reason is AutotradeExitReason.TRAILING_TAKE_PROFIT


def test_trailing_stop_ratchets_up_and_never_down():
    r = rules(trailing_stop_pct=D("2"))
    # No fixed target here, so the trail is the only thing being tested.
    up = manage(state(take_profit_price=None), bar_high=D("110"), bar_low=D("105"),
                bar_close=D("109"), rules=r, session_ending=False)
    assert up.state.stop_price == D("107.8")  # 110 * 0.98
    down = manage(up.state, bar_high=D("109"), bar_low=D("108"), bar_close=D("108.5"),
                  rules=r, session_ending=False)
    assert down.state.stop_price == D("107.8")  # unchanged on a lower high
    stopped = manage(down.state, bar_high=D("108"), bar_low=D("107.5"), bar_close=D("107.6"),
                     rules=r, session_ending=False)
    assert stopped.exit_reason is AutotradeExitReason.TRAILING_STOP


def test_session_end_flattens_a_position_that_hit_nothing():
    d = manage(state(), bar_high=D("101"), bar_low=D("99.5"), bar_close=D("100.5"),
               rules=rules(), session_ending=True)
    assert d.exit_reason is AutotradeExitReason.SESSION_END
