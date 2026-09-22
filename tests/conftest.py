"""Test-session environment.

The Autotrade Bot runner defaults ON (D098). Under the test suite that
means every test that boots the app also starts a real runner cycle —
vendor calls when credentials are present, and a `FOR UPDATE` lock on the
paper broker that other tests then wait on. The suite still passes but
takes ~2.5x longer (measured: 21 minutes vs 8). Tests exercise the runner
explicitly through `run_bot_cycle` / `run_autotrade_cycle`; the scheduled
loop is not what they are testing, so it is off here.

Set before any `apps.api` import: `get_settings()` is cached on first call
and `main.py` reads it at import time.
"""

import os

os.environ.setdefault("AUTOTRADE_RUNNER_ENABLED", "false")
