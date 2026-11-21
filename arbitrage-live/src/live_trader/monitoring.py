from __future__ import annotations

import json
import gzip
import os
import queue
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple, Union


STREAMS = ("raw", "events", "signals", "orders", "fills", "settlements", "health", "sessions")
CRITICAL_STREAMS = {"orders", "fills", "settlements", "sessions"}
RUNTIME_STATE_FILENAME = "runtime_state.json"


def _utc_from_us(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1_000_000, tz=timezone.utc)


def _now_us() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000)


class _AsyncPartitionWriter:
    def __init__(self, root: Path, max_queue: int) -> None:
        self.root = root
        self.queue: queue.Queue[Optional[Tuple[str, Dict[str, Any], int]]] = queue.Queue(maxsize=max_queue)
        self.dropped = 0
        self._running = True
        self._thread = threading.Thread(target=self._run, name="live-monitor-writer", daemon=True)
        self._thread.start()

    def enqueue(self, stream: str, event: Dict[str, Any], partition_time_us: int) -> bool:
        try:
            self.queue.put_nowait((stream, event, partition_time_us))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def close(self) -> None:
        # Closing is off the hot path: let the worker drain every queued raw
        # event before exiting.  Do not evict an event just to enqueue a
        # sentinel; that used to lose the final message on shutdown.
        self._running = False
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while self._running or not self.queue.empty():
            try:
                item = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                continue
            stream, event, partition_time_us = item
            path = _partition_path(self.root, stream, partition_time_us, compressed=(stream == "raw"))
            path.parent.mkdir(parents=True, exist_ok=True)
            opener = gzip.open if path.suffix == ".gz" else open
            with opener(path, "at", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str) + "\n")
                handle.flush()


class _RuntimeStateStore:
    """Small, bounded latest-state sidecar for the dashboard.

    This is not a replay stream: it is atomically replaced, capped to a small
    number of summaries, and is used only so the UI can stay live while the
    operator pauses high-frequency historical recording.
    """

    def __init__(self, root: Path, flush_interval_seconds: float = 0.5) -> None:
        self.path = root / RUNTIME_STATE_FILENAME
        self.flush_interval_seconds = flush_interval_seconds
        self._lock = threading.Lock()
        self._events: list[dict[str, Any]] = []
        self._updated_time_us = 0
        self._last_flush = 0.0

    @staticmethod
    def _compact(event: Dict[str, Any], stream: str) -> dict[str, Any]:
        compact = dict(event)
        compact["_stream"] = stream
        payload = dict(event.get("payload") or {})
        if event.get("eventType") == "ORDERBOOK_SNAPSHOT":
            payload = {
                key: payload.get(key)
                for key in ("market_id", "down_price_now", "down_price_five_seconds_ago", "remaining_seconds", "received_time_us")
                if payload.get(key) is not None
            }
            payload["asks"] = list(payload.get("asks") or [])[:10]
            payload["bids"] = list(payload.get("bids") or [])[:10]
        compact["payload"] = payload
        return compact

    def observe(self, stream: str, event: Dict[str, Any]) -> None:
        if stream == "raw":
            return
        with self._lock:
            self._events.append(self._compact(event, stream))
            self._events = self._events[-60:]
            self._updated_time_us = max(self._updated_time_us, int(event.get("receivedTimeUs", 0) or 0))
            now = time.monotonic()
            if now - self._last_flush >= self.flush_interval_seconds:
                try:
                    self._flush_locked()
                except OSError:
                    # Runtime UI state is optional; a disk issue must not
                    # interrupt market processing or critical order ledgers.
                    self._last_flush = now

    def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        payload = {"updatedTimeUs": self._updated_time_us, "events": self._events}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8")
        os.replace(temporary, self.path)
        self._last_flush = time.monotonic()

    def close(self) -> None:
        with self._lock:
            try:
                self._flush_locked()
            except OSError:
                pass


