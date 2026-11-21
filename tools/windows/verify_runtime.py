"""Run with the embedded interpreter to verify the slim collector dependencies."""
import importlib.util
import json
from pathlib import Path
import tempfile


def verify() -> None:
    for module in ("numpy", "duckdb", "polars"):
        assert importlib.util.find_spec(module) is None, f"Offline-only dependency bundled: {module}"
    import pyarrow as pa
    import pyarrow.parquet as pq
    from arb_research.live import strategy_monitor, cross_market_validation
    from live_trader import dashboard
    from realtime_probability import web
    # Real Arrow buffers, native compression and Parquet read/write, without
    # NumPy or Arrow's test fixtures/compilation headers.
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "collector.parquet"
        rows = [{"time": 123, "price": 0.83, "side": "DOWN"}]
        pq.write_table(pa.Table.from_pylist(rows), path, compression="snappy")
        assert pq.ParquetFile(path).read().to_pylist() == rows
    report = json.loads((Path(__file__).resolve().parents[2] / "runtime/dependency-layout.json").read_text())
    assert report["profile"] == "collector" and report["omittedFiles"] > 0
    print("SLIM RUNTIME PASSED: collector imports, native Parquet roundtrip, no offline analysis libraries")


if __name__ == "__main__":
    verify()
