"""Explicit first-party file boundary for the customer Windows package."""
from __future__ import annotations

from pathlib import Path, PurePosixPath


# New documentation, analysis scripts and development tools are excluded by
# default. Runtime entry points and their non-package dependencies are explicit.
FILES = {
    name: name for name in (
        "开启.cmd", "停止.cmd",
        "tools/windows/Arbitrage.cmd", "tools/windows/launch.ps1",
        "tools/windows/manager.py", "tools/windows/runtime_setup.py",
        "tools/windows/package_layout.py", "tools/windows/requirements.txt",
        "tools/windows/runtime-spec.json", "tools/windows/verify_runtime.py",
        "arbitrage-research/scripts/monitor/run_research_paper_monitor.py",
        "arbitrage-research/scripts/monitor/run_cross_market_5m_validation.py",
        "arbitrage-research/scripts/monitor/run_cross_market_15m_validation.py",
        "arbitrage-research/scripts/monitor/run_gate_direct_monitor.mjs",
        "arbitrage-research/scripts/monitor/run_gate_low_frequency_monitor.mjs",
        "arbitrage-research/configs/research/live_paper_monitor_v1.json",
        "arbitrage-research/configs/research/cross_market_5m_validation_v1.json",
        "arbitrage-research/configs/research/cross_market_15m_validation_v1.json",
        "arbitrage-live/config/live.env.example",
        "arbitrage-live/config/recording_policy.json",
        "arbitrage-live/config/risk_limits.json",
        "arbitrage-live/config/strategy_down_spot5m_v1.json",
        "arbitrage-live/config/venue.json",
        "arbitrage-realtime/config/realtime.json",
        "arbitrage-realtime/web/index.html",
    )
}
FILES["tools/windows/customer-guide.md"] = "使用说明.md"
SOURCE_ROOTS = tuple(PurePosixPath(name) for name in (
    "arbitrage-research/src/arb_research",
    "arbitrage-live/src/live_trader",
    "arbitrage-realtime/src/realtime_probability",
))
SOURCE_SUFFIXES = {".py", ".html", ".css", ".js"}
FORBIDDEN_PARTS = {
    "data", "runs", "node_modules", ".venv", "__pycache__", ".git",
    "logs", ".license-private",
}


def select_files(names: list[str]) -> list[tuple[str, str]]:
    selected = []
    for name in filter(None, names):
        relative = PurePosixPath(name)
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name
                or ":" in name or relative.as_posix() != name):
            raise ValueError(f"Unsafe distribution source: {name}")
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            raise ValueError(f"Unexpected private/generated file in Git index: {name}")
        if ((relative.name.startswith(".env") and relative.name != ".env.example")
                or relative.suffix in {".pem", ".key", ".pfx", ".p12"}):
            raise ValueError(f"Unexpected credential file: {name}")
        if name in FILES:
            selected.append((name, FILES[name]))
        elif (any(relative.is_relative_to(root) for root in SOURCE_ROOTS)
                and relative.suffix in SOURCE_SUFFIXES
                and not {"tests", "docs", "design-system"}.intersection(relative.parts)):
            selected.append((name, name))
    missing = FILES.keys() - {source for source, _ in selected}
    if missing:
        raise ValueError(f"Missing required distribution files: {sorted(missing)}")
    if len({target for _, target in selected}) != len(selected):
        raise ValueError("Duplicate distribution target")
    return sorted(selected)


def copy_application(root: Path, target: Path, names: list[str]) -> dict:
    import shutil

    selected = select_files(names)
    # Check all sources before copying anything, including parent symlinks.
    for source_name, _ in selected:
        source = root / source_name
        if (any(parent.is_symlink() for parent in (source, *source.parents))
                or not source.is_file()):
            raise ValueError(f"Missing source or source link: {source_name}")
    for source_name, target_name in selected:
        destination = target / target_name
        if destination.exists():
            raise ValueError(f"Refusing to overwrite distribution file: {target_name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / source_name, destination)
    return {"schemaVersion": 1, "files": [name for _, name in selected],
            "keptFiles": len(selected), "omittedFiles": len(list(filter(None, names))) - len(selected)}