def load_runtime_state(root: Path) -> Dict[str, Any]:
    try:
        value = json.loads((root / RUNTIME_STATE_FILENAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {"updatedTimeUs": 0, "events": []}
    if not isinstance(value, dict) or not isinstance(value.get("events"), list):
        return {"updatedTimeUs": 0, "events": []}
    return value


def _partition_path(root: Path, stream: str, received_time_us: int, compressed: bool = False) -> Path:
    timestamp = _utc_from_us(received_time_us)
    return (
        root
        / stream
        / f"date={timestamp.strftime('%Y-%m-%d')}"
        / f"hour={timestamp.strftime('%H')}"
        / ("part-001.ndjson.gz" if compressed else "part-001.ndjson")
    )


class MonitoringRecorder:
    """Versioned live event recorder compatible with the research envelope.

    The SQLite database is intentionally rooted inside the live data directory;
    it is never shared with paper-monitor data. Critical streams use synchronous
    SQLite + NDJSON writes, while raw/events/signals/health are bounded async.
    """

    def __init__(self, root: Path, live_mode: str = "observe", raw_queue_size: int = 1000) -> None:
        if live_mode not in {"observe", "canary", "live"}:
            raise ValueError("live_mode must be observe, canary, or live")
        self.root = root
        self.live_mode = live_mode
        self.enabled = True
        self.accepting_orders = True
        self.root.mkdir(parents=True, exist_ok=True)
        self.sqlite_path = self.root / "monitoring.sqlite3"
        self._critical_db = sqlite3.connect(self.sqlite_path, check_same_thread=False)
        self._critical_db.execute(
            "CREATE TABLE IF NOT EXISTS monitor_events ("
            "event_id TEXT PRIMARY KEY, stream TEXT NOT NULL, event_json TEXT NOT NULL, recorded_at TEXT NOT NULL)"
        )
        self._critical_db.commit()
        self._lock = threading.Lock()
        self._async_writer = _AsyncPartitionWriter(self.root, raw_queue_size)
        self._runtime_state = _RuntimeStateStore(self.root)

    def record(
        self,
        stream: str,
        event_type: str,
        *,
        venue: str,
        market_id: str = "",
        outcome: Optional[str] = None,
        source_time_us: Optional[int] = None,
        received_time_us: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
        event_id: Optional[str] = None,
    ) -> Union[str, bool]:
        if stream not in STREAMS:
            raise ValueError(f"unsupported monitoring stream: {stream}")
        if stream in CRITICAL_STREAMS and not self.accepting_orders:
            raise OSError("critical monitoring ledger failed; new orders are disabled")
        source_time_us = source_time_us if source_time_us is not None else _now_us()
        received_time_us = received_time_us if received_time_us is not None else _now_us()
        event_id = event_id or str(uuid.uuid4())
        event: Dict[str, Any] = {
            "schemaVersion": 1,
            "eventId": event_id,
            "eventType": event_type,
            "venue": venue,
            "marketId": market_id,
            "outcome": outcome,
            "sourceTimeUs": source_time_us,
            "receivedTimeUs": received_time_us,
            "paperOnly": self.live_mode == "observe",
            "liveMode": self.live_mode,
            "payload": payload or {},
        }
        self._runtime_state.observe(stream, event)
        # Pausing collection suppresses persisted high-volume streams, while
        # the bounded runtime sidecar above keeps the dashboard current.
        if not self.enabled and stream not in CRITICAL_STREAMS:
            return ""
        if stream in CRITICAL_STREAMS:
            self._write_critical(stream, event)
            return event_id
        return event_id if self._async_writer.enqueue(stream, event, received_time_us) else False

    def _write_critical(self, stream: str, event: Dict[str, Any]) -> None:
        serialized = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
        path = _partition_path(self.root, stream, int(event["receivedTimeUs"]))
        try:
            with self._lock:
                self._critical_db.execute(
                    "INSERT INTO monitor_events(event_id, stream, event_json, recorded_at) VALUES (?, ?, ?, ?)",
                    (event["eventId"], stream, serialized, datetime.now(timezone.utc).isoformat()),
                )
                self._critical_db.commit()
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(serialized + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except (OSError, sqlite3.Error):
            self.accepting_orders = False
            raise

    def fail_critical_writes(self) -> None:
        self.accepting_orders = False

    @property
    def raw_dropped(self) -> int:
        return self._async_writer.dropped

    def close(self) -> None:
        self._async_writer.close()
        self._runtime_state.close()
        with self._lock:
            self._critical_db.commit()
            self._critical_db.close()

    def status(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "liveMode": self.live_mode,
            "acceptingOrders": self.accepting_orders,
            "rawQueueDepth": self._async_writer.queue.qsize(),
            "rawQueueCapacity": self._async_writer.queue.maxsize,
            "rawDropped": self._async_writer.dropped,
        }


def iter_partitioned_events(root: Path, stream: str) -> Iterator[Dict[str, Any]]:
    if stream not in STREAMS:
        raise ValueError(f"unsupported monitoring stream: {stream}")
    paths = list((root / stream).glob("date=*/hour=*/part-*.ndjson"))
    paths.extend((root / stream).glob("date=*/hour=*/part-*.ndjson.gz"))
    for path in sorted(paths):
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def read_partitioned_events_tail(root: Path, stream: str, limit: int = 100) -> list[Dict[str, Any]]:
    """Read only the newest events from partition files.

    Dashboard refreshes must never replay an entire high-frequency partition;
    a live events file can grow to hundreds of megabytes in one hour.  Raw is
    compressed and intentionally not used by the dashboard, but gzip files
    still fall back to the normal iterator for callers that request them.
    """
    if stream not in STREAMS:
        raise ValueError(f"unsupported monitoring stream: {stream}")
    if limit <= 0:
        return []
    paths = list((root / stream).glob("date=*/hour=*/part-*.ndjson"))
    paths.extend((root / stream).glob("date=*/hour=*/part-*.ndjson.gz"))
    newest: list[Dict[str, Any]] = []
    for path in sorted(paths, reverse=True):
        remaining = limit - len(newest)
        if remaining <= 0:
            break
        if path.suffix == ".gz":
            # gzip members are not efficiently seekable; this branch is only
            # for explicit callers (the dashboard excludes raw).
            chunk = list(iter_partitioned_events(path.parent.parent.parent.parent, stream))
            newest[0:0] = chunk[-remaining:]
            continue
        chunks: list[bytes] = []
        lines: list[bytes] = []
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            while position > 0 and len(lines) <= remaining:
                size = min(64 * 1024, position)
                position -= size
                handle.seek(position)
                chunks.insert(0, handle.read(size))
                lines = b"".join(chunks).splitlines()
        for line in lines[-remaining:]:
            if line.strip():
                newest.append(json.loads(line))
    newest.sort(key=lambda event: int(event.get("receivedTimeUs", 0) or 0))
    return newest[-limit:]


def data_usage(root: Path, recorder: Optional[MonitoringRecorder] = None) -> Dict[str, Any]:
    directory_bytes = 0
    stream_bytes: Dict[str, int] = {}
    for stream in STREAMS:
        total = 0
        stream_root = root / stream
        if stream_root.exists():
            for path in stream_root.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        stream_bytes[stream] = total
    if root.exists():
        directory_bytes = sum(path.stat().st_size for path in root.rglob("*") if path.is_file())
    stream_bytes_total = sum(stream_bytes.values())
    status = recorder.status() if recorder is not None else {
        "rawQueueDepth": 0,
        "rawQueueCapacity": 0,
        "rawDropped": 0,
        "acceptingOrders": True,
        "liveMode": "unknown",
        "root": str(root),
    }
    return {
        "root": str(root),
        "directoryBytes": directory_bytes,
        "streamBytes": stream_bytes,
        "otherBytes": max(0, directory_bytes - stream_bytes_total),
        "runtimeStateBytes": (root / RUNTIME_STATE_FILENAME).stat().st_size if (root / RUNTIME_STATE_FILENAME).exists() else 0,
        "rawQueueDepth": status["rawQueueDepth"],
        "rawQueueCapacity": status["rawQueueCapacity"],
        "rawDropped": status["rawDropped"],
        "acceptingOrders": status["acceptingOrders"],
        "liveMode": status["liveMode"],
    }
