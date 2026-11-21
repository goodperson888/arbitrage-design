"""Check/prepare a private runtime; never install into the user's system Python."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES = ("httpx", "requests", "websockets", "python_socks", "pyarrow",
                "numpy", "duckdb", "polars", "tzdata", "py_clob_client_v2", "eth_account")


def check(runtime: Path) -> None:
    for name in DEPENDENCIES:
        importlib.import_module(name)
    from zoneinfo import ZoneInfo
    ZoneInfo("Asia/Shanghai")
    subprocess.run([str(runtime / "node/node.exe"), "--version"], check=True, stdout=subprocess.DEVNULL)


def ensure_idle() -> None:
    spec = importlib.util.spec_from_file_location("arb_windows_manager", ROOT / "tools/windows/manager.py")
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    if manager.settings_path().exists():
        config = manager.load_settings()
        if any(manager.is_running(Path(config["data_root"]), name) for name in manager.SERVICES):
            raise RuntimeError("Stop all services before repairing the runtime. Existing data is preserved.")


def install(runtime: Path, downloads: Path) -> None:
    if runtime.resolve() != Path(sys.executable).resolve().parent:
        raise ValueError("Setup must use its own staged Python")
    wheels = downloads / "wheels"
    command = [sys.executable, "-m", "pip", "--isolated", "download", "--only-binary=:all:",
               "--dest", str(wheels), "-r", str(ROOT / "tools/windows/requirements.txt")]
    proxy = os.environ.get("ARB_SETUP_PROXY", "")
    if proxy:
        command += ["--proxy", proxy]
    subprocess.run(command, check=True)
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", "--no-index", "--no-compile",
                    "--find-links", str(wheels), "--target", str(runtime / "Lib/site-packages"),
                    "-r", str(ROOT / "tools/windows/requirements.txt")], check=True)
    # New process so imports see the freshly installed packages.
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "check", str(runtime)], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "install", "idle"))
    parser.add_argument("runtime", type=Path)
    parser.add_argument("downloads", type=Path, nargs="?")
    args = parser.parse_args()
    try:
        if args.action == "check":
            check(args.runtime)
        elif args.action == "idle":
            ensure_idle()
        else:
            install(args.runtime, args.downloads)
    except Exception as exc:
        print(f"Runtime setup: {exc}", file=sys.stderr)
        raise SystemExit(1)
