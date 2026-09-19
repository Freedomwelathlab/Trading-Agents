"""The Autotrade Bot (Phase 81, D098): operator-configured intraday robots.

    scanner.py   — turn stored bars into ranked `SetupSignal`s for the
                   latest bar, using the SAME context the intraday backtest
                   engine builds, so the live bot can never see a signal
                   the backtest could not.
    brackets.py  — the per-position exit rules: stop, trailing stop, take
                   profit, trailing take profit, session-end flat.
    learning.py  — the improvement loop: per-setup expectancy on this bot's
                   own closed trades, and the demotion rule `auto` mode
                   applies from it.
    engine.py    — one cycle of one bot: refresh bars, manage open
                   positions, scan, open the best signals, write the run.
    runner.py    — the periodic loop that calls the engine.
    service.py   — lifecycle: create / approve / pause / resume / stop.
"""
