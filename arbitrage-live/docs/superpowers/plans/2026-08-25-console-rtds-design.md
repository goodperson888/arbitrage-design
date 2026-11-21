# Console and RTDS Read-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the local live console around monitoring-first workflows and add a disabled-by-default Polymarket RTDS reference-price adapter without changing the Binance strategy gate or enabling orders.

**Architecture:** Keep the public Market WebSocket as the executable order-book/trade feed. Add an independent RTDS adapter that normalizes Binance and Chainlink/Chainlink-TWAP reference events into the existing runner envelope; it is read-only and opt-in. Restructure the dashboard into a compact overview, live market/strategy panels, collapsible account settings, and lower-volume ledger/health sections.

**Tech Stack:** Python 3.12 standard library, unittest, existing `MonitoringRecorder`/`LiveRunner`, static HTML/CSS/JavaScript dashboard, optional `websockets` client.

**Spec:** `docs/live_run.md` and the approved console/RTDS design in the conversation.

## Global Constraints

- Default mode remains `observe`; no real order is sent by any test or command.
- RTDS and all network feeds remain read-only and require explicit environment switches.
- Binance Spot 5-minute direction remains the confirmed strategy hard condition.
- Chainlink/RTDS values are diagnostic until separately approved; they do not replace Binance or settlement truth.
- Private keys are read only from environment/Keychain and never logged or written to files.

### Task 1: RTDS message normalization and safety gates

**Files:**
- Create: `src/live_trader/rtds.py`
- Modify: `src/live_trader/runner.py` only if a normalized reference event hook is required
- Test: `tests/test_rtds.py`

**Interfaces:**
- `normalize_rtds_message(message: Mapping[str, Any]) -> dict[str, Any]`
- `RTDSReadOnlyAdapter` exposes subscription payloads and normalized events; it has no order methods.
- `RTDS_NETWORK_ENABLED=1` is required before network connection; default is disabled.

- [ ] Write failing tests for Binance RTDS, Chainlink RTDS, 30/60-second TWAP normalization, malformed messages, and disabled network construction.
- [ ] Run `PYTHONPATH=src python3 -m unittest tests.test_rtds -v` and confirm the new tests fail for missing module/API.
- [ ] Implement decimal-preserving normalization and a disabled-by-default connector interface.
- [ ] Run the RTDS tests and confirm they pass without network access.
- [ ] Run the complete test suite.

### Task 2: Monitoring-first dashboard state

**Files:**
- Modify: `src/live_trader/dashboard.py`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Extend `build_state(root)` with `market`, `referencePrices`, `connectionSummary`, and current gate fields while preserving existing keys.

- [ ] Add failing state tests for latest order-book values, latest trade, Binance and Chainlink reference values, and explicit Market WebSocket/CLOB/RTDS statuses.
- [ ] Run the targeted dashboard tests and confirm failure.
- [ ] Implement state derivation from versioned event streams without parsing raw data on refresh.
- [ ] Run targeted and complete dashboard tests.

### Task 3: Console layout and operator controls

**Files:**
- Modify: `src/live_trader/static/index.html`
- Modify: `src/live_trader/static/app.js`
- Modify: `src/live_trader/static/styles.css`
- Modify: `README.md`, `docs/live_run.md`
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Preserve existing control element IDs and API endpoints so settings and runner controls remain compatible.
- Add stable IDs for overview status, reference sources, and the connection summary.

- [ ] Add failing DOM/text tests for compact overview, collapsible advanced settings, RTDS source labels, and no single-order button.
- [ ] Run targeted tests and confirm failure.
- [ ] Implement the monitoring-first layout and responsive styles, keeping observe as the visually safe default.
- [ ] Verify the live DOM in the local browser and run all tests.

### Task 4: Documentation and verification

**Files:**
- Modify: `docs/phase3_readonly.md`, `docs/monitoring_compatibility.md`, `README.md`
- Test: all existing tests plus `compileall`

- [ ] Document Market WebSocket vs RTDS vs authenticated user channel vs CLOB REST, and clarify UMA is settlement resolution rather than a quote feed.
- [ ] Document that RTDS is disabled by default and Chainlink values are diagnostic only.
- [ ] Run `PYTHONPYCACHEPREFIX=/tmp/arbitrage-live-pycache PYTHONPATH=src python3 -m compileall -q src tests`.
- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- [ ] Run `PYTHONPATH=src python3 -m live_trader.cli preflight` and verify all safe defaults pass.
