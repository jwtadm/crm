#!/usr/bin/env bash
# CI entrypoint for the rrweb regression suite.
# Runs each test under tests/rrweb/<name>/ against a freshly-booted dev Flask
# (with platform stubs). Exit code 0 == every test pass.
#
# Expected env:
#   - python3 in PATH must have `playwright` installed (`pip install playwright
#     && playwright install chromium`).
#   - .venv/bin/python in repo can be used as the runner driver.
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$(pwd)"

# Prefer the repo venv if it can drive the runner; otherwise fall back to system.
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi

# --no-share avoids the cloudflared tunnel (CI doesn't need it).
exec "$PY" tests/rrweb/run.py --no-share "$@"
