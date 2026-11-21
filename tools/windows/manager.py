"""Portable launcher. Owns only its workers; never kills by name or PID."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import sys
import time
from uuid import uuid4
import webbrowser
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
for project in ("arbitrage-research", "arbitrage-live", "arbitrage-realtime"):
    sys.path.insert(0, str(ROOT / project / "src"))

from arb_research.platform_io import exclusive_lock

COLLECTORS = ("paper", "cross", "cross15", "gate")
SERVICES = COLLECTORS + ("realtime", "live")
PORTS = {"paper": 8765, "live": 8766, "realtime": 8787}


def settings_path() -> Path:
    return Path(os.environ.get("ARB_SETTINGS_FILE") or
                Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) /
                "ArbitrageDesign" / "launcher.json")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, indent=2)
        target.flush()
        os.fsync(target.fileno())
    os.replace(temporary, path)


def validate_settings(config: dict) -> dict:
    config = dict(config)
    directory = Path(config["data_root"]).expanduser()
    if not directory.is_absolute() or directory == Path(directory.anchor):
        raise ValueError("数据目录必须是绝对路径，并且不能是磁盘根目录")
    # Don't let an upgrade replace the data together with the application.
    if directory.resolve().is_relative_to(ROOT):
        raise ValueError("数据必须保存在程序目录以外，例如 D:\\ArbData")
    for current in (directory, *directory.parents):
        if current.exists() and (current.is_symlink() or
                getattr(current.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("数据目录不能经过符号链接或 junction")
    directory.mkdir(parents=True, exist_ok=True)
    config["data_root"] = str(directory.resolve())
    proxy = str(config.get("proxy", ""))
    config["proxy"] = parse_proxy(proxy)
    return config


def load_settings() -> dict:
    path = settings_path()
    if not path.exists():
        raise ValueError("尚未配置，请双击 开启.cmd 完成首次设置。")
    return validate_settings(json.loads(path.read_text(encoding="utf-8-sig")))


def parse_proxy(value: str) -> str:
    value = value.strip()
    if value.lower() in {"", "0", "none", "direct"}:
        return ""
    if value.isascii() and value.isdigit():
        value = f"http://127.0.0.1:{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise ValueError("请输入代理端口（1–65535），或完整 http(s)/socks5 地址；0 表示直连")
    if parsed.port is None or not 1 <= parsed.port <= 65535 or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("代理需要有效端口（1–65535），且不能含路径、查询或片段")
    return value


def prompt_proxy(default: str = "http://127.0.0.1:7890") -> str:
    display = default.removeprefix("http://127.0.0.1:") or "0"
    while True:
        try:
            return parse_proxy(input(f"本机代理端口 [{display}]，0=不使用代理: ").strip() or default)
        except ValueError as exc:
            print(exc)


def configure() -> None:
    path = settings_path()
    current = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    default = current.get("data_root", str(path.parent / "data"))
    directory = input(f"数据目录 [{default}]: ").strip().strip('"') or default
    # The downloader already asked when preparing a missing portable runtime.
    setup_proxy = os.environ.get("ARB_SETUP_PROXY", "") if os.environ.get("ARB_SETUP_PROXY_SET") else None
    proxy = parse_proxy(setup_proxy) if setup_proxy is not None else prompt_proxy(current.get("proxy", "http://127.0.0.1:7890"))
    if path.exists():
        old = load_settings()
        if any(is_running(Path(old["data_root"]), name) for name in SERVICES):
            raise ValueError("请先停止全部服务，再修改数据目录或代理")
    config = validate_settings({"data_root": directory, "proxy": proxy})
    write_json(path, config)
    print(f"配置已保存：{path}\n数据：{config['data_root']}\n代理：{proxy or '直连'}")


def check_local_proxy(config: dict) -> dict:
    """Check the configured listener, not access to any particular exchange."""
    while config.get("proxy"):
        parsed = urlsplit(config["proxy"])
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            break
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
                pass
            break
        except OSError:
            print(f"本机代理端口 {parsed.port} 未连接。请先开启代理软件，或填写正确端口。")
            if any(is_running(Path(config["data_root"]), name) for name in SERVICES):
                raise ValueError("已有服务使用当前配置；请先双击 停止.cmd，再修改代理")
            config = dict(config, proxy=prompt_proxy(config["proxy"]))
            write_json(settings_path(), config)
    return config


def control_dir(directory: Path) -> Path:
    target = directory / ".control"
    if target.exists() and (target.is_symlink() or getattr(target.lstat(), "st_file_attributes", 0) & 0x400):
        raise ValueError("控制目录不能是符号链接或 junction")
    target.mkdir(parents=True, exist_ok=True)
    return target


def is_running(directory: Path, name: str) -> bool:
    try:
        lock = exclusive_lock(control_dir(directory) / f"{name}.lock")
    except BlockingIOError:
        return True
    lock.close()
    return False


def command_for(name: str, directory: Path, proxy: str, stop: Path) -> tuple[list[str], Path]:
    research = ROOT / "arbitrage-research"
    common = ["--stop-file", str(stop)]
    if name in {"paper", "cross", "cross15"}:
        script, subdir = {
            "paper": ("run_research_paper_monitor.py", "live_monitor"),
            "cross": ("run_cross_market_5m_validation.py", "cross_market_validation"),
            "cross15": ("run_cross_market_15m_validation.py", "cross_market_validation_15m"),
        }[name]
        command = [str(research / "scripts/monitor" / script), "--data-dir", str(directory / subdir), *common]
        if name == "paper":
            command += ["--data-root", str(directory), "--port", str(PORTS[name])]
        if proxy:
            command += ["--proxy", proxy]
        return command, research
    if name == "gate":
        node = ROOT / "runtime/node/node.exe"
        binary = str(node) if node.exists() else shutil.which("node")
        if not binary:
            raise ValueError("未找到 Node.js；请使用完整 Windows 便携包")
        command = [binary, str(research / "scripts/monitor/run_gate_direct_monitor.mjs"),
                "--data-dir", str(directory / "gate_monitor"),
                "--live-state", str(directory / "live_monitor/state/latest.json"),
                "--snapshot-seconds", "10", "--max-levels", "3", *common]
        if os.environ.get("ARB_GATE_WS_URL"):
            command += ["--websocket-url", os.environ["ARB_GATE_WS_URL"]]
        return command, research
    if name == "realtime":
        command = ["-m", "realtime_probability.web", "--history-path", str(directory / "realtime/facts.ndjson"), *common]
        if proxy:
            command += ["--proxy", proxy]
        return command, ROOT / "arbitrage-realtime"
    if name == "live":
        # Starts the management UI only. Trading still requires the existing
        # explicit UI action, credentials and complete preflight.
        return ["-m", "live_trader.cli", "dashboard", "--data-dir", str(directory / "live_trading"), *common], ROOT / "arbitrage-live"
    raise ValueError(f"未知服务：{name}")


@contextmanager
def manager_lock(directory: Path):
    try:
        handle = exclusive_lock(control_dir(directory) / "manager.lock")
    except BlockingIOError:
        raise ValueError("另一个启动/停止操作正在进行，请稍后重试") from None
    with handle:
        yield


def start_one(config: dict, name: str) -> None:
    directory = Path(config["data_root"])
    if is_running(directory, name):
        print(f"{name} 已运行，不重复启动")
        return
    # Check prerequisites before starting a detached worker.
    command_for(name, directory, config.get("proxy", ""), control_dir(directory) / f"{name}.stop")
    if name in PORTS:
        import socket
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", PORTS[name])) == 0:
                raise ValueError(f"端口 {PORTS[name]} 已被其他进程占用；未启动 {name}")
    stop = control_dir(directory) / f"{name}.stop"
    stop.unlink(missing_ok=True)
    boot = uuid4().hex
    logs = directory / "launcher_logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{boot[:8]}.log"
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with log.open("ab") as target:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-u", str(Path(__file__).resolve()),
                                    "worker", name, "--boot", boot], cwd=str(ROOT), env=env,
                                   stdout=target, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                   creationflags=flags, start_new_session=os.name != "nt")
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name} 启动失败，查看日志：{log}")
        if is_running(directory, name):
            if name not in PORTS:
                time.sleep(0.5)
                if process.poll() is not None:
                    raise RuntimeError(f"{name} 初始化失败，查看日志：{log}")
                break
            import urllib.request
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f"http://127.0.0.1:{PORTS[name]}/", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
        time.sleep(0.1)
    else:
        stop.touch()
        raise RuntimeError(f"{name} 启动未通过检查，已请求正常停止；日志：{log}")
    print(f"{name} 已启动；日志：{log}")


def stop_one(directory: Path, name: str, timeout: float = 60) -> bool:
    if not is_running(directory, name):
        print(f"{name} 已停止")
        return True
    (control_dir(directory) / f"{name}.stop").touch()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(directory, name):
            print(f"{name} 已正常停止，数据保留")
            return True
        time.sleep(0.2)
    print(f"{name} 仍在收尾；已请求停止，未强制终止。稍后再检查状态。")
    return False


def worker(config: dict, name: str, boot: str) -> int:
    directory = Path(config["data_root"])
    control = control_dir(directory)
    command, cwd = command_for(name, directory, config.get("proxy", ""), control / f"{name}.stop")
    with exclusive_lock(control / f"{name}.lock"):
        state = {"service": name, "pid": os.getpid(), "boot": boot,
                 "startedAt": datetime.now(timezone.utc).isoformat(), "status": "running"}
        write_json(control / f"{name}.json", state)
        previous = Path.cwd()
        try:
            os.chdir(cwd)
            if name == "gate":
                return subprocess.call(command)
            sys.argv = command
            if command[0] == "-m":
                sys.argv = [command[1], *command[2:]]
                runpy.run_module(command[1], run_name="__main__")
            else:
                runpy.run_path(command[0], run_name="__main__")
            return 0
        finally:
            os.chdir(previous)
            state.update(status="stopped", stoppedAt=datetime.now(timezone.utc).isoformat())
            write_json(control / f"{name}.json", state)


def status(config: dict) -> None:
    directory = Path(config["data_root"])
    print(f"数据目录：{directory}\n代理：{config.get('proxy') or '直连'}")
    for name in SERVICES:
        running = is_running(directory, name)
        url = f" http://127.0.0.1:{PORTS[name]}/" if name in PORTS else ""
        print(f"{name:8} {'运行中' if running else '已停止'}{url if running else ''}")
    for name, folder in (("paper", "live_monitor"), ("cross", "cross_market_validation"), ("cross15", "cross_market_validation_15m")):
        path = directory / folder / "state/latest.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                service = data.get("service", {})
                print(f"  {name} 行情状态={service.get('status', '?')} 更新距今={max(0, time.time()-path.stat().st_mtime):.1f}秒")
            except (OSError, ValueError):
                print(f"  {name} 状态文件更新中，稍后重试")


def doctor(config: dict) -> None:
    import importlib
    for name in ("httpx", "requests", "websockets", "python_socks", "pyarrow", "duckdb", "polars", "tzdata", "py_clob_client_v2", "eth_account"):
        importlib.import_module(name)
        print(f"依赖正常：{name}")
    from zoneinfo import ZoneInfo
    ZoneInfo("Asia/Shanghai")
    for name in SERVICES:
        command_for(name, Path(config["data_root"]), config.get("proxy", ""), Path(config["data_root"]) / ".control" / f"{name}.stop")
    print(f"检查完成；数据目录：{config['data_root']}。联网状态请启动后在页面查看。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Arbitrage Design Windows 便携版管理器")
    parser.add_argument("action", choices=("launch", "configure", "start", "stop", "status", "doctor", "open", "worker"))
    parser.add_argument("service", nargs="?", choices=SERVICES + ("all",), default="all")
    parser.add_argument("--boot", default="")
    args = parser.parse_args()
    try:
        if args.action == "configure":
            configure()
            return 0
        if args.action == "stop" and not settings_path().exists():
            print("尚未配置启动，无需停止。")
            return 0
        if args.action == "launch" and not settings_path().exists():
            configure()
        config = load_settings()
        launching = args.action == "launch"
        if launching:
            config = check_local_proxy(config)
            doctor(config)
            args.action = "start"
        if args.action == "worker":
            return worker(config, args.service, args.boot)
        if args.action == "status":
            status(config)
        elif args.action == "doctor":
            doctor(config)
        elif args.action == "open":
            webbrowser.open(f"http://127.0.0.1:{PORTS.get(args.service, 8765)}/")
        else:
            directory = Path(config["data_root"])
            names = (COLLECTORS if args.action == "start" else SERVICES) if args.service == "all" else (args.service,)
            with manager_lock(directory):
                if args.action == "start":
                    for name in names:
                        start_one(config, name)
                else:
                    # Request every stop first so shutdown runs concurrently.
                    for name in names:
                        (control_dir(directory) / f"{name}.stop").touch()
                    results = [stop_one(directory, name) for name in names]
                    if not all(results):
                        return 1
            if launching:
                status(config)
                webbrowser.open("http://127.0.0.1:8765/")
        return 0
    except (OSError, ValueError, RuntimeError, EOFError, KeyError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
