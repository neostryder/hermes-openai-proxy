"""CLI entry point for hermes-openai-proxy.

Foreground (dev):
    python -m hermes_openai_proxy                # listens on 0.0.0.0:8765
    python -m hermes_openai_proxy --port 9000
    python -m hermes_openai_proxy --host 127.0.0.1

Service management (writes plists / sc tasks / systemd units):
    python -m hermes_openai_proxy --install       # register + start
    python -m hermes_openai_proxy --uninstall     # remove
    python -m hermes_openai_proxy --upgrade       # pip-upgrade + restart
    python -m hermes_openai_proxy --status        # report mechanism + PID

Tray icon (optional, requires pystray / rumps):
    python -m hermes_openai_proxy --tray
    HERMES_NO_TRAY=1 python -m hermes_openai_proxy   # skip tray
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from . import __version__
from . import _service as service_mod


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="hermes-openai-proxy",
        description="OpenAI-compatible HTTP API exposing your Hermes credentials.",
    )
    p.add_argument("--host",
                   default=os.environ.get("HERMES_PROXY_HOST", "0.0.0.0"),
                   help="Bind address. Default 0.0.0.0 (all interfaces). "
                        "Use 127.0.0.1 for localhost-only.")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("HERMES_PROXY_PORT", "8765")),
                   help="TCP port. Default 8765.")
    p.add_argument("--log-level",
                   default=os.environ.get("HERMES_PROXY_LOG_LEVEL", "info"),
                   choices=["debug", "info", "warning", "error"])
    p.add_argument("--install", action="store_true",
                   help="Install as a background service (NSSM / launchd / systemd). "
                        "Idempotent: re-running with the same host/port is a no-op.")
    p.add_argument("--uninstall", action="store_true",
                   help="Remove the background service.")
    p.add_argument("--upgrade", action="store_true",
                   help="pip-install --upgrade, then restart the service.")
    p.add_argument("--status", action="store_true",
                   help="Report service mechanism, PID, port, recent log lines.")
    p.add_argument("--tray", action="store_true",
                   help="Run the system-tray / menu-bar icon only. "
                        "Connects to an already-running proxy on --host:port; "
                        "if no proxy is found, prints a hint to run --install "
                        "first. Combine with --start-proxy to also start the "
                        "server in-process when nothing is listening.")
    p.add_argument("--start-proxy", action="store_true",
                   help="When --tray is set and no proxy is running on "
                        "--host:port, start one in this process before "
                        "opening the tray.")
    p.add_argument("--tray-autostart", action="store_true",
                   help="Register the tray icon to start at user logon "
                        "(Windows: HKCU Run key; macOS: LaunchAgent; "
                        "Linux: systemd --user). Idempotent.")
    p.add_argument("--tray-stop-autostart", action="store_true",
                   help="Remove the autostart registration. The tray will "
                        "still work if you launch it manually with --tray.")
    p.add_argument("--no-tray", action="store_true",
                   help="Disable the tray icon even if --tray is the default "
                        "(HERMES_TRAY env var or env-controlled CI).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def _configure_logging(args):
    log = logging.getLogger("hermes-openai-proxy")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(args.log_level.upper())
    return log


def run_server(args) -> int:
    """Foreground uvicorn with optional single-instance lock + tray icon."""
    log = _configure_logging(args)
    log.info("=" * 60)
    log.info("hermes-openai-proxy %s starting", __version__)
    log.info("Host: %s  Port: %d", args.host, args.port)
    log.info("=" * 60)

    # Single-instance guard. Catch the lock BEFORE we hand control to
    # uvicorn so a second proxy prints a clean message instead of an
    # EADDRINUSE traceback.
    from ._service_lock import LockHeld
    from ._service_lock import acquire as lock_acquire
    from ._service_lock import release as lock_release
    fd = -1
    try:
        fd = lock_acquire()
    except LockHeld as e:
        print(str(e), file=sys.stderr)
        return 78  # EX_CONFIG -- user-facing diagnostic

    # Optional tray. We do this AFTER the lock so the second-proxy
    # diagnostic is unambiguous.
    tray_thread = None
    if (args.tray or os.environ.get("HERMES_TRAY") == "1") and not args.no_tray:
        import threading

        from . import _tray
        tray_thread = threading.Thread(
            target=_tray.run,
            args=(args.host, args.port, __version__),
            daemon=True,
            name="hermes-tray",
        )
        tray_thread.start()

    # Run uvicorn. We let uvicorn handle bind failures (EADDRINUSE) --
    # the lock is advisory; the bind is authoritative.
    try:
        from .server import app
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level=args.log_level,
            access_log=False,
        )
    finally:
        if fd >= 0:
            lock_release(fd)
    return 0


def main(argv=None):
    args = parse_args(argv)
    # Service-management subcommands are mutually exclusive.
    sub = sum(bool(x) for x in (args.install, args.uninstall, args.upgrade, args.status))
    if sub > 1:
        print("--install, --uninstall, --upgrade, --status are mutually exclusive.",
              file=sys.stderr)
        return 2
    if args.install:
        return service_mod.install(args)
    if args.uninstall:
        return service_mod.uninstall(args)
    if args.upgrade:
        return service_mod.upgrade(args)
    if args.status:
        return service_mod.status(args)
    if args.tray_autostart:
        return service_mod.tray_autostart(args, enable=True)
    if args.tray_stop_autostart:
        return service_mod.tray_autostart(args, enable=False)

    if args.tray:
        # --tray alone: connect to an existing proxy; start one in this
        # process if --start-proxy is set AND nothing is listening.
        return _run_tray(args)

    return run_server(args)


def _run_tray(args) -> int:
    """Spawn the platform tray icon.

    Default behavior: connect to the proxy already listening on
    args.host:args.port. If nothing is listening, error out with a
    hint unless --start-proxy was passed.
    """
    if _health_ok(args.host, args.port, timeout=1.5):
        from . import _tray
        _tray.run(args.host, args.port, __version__)
        return 0

    if args.start_proxy:
        # No proxy running. Start one in this process in a worker
        # thread, wait for /healthz to respond, then open the tray.
        # The tray will close the proxy when it exits (it doesn't,
        # actually -- the tray quits cleanly and leaves the in-process
        # proxy alive until the user Ctrl-C's this terminal).
        import threading

        from . import _tray

        def _serve():
            run_server(args)
        t = threading.Thread(target=_serve, daemon=True,
                             name="proxy-in-tray")
        t.start()
        # Wait up to 10 s for the server to come up.
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            if _health_ok(args.host, args.port, timeout=1.0):
                break
            time.sleep(0.2)
        else:
            print("Failed to start proxy in 10s; tray not opening.",
                  file=sys.stderr)
            return 1
        _tray.run(args.host, args.port, __version__)
        return 0

    print(f"No proxy listening on http://{args.host}:{args.port}.",
          file=sys.stderr)
    print("Run --install first to register the proxy as a service,",
          file=sys.stderr)
    print("or use --tray --start-proxy to launch a foreground proxy",
          file=sys.stderr)
    print("alongside the tray in this terminal.", file=sys.stderr)
    return 1


def _health_ok(host: str, port: int, timeout: float = 1.5) -> bool:
    """Lightweight liveness probe used by --tray and friends.

    `0.0.0.0` is a wildcard BIND address, not a valid connect target.
    Normalize to 127.0.0.1 so the probe succeeds against a service
    installed with the default `--host 0.0.0.0`."""
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
