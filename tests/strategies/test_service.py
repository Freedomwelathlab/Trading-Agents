"""Pure unit tests for compute_definition_hash (Phase 54).

The other two helpers in apps/api/app/strategies/service.py
(`next_version_number`, `latest_version`) are SQL queries and are exercised
end to end through the fork route in tests/api/test_strategies.py rather
than mocked here - a mocked AsyncSession would assert that this module
builds the query it builds, which is not a fact about the system.
"""

from apps.api.app.strategies.service import compute_definition_hash

DEFINITION = {
    "indicators": [{"id": "sma_20", "type": "sma", "period": 20}],
    "entry_rule": {"op": "crosses_above", "left": "sma_20", "right": "close"},
    "exit_rule": {"op": "crosses_below", "left": "sma_20", "right": "close"},
    "position_sizing": {"type": "all_in"},
}


def test_the_hash_is_64_lowercase_hex_characters():
    digest = compute_definition_hash(DEFINITION)
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(character in "0123456789abcdef" for character in digest)


def test_key_order_does_not_change_the_hash():
    """The point of canonical (sorted-key) JSON: `{"a": 1, "b": 2}` and
    `{"b": 2, "a": 1}` are the same strategy, so a fork of an unchanged
    definition must not look like a change just because a client serialized
    it differently."""
    reordered = {
        "position_sizing": {"type": "all_in"},
        "exit_rule": {"right": "close", "left": "sma_20", "op": "crosses_below"},
        "entry_rule": {"right": "close", "op": "crosses_above", "left": "sma_20"},
        "indicators": [{"period": 20, "type": "sma", "id": "sma_20"}],
    }
    assert compute_definition_hash(reordered) == compute_definition_hash(DEFINITION)


def test_a_different_definition_hashes_differently():
    changed = {
        **DEFINITION,
        "indicators": [{"id": "sma_20", "type": "sma", "period": 50}],
    }
    assert compute_definition_hash(changed) != compute_definition_hash(DEFINITION)


def test_list_order_does_change_the_hash():
    """Sorting applies to object KEYS, not array elements - a list is
    ordered data, and two indicator lists in different orders are not
    self-evidently the same strategy."""
    two = {
        **DEFINITION,
        "indicators": [
            {"id": "sma_20", "type": "sma", "period": 20},
            {"id": "rsi_14", "type": "rsi", "period": 14},
        ],
    }
    reversed_list = {**two, "indicators": list(reversed(two["indicators"]))}
    assert compute_definition_hash(reversed_list) != compute_definition_hash(two)


def test_the_empty_draft_definition_hashes_deterministically():
    """An empty `{}` is a legal stored draft, so it must hash rather than
    raise - and it must hash to the same thing every time, or every empty
    draft would look like a distinct definition."""
    assert compute_definition_hash({}) == compute_definition_hash({})
