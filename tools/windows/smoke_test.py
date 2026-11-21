"""Exercise the built embedded runtime and real workers, without live orders."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.request


def smoke(root: Path) -> None:
    python = root / "runtime/python.exe"
    manager = root / "tools/windows/manager.py"
    with tempfile.TemporaryDirectory(prefix="Arbitrage Data 中文 ") as temporary:
        data = Path(temporary)
        config = data / "settings.json"
        config.write_text(json.dumps({"data_root": str(data / "persistent"), "proxy": "http://127.0.0.1:9"}), encoding="utf-8")
        env = dict(os.environ, ARB_SETTINGS_FILE=str(config), PYTHONUTF8="1",
                   ARB_GATE_WS_URL="ws://127.0.0.1:9")
        def run(*args):
            subprocess.run([str(python), "-X", "utf8", "-u", str(manager), *args],
                           env=env, cwd=root, check=True, timeout=150)

        run("doctor")
        services = ("paper", "cross", "cross15", "gate", "realtime", "live")
        try:
            for service in services:
                run("start", service)
            # Starting twice must not create a second writer.
            before = (data / "persistent/.control/paper.json").read_bytes()
            run("start", "paper")
            assert (data / "persistent/.control/paper.json").read_bytes() == before
            for port in (8765, 8766, 8787):
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    assert response.status == 200
                    assert b"<html" in response.read().lower() or port == 8766
            run("status")
        finally:
            run("stop", "all")
        # Closed gzip partitions must have trailers and be decompressible.
        for path in (data / "persistent").rglob("*.gz"):
            gzip.decompress(path.read_bytes())
        accounts = data / "persistent/live_monitor/candidates/v1/accounts.ndjson"
        assert accounts.exists(), "Candidate ledgers were not created"
        original = accounts.read_bytes()
        run("start", "paper")
        run("stop", "paper")
        assert accounts.read_bytes().startswith(original), "Restart replaced the prior ledger"
        # Includes the launcher and all six worker imports in an isolated,
        # embedded interpreter with external data and Unicode paths.
        print("WINDOWS PORTABLE SMOKE PASSED: six services, duplicate guard, HTTP, graceful stop, gzip, ledger restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    smoke(parser.parse_args().root.resolve())
