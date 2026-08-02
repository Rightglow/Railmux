"""Advisory file locks used by small Railmux state records."""
from __future__ import annotations

import os


def try_lock(fd: int) -> bool:
    """Acquire an exclusive non-blocking lock for *fd*."""
    if os.name != "nt":
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    import msvcrt

    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    """Release a lock previously acquired by :func:`try_lock`."""
    if os.name != "nt":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return

    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

