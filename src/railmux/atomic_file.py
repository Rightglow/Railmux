"""Small atomic-file helpers for railmux-owned state."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Replace *path* atomically after writing *text* beside it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    tmp = Path(raw_tmp)
    stream = None
    try:
        stream = os.fdopen(fd, "w", encoding=encoding)
        fd = -1
        with stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        stream = None
        os.replace(tmp, path)
        # The replace is already atomic, but persisting the parent directory
        # makes the new name durable across a sudden power loss where the
        # filesystem supports directory fsync. Some Unix-like filesystems and
        # Android storage layers reject it, so durability remains best-effort
        # after the successful replacement.
        directory_fd = -1
        try:
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            os.fsync(directory_fd)
        except OSError:
            pass
        finally:
            if directory_fd >= 0:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass
    finally:
        if stream is not None:
            stream.close()
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
