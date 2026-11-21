# Live Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a local visual settings flow that stores non-secrets locally, stores the wallet key in macOS Keychain, detects Polygon EOA/proxy accounts, and exposes a safe preflight status.

**Architecture:** Extend the existing localhost dashboard with a small settings store and JSON POST endpoints. Keep the runner's environment-based execution unchanged; the dashboard prepares non-sensitive settings and a Keychain-backed secret, while preflight reports whether startup is safe.

**Tech Stack:** Python stdlib `http.server`, `security` CLI, Polygon JSON-RPC over `urllib`, existing HTML/CSS/JS dashboard, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-live-settings-design.md`

## Global Constraints

- Private keys never enter repository, monitoring directory, SQLite, NDJSON, logs, or `/api/state`.
- Account detection is read-only `eth_getCode` only.
- Default order switches remain fail-closed.
- Dashboard remains bound to localhost and has no order button.

### Task 1: Runtime secret and account utilities

**Files:** Create `src/live_trader/settings.py`; modify `tests/test_phase3.py`.

- [ ] Write tests for Keychain command construction, redacted settings response, EOA/contract/invalid classification, and Polygon RPC request parsing.
- [ ] Run the focused tests and verify failure because the module is absent.
- [ ] Implement `SettingsStore`, `KeychainSecretStore`, `classify_polygon_code`, and `PolygonAccountDetector` with injectable command/RPC functions.
- [ ] Run focused tests until green.

### Task 2: Dashboard JSON API

**Files:** Modify `src/live_trader/dashboard.py`; modify `tests/test_dashboard.py`.

- [ ] Add GET/POST `/api/settings`, POST `/api/account/detect`, and GET `/api/preflight` tests using a temporary data directory and injected detector.
- [ ] Run tests to verify the new routes fail.
- [ ] Add localhost-only JSON handlers, JSON body size limit, non-secret persistence, and safe error responses.
- [ ] Run dashboard tests and all Python tests.

### Task 3: Visual settings and preflight panel

**Files:** Modify `src/live_trader/static/index.html`, `styles.css`, `app.js`; modify `tests/test_dashboard.py`.

- [ ] Add accessible fields for wallet address, private key, RPC, proxy, strategy switches, account detect, save, preflight, and start command status.
- [ ] Add masked secret UX and no secret rendering from API responses.
- [ ] Add responsive layout and inline error/status feedback.
- [ ] Run static-content tests and `node --check src/live_trader/static/app.js`.

### Task 4: Runner integration and documentation

**Files:** Modify `src/live_trader/cli.py`, `ops/start_live.sh`, `docs/live_run.md`, `README.md`; add tests.

- [ ] Add a settings export/read path that only injects the Keychain private key at process launch.
- [ ] Add preflight output for account type and Keychain state; keep checked-in transport disabled.
- [ ] Document the UI workflow and the fact that the private key is never returned by API.
- [ ] Run full tests, compileall, and local dashboard API smoke.
