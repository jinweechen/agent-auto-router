"""Cross-process locks for privacy-safe router control-plane state."""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN})
BUSY_WINERRORS = frozenset({32, 33})


def _prepare_windows_lock_file(handle: BinaryIO) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def _try_lock(handle: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            _prepare_windows_lock_file(handle)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in BUSY_ERRNOS or getattr(exc, "winerror", None) in BUSY_WINERRORS:
            return False
        raise


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_file_lock(
    path: Path,
    *,
    timeout_seconds: float = 0,
    poll_interval_seconds: float = 0.025,
) -> Iterator[bool]:
    """Yield whether an OS lock was acquired, waiting only for the bounded timeout."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    deadline = time.monotonic() + max(0, timeout_seconds)
    try:
        while True:
            acquired = _try_lock(handle)
            if acquired:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval_seconds, remaining))
        yield acquired
    finally:
        if acquired:
            _unlock(handle)
        handle.close()


def control_plane_lock(state_dir: Path, *, timeout_seconds: float = 0):
    """Serialize active-policy and guarded lifecycle mutations."""
    root = state_dir.resolve(strict=False)
    lock_path = (state_dir / ".guarded-auto.lock").resolve(strict=False)
    try:
        lock_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("control-plane lock path is outside the state directory") from exc
    return try_file_lock(
        lock_path,
        timeout_seconds=timeout_seconds,
    )


def append_lock(path: Path, *, timeout_seconds: float = 5):
    """Serialize one JSONL stream without coupling unrelated state directories."""
    return try_file_lock(
        path.with_name(f".{path.name}.lock"),
        timeout_seconds=timeout_seconds,
    )
