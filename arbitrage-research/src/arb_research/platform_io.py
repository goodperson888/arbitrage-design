"""Small OS boundary for exclusive ledger locks and safe regular files."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
from typing import BinaryIO


def open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    """Reject symlinks/reparse points, including a file replaced during open."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        before = None
    if before is not None and (
        not stat.S_ISREG(before.st_mode)
        or getattr(before, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError(f"not a regular file: {path}")
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        opened, current = os.fstat(descriptor), path.lstat()
        if (not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or getattr(current, "st_file_attributes", 0) & 0x400
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError(f"file changed or is a reparse point: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def exclusive_lock(path: Path) -> BinaryIO:
    """Nonblocking process lock. Closing the returned handle releases it."""
    handle = os.fdopen(open_regular(path, os.O_CREAT | os.O_RDWR), "r+b")
    try:
        if os.name == "nt":
            import msvcrt
            # Windows permits byte-range locks beyond EOF; no journal bytes
            # are added just to obtain a lock.
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise BlockingIOError(errno.EAGAIN, "file already locked", str(path)) from exc
                raise
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def sync_directory(directory: Path) -> None:
    # Windows doesn't support fsync on directory descriptors. Individual
    # files are still flushed before publication; POSIX also syncs metadata.
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
