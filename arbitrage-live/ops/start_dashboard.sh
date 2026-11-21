#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
DATA_DIR="${1:-data/live_trading}"
PORT="${LIVE_DASHBOARD_PORT:-8766}"
BUNDLED_PYTHON="/Users/laochen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
DEFAULT_PYTHON="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$DEFAULT_PYTHON" ]; then DEFAULT_PYTHON="$BUNDLED_PYTHON"; fi
PYTHON_BIN="${ARBITRAGE_PYTHON_BIN:-$DEFAULT_PYTHON}"
if [ ! -x "$PYTHON_BIN" ]; then PYTHON_BIN=python3; fi
export ARBITRAGE_PYTHON_BIN="$PYTHON_BIN"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PYTHON_BIN" -m live_trader.cli dashboard --data-dir "$DATA_DIR" --host "${LIVE_DASHBOARD_HOST:-127.0.0.1}" --port "$PORT"
