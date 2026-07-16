"""Cross-platform process lifetime lease for a running collaboration."""
from __future__ import annotations

import os
from pathlib import Path


class RunLease:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self._file.write(b"0")
            self._file.flush()
        self._file.seek(0)
        self._locked = _try_lock(self._file)
        if not self._locked:
            self._file.close()
            raise RuntimeError("run lease is already owned by another process")

    def close(self) -> None:
        if self._locked:
            self._file.seek(0)
            _unlock(self._file)
            self._locked = False
        self._file.close()


def owner_is_dead(path: str | Path) -> bool | None:
    """Return True with lock evidence, False if held, None for legacy no-lock runs."""
    target = Path(path)
    if not target.is_file():
        return None
    try:
        lease = RunLease(target)
    except (OSError, RuntimeError):
        return False
    lease.close()
    return True


if os.name == "nt":
    import msvcrt

    def _try_lock(file) -> bool:
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(file) -> None:
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(file) -> bool:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(file) -> None:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)
