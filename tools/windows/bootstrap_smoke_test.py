"""Exercise automatic setup on Windows with no installed app runtime."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def smoke(root: Path) -> None:
    bundled = root / "runtime"
    if bundled.exists():
        # The normal portable smoke already stopped every worker.
        bundled.rename(root / "runtime-from-package")
    with tempfile.TemporaryDirectory(prefix="Arbitrage Setup 中文 ") as temporary:
        settings = Path(temporary) / "launcher.json"
        settings.write_text(json.dumps({"data_root": str(Path(temporary) / "data"), "proxy": ""}), encoding="utf-8")
        env = dict(os.environ, ARB_SETTINGS_FILE=str(settings), ARB_NO_PAUSE="1")
        def run(*args):
            subprocess.run(["powershell.exe", "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(root / "tools/windows/launch.ps1"), *args],
                           env=env, cwd=root, check=True, timeout=600)
        run("doctor")
        assert (root / "runtime/python.exe").is_file()
        assert (root / "runtime/node/node.exe").is_file()
        assert not list(root.glob(".runtime-setup-*/")), "Temporary installers must be cleaned"
        # Reuses the completed installation and doesn't replace it.
        before = (root / "runtime/python.exe").stat().st_mtime_ns
        run("doctor")
        assert (root / "runtime/python.exe").stat().st_mtime_ns == before
        print("WINDOWS AUTOMATIC SETUP PASSED: no preinstalled runtime, verified downloads, dependencies, reuse, temporary cleanup")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    smoke(parser.parse_args().root.resolve())
