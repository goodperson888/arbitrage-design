from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "arbitrage-live/src"))

from live_trader.order_state import OrderRecord, OrderState, SQLiteOrderStore
from live_trader.ledger import CriticalLedger


def track_connections(monkeypatch):
    original = sqlite3.connect
    opened = []
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection
    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened, original


def assert_closed(opened):
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_order_store_closes_every_operation_but_preserves_atomic_claim(tmp_path, monkeypatch):
    opened, original = track_connections(monkeypatch)
    path = tmp_path / "orders.sqlite3"
    store = SQLiteOrderStore(path)
    record = OrderRecord("one", "market", "token", OrderState.INTENT, {"fixture": True})
    store.save(record)
    assert store.get("one") == record
    assert store.list_all() == [record]
    assert store.claim_submission("one")
    assert not store.claim_submission("one")
    assert store.get("one").state == OrderState.SUBMITTING
    with pytest.raises(KeyError):
        store.get("missing")
    assert_closed(opened)
    # On Windows a leaked handle prevents rename/removal, even after the
    # SQL transaction was committed. Keep the data and verify after moving.
    moved = tmp_path / "preserved-orders.sqlite3"
    path.rename(moved)
    connection = original(moved)
    try:
        assert connection.execute("SELECT count(*) FROM orders").fetchone() == (1,)
    finally:
        connection.close()


def test_critical_ledger_commits_and_releases_connection(tmp_path, monkeypatch):
    opened, original = track_connections(monkeypatch)
    ledger = CriticalLedger(tmp_path / "critical.sqlite3")
    ledger.record("FIXTURE", {"amount": "1"})
    assert_closed(opened)
    connection = original(ledger.sqlite_path)
    try:
        assert connection.execute("SELECT event_type FROM ledger_events").fetchall() == [("FIXTURE",)]
    finally:
        connection.close()
    assert ledger.ndjson_path.read_text(encoding="utf-8").count("FIXTURE") == 1
