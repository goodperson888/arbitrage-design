from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import duckdb
except ImportError:
    duckdb = None  # type: ignore


@dataclass
class QueryConfig:
    """Configuration for DuckDB analytics."""
    database_path: Path | None = None  # None = in-memory database
    parquet_dir: Path | None = None
    ndjson_file: Path | None = None


class DuckDBAnalytics:
    """DuckDB-based analytics for market event data."""
    
    def __init__(self, config: QueryConfig):
        if duckdb is None:
            raise ImportError("duckdb is required for analytics. Install with: pip install duckdb")
        
        self.config = config
        
        # Initialize DuckDB connection
        if config.database_path:
            config.database_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = duckdb.connect(str(config.database_path))
        else:
            self.conn = duckdb.connect()
        
        # Install and load extensions
        self._setup_extensions()
        
        # Load data if provided
        if config.parquet_dir:
            self._load_parquet_data(config.parquet_dir)
        elif config.ndjson_file:
            self._load_ndjson_data(config.ndjson_file)
    
    def _setup_extensions(self) -> None:
        """Setup DuckDB extensions for Parquet support."""
        try:
            self.conn.execute("INSTALL parquet")
            self.conn.execute("LOAD parquet")
        except Exception as e:
            print(f"Warning: Could not setup parquet extension: {e}")
    
    def _load_parquet_data(self, parquet_dir: Path) -> None:
        """Load Parquet data into DuckDB."""
        if not parquet_dir.exists():
            raise FileNotFoundError(f"Parquet directory not found: {parquet_dir}")
        
        # Check if it's a partitioned dataset or single file
        if parquet_dir.is_dir():
            # Try to load as partitioned dataset
            parquet_files = list(parquet_dir.rglob("*.parquet"))
            if parquet_files:
                glob_pattern = str(parquet_dir / "**" / "*.parquet")
                self.conn.execute(f"CREATE OR REPLACE VIEW events AS SELECT * FROM read_parquet('{glob_pattern}', hive_partitioning=1)")
                print(f"Loaded partitioned Parquet dataset from {parquet_dir}")
            else:
                print(f"No Parquet files found in {parquet_dir}")
        else:
            # Load single file
            self.conn.execute(f"CREATE OR REPLACE VIEW events AS SELECT * FROM read_parquet('{parquet_dir}')")
            print(f"Loaded Parquet file: {parquet_dir}")
    
    def _load_ndjson_data(self, ndjson_file: Path) -> None:
        """Load NDJSON data into DuckDB."""
        if not ndjson_file.exists():
            raise FileNotFoundError(f"NDJSON file not found: {ndjson_file}")
        
        # Create table from NDJSON
        self.conn.execute(f"""
            CREATE OR REPLACE VIEW events AS 
            SELECT * FROM read_json_auto('{ndjson_file}')
        """)
        print(f"Loaded NDJSON file: {ndjson_file}")
    
    def get_table_info(self) -> dict[str, Any]:
        """Get information about the events table."""
        try:
            # Get column info
            columns = self.conn.execute("DESCRIBE events").fetchall()
            
            # Get row count
            count = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            
            return {
                "columns": [{"name": col[0], "type": col[1]} for col in columns],
                "row_count": count,
            }
        except Exception as e:
            return {"error": str(e)}
    
    def query(self, sql: str) -> list[tuple]:
        """Execute a SQL query and return results as list of tuples."""
        try:
            return self.conn.execute(sql).fetchall()
        except Exception as e:
            print(f"Query error: {e}")
            return []
    
    def get_event_summary(self) -> dict[str, Any]:
        """Get summary statistics of events."""
        summary = {}
        
        try:
            # Total count
            summary["total_events"] = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            
            # By event type
            event_types = self.conn.execute("""
                SELECT eventType, COUNT(*) as count 
                FROM events 
                GROUP BY eventType 
                ORDER BY count DESC
            """).fetchall()
            summary["by_event_type"] = {row[0]: row[1] for row in event_types}
            
            # By venue
            venues = self.conn.execute("""
                SELECT venue, COUNT(*) as count 
                FROM events 
                GROUP BY venue 
                ORDER BY count DESC
            """).fetchall()
            summary["by_venue"] = {row[0]: row[1] for row in venues}
            
            # Time range
            time_range = self.conn.execute("""
                SELECT 
                    MIN(sourceTimeUs) as min_time,
                    MAX(sourceTimeUs) as max_time
                FROM events
            """).fetchone()
            summary["time_range"] = {
                "min_source_time_us": time_range[0],
                "max_source_time_us": time_range[1],
            }
            
            # Data quality - missing fields
            missing_fields = self.conn.execute("""
                SELECT 
                    COUNT(*) - COUNT(eventId) as missing_eventId,
                    COUNT(*) - COUNT(marketId) as missing_marketId,
                    COUNT(*) - COUNT(venue) as missing_venue,
                    COUNT(*) - COUNT(eventType) as missing_eventType
                FROM events
            """).fetchone()
            summary["data_quality"] = {
                "missing_eventId": missing_fields[0],
                "missing_marketId": missing_fields[1],
                "missing_venue": missing_fields[2],
                "missing_eventType": missing_fields[3],
            }
        
        except Exception as e:
            summary["error"] = str(e)
        
        return summary
    
    def get_market_summary(self, market_id: str) -> dict[str, Any]:
        """Get summary for a specific market."""
        try:
            market_summary = {}
            
            # Basic info
            basic_info = self.conn.execute(f"""
                SELECT 
                    venue,
                    eventType,
                    COUNT(*) as event_count,
                    MIN(sourceTimeUs) as first_event_time,
                    MAX(sourceTimeUs) as last_event_time
                FROM events 
                WHERE marketId = '{market_id}'
                GROUP BY venue, eventType
                ORDER BY eventType
            """).fetchall()
            
            market_summary["by_event_type"] = [
                {
                    "venue": row[0],
                    "event_type": row[1],
                    "count": row[2],
                    "first_time": row[3],
                    "last_time": row[4],
                }
                for row in basic_info
            ]
            
            # Settlement info if available
            settlement = self.conn.execute(f"""
                SELECT payload 
                FROM events 
                WHERE marketId = '{market_id}' AND eventType = 'SETTLEMENT'
                LIMIT 1
            """).fetchone()
            
            if settlement:
                import json
                market_summary["settlement"] = json.loads(settlement[0]) if settlement[0] else None
            
            return market_summary
        
        except Exception as e:
            return {"error": str(e)}
    
    def analyze_price_quotes(self, market_id: str, outcome: str | None = None) -> dict[str, Any]:
        """Analyze price quotes for a market."""
        try:
            where_clause = f"marketId = '{market_id}'"
            if outcome:
                where_clause += f" AND outcome = '{outcome}'"
            
            analysis = {}
            
            # Price statistics
            price_stats = self.conn.execute(f"""
                SELECT 
                    AVG(CAST(payload->>'price' AS DOUBLE)) as avg_price,
                    MIN(CAST(payload->>'price' AS DOUBLE)) as min_price,
                    MAX(CAST(payload->>'price' AS DOUBLE)) as max_price,
                    STDDEV(CAST(payload->>'price' AS DOUBLE)) as stddev_price
                FROM events 
                WHERE {where_clause} 
                AND eventType IN ('PRICE_QUOTE', 'ORDERBOOK_SNAPSHOT', 'ORDERBOOK_DELTA')
                AND payload->>'price' IS NOT NULL
            """).fetchone()
            
            analysis["price_statistics"] = {
                "avg": price_stats[0],
                "min": price_stats[1],
                "max": price_stats[2],
                "stddev": price_stats[3],
            }
            
            # Quote frequency
            quote_frequency = self.conn.execute(f"""
                SELECT 
                    DATE_TRUNC('minute', to_timestamp(sourceTimeUs / 1000000)) as minute,
                    COUNT(*) as quote_count
                FROM events 
                WHERE {where_clause}
                AND eventType IN ('PRICE_QUOTE', 'ORDERBOOK_SNAPSHOT', 'ORDERBOOK_DELTA')
                GROUP BY minute
                ORDER BY minute
            """).fetchall()
            
            analysis["quote_frequency"] = [
                {"minute": str(row[0]), "count": row[1]} for row in quote_frequency
            ]
            
            return analysis
        
        except Exception as e:
            return {"error": str(e)}
    
    def export_query_to_csv(self, sql: str, output_path: Path) -> bool:
        """Export query results to CSV file."""
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self.conn.execute(f"COPY ({sql}) TO '{output_path}' (FORMAT CSV, HEADER)")
            print(f"Exported query results to {output_path}")
            return True
        except Exception as e:
            print(f"Export error: {e}")
            return False
    
    def close(self) -> None:
        """Close the database connection."""
        self.conn.close()
    
    def __enter__(self) -> "DuckDBAnalytics":
        return self
    
    def __exit__(self, *args: Any) -> None:
        self.close()