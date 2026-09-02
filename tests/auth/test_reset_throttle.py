"""Unit tests for the per-IP reset throttle (docs/DECISIONS.md D063).

Pure: the throttle takes `now` from its caller, so the one-hour window is
exercised by passing timestamps rather than by waiting an hour.
"""

from datetime import UTC, datetime, timedelta

from apps.api.app.auth.reset_throttle import MAX_TRACKED_KEYS, FixedWindowThrottle

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_requests_up_to_the_limit_are_allowed():
    throttle = FixedWindowThrottle()
    assert [throttle.allow("1.2.3.4", NOW, limit=3) for _ in range(3)] == [True] * 3


def test_the_request_after_the_limit_is_refused():
    throttle = FixedWindowThrottle()
    for _ in range(3):
        throttle.allow("1.2.3.4", NOW, limit=3)
    assert throttle.allow("1.2.3.4", NOW, limit=3) is False


def test_keys_are_counted_independently():
    """Otherwise one noisy client would lock out everyone else, which is a
    denial of service wearing a throttle's clothes."""
    throttle = FixedWindowThrottle()
    for _ in range(3):
        throttle.allow("1.2.3.4", NOW, limit=3)
    assert throttle.allow("5.6.7.8", NOW, limit=3) is True


def test_the_window_slides_so_a_blocked_client_recovers():
    throttle = FixedWindowThrottle()
    for _ in range(3):
        throttle.allow("1.2.3.4", NOW, limit=3)
    assert throttle.allow("1.2.3.4", NOW + timedelta(minutes=59), limit=3) is False
    assert throttle.allow("1.2.3.4", NOW + timedelta(minutes=61), limit=3) is True


def test_a_refused_request_is_not_counted_against_the_next_window():
    """Counting refusals would let one burst extend the block indefinitely
    for as long as the client kept retrying - punishing exactly the caller
    least able to tell what is happening."""
    throttle = FixedWindowThrottle()
    for _ in range(3):
        throttle.allow("1.2.3.4", NOW, limit=3)
    for _ in range(50):
        throttle.allow("1.2.3.4", NOW + timedelta(minutes=30), limit=3)
    # The original three aged out at +61m; the 50 refusals recorded nothing.
    assert throttle.allow("1.2.3.4", NOW + timedelta(minutes=61), limit=3) is True


def test_a_zero_limit_disables_the_throttle_and_records_nothing():
    throttle = FixedWindowThrottle()
    assert all(throttle.allow("1.2.3.4", NOW, limit=0) for _ in range(1000))
    # Nothing accumulated, so turning the throttle back on later starts clean
    # rather than instantly blocking whoever was around while it was off.
    assert throttle.allow("1.2.3.4", NOW, limit=1) is True


def test_tracked_keys_are_bounded_so_the_throttle_cannot_be_a_memory_leak():
    """An attacker cycling source addresses must not be able to turn the
    mitigation into the vulnerability."""
    throttle = FixedWindowThrottle()
    for index in range(MAX_TRACKED_KEYS + 100):
        throttle.allow(f"key-{index}", NOW, limit=5)
    assert len(throttle._hits) <= MAX_TRACKED_KEYS


def test_eviction_drops_the_least_recently_seen_key_not_the_active_one():
    throttle = FixedWindowThrottle()
    throttle.allow("old", NOW, limit=5)
    for index in range(MAX_TRACKED_KEYS - 1):
        throttle.allow(f"filler-{index}", NOW, limit=5)
    # Touch "old" again so it is the most recently seen, then overflow.
    throttle.allow("old", NOW, limit=5)
    throttle.allow("overflow", NOW, limit=5)
    assert "old" in throttle._hits
