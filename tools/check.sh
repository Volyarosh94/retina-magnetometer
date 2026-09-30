#!/usr/bin/env bash
# Every gate CI runs, in the order it runs them. ci.yml and release.yml both
# call this, so a gate cannot be added to one and forgotten in the other.
set -euo pipefail
cd "$(dirname "$0")/.."

uv run --locked pre-commit run --all-files --show-diff-on-failure
uv run --locked pytest --cov --cov-report=term-missing:skip-covered
