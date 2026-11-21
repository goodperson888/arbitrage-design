from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..models import MarketEvent
from ..io import read_events


class ArbDeskImporter:
    """Import and validate ArbDesk-recorded NDJSON files."""
    
    def __init__(self, arbdesk_recording_dir: Path):
        self.recording_dir = Path(arbdesk_recording_dir)
    
    def discover_files(self) -> list[Path]:
        """Discover all NDJSON files in the ArbDesk recording directory."""
        if not self.recording_dir.exists():
            raise FileNotFoundError(f"ArbDesk recording directory not found: {self.recording_dir}")
        
        # Look for both .ndjson and .ndjson.part files
        ndjson_files = list(self.recording_dir.rglob("*.ndjson"))
        part_files = list(self.recording_dir.rglob("*.ndjson.part"))
        
        all_files = ndjson_files + part_files
        return sorted(all_files)
    
    def validate_file_structure(self, file_path: Path) -> dict[str, Any]:
        """Validate that a file follows the expected ArbDesk structure."""
        path_parts = file_path.parts
        
        validation_result = {
            "valid": True,
            "issues": [],
            "path": str(file_path),
            "expected_pattern": "recordings/schema=v1/venue=<venue>/date=<YYYY-MM-DD>/hour=<HH>/part-<num>.ndjson",
        }
        
        # Check if file follows the expected pattern
        # Expected: .../venue=<venue>/date=<date>/hour=<hour>/part-<num>.ndjson
        try:
            # Find the parts that match our pattern
            venue_idx = None
            date_idx = None
            hour_idx = None
            
            for i, part in enumerate(path_parts):
                if part.startswith("venue="):
                    venue_idx = i
                elif part.startswith("date="):
                    date_idx = i
                elif part.startswith("hour="):
                    hour_idx = i
            
            if venue_idx is None or date_idx is None or hour_idx is None:
                validation_result["valid"] = False
                validation_result["issues"].append("File path does not follow expected directory structure")
                return validation_result
            
            # Check if indices are in correct order
            if not (venue_idx < date_idx < hour_idx):
                validation_result["valid"] = False
                validation_result["issues"].append("Directory structure order is incorrect")
                return validation_result
            
            # Validate date format
            date_str = path_parts[date_idx].replace("date=", "")
            try:
                from datetime import datetime
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                validation_result["valid"] = False
                validation_result["issues"].append(f"Invalid date format: {date_str}")
            
            # Validate hour format
            hour_str = path_parts[hour_idx].replace("hour=", "")
            try:
                hour_int = int(hour_str)
                if not (0 <= hour_int <= 23):
                    validation_result["valid"] = False
                    validation_result["issues"].append(f"Invalid hour: {hour_int}")
            except ValueError:
                validation_result["valid"] = False
                validation_result["issues"].append(f"Invalid hour format: {hour_str}")
        
        except Exception as e:
            validation_result["valid"] = False
            validation_result["issues"].append(f"Error validating structure: {e}")
        
        return validation_result
    
    def validate_events(self, file_path: Path, sample_size: int = 10) -> dict[str, Any]:
        """Validate that events in the file follow the MarketEvent schema."""
        validation_result = {
            "valid": True,
            "issues": [],
            "path": str(file_path),
            "sample_count": 0,
            "total_count": 0,
        }
        
        try:
            event_count = 0
            for event in read_events(file_path):
                event_count += 1
                
                # Validate schema version
                if event.schema_version != 1:
                    validation_result["valid"] = False
                    validation_result["issues"].append(f"Unsupported schema version: {event.schema_version}")
                
                # Check required fields
                if not event.event_type:
                    validation_result["valid"] = False
                    validation_result["issues"].append("Missing event_type")
                
                if not event.venue:
                    validation_result["valid"] = False
                    validation_result["issues"].append("Missing venue")
                
                if not event.market_id:
                    validation_result["valid"] = False
                    validation_result["issues"].append("Missing market_id")
                
                # Sample validation
                if event_count <= sample_size:
                    validation_result["sample_count"] += 1
                
                # Only validate first few events deeply
                if event_count > sample_size:
                    break
            
            validation_result["total_count"] = event_count
        
        except Exception as e:
            validation_result["valid"] = False
            validation_result["issues"].append(f"Error reading events: {e}")
        
        return validation_result
    
    def import_file(self, file_path: Path) -> Iterator[MarketEvent]:
        """Import events from a single ArbDesk file."""
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        
        # Handle .part files (crash recovery)
        if file_path.suffix == ".part":
            print(f"Warning: Importing from .part file (may be incomplete): {file_path}")
        
        yield from read_events(file_path)
    
    def import_directory(self) -> Iterator[MarketEvent]:
        """Import events from all files in the ArbDesk recording directory."""
        files = self.discover_files()
        
        if not files:
            print(f"No NDJSON files found in {self.recording_dir}")
            return
        
        print(f"Found {len(files)} files to import")
        
        for file_path in files:
            print(f"Importing: {file_path}")
            
            # Validate file structure
            structure_validation = self.validate_file_structure(file_path)
            if not structure_validation["valid"]:
                print(f"  Structure validation failed: {structure_validation['issues']}")
                continue
            
            # Validate events
            event_validation = self.validate_events(file_path)
            if not event_validation["valid"]:
                print(f"  Event validation failed: {event_validation['issues']}")
                continue
            
            print(f"  Validated: {event_validation['total_count']} events")
            
            # Import events
            yield from self.import_file(file_path)
    
    def get_import_summary(self) -> dict[str, Any]:
        """Get a summary of the ArbDesk recording directory."""
        files = self.discover_files()
        
        summary = {
            "recording_dir": str(self.recording_dir),
            "total_files": len(files),
            "venues": set(),
            "dates": set(),
            "total_events": 0,
            "valid_files": 0,
            "invalid_files": 0,
        }
        
        for file_path in files:
            # Extract venue and date from path
            for part in file_path.parts:
                if part.startswith("venue="):
                    summary["venues"].add(part.replace("venue=", ""))
                elif part.startswith("date="):
                    summary["dates"].add(part.replace("date=", ""))
            
            # Validate file
            structure_validation = self.validate_file_structure(file_path)
            event_validation = self.validate_events(file_path)
            
            if structure_validation["valid"] and event_validation["valid"]:
                summary["valid_files"] += 1
                summary["total_events"] += event_validation["total_count"]
            else:
                summary["invalid_files"] += 1
        
        summary["venues"] = sorted(summary["venues"])
        summary["dates"] = sorted(summary["dates"])
        
        return summary