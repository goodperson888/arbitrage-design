#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"
PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
  exec python3 -m realtime_probability.cli \
    --mode binance --symbol "${REALTIME_SYMBOL:-BTCUSDT}" \
    --interval-seconds "${REALTIME_INTERVAL_SECONDS:-300}" "$@"
