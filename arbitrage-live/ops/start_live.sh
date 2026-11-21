#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="${ARBITRAGE_PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="${ARBITRAGE_PYTHON_BIN:-/Users/laochen/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3}"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

: "${POLYMARKET_PRIVATE_KEY:?请先在当前终端设置 POLYMARKET_PRIVATE_KEY；不要把私钥写入仓库或聊天消息}"
: "${POLYMARKET_ORDER_ENABLED:?实盘必须显式设置 POLYMARKET_ORDER_ENABLED=1}"
: "${LIVE_TRADING_ENABLE:?实盘必须显式设置 LIVE_TRADING_ENABLE=1}"
: "${POLYMARKET_PREFLIGHT_APPROVED:?实盘必须显式设置 POLYMARKET_PREFLIGHT_APPROVED=1}"
: "${LIVE_TRANSPORT_ENABLED:?实盘必须显式设置 LIVE_TRANSPORT_ENABLED=1}"

export POLYMARKET_READONLY_NETWORK_ENABLED="1"
export BINANCE_READONLY_NETWORK_ENABLED="1"
export LIVE_MODE="${LIVE_MODE:-live}"
export PYTHONPATH="$ROOT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON_BIN" -m live_trader.cli run --run-mode "$LIVE_MODE" --data-dir "${LIVE_DATA_DIR:-$ROOT_DIR/data/live_trading}" "$@"
