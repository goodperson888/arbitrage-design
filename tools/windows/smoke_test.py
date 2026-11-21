"""Exercise the built embedded runtime and real workers, without live orders."""
from __future__ import annotations

import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request


def smoke(root: Path) -> None:
    first_party = [path.relative_to(root) for path in root.rglob("*")
                   if path.is_file() and not path.relative_to(root).parts[0] == "runtime"]
    assert [path.as_posix() for path in first_party if path.suffix == ".md"] == ["使用说明.md"]
    assert not any({"docs", "design-system", "analysis", "tests", ".github"}.intersection(path.parts)
                   for path in first_party), "Development files leaked into customer package"
    manifest = json.loads((root / "application-manifest.json").read_text(encoding="utf-8"))
    assert all((root / name).is_file() for name in manifest["files"])
    python = root / "runtime/python.exe"
    manager = root / "tools/windows/manager.py"
    subprocess.run([str(python), str(root / "tools/windows/verify_runtime.py")],
                   cwd=root, check=True, timeout=60)
    with tempfile.TemporaryDirectory(prefix="Arbitrage Data 中文 ") as temporary:
        data = Path(temporary)
        config = data / "settings.json"
        env = dict(os.environ, ARB_SETTINGS_FILE=str(config), PYTHONUTF8="1",
                   ARB_GATE_WS_URL="ws://127.0.0.1:9", ARB_NO_PAUSE="1", BROWSER="echo")
        # A reachable local listener, but never a real exchange proxy or order.
        class OfflineProxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                self.send_error(503)
            do_GET = do_CONNECT
            def log_message(self, *args):
                pass
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), OfflineProxy)
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        def run(*args):
            subprocess.run([str(python), "-X", "utf8", "-u", str(manager), *args],
                           env=env, cwd=root, check=True, timeout=150)

        def button(name, answers=None):
            subprocess.run(f'"{root / name}"', shell=True, input=answers, encoding="utf-8",
                           env=env, cwd=root, check=True, timeout=150)
        services = ("paper", "cross", "cross15", "gate", "realtime", "live")
        class UnrelatedService(BaseHTTPRequestHandler):
            requests = 0
            def do_GET(self):
                type(self).requests += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"Unrelated service must not be opened")
            def log_message(self, *args):
                pass
        occupied = []
        try:
            # Exercise the real Windows binding path with ALL preferred web
            # ports already held by unrelated processes/services.
            for port in (8765, 8766, 8787):
                server = ThreadingHTTPServer(("127.0.0.1", port), UnrelatedService)
                occupied.append(server)
                threading.Thread(target=server.serve_forever, daemon=True).start()
            button("开启.cmd", f"{data / 'persistent'}\n{proxy.server_port}\n")
            assert json.loads(config.read_text(encoding="utf-8"))["proxy"] == f"http://127.0.0.1:{proxy.server_port}"
            for service in ("realtime", "live"):
                run("start", service)
            ports = {}
            for service in services:
                with (data / f"persistent/.control/{service}.json").open(encoding="utf-8") as handle:
                    state = json.load(handle)
                    assert state["status"] == "running", f"{service} exited during startup"
                    if service in {"paper", "live", "realtime"}:
                        ports[service] = state["port"]
                        assert state["port"] not in (8765, 8766, 8787), "Must bind a separate port"
            # Starting twice must not create a second writer.
            before = (data / "persistent/.control/paper.json").read_bytes()
            button("开启.cmd", "")
            assert (data / "persistent/.control/paper.json").read_bytes() == before
            for service, port in ports.items():
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    assert response.status == 200
                    assert b"<html" in response.read().lower() or service == "live"
            run("open", "paper")
            assert UnrelatedService.requests == 0, "Launcher must never probe/open another service"
            run("status")
        except BaseException:
            for log in sorted((data / "persistent/launcher_logs").glob("*.log")):
                print(f"--- {log.name} ---", flush=True)
                print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-45:]), flush=True)
            raise
        finally:
            button("停止.cmd", "")
            proxy.shutdown()
            proxy.server_close()
            for server in occupied:
                server.shutdown()
                server.server_close()
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
        print("WINDOWS PORTABLE SMOKE PASSED: two buttons, first-use port setup, six services, occupied port fallback, actual URL, duplicate guard, HTTP, graceful stop, gzip, ledger restart")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    smoke(parser.parse_args().root.resolve())
