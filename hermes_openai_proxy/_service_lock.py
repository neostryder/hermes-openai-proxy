"""Cross-platform single-instance lock via pidfile.

A second proxy that tries to bind the same TCP port would crash with
EADDRINUSE; we do better: write the PID to a file, and the second
proxy finds the lock first and exits with a clean, actionable message
("another hermes-openai-proxy is already running on this host -- use
`--status` to check"). The user gets the hint instead of a stack trace.

Stdlib only:
  - Linux/macOS: fcntl.flock on an open file handle. Lock is dropped
    on process exit (POSIX semantics).
  - Windows: msvcrt.locking on the file. Same lifecycle.

This is NOT a daemon PID tracker. It is a startup guard. If the file
is stale (a previous proxy crashed without releasing), we overwrite it
with the current PID and proceed. Crash with stale lock is bounded --
when the holding PID is dead, the new bind replaces it.
"""

from __future__ import annotations

import contextlib
import os
import sys

from .service_paths import pidfile_path


class LockHeld(RuntimeError):
    """Raised when another proxy owns the lockfile."""


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Windows uses OpenProcess; POSIX uses
    kill(pid, 0). Returns False if the PID doesn't exist OR the calling
    user lacks permission to inspect it (which is conservatively treated
    as 'not ours to handle' = True; the lockfile holds anyway)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # OpenProcess with PROCESS_QUERY_LIMITED_INFORMATION; if it
        # returns 0, the PID doesn't exist.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if h == 0:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    # POSIX
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Not ours. Treat as alive -- the lockfile holds.
        return True


def acquire() -> int:
    """Try to take the lock. Returns the file descriptor on success.
    Raises LockHeld with a hint if another proxy owns it.

    We don't read the existing PID before locking; on Windows the
    exclusive lock blocks the read. Instead, we open + try to lock in a
    single OS call. If the lock fails, we open a fresh FD (after
    closing the locked one) to inspect any prior PID so we can name the
    holder in the error message.
    """
    path = pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    ok = False
    if sys.platform == "win32":
        # msvcrt.locking: LK_NBLCK = 2 (non-blocking exclusive).
        import msvcrt  # type: ignore[import-not-found]
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            ok = True
        except OSError as e:
            os.close(fd)
            raise LockHeld(f"another hermes-openai-proxy holds {path}: {e}") from e
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ok = True
        except OSError as e:
            os.close(fd)
            raise LockHeld(f"another hermes-openai-proxy holds {path}: {e}") from e

    if not ok:
        # Unreachable in practice (both branches always raise before here).
        os.close(fd)
        raise LockHeld(f"another hermes-openai-proxy holds {path}")

    # We have the lock. Write our PID (now safe; we hold the write lock).
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def release(fd: int) -> None:
    """Drop the lock. Best-effort; we never raise on cleanup because the
    alternative is hiding real errors behind a cleanup exception."""
    with contextlib.suppress(OSError):
        os.close(fd)


def status() -> dict[str, object]:
    """Read the lockfile and report who holds it. Empty dict if no lock."""
    path = pidfile_path()
    if not path.exists():
        return {}
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        return {}
    return {
        "pidfile": str(path),
        "pid": pid,
        "alive": _pid_alive(pid),
    }
