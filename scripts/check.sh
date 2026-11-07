#!/usr/bin/env bash
# One command for the checks: ruff, then the test suite (fast tier by default).
#   scripts/check.sh          ruff check, then pytest -m "not slow" in parallel (fast tier)
#   scripts/check.sh --full   ruff check, then the whole suite in parallel
# Python: $PYTHON if set, else .venv/bin/python in the repo root if it exists, else python.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

case "${1:-}" in
  "")      MARKER=(-m "not slow") ;;
  --full)  MARKER=() ;;
  -h|--help)
    sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  *)
    echo "usage: scripts/check.sh [--full]" >&2
    exit 2 ;;
esac

if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python
fi

if ! "$PY" -m ruff --version >/dev/null 2>&1; then
  echo "check.sh: ruff is not installed for $PY. Install the dev requirements:" >&2
  echo "  $PY -m pip install -r requirements.txt" >&2
  exit 1
fi

echo "== ruff check ."
"$PY" -m ruff check .

echo "== pytest ${MARKER[*]:-(full suite)} -n auto"
# ${arr[@]+...} keeps an empty array from tripping `set -u` on macOS bash 3.2.
"$PY" -m pytest -q -n auto ${MARKER[@]+"${MARKER[@]}"}
