#!/usr/bin/env bash
#
# Block committed credentials (spec section 38 / section 51).
#
# Run from the repository root:
#
#     bash scripts/secret_scan.sh
#
# Exits 0 when clean, 1 (printing every offending line) when not. CI calls
# this file rather than inlining the pattern into .github/workflows/ci.yml,
# because a workflow that spells out the pattern matches itself and fails
# every build — which is exactly what happened between Phase 1 and Phase 39
# (see docs/DECISIONS.md D052).
#
# Two escape hatches, both deliberate and both narrow:
#
#   1. This file is excluded from its own scan. It cannot avoid containing
#      the strings it searches for.
#   2. Any single line carrying the marker `pragma: allowlist secret` is
#      ignored. That is for fixtures whose whole purpose is to look like a
#      credential — e.g. the redaction test in tests/test_logging.py, which
#      asserts that an api_key value never reaches the logs. The marker is
#      per-line, so it cannot silently widen to a whole file.
#
# Markdown is excluded because the docs quote these patterns when explaining
# them; prose is not a place a live credential leaks from.

set -euo pipefail

PATTERN='(BEGIN (RSA|EC|OPENSSH) PRIVATE KEY|github_pat_|sk-live-)'

matches="$(
  git grep -InE "$PATTERN" -- . ':!*.md' ':!scripts/secret_scan.sh' \
    | grep -v 'pragma: allowlist secret' \
    || true
)"

if [ -n "$matches" ]; then
  echo "$matches"
  echo
  echo "Potential committed secret found — failing build (spec section 38/51)."
  echo "If this is a deliberate fixture, append a 'pragma: allowlist secret'"
  echo "comment to that exact line. Do not exclude whole files."
  exit 1
fi

echo "Secret scan clean."
