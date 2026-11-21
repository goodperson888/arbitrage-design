from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any, BinaryIO, Iterator
from uuid import uuid4

from arb_research.platform_io import exclusive_lock, open_regular, sync_directory


GIB = 1024 ** 3
COMPRESSED_PAPER_STREAMS = ("raw", "events", "signals")
_PARTITION = re.compile(r"(events|signals)/date=\d{4}-\d{2}-\d{2}/hour=\d{2}/part-\d+\.ndjson$")


def partition_lock(path: Path) -> BinaryIO:
    """The writer and archiver use the same lock for a plain/gzip pair."""
    plain_name = path.name.removesuffix(".gz")
    lock_path = path.with_name(f".{plain_name}.lock")
    return exclusive_lock(lock_path)


def disk_space_state(directory: Path) -> dict[str, Any]:
    """A statvfs check only; never walk the dataset on the dashboard path."""
    checked = time.time_ns() // 1000
    try:
        usage = shutil.disk_usage(directory)
    except OSError as exc:
        return {"level": "unknown", "checkedAtUs": str(checked),
                "message": f"磁盘余量检查失败：{type(exc).__name__}；请检查数据盘。"}
    ratio = usage.free / usage.total if usage.total else 0
    level = ("critical" if usage.free < 5 * GIB or ratio < 0.02 else
             "warning" if usage.free < 20 * GIB or ratio < 0.10 else "ok")
    prefix = {"ok": "磁盘正常", "warning": "磁盘空间偏低", "critical": "磁盘空间紧张"}[level]
    suffix = "；请及时释放空间或迁移数据，采集仍在继续。" if level != "ok" else "。"
    return {
        "level": level, "checkedAtUs": str(checked), "totalBytes": usage.total,
        "freeBytes": usage.free, "freeRatio": ratio,
        "warningFreeBytes": 20 * GIB, "criticalFreeBytes": 5 * GIB,
        "warningFreeRatio": 0.10, "criticalFreeRatio": 0.02,
        "compressedStreams": list(COMPRESSED_PAPER_STREAMS),
        "message": f"{prefix}：剩余 {usage.free / GIB:.1f} GiB（{ratio:.1%}）{suffix}",
    }


def read_partition_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Read live gzip through its flushed rows; offline truncation is an error."""
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rt", encoding="utf-8") as handle:
            for number, line in enumerate(handle, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"invalid JSON at {path}:{number}: {exc}") from exc
    except EOFError:
        try:
            lock = partition_lock(path)
        except BlockingIOError:
            return  # An active gzip stream has no final trailer yet.
        else:
            lock.close()
            raise


def _validated_source(root: Path, path: Path) -> os.stat_result:
    if not root.is_absolute() or root != root.resolve() or root == Path(root.anchor):
        raise ValueError("data root must be an explicit real directory")
    relative = path.relative_to(root)
    if not _PARTITION.fullmatch(relative.as_posix()):
        raise ValueError(f"not an events/signals partition: {path}")
    current = root
    for piece in relative.parts:
        current = current / piece
        if current.is_symlink():
            raise ValueError(f"refusing symlink: {current}")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"not a regular single-link file: {path}")
    return info


def archive_candidates(root: Path) -> list[Path]:
    paths = sorted(path for stream in ("events", "signals")
                   for path in (root / stream).glob("date=*/hour=*/part-*.ndjson"))
    for path in paths:
        _validated_source(root, path)
    return paths


def _signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _digest(handle: BinaryIO) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    size = lines = 0
    while block := handle.read(1024 * 1024):
        digest.update(block)
        size += len(block)
        lines += block.count(b"\n")
    return digest.hexdigest(), size, lines


def _journal(root: Path, entry: dict[str, Any]) -> None:
    path = root / "storage_archive_manifest.ndjson"
    descriptor = open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(directory: Path) -> None:
    sync_directory(directory)


def archive_partition(root: Path, path: Path) -> dict[str, Any]:
    """Verify recovery, publish durably, then remove only the source.

    Upgrade/restart the writer before use; legacy writers do not take this lock.
    """
    _validated_source(root, path)
    try:
        lock = partition_lock(path)
    except BlockingIOError:
        return {"status": "active", "source": str(path)}
    with lock:
        before = _validated_source(root, path)
        target = path.with_name(path.name + ".gz")
        if target.is_symlink():
            raise ValueError(f"refusing symlink: {target}")
        temporary: Path | None = None
        if target.exists():
            if not target.is_file():
                raise ValueError(f"not a regular gzip file: {target}")
            with path.open("rb") as handle:
                original = _digest(handle)
            with gzip.open(target, "rb") as handle:
                recovered = _digest(handle)
        else:
            # Hidden temporary files are never selected by replay globs.
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.compressing")
            digest = hashlib.sha256()
            size = lines = 0
            with path.open("rb") as source, temporary.open("xb") as raw:
                with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=3, mtime=0) as zipped:
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                        size += len(block)
                        lines += block.count(b"\n")
                        zipped.write(block)
                raw.flush()
                os.fsync(raw.fileno())
            original = digest.hexdigest(), size, lines
            with gzip.open(temporary, "rb") as handle:
                recovered = _digest(handle)
        if original != recovered or original[1] != before.st_size:
            raise ValueError(f"gzip verification failed; source retained: {path}")
        if _signature(_validated_source(root, path)) != _signature(before):
            raise RuntimeError(f"source changed; source retained: {path}")
        if temporary is not None:
            # link() refuses to overwrite a concurrently created destination.
            os.link(temporary, target)
            temporary.unlink()
            _sync_directory(path.parent)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(), "status": "verified",
            "source": str(path.relative_to(root)), "target": str(target.relative_to(root)),
            "sourceBytes": original[1], "gzipBytes": target.stat().st_size,
            "lineCount": original[2], "sha256": original[0],
        }
        _journal(root, entry)
        if _signature(_validated_source(root, path)) != _signature(before):
            raise RuntimeError(f"source changed before removal; source retained: {path}")
        path.unlink()
        _sync_directory(path.parent)
        entry = {**entry, "status": "archived"}
        _journal(root, entry)
        return entry
