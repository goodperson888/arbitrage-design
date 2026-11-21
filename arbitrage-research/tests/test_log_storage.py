from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from arb_research.experiments.live_strategy_cache import connect_cache, refresh_event_cache
from arb_research.io import read_events
from arb_research.live.log_storage import (
    GIB, archive_candidates, archive_partition, disk_space_state, read_partition_rows,
)
from arb_research.live.strategy_monitor import (
    LiveMonitorConfig, PartitionedNDJSONStore, ResearchPaperMonitor,
)


class LogStorageTest(unittest.TestCase):
    def partition(self, root: Path, data: bytes = b'{"x":1}\n') -> Path:
        path = root / "events/date=2026-11-20/hour=01/part-001.ndjson"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_archive_preserves_exact_bytes_and_manifest(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            data = ('{"unicode":"中文", "value":1}\n' * 1000).encode()
            path = self.partition(root, data)
            self.assertEqual(archive_candidates(root), [path])
            result = archive_partition(root, path)
            self.assertFalse(path.exists())
            target = Path(str(path) + ".gz")
            self.assertEqual(gzip.decompress(target.read_bytes()), data)
            self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(result["lineCount"], 1000)
            self.assertLess(result["gzipBytes"], result["sourceBytes"])
            manifest = [json.loads(line) for line in (root / "storage_archive_manifest.ndjson").read_text().splitlines()]
            self.assertEqual([item["status"] for item in manifest], ["verified", "archived"])
            self.assertEqual(archive_candidates(root), [])

    def test_active_writer_is_skipped_then_archivable_after_close(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            store = PartitionedNDJSONStore(root)
            store.append("events", {"value": 1}, 1_700_000_000_000_000)
            path = archive_candidates(root)[0]
            try:
                self.assertEqual(archive_partition(root, path)["status"], "active")
                self.assertTrue(path.exists())
                store.append("events", {"value": 2}, 1_700_000_000_000_001)
            finally:
                store.close()
            self.assertEqual(archive_partition(root, path)["lineCount"], 2)

    def test_corrupt_existing_destination_never_removes_source(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.partition(root)
            Path(str(path) + ".gz").write_bytes(gzip.compress(b"different"))
            with self.assertRaises(ValueError):
                archive_partition(root, path)
            self.assertEqual(path.read_bytes(), b'{"x":1}\n')

    def test_resume_after_published_gzip_before_source_removal(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.partition(root)
            Path(str(path) + ".gz").write_bytes(gzip.compress(path.read_bytes()))
            result = archive_partition(root, path)
            self.assertEqual(result["status"], "archived")
            self.assertFalse(path.exists())

    def test_source_mutation_prevents_removal(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.partition(root)
            from arb_research.live.log_storage import _digest
            def changed(handle):
                value = _digest(handle)
                with path.open("ab") as writer:
                    writer.write(b'{"later":true}\n')
                return value
            with patch("arb_research.live.log_storage._digest", side_effect=changed):
                with self.assertRaises(RuntimeError):
                    archive_partition(root, path)
            self.assertTrue(path.exists())
            self.assertFalse(Path(str(path) + ".gz").exists())

    def test_symlinks_and_out_of_scope_paths_are_rejected(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = self.partition(root)
            outside = root / "preserve.ndjson"
            outside.write_bytes(b"keep")
            link = path.with_name("part-002.ndjson")
            link.symlink_to(outside)
            with self.assertRaises(ValueError):
                archive_candidates(root)
            with self.assertRaises(ValueError):
                archive_partition(root, outside)
            self.assertEqual(outside.read_bytes(), b"keep")

    def test_new_writer_and_archive_share_mixed_hour_without_missing_events(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            timestamp = 1_700_000_000_000_000
            old = PartitionedNDJSONStore(root)
            definition = {"schemaVersion": 1, "eventType": "MARKET_DEFINITION", "eventId": "definition",
                          "marketId": "market", "venue": "POLYMARKET", "receivedTimeUs": str(timestamp),
                          "sourceTimeUs": str(timestamp),
                          "payload": {"eventStartUs": timestamp, "eventEndUs": timestamp + 300_000_000}}
            old.append("events", definition, timestamp)
            old.close()
            source = archive_candidates(root)[0]
            writer = PartitionedNDJSONStore(root, compressed_streams=("events",))
            writer.append("events", {**definition, "eventType": "SETTLEMENT", "eventId": "settled",
                                     "payload": {"winnerOutcome": "UP"}}, timestamp + 1)
            try:
                self.assertEqual(archive_partition(root, source)["status"], "archived")
                connection = connect_cache(root / "cache.sqlite3")
                try:
                    result = refresh_event_cache(connection, root / "events")
                    market = connection.execute("SELECT winner FROM markets").fetchone()
                    self.assertEqual(market["winner"], "UP")
                    self.assertEqual(result["rowsRead"], 2)
                finally:
                    connection.close()
            finally:
                writer.close()
            paths = sorted((root / "events").glob("date=*/hour=*/part-*.ndjson.gz"))
            self.assertEqual([path.name for path in paths], ["part-001.ndjson.gz", "part-002.ndjson.gz"])
            self.assertEqual(sum(len(list(read_events(path))) for path in paths), 2)

    def test_disk_thresholds_and_failure(self) -> None:
        usage = shutil._ntuple_diskusage
        for free, total, expected in ((40, 200, "ok"), (15, 200, "warning"),
                                      (30, 500, "warning"), (4, 200, "critical"),
                                      (10, 1000, "critical")):
            with self.subTest(free=free, total=total):
                with patch("shutil.disk_usage", return_value=usage(total * GIB, (total-free) * GIB, free * GIB)):
                    self.assertEqual(disk_space_state(Path("."))["level"], expected)
        with patch("shutil.disk_usage", side_effect=OSError("unavailable")):
            self.assertEqual(disk_space_state(Path("."))["level"], "unknown")

    def test_export_reads_active_gzip_but_rejects_offline_truncation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            writer = PartitionedNDJSONStore(root, compressed_streams=("events",))
            writer.append("events", {"value": 1}, 1_700_000_000_000_000)
            path = next((root / "events").rglob("*.gz"))
            try:
                self.assertEqual(list(read_partition_rows(path)), [{"value": 1}])
            finally:
                writer.close()
            data = path.read_bytes()
            path.write_bytes(data[:-8])
            with self.assertRaises(EOFError):
                list(read_partition_rows(path))

    def test_restart_writes_new_part_after_truncated_prior_gzip(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            timestamp = 1_700_000_000_000_000
            first = PartitionedNDJSONStore(root, compressed_streams=("events",))
            first.append("events", {"value": 1}, timestamp)
            first.close()
            path = next((root / "events").rglob("*.gz"))
            path.write_bytes(path.read_bytes()[:-8])
            second = PartitionedNDJSONStore(root, compressed_streams=("events",))
            second.append("events", {"value": 2}, timestamp + 1)
            second.close()
            target = path.with_name("part-002.ndjson.gz")
            self.assertEqual(list(read_partition_rows(target)), [{"value": 2}])

    def test_disk_health_only_records_transitions(self) -> None:
        with TemporaryDirectory() as temporary:
            monitor = ResearchPaperMonitor(LiveMonitorConfig(data_dir=Path(temporary)))
            try:
                with patch.object(monitor, "_record_health") as record:
                    monitor._disk_state = {"level": "warning", "message": "low"}
                    monitor._report_disk_space()
                    monitor._report_disk_space()
                    self.assertEqual(record.call_count, 1)
                    monitor._disk_state = {"level": "ok", "message": "recovered"}
                    monitor._report_disk_space()
                    record.assert_called_with("INFO", "recovered")
                self.assertEqual(monitor.get_state()["storage"]["level"], "ok")
                self.assertEqual(monitor.store.compressed_streams, {"raw", "events", "signals"})
            finally:
                monitor.close()
