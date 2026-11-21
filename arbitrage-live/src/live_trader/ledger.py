from __future__ import annotations

import json
import queue
import sqlite3
from contextlib import closing
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class CriticalLedger:
    """Append-only JSONL ledger; a write failure permanently gates new orders."""

    def __init__(self, sqlite_path: Path, ndjson_path: Optional[Path] = None) -> None:
        self.sqlite_path = sqlite_path
        self.ndjson_path = ndjson_path or sqlite_path.with_suffix(".ndjson")
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.ndjson_path.parent.mkdir(parents=True, exist_ok=True)
        self.accepting_orders = True
        with closing(sqlite3.connect(self.sqlite_path)) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ledger_events ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, "
                "recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL)"
            )
            connection.commit()

    def record(self, event_type: str, payload: Dict[str, Any]) -> None:
        if not self.accepting_orders:
            raise OSError("critical ledger is failed; new orders are disabled")
        event = {
            "event_type": event_type,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        }
        try:
            payload_json = json.dumps(payload, sort_keys=True, default=str)
            with closing(sqlite3.connect(self.sqlite_path)) as connection:
                connection.execute(
                    "INSERT INTO ledger_events(event_type, recorded_at, payload_json) VALUES (?, ?, ?)",
                    (event["event_type"], event["recorded_at"], payload_json),
                )
                connection.commit()
            with self.ndjson_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, sort_keys=True, default=str) + "\n")
                handle.flush()
                import os

                os.fsync(handle.fileno())
        except (OSError, sqlite3.Error):
            self.accepting_orders = False
            raise


class RawAsyncWriter:
    """Bounded asynchronous raw-event writer; raw drops never block order logic."""

    def __init__(self, directory: Path, max_queue: int = 1000) -> None:
        self.directory = directory
        self.queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="raw-writer", daemon=True)
        self._thread.start()

    def enqueue(self, event: Dict[str, Any]) -> bool:
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(None)
        self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        path = self.directory / "raw.ndjson"
        while not self._stop.is_set():
            item = self.queue.get()
            if item is None:
                break
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, sort_keys=True, default=str) + "\n")
