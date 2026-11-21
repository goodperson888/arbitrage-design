"""Build an offline Windows x64 ZIP using official, checksum-pinned runtimes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_layout import copy_runtime_packages

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_SPEC = json.loads((ROOT / "tools/windows/runtime-spec.json").read_text(encoding="utf-8"))
PYTHON_URL, PYTHON_SHA256 = (RUNTIME_SPEC["python"][key] for key in ("url", "sha256"))
NODE_URL, NODE_SHA256 = (RUNTIME_SPEC["node"][key] for key in ("url", "sha256"))


def download(url: str, target: Path, expected: str) -> None:
    with urllib.request.urlopen(url, timeout=90) as response, target.open("xb") as output:
        shutil.copyfileobj(response, output)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError(f"Runtime checksum mismatch: {target.name}")


def extract(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts or "\\" in info.filename or ":" in info.filename:
                raise ValueError("Unsafe archive entry")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive must not contain symbolic links")
        source.extractall(target)


def build(destination: Path) -> Path:
    if sys.platform != "win32":
        raise RuntimeError("Build on Windows x64; use the GitHub Windows package workflow")
    if destination.exists():
        raise ValueError("Use a new output directory; the builder never removes existing files")
    destination.mkdir(parents=True)
    app = destination / "ArbitrageDesign"
    app.mkdir()
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    forbidden = {"data", "runs", "node_modules", ".venv", "__pycache__", ".git", "logs", ".license-private"}
    for name in filter(None, files):
        relative = PurePosixPath(name)
        if any(part in forbidden for part in relative.parts):
            raise ValueError(f"Unexpected private/generated file in Git index: {name}")
        if (relative.name.startswith(".env") and relative.name != ".env.example") or relative.suffix in {".pem", ".key", ".pfx", ".p12"}:
            raise ValueError(f"Unexpected credential file: {name}")
        if relative.parts[0] == ".github" or relative.name == ".gitignore" or "tests" in relative.parts:
            continue
        source = ROOT / name
        if source.is_symlink():
            raise ValueError(f"Refusing source symlink: {name}")
        target = app / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    runtime = app / "runtime"
    runtime.mkdir()
    with tempfile.TemporaryDirectory(prefix="arb-build-") as scratch:
        scratch = Path(scratch)
        python_zip, node_zip = scratch / "python.zip", scratch / "node.zip"
        download(PYTHON_URL, python_zip, PYTHON_SHA256)
        extract(python_zip, runtime)
        download(NODE_URL, node_zip, NODE_SHA256)
        with zipfile.ZipFile(node_zip) as source:
            for name in ("node.exe", "LICENSE"):
                target = runtime / "node" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(f"node-v24.22.3-win-x64/{name}"))
        wheels = scratch / "wheels"
        subprocess.run([sys.executable, "-m", "pip", "download", "--only-binary=:all:",
                        "--dest", str(wheels), "-r", str(ROOT / "tools/windows/requirements.txt")], check=True)
        packages = scratch / "installed-packages"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-index", "--no-compile",
                        "--find-links", str(wheels), "--target", str(packages),
                        "-r", str(ROOT / "tools/windows/requirements.txt")], check=True)
        lock = {"wheels": [{"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                           for p in sorted(wheels.glob("*.whl"))]}
        lock["versions"] = json.loads(subprocess.check_output(
            [sys.executable, "-m", "pip", "list", "--path", str(packages), "--format=json"]))
        layout = copy_runtime_packages(packages, runtime / "Lib/site-packages")
    (runtime / "python312._pth").write_text(
        "python312.zip\n.\nLib/site-packages\n../arbitrage-research/src\n"
        "../arbitrage-live/src\n../arbitrage-realtime/src\nimport site\n", encoding="utf-8")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    (app / "dependency-lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
    (runtime / "dependency-layout.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")
    (app / "build-info.json").write_text(json.dumps({"commit": commit, "python": "3.12.10",
        "node": "24.22.3", "pythonSha256": PYTHON_SHA256, "nodeSha256": NODE_SHA256,
        "platform": "windows-x64"}, indent=2), encoding="utf-8")
    # Batch files must work from Explorer and from paths with spaces/non-ASCII.
    for path in app.rglob("*.cmd"):
        value = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        path.write_bytes(value.replace("\n", "\r\n").encode("utf-8"))
    archive = destination / "ArbitrageDesign-Windows-x64.zip"
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as output:
        for path in sorted(app.rglob("*")):
            if path.is_file():
                output.write(path, path.relative_to(destination))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS.txt").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    with zipfile.ZipFile(archive) as result:
        entries = result.infolist()
        print(f"Package: {len(entries)} files; extracted {sum(p.file_size for p in entries) / 1048576:.1f} MiB")
    print(f"Built {archive} ({archive.stat().st_size / 1048576:.1f} MiB); commit {commit}")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output.resolve())
