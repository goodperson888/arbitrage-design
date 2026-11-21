import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "arbitrage-research/src"))
sys.path.insert(0, str(ROOT / "arbitrage-live/src"))

from arb_research.platform_io import exclusive_lock, open_regular
from arb_research.runtime_control import shutdown_control


def test_lock_excludes_other_process_and_is_released_on_close(tmp_path):
    path = tmp_path / "中文 ledger.lock"
    program = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from arb_research.platform_io import exclusive_lock
try:
    with exclusive_lock(Path(sys.argv[2])):
        print('acquired')
except BlockingIOError:
    print('blocked')
"""
    def probe():
        return subprocess.check_output([sys.executable, "-X", "utf8", "-c", program,
                                        str(ROOT / "arbitrage-research/src"), str(path)], text=True).strip()
    with exclusive_lock(path):
        assert probe() == "blocked"
        with pytest.raises(BlockingIOError):
            exclusive_lock(path)
    assert probe() == "acquired"


def test_lock_released_when_worker_exits_without_cleanup(tmp_path):
    path = tmp_path / "crash.lock"
    program = """
import sys, os
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from arb_research.platform_io import exclusive_lock
handle = exclusive_lock(Path(sys.argv[2]))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", program, str(ROOT / "arbitrage-research/src"), str(path)], check=True)
    with exclusive_lock(path):
        pass


def test_regular_file_guard_rejects_link_without_touching_target(tmp_path):
    target = tmp_path / "private"
    target.write_bytes(b"preserve")
    link = tmp_path / "lock"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink creation requires Developer Mode or privilege")
    with pytest.raises(ValueError):
        open_regular(link, os.O_RDWR)
    assert target.read_bytes() == b"preserve"


def test_stop_file_requests_callback_and_restores_handlers(tmp_path):
    import signal
    before = signal.getsignal(signal.SIGINT)
    stop = tmp_path / "paper.stop"
    async def exercise():
        requested = asyncio.Event()
        async with shutdown_control(requested.set, stop):
            stop.touch()
            await asyncio.wait_for(requested.wait(), 2)
    asyncio.run(exercise())
    assert signal.getsignal(signal.SIGINT) == before


@pytest.mark.skipif(os.name != "nt", reason="Real Windows Credential Manager integration")
def test_native_credentials_round_trip_without_plaintext_file():
    from uuid import uuid4
    from live_trader.windows_credentials import read_secret, write_secret, delete_secret
    target = f"ArbitrageDesign-CI-{uuid4().hex}"
    try:
        assert read_secret(target) == ""
        write_secret(target, "fixture-secret-中文")
        assert read_secret(target) == "fixture-secret-中文"
        write_secret(target, "replacement-fixture")
        assert read_secret(target) == "replacement-fixture"
    finally:
        delete_secret(target)
    assert read_secret(target) == ""
