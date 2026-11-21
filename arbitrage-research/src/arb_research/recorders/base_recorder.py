from __future__ import annotations

import json
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import MarketEvent


@dataclass
class RecorderConfig:
    """Configuration for market data recorder."""
    base_dir: Path
    venue: str
    max_file_size_mb: int = 128
    max_buffer_size_kb: int = 128
    flush_interval_ms: int = 250
    queue_size: int = 10000


class BaseRecorder(ABC):
    """Base class for market data recorders with file rotation and async writing."""
    
    def __init__(self, config: RecorderConfig):
        self.config = config
        self._queue: queue.Queue[MarketEvent] = queue.Queue(maxsize=config.queue_size)
        self._running = False
        self._writer_thread: threading.Thread | None = None
        self._current_file: Path | None = None
        self._current_handle: Any = None
        self._current_size = 0
        self._buffer: list[str] = []
        self._last_flush_time = time.time()
        self._current_hour = None
        self._part_number = 1
        
        # Ensure base directory exists
        self.config.base_dir.mkdir(parents=True, exist_ok=True)
    
    def _get_file_path(self, current_time: datetime) -> Path:
        """Generate file path based on time-based partitioning."""
        hour = current_time.strftime("%Y-%m-%d/%H")
        filename = f"part-{self._part_number:03d}.ndjson"
        
        path = self.config.base_dir / f"venue={self.config.venue.lower()}" / f"date={current_time.strftime('%Y-%m-%d')}" / f"hour={current_time.strftime('%H')}" / filename
        return path
    
    def _should_rotate(self, current_time: datetime) -> bool:
        """Check if file rotation is needed."""
        # Rotate on hour change
        current_hour = current_time.strftime("%Y-%m-%d/%H")
        if current_hour != self._current_hour:
            self._current_hour = current_hour
            self._part_number = 1
            return True
        
        # Rotate on size limit
        if self._current_size >= self.config.max_file_size_mb * 1024 * 1024:
            self._part_number += 1
            return True
        
        return False
    
    def _close_current_file(self) -> None:
        """Close current file and rename from .part to final."""
        if self._current_handle is not None:
            self._current_handle.close()
            self._current_handle = None
        
        if self._current_file is not None:
            # Rename from .part to final
            final_file = self._current_file.with_suffix("")
            if self._current_file.exists():
                self._current_file.rename(final_file)
            self._current_file = None
        
        self._current_size = 0
    
    def _open_new_file(self, current_time: datetime) -> None:
        """Open a new file for writing."""
        self._close_current_file()
        
        new_file = self._get_file_path(current_time)
        new_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Open with .part extension for crash recovery
        self._current_file = new_file.with_suffix(".ndjson.part")
        self._current_handle = self._current_file.open("a", encoding="utf-8")
        self._current_size = self._current_file.stat().st_size if self._current_file.exists() else 0
    
    def _write_buffer(self) -> None:
        """Write buffered events to current file."""
        if not self._buffer or self._current_handle is None:
            return
        
        content = "".join(self._buffer)
        self._current_handle.write(content)
        self._current_handle.flush()
        self._current_size += len(content.encode("utf-8"))
        self._buffer.clear()
        self._last_flush_time = time.time()
    
    def _writer_loop(self) -> None:
        """Background writer thread that processes events from queue."""
        while self._running or not self._queue.empty():
            try:
                # Try to get event with timeout
                event = self._queue.get(timeout=0.1)
                
                # Convert event to JSON line
                event_dict = {
                    "schemaVersion": event.schema_version,
                    "eventId": event.event_id,
                    "eventType": event.event_type,
                    "venue": event.venue,
                    "marketId": event.market_id,
                    "instrumentId": event.instrument_id,
                    "outcome": event.outcome,
                    "sourceTimeUs": event.source_time_us,
                    "receivedTimeUs": event.received_time_us,
                    "sequence": event.sequence,
                    "source": event.source,
                    "payload": event.payload,
                }
                # Remove None values
                event_dict = {k: v for k, v in event_dict.items() if v is not None}
                json_line = json.dumps(event_dict, ensure_ascii=False, sort_keys=True) + "\n"
                
                # Check if we need to rotate file
                current_time = datetime.now(timezone.utc)
                if self._current_file is None or self._should_rotate(current_time):
                    self._write_buffer()
                    self._open_new_file(current_time)
                
                # Add to buffer
                self._buffer.append(json_line)
                
                # Check if buffer should be flushed
                buffer_size = sum(len(line.encode("utf-8")) for line in self._buffer)
                time_since_flush = (time.time() - self._last_flush_time) * 1000
                
                if buffer_size >= self.config.max_buffer_size_kb * 1024 or time_since_flush >= self.config.flush_interval_ms:
                    self._write_buffer()
                
            except queue.Empty:
                # Check if we should flush periodically
                time_since_flush = (time.time() - self._last_flush_time) * 1000
                if time_since_flush >= self.config.flush_interval_ms:
                    self._write_buffer()
                continue
            except Exception as e:
                print(f"Error in writer thread: {e}")
                continue
        
        # Final flush when stopping
        self._write_buffer()
        self._close_current_file()
    
    def start(self) -> None:
        """Start the recorder."""
        if self._running:
            return
        
        self._running = True
        self._current_hour = datetime.now(timezone.utc).strftime("%Y-%m-%d/%H")
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
    
    def stop(self) -> None:
        """Stop the recorder and wait for writer to finish."""
        if not self._running:
            return
        
        self._running = False
        if self._writer_thread is not None:
            self._writer_thread.join(timeout=10)
    
    def record_event(self, event: MarketEvent) -> bool:
        """Queue an event for recording. Returns False if queue is full."""
        if not self._running:
            return False
        
        try:
            self._queue.put_nowait(event)
            return True
        except queue.Full:
            return False
    
    def record_events(self, events: Iterable[MarketEvent]) -> int:
        """Queue multiple events for recording. Returns number of successfully queued events."""
        count = 0
        for event in events:
            if self.record_event(event):
                count += 1
        return count
    
    @abstractmethod
    def collect(self) -> None:
        """Start collecting data from the platform. Must be implemented by subclasses."""
        pass