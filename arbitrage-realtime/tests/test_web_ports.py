import socket
import threading
import time
import urllib.request

from realtime_probability import web


def test_realtime_page_uses_free_port_and_still_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(web.BinanceWorker, "start", lambda self: None)
    monkeypatch.setattr(web.BinanceWorker, "stop", lambda self: None)
    monkeypatch.setattr(web.BinanceWorker, "join", lambda self, **kwargs: None)
    ready = tmp_path / "realtime.port"
    stop = tmp_path / "realtime.stop"
    monkeypatch.setenv("ARB_WEB_READY_FILE", str(ready))
    failures = []
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        original = occupied.getsockname()[1]
        def run():
            try:
                web.run_server(port=original, history_path=str(tmp_path / "facts.ndjson"), stop_file=stop)
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline and not failures:
                time.sleep(.01)
            assert not failures
            actual = int(ready.read_text())
            assert actual != original
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{actual}/", timeout=2) as response:
                assert response.status == 200
        finally:
            stop.touch()
            thread.join(timeout=3)
        assert not thread.is_alive() and not failures
