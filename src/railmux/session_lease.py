"""Cross-host provider-session leases for shared Codex/Claude homes.

The provider history root is the sharing authority: if two hosts see the same
rollout/transcript files, they also see the same Railmux lease files. Advisory
locks are held by a small process tied to the exact provider pane lifetime, so
Soft Quit can leave the provider running without silently releasing its lease.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


SCHEMA_VERSION = 1
LEASE_DIRECTORY = "railmux-session-leases-v1"
_MAX_OWNER_BYTES = 4096
_READY_TIMEOUT_S = 3.0
_PROCFS_POLL_INTERVAL_S = 0.25
_FALLBACK_POLL_INTERVAL_S = 2.0
_PROBE_RETRY_WINDOW_S = 0.2
_STALE_LOCAL_RETRY_WINDOW_S = 0.75
_RETRY_INTERVAL_S = 0.05
_MODE_MASKING_ERRNOS = frozenset(
    {errno.EPERM, errno.EOPNOTSUPP, getattr(errno, "ENOTSUP", errno.EOPNOTSUPP)}
)


@dataclass(frozen=True)
class LeaseOwner:
    provider: str
    session_id: str
    host: str
    instance: str
    pane_id: str | None = None
    pane_pid: int | None = None
    process_start: str | None = None

    @property
    def display_host(self) -> str:
        return self.host or "another host"


class LeaseError(RuntimeError):
    """The shared lease authority could not be used safely."""


class LeaseConflict(LeaseError):
    def __init__(self, owner: LeaseOwner | None) -> None:
        self.owner = owner
        host = owner.display_host if owner is not None else "another host"
        super().__init__(f"session is already running on {host}")


@dataclass
class LeaseClaim:
    root: Path
    provider: str
    session_ids: tuple[str, ...]
    instance: str
    files: tuple[tuple[str, Path, int], ...]
    _detached: bool = False

    def close(self) -> None:
        if self._detached:
            return
        for _session_id, _path, fd in self.files:
            # Lease paths are persistent rendezvous points.  Unlinking a
            # locked file creates a second-inode race on shared filesystems:
            # another host can create and lock the same pathname before this
            # fd is closed.  An unlocked stale record is explicitly inactive.
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._detached = True

    def detach(self) -> None:
        """Close parent copies after a holder inherited the locked fds."""
        if self._detached:
            return
        for _session_id, _path, fd in self.files:
            try:
                os.close(fd)
            except OSError:
                pass
        self._detached = True


def _bounded_host() -> str:
    try:
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    host = "".join(c if c.isalnum() or c in ".-_" else "-" for c in host)
    return host[:128] or "unknown-host"


def _lease_directory(provider_root: Path) -> Path:
    return provider_root / LEASE_DIRECTORY


def _lease_path(provider_root: Path, provider: str, session_id: str) -> Path:
    material = f"{provider}\0{session_id}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return _lease_directory(provider_root) / f"{provider}-{digest}.lock"


def process_start_token(pid: int) -> str | None:
    """Best available immutable birth token for one local process."""
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        _head, separator, tail = raw.rpartition(")")
        fields = tail.strip().split() if separator else []
        start_ticks = fields[19]
        if start_ticks.isdecimal():
            return f"proc:{start_ticks}"
    except (OSError, UnicodeError, IndexError):
        pass
    try:
        started = subprocess.check_output(
            ["ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
        ).strip()
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    return (
        f"ps:{hashlib.sha256(started.encode('utf-8')).hexdigest()}"
        if started else None
    )


def _process_matches(pid: int, token: str | None) -> bool:
    if token is None:
        return False
    return process_start_token(pid) == token


def owner_is_stale_local(owner: LeaseOwner | None) -> bool:
    """Whether an owner record proves its exact same-host pane has exited."""
    return bool(
        owner is not None
        and owner.host == _bounded_host()
        and owner.pane_pid is not None
        and owner.process_start is not None
        and not _process_matches(owner.pane_pid, owner.process_start)
    )


def _ensure_lease_directory(provider_root: Path) -> Path:
    directory = _lease_directory(provider_root)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # lstat keeps a pre-created symlink from redirecting the predictable
        # rendezvous directory outside the provider root.
        info = directory.lstat()
    except OSError as exc:
        raise LeaseError(
            "shared session lease directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise LeaseError("shared session lease directory is unsafe")
    try:
        # Repair a permissive directory on ordinary POSIX filesystems. DrvFs,
        # CIFS, and other mode-masking stores may accept chmod while continuing
        # to report their mount-wide mode; type/owner/no-symlink checks still
        # apply there instead of disabling the lease entirely.
        directory.chmod(0o700)
    except OSError as exc:
        if exc.errno not in _MODE_MASKING_ERRNOS:
            raise LeaseError(
                "shared session lease directory is unavailable") from exc
    return directory


def _decode_owner(raw: bytes) -> LeaseOwner | None:
    if not raw or len(raw) > _MAX_OWNER_BYTES:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != SCHEMA_VERSION:
        return None
    provider = value.get("provider")
    session_id = value.get("session_id")
    host = value.get("host")
    instance = value.get("instance")
    if not all(isinstance(item, str) and item for item in (
        provider, session_id, host, instance,
    )):
        return None
    pane_id = value.get("pane_id")
    pane_pid = value.get("pane_pid")
    process_start = value.get("process_start")
    return LeaseOwner(
        provider=provider[:32],
        session_id=session_id[:128],
        host=host[:128],
        instance=instance[:128],
        pane_id=pane_id[:64] if isinstance(pane_id, str) else None,
        pane_pid=(pane_pid if isinstance(pane_pid, int)
                  and not isinstance(pane_pid, bool) and pane_pid > 0 else None),
        process_start=(process_start[:128]
                       if isinstance(process_start, str) else None),
    )


def _read_owner_fd(fd: int) -> LeaseOwner | None:
    try:
        return _decode_owner(os.pread(fd, _MAX_OWNER_BYTES + 1, 0))
    except OSError:
        return None


def _write_owner_fd(fd: int, payload: dict) -> None:
    data = json.dumps(
        payload, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    if len(data) > _MAX_OWNER_BYTES:
        raise LeaseError("session lease owner record is too large")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        if os.write(fd, data) != len(data):
            raise OSError("short session lease owner write")
    except OSError as exc:
        raise LeaseError("could not publish session lease owner") from exc


def _open_lease(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
        info = os.fstat(fd)
    except OSError as exc:
        raise LeaseError("could not open shared session lease") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        os.close(fd)
        raise LeaseError("shared session lease file is unsafe")
    try:
        os.fchmod(fd, 0o600)
    except OSError as exc:
        if exc.errno not in _MODE_MASKING_ERRNOS:
            os.close(fd)
            raise LeaseError("shared session lease file is unavailable") from exc
    return fd


def _acquire_once(
    provider_root: Path,
    provider: str,
    ids: tuple[str, ...],
) -> LeaseClaim:
    instance = uuid.uuid4().hex
    files: list[tuple[str, Path, int]] = []
    try:
        for session_id in ids:
            path = _lease_path(provider_root, provider, session_id)
            fd = _open_lease(path)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                owner = _read_owner_fd(fd)
                os.close(fd)
                raise LeaseConflict(owner) from exc
            except OSError as exc:
                os.close(fd)
                raise LeaseError(
                    "shared session locking is unavailable") from exc
            files.append((session_id, path, fd))
        for session_id, _path, fd in files:
            _write_owner_fd(fd, {
                "version": SCHEMA_VERSION,
                "provider": provider,
                "session_id": session_id,
                "host": _bounded_host(),
                "instance": instance,
                "phase": "reserving",
            })
        return LeaseClaim(
            provider_root, provider, ids, instance, tuple(files))
    except BaseException:
        for _session_id, _path, fd in files:
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def acquire(
    provider_root: Path,
    provider: str,
    session_ids: Sequence[str],
) -> LeaseClaim:
    """Atomically reserve every alias needed for one logical conversation.

    Read-only owner probes briefly take shared advisory locks. Retry a bounded
    contention window so one such probe cannot be mistaken for a durable
    writer; a proven dead same-host pane receives the slightly longer window
    needed for its independent holder to observe process exit.
    """
    ids = tuple(sorted(set(session_ids)))
    if provider not in {"claude", "codex"} or not ids:
        raise LeaseError("invalid provider session lease identity")
    if any(not session_id or len(session_id) > 128 for session_id in ids):
        raise LeaseError("invalid provider session id for lease")
    _ensure_lease_directory(provider_root)
    started = time.monotonic()
    while True:
        try:
            return _acquire_once(provider_root, provider, ids)
        except LeaseConflict as exc:
            retry_window = (
                _STALE_LOCAL_RETRY_WINDOW_S
                if owner_is_stale_local(exc.owner)
                else _PROBE_RETRY_WINDOW_S
            )
            if time.monotonic() - started >= retry_window:
                raise
            time.sleep(_RETRY_INTERVAL_S)


def active_owner(
    provider_root: Path,
    provider: str,
    session_ids: Sequence[str],
) -> LeaseOwner | None:
    """Return a currently locked owner for any alias, without mutation."""
    for session_id in sorted(set(session_ids)):
        path = _lease_path(provider_root, provider, session_id)
        # NFS flock emulation may require write access even for a shared probe.
        # O_RDWR does not mutate the file; the non-blocking lock result remains
        # the sole active-owner authority. Shared probes do not serialize each
        # other, while acquire retries their very short collision window.
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LeaseError("shared session lease state is unavailable") from exc
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return _read_owner_fd(fd) or LeaseOwner(
                    provider, session_id, "another host", "unknown")
            except OSError as exc:
                raise LeaseError(
                    "shared session locking is unavailable") from exc
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError as exc:
                    raise LeaseError(
                        "shared session locking is unavailable") from exc
        finally:
            os.close(fd)
    return None


def owner_matches_pane(
    owner: LeaseOwner,
    pane_id: str,
    pane_pid: int,
    *,
    process_start: str | None = None,
) -> bool:
    current_start = process_start or process_start_token(pane_pid)
    return bool(
        owner.host == _bounded_host()
        and owner.pane_id == pane_id
        and owner.pane_pid == pane_pid
        and owner.process_start == current_start
    )


def start_holder(claim: LeaseClaim, *, pane_id: str, pane_pid: int) -> bool:
    """Transfer one acquired claim to an independent pane-lifetime holder."""
    token = process_start_token(pane_pid)
    if not pane_id.startswith("%") or token is None:
        claim.close()
        return False
    argv = [
        sys.executable,
        "-m",
        "railmux.session_lease",
        "hold",
        "--provider",
        claim.provider,
        "--instance",
        claim.instance,
        "--pane-id",
        pane_id,
        "--pane-pid",
        str(pane_pid),
        "--process-start",
        token,
    ]
    for session_id, _path, fd in claim.files:
        argv.extend(("--lease", f"{session_id}:{fd}"))
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=tuple(fd for _session_id, _path, fd in claim.files),
            start_new_session=True,
        )
    except OSError:
        claim.close()
        return False
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    ready = False
    try:
        if selector.select(_READY_TIMEOUT_S):
            ready = process.stdout.readline().strip() == b"ready"
    finally:
        selector.close()
        process.stdout.close()
    if not ready:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        claim.close()
        return False
    claim.detach()
    # The holder may outlive Railmux after Soft Quit. While Railmux remains
    # alive, reap it asynchronously when its provider pane eventually exits.
    threading.Thread(
        target=process.wait,
        name="railmux-session-lease-reaper",
        daemon=True,
    ).start()
    return True


def _hold(args: argparse.Namespace) -> int:
    files: list[tuple[str, Path | None, int]] = []
    try:
        for raw in args.lease:
            session_id, separator, raw_fd = raw.rpartition(":")
            if not separator or not raw_fd.isdecimal() or not session_id:
                return 2
            fd = int(raw_fd)
            os.fstat(fd)
            files.append((session_id, None, fd))
        if not files or not _process_matches(args.pane_pid, args.process_start):
            return 2
        for session_id, _path, fd in files:
            _write_owner_fd(fd, {
                "version": SCHEMA_VERSION,
                "provider": args.provider,
                "session_id": session_id,
                "host": _bounded_host(),
                "instance": args.instance,
                "phase": "running",
                "pane_id": args.pane_id,
                "pane_pid": args.pane_pid,
                "process_start": args.process_start,
            })
        print("ready", flush=True)
        poll_interval = (
            _PROCFS_POLL_INTERVAL_S
            if args.process_start.startswith("proc:")
            else _FALLBACK_POLL_INTERVAL_S
        )
        while _process_matches(args.pane_pid, args.process_start):
            time.sleep(poll_interval)
        return 0
    except (OSError, LeaseError):
        return 1
    finally:
        # Persistent empty files avoid directory churn on NFS. The advisory
        # lock, not stale JSON content, is the active-lease authority.
        for _session_id, _path, fd in files:
            try:
                os.close(fd)
            except OSError:
                pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)
    hold = sub.add_parser("hold", add_help=False)
    hold.add_argument("--provider", choices=("claude", "codex"), required=True)
    hold.add_argument("--instance", required=True)
    hold.add_argument("--pane-id", required=True)
    hold.add_argument("--pane-pid", type=int, required=True)
    hold.add_argument("--process-start", required=True)
    hold.add_argument("--lease", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return _hold(args) if args.command == "hold" else 2


if __name__ == "__main__":
    raise SystemExit(main())
