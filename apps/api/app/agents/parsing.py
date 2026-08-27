"""Shared LLM-response parsing helper, used by every agent that expects a
JSON object back from a provider (TraderAgent, TechnicalAnalyst). Kept in
one place rather than duplicated per-agent (docs/DECISIONS.md D019).
"""


def extract_json_object(text: str) -> str:
    """Models frequently wrap JSON in prose or code fences despite
    instructions. Take the first {...} span rather than trusting the
    whole response is bare JSON - still fails loudly (raises ValueError)
    if no valid object is found, never silently guesses a default."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("no JSON object found in LLM response")
    return text[start : end + 1]
