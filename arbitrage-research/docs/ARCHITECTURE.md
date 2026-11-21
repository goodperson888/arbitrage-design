# Arbitrage Research Architecture

## Boundary

This project is a research and backtesting system. It must not become a live execution dependency for ArbDesk.

```text
platform APIs / websocket
  -> raw recordings
  -> normalized MarketEvent NDJSON
  -> replay state
  -> strategy signal
  -> simulated fill
  -> settlement and metrics
```

## Layers

### Contracts

`contracts/schemas/` owns versioned event schemas. Live systems and research importers should write events that match these schemas.

### IO

`arb_research.io` reads and writes line-oriented datasets and run artifacts. Large datasets should later be converted to Parquet, but NDJSON stays useful for raw append-only recording and debugging.

### Replay

`arb_research.orderbook` reconstructs best quotes from either compact `PRICE_QUOTE` events or full `ORDERBOOK_SNAPSHOT` / `ORDERBOOK_DELTA` events.

### Strategy

`arb_research.strategy` contains deterministic strategy logic. A strategy should consume replay state and emit intended fills. It should not call platform APIs directly.

### Backtest

`arb_research.backtest` walks events in source-time order, applies replay updates, asks the strategy for fills, then settles PnL when `SETTLEMENT` events arrive.

## First Supported Strategy

`prediction_last_window_threshold`:

- binary prediction markets;
- UP/DOWN outcomes;
- buy taker liquidity in the final configurable window;
- trigger when best ask is at or above `threshold_price`;
- reject entries above `max_entry_price`;
- hold to settlement;
- report win rate, breakeven win rate, and net PnL.

## Next Good Additions

1. Platform backfill adapters for ended markets and settlements.
2. Live recorders with hourly NDJSON rotation.
3. Parquet builder and DuckDB catalog.
4. Latency and slippage simulation.
5. Parameter scans across threshold, remaining time, market duration, and venue.
6. Cross-platform complete-set and resolution-fingerprint matching strategies.
7. HTML/Markdown reports with confidence intervals and market-level diagnostics.
