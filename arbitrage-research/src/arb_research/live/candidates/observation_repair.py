"""Explicit offline recovery of legacy, interrupted candidate gzip streams.

Only newline-complete, valid OBSERVATION rows are recovered. Original bytes
are backed up before replacement; account journals are never touched.
"""
from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zlib

MAX_COMPRESSED = 64 * 1024 * 1024
MAX_RECOVERED = 256 * 1024 * 1024


def recover_rows(data: bytes) -> tuple[bytes, int, int]:
    """Refuse undecodable segments rather than guessing through corruption."""
    starts = [match.start() for match in re.finditer(b"\x1f\x8b\x08", data)]
    if not starts or starts[0] != 0:
        raise ValueError("Missing gzip header")
    output = bytearray()
    partial_bytes = 0
    unfinished = 0
    for start, end in zip(starts, starts[1:] + [len(data)]):
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(data[start:end], MAX_RECOVERED + 1 - len(output))
        if decoder.unconsumed_tail or decoder.unused_data or len(output) + len(raw) > MAX_RECOVERED:
            raise ValueError("Ambiguous gzip boundary or recovery size limit exceeded")
        unfinished += int(not decoder.eof)
        complete_end = raw.rfind(b"\n") + 1
        for line in raw[:complete_end].splitlines():
            row = json.loads(line)
            if not isinstance(row, dict) or row.get("type") != "OBSERVATION" or not isinstance(row.get("atUs"), int):
                raise ValueError("Unexpected row in candidate observations")
        output.extend(raw[:complete_end])
        partial_bytes += len(raw) - complete_end
    return bytes(output), unfinished, partial_bytes


def repair_observations(directory: Path, *, apply: bool = False) -> list[dict]:
    """Hold the same exclusive lock as CandidateEngine, including for preview."""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Expected an existing candidate directory, not a symbolic link")
    directory = directory.resolve()
    descriptor = os.open(directory / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        results = []
        observations = directory / "observations"
        if observations.is_symlink():
            raise ValueError("Observation directory must not be a symbolic link")
        for path in sorted(observations.glob("*/*.ndjson.gz")):
            if path.parent.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"Not a regular observation partition: {path}")
            if path.stat().st_size > MAX_COMPRESSED:
                raise ValueError(f"Partition too large for this offline repair: {path}")
            original = path.read_bytes()
            try:
                gzip.decompress(original)
            except (EOFError, OSError, zlib.error):
                recovered, unfinished, partial = recover_rows(original)
            else:
                continue
            item = {"path": str(path.relative_to(directory)), "rows": recovered.count(b"\n"),
                    "unfinishedMembers": unfinished, "partialRowBytes": partial,
                    "originalSha256": hashlib.sha256(original).hexdigest(),
                    "recoveredSha256": hashlib.sha256(recovered).hexdigest(), "applied": False}
            if apply:
                backup = directory / "observation_repair_backups" / stamp / path.relative_to(directory)
                backup.parent.mkdir(parents=True, exist_ok=True)
                with backup.open("xb") as target:
                    target.write(original)
                    target.flush()
                    os.fsync(target.fileno())
                packed = gzip.compress(recovered, compresslevel=3, mtime=0)
                if gzip.decompress(packed) != recovered or backup.read_bytes() != original:
                    raise RuntimeError("Repair verification failed; original partition remains unchanged")
                # A unique temporary next to the partition permits atomic publication.
                descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.repair-", dir=path.parent)
                with os.fdopen(descriptor, "wb") as target:
                    target.write(packed)
                    target.flush()
                    os.fsync(target.fileno())
                if path.read_bytes() != original:
                    raise RuntimeError("Partition changed during offline repair; refusing replacement")
                os.replace(temp_name, path)
                item.update(applied=True, backup=str(backup.relative_to(directory)))
                with (directory / "observation_repairs.ndjson").open("a") as manifest:
                    manifest.write(json.dumps(item, ensure_ascii=False) + "\n")
                    manifest.flush()
                    os.fsync(manifest.fileno())
            results.append(item)
        return results
