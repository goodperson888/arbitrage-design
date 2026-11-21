from __future__ import annotations

import json
import gzip
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    pa = None  # type: ignore
    pq = None  # type: ignore

from ..models import MarketEvent


@dataclass
class ConversionConfig:
    """Configuration for NDJSON to Parquet conversion."""
    input_file: Path
    output_dir: Path
    row_group_size: int = 10000
    compression: str = "snappy"  # Options: snappy, gzip, brotli, lz4
    partition_by: list[str] | None = None  # e.g., ["venue", "event_type"]


class NDJSONToParquetConverter:
    """Convert NDJSON MarketEvent files to Parquet format for better query performance."""
    
    def __init__(self, config: ConversionConfig):
        if pa is None or pq is None:
            raise ImportError("pyarrow is required for Parquet conversion. Install with: pip install pyarrow")
        
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
    
    def _event_to_dict(self, event: MarketEvent) -> dict[str, Any]:
        """Convert MarketEvent to flat dictionary for Parquet."""
        return {
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
            # Flatten source dict
            "sourceType": event.source.get("type") if event.source else None,
            "sourceCollector": event.source.get("collector") if event.source else None,
            "sourceConnectorVersion": event.source.get("connectorVersion") if event.source else None,
            # Store payload as JSON string for flexibility
            "payloadJson": json.dumps(event.payload) if event.payload else None,
        }
    
    def _create_schema(self) -> pa.Schema:
        """Create PyArrow schema for MarketEvent."""
        return pa.schema([
            ("schemaVersion", pa.int32()),
            ("eventId", pa.string()),
            ("eventType", pa.string()),
            ("venue", pa.string()),
            ("marketId", pa.string()),
            ("instrumentId", pa.string()),
            ("outcome", pa.string()),
            ("sourceTimeUs", pa.int64()),
            ("receivedTimeUs", pa.int64()),
            ("sequence", pa.string()),
            ("sourceType", pa.string()),
            ("sourceCollector", pa.string()),
            ("sourceConnectorVersion", pa.string()),
            ("payloadJson", pa.string()),
        ])
    
    def _read_ndjson(self) -> Iterator[dict[str, Any]]:
        """Read and parse NDJSON file."""
        opener = gzip.open if self.config.input_file.suffix == ".gz" else open
        with opener(self.config.input_file, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    event = MarketEvent.from_dict(row)
                    yield self._event_to_dict(event)
                except Exception as e:
                    print(f"Error parsing line: {e}")
                    continue
    
    def convert(self) -> Path:
        """Convert NDJSON file to Parquet."""
        schema = self._create_schema()
        
        # Read all events
        events = list(self._read_ndjson())
        
        if not events:
            print(f"No events found in {self.config.input_file}")
            return self.config.output_dir
        
        print(f"Converting {len(events)} events from {self.config.input_file}")
        
        # Create PyArrow table
        table = pa.Table.from_pylist(events, schema=schema)
        
        # Determine output file path
        output_file = self.config.output_dir / self.config.input_file.with_suffix(".parquet").name
        
        # Write to Parquet
        writer_properties = None
        if self.config.compression:
            compression_map = {
                "snappy": pa.Codec("snappy"),
                "gzip": pa.Codec("gzip"),
                "brotli": pa.Codec("brotli"),
                "lz4": pa.Codec("lz4"),
            }
            compression_codec = compression_map.get(self.config.compression.lower())
            if compression_codec:
                writer_properties = pa.ParquetWriterProperties(
                    compression=compression_codec,
                )
        
        # Write with partitioning if specified
        if self.config.partition_by:
            # Write to partitioned dataset
            pq.write_to_dataset(
                table,
                root_path=str(self.config.output_dir),
                partition_cols=self.config.partition_by,
                compression=self.config.compression if self.config.compression else None,
                row_group_size=self.config.row_group_size,
            )
            print(f"Created partitioned dataset in {self.config.output_dir}")
        else:
            # Write to single file
            pq.write_table(
                table,
                output_file,
                row_group_size=self.config.row_group_size,
                compression=self.config.compression if self.config.compression else None,
                writer_properties=writer_properties,
            )
            print(f"Created Parquet file: {output_file}")
        
        return output_file if not self.config.partition_by else self.config.output_dir
    
    def get_conversion_stats(self) -> dict[str, Any]:
        """Get statistics about the conversion."""
        events = list(self._read_ndjson())
        
        if not events:
            return {"event_count": 0, "file_size_mb": 0}
        
        # Calculate statistics
        event_types = {}
        venues = {}
        
        for event in events:
            event_type = event.get("eventType", "unknown")
            venue = event.get("venue", "unknown")
            
            event_types[event_type] = event_types.get(event_type, 0) + 1
            venues[venue] = venues.get(venue, 0) + 1
        
        input_size = self.config.input_file.stat().st_size if self.config.input_file.exists() else 0
        
        return {
            "event_count": len(events),
            "input_size_mb": input_size / (1024 * 1024),
            "event_types": event_types,
            "venues": venues,
        }
