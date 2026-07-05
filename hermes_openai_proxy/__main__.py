"""CLI entry point.

Usage:
    python -m hermes_openai_proxy                # foreground (dev)
    python -m hermes_openai_proxy --port 9000    # custom port
    python -m hermes_openai_proxy --install      # install as a service (NSSM/launchd/systemd)
    python -m hermes_openai_proxy --uninstall
    python -m hermes_openai_proxy --status
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import sys
from pathlib import Path

import uvicorn

from . import __version__


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="hermes-openai-proxy",
        description="OpenAI-compatible HTTP API exposing your Hermes credentials.",
    )
    p.add_argument("--host", default=os.environ.get("HERMES_PROXY_HOST", "0.0.0.0"),
                   help="Bind address. Default 0.0.0.0 (all interfaces).")
    p.add_argument("--port", type=int, default=int(os.environ.get("HERMES_PROXY_PORT", "8765")),
                   help="TCP port. Default 8765.")
    p.add_argument("--log-level", default=os.environ.get("HERMES_PROXY_LOG_LEVEL", "info"),
                   choices=["debug", "info", "warning", "error"])
    p.add_argument("--install", action="store_true",
                   help="Install as a background service (NSSM on Windows, launchd on macOS, systemd on Linux).")
    p.add_argument("--uninstall", action="store_true",
                   help="Uninstall the background service.")
    p.add_argument("--status", action="store_true",
                   help="Check whether the background service is running.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def run_server(args):
    """Run uvicorn in the foreground."""
    from .server import app
    # Print a startup banner so the operator knows where logs go.
    log = logging.getLogger("hermes-openai-proxy")
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(args.log_level.upper())
    log.info("=" * 60)
    log.info("hermes-openai-proxy %s starting", __version__)
    log.info("Host: %s  Port: %d", args.host, args.port)
    log.info("Per-request log: every POST /v1/chat/completions is logged at INFO")
    log.info("Log destination: stderr (foreground) -- redirect to file if you need it")
    log.info("=" * 60)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,  # We log per-request bodies ourselves; uvicorn's
                            # access log just dumps URLs and produces noise.
    )


def install_service(args):
    """Install as a background service on the current platform.

    Strategy by platform:
      Windows:  try NSSM (needs admin). If it fails, fall back to
                Task Scheduler registration (no admin needed) so the
                proxy starts at logon.
      macOS:    launchd LaunchAgent (no admin needed; user-context).
      Linux:    systemd --user (no admin needed).
    """
    # Verify the package is importable in the python interpreter that
    # would actually run the service. Catches the common failure mode
    # where the user runs --install from a venv without the package
    # installed, or runs it against the system python while the package
    # is in a user-site venv. Without this check, we'd register a
    # launchd plist / NSSM service / systemd unit that points at a
    # python interpreter that can't import hermes_openai_proxy, and
    # the service would silently fail to start on next boot.
    try:
        import hermes_openai_proxy  # noqa: F401
    except ImportError as e:
        print(
            f"ERROR: hermes_openai_proxy is not importable in this "
            f"python interpreter ({sys.executable}): {e}\n"
            f"Install it first with:\n"
            f"  {sys.executable} -m pip install "
            f"git+https://github.com/neostryder/hermes-openai-proxy.git\n"
            f"Then re-run --install.",
            file=sys.stderr,
        )
        sys.exit(2)

    system = platform.system()
    if system == "Windows":
        nssm_ok = _try_install_windows_nssm(args)
        if not nssm_ok:
            print()
            print("NSSM install failed (no admin). Falling back to Task Scheduler...")
            _install_windows_taskscheduler(args)
    elif system == "Darwin":
        _install_macos(args)
    elif system == "Linux":
        _install_linux(args)
    else:
        print(f"unsupported platform: {system}", file=sys.stderr)
        sys.exit(1)


def uninstall_service(args):
    system = platform.system()
    if system == "Windows":
        _uninstall_windows()
    elif system == "Darwin":
        _uninstall_macos()
    elif system == "Linux":
        _uninstall_linux()
    else:
        print(f"unsupported platform: {system}", file=sys.stderr)
        sys.exit(1)


def status_service(args):
    system = platform.system()
    if system == "Windows":
        _status_windows()
    elif system == "Darwin":
        _status_macos()
    elif system == "Linux":
        _status_linux()
    else:
        print(f"unsupported platform: {system}", file=sys.stderr)
        sys.exit(1)


def _service_python() -> str:
    """Best-effort: find the python interpreter running this code."""
    return sys.executable


# ---- Windows (NSSM) ----

def _try_install_windows_nssm(args) -> bool:
    """Try NSSM install. Returns True on success, False if it failed
    (typically because we don't have admin)."""
    import subprocess
    nssm = _find_nssm()
    if not nssm:
        print("NSSM not found; install with: winget install -e --id NSSM.NSSM",
              file=sys.stderr)
        return False
    py = _service_python()
    service_name = "HermesOpenAIProxy"
    print(f"Installing '{service_name}' via NSSM...")
    try:
        subprocess.run(
            [nssm, "install", service_name, py, "-m", "hermes_openai_proxy",
             "--host", args.host, "--port", str(args.port)],
            check=True,
        )
        subprocess.run([nssm, "set", service_name, "DisplayName",
                        "Hermes OpenAI-Compatible Proxy"], check=True)
        subprocess.run([nssm, "set", service_name, "Description",
                        "OpenAI-compatible HTTP API exposing Hermes credentials. http://<host>:<port>/v1"], check=True)
        subprocess.run([nssm, "set", service_name, "Start", "SERVICE_AUTO_START"], check=True)
        subprocess.run([nssm, "set", service_name, "AppStdout",
                        str(Path.home() / "hermes-openai-proxy.log")], check=True)
        subprocess.run([nssm, "set", service_name, "AppStderr",
                        str(Path.home() / "hermes-openai-proxy.err.log")], check=True)
        subprocess.run([nssm, "set", service_name, "AppRotateFiles", "1"], check=True)
        subprocess.run([nssm, "set", service_name, "AppRotateBytes", "10485760"], check=True)
        print("Starting service...")
        subprocess.run(["net", "start", service_name], check=True)
        print(f"Service installed and started. Listening on http://{args.host}:{args.port}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"NSSM install failed: {e}", file=sys.stderr)
        # Clean up the half-installed service so it doesn't linger.
        import contextlib
        with contextlib.suppress(Exception):
            subprocess.run(["sc", "delete", service_name],
                           capture_output=True, timeout=10)
        return False
    except FileNotFoundError as e:
        print(f"NSSM not available: {e}", file=sys.stderr)
        return False


_install_windows = _try_install_windows_nssm  # backwards-compat alias


def _install_windows_taskscheduler(args):
    """Install via Task Scheduler as a startup task (no admin needed).

    Strategy:
      1. Try schtasks /SC ONLOGON first (ideal but often needs admin).
      2. Fall back to /SC ONSTART (system boot, may need admin).
      3. Fall back to writing to HKCU\\...\\Run (always works, no admin).
         This is the most reliable cross-machine path.
    """
    import subprocess
    py = _service_python()
    task_name = "HermesOpenAIProxy"
    tr = f'"{py}" -m hermes_openai_proxy --host {args.host} --port {args.port}'

    # Try ONLOGON first
    r = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/TR", tr,
         "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        print(f"Task '{task_name}' registered (ONLOGON). Will run at next logon.")
        subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True)
        print(f"Running. Listening on http://{args.host}:{args.port}")
        return

    # Fall back to registry Run key (always works)
    print(f"schtasks failed ({r.stderr.strip()!r}); using HKCU\\...\\Run instead.")
    import winreg
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        )
        winreg.SetValueEx(key, "HermesOpenAIProxy", 0, winreg.REG_SZ, tr)
        key.Close()
        print("Registry Run entry added. Will run at next logon.")
        # Start now
        import subprocess as sp
        sp.Popen([py, "-m", "hermes_openai_proxy",
                  "--host", args.host, "--port", str(args.port)],
                 creationflags=0x00000008)  # DETACHED_PROCESS
        print(f"Started. Listening on http://{args.host}:{args.port}")
    except Exception as e:
        print(f"All auto-start mechanisms failed: {e}", file=sys.stderr)
        sys.exit(1)


def _uninstall_windows():
    """Remove NSSM service (if any), Task Scheduler entry, AND HKCU Run key."""
    import subprocess
    nssm = _find_nssm()
    if nssm:
        service_name = "HermesOpenAIProxy"
        subprocess.run(["net", "stop", service_name], capture_output=True)
        subprocess.run([nssm, "remove", service_name, "confirm"], capture_output=True)
    subprocess.run(["schtasks", "/Delete", "/TN", "HermesOpenAIProxy", "/F"],
                   capture_output=True)
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        )
        winreg.DeleteValue(key, "HermesOpenAIProxy")
        key.Close()
        print("Removed HKCU Run entry.")
    except FileNotFoundError:
        pass  # No entry to remove
    except Exception as e:
        print(f"Note: registry cleanup failed ({e}); you may want to remove manually.")
    print("Uninstall complete.")


def _status_windows():
    import subprocess
    r = subprocess.run(["sc", "query", "HermesOpenAIProxy"],
                       capture_output=True, text=True)
    if r.returncode == 0 and "RUNNING" in r.stdout:
        print("NSSM service: RUNNING")
    elif r.returncode == 0:
        print("NSSM service: installed but not running")
    else:
        print("NSSM service: not installed")
    r2 = subprocess.run(["schtasks", "/Query", "/TN", "HermesOpenAIProxy"],
                        capture_output=True, text=True)
    if r2.returncode == 0:
        print("Task Scheduler: registered")
    else:
        print("Task Scheduler: not registered")
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        val, _ = winreg.QueryValueEx(key, "HermesOpenAIProxy")
        key.Close()
        print(f"HKCU Run: registered ({val[:80]}...)")
    except FileNotFoundError:
        print("HKCU Run: not registered")
    # Also check whether the proxy is actually listening
    r3 = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue | "
         "Select-Object -ExpandProperty OwningProcess -Unique"],
        capture_output=True, text=True, timeout=10,
    )
    pids = [p.strip() for p in r3.stdout.splitlines() if p.strip()]
    if pids:
        print(f"Listening on 8765: PID(s) {pids}")
    else:
        print("Not currently listening on 8765")


def _find_nssm():
    """Look for nssm.exe on PATH, then in C:\\Program Files\\nssm."""
    import shutil
    p = shutil.which("nssm")
    if p:
        return p
    for c in [r"C:\Program Files\nssm\win64\nssm.exe", r"C:\tools\nssm\win64\nssm.exe"]:
        if Path(c).exists():
            return c
    return None


# ---- macOS (launchd) ----

def _install_macos(args):
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / "com.hermes.openai-proxy.plist"
    plist_dir.mkdir(parents=True, exist_ok=True)
    py = _service_python()
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.hermes.openai-proxy</string>
    <key>ProgramArguments</key>
    <array>
        <string>{py}</string>
        <string>-m</string>
        <string>hermes_openai_proxy</string>
        <string>--host</string>
        <string>{args.host}</string>
        <string>--port</string>
        <string>{args.port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/hermes-openai-proxy.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/hermes-openai-proxy.err.log</string>
    <key>WorkingDirectory</key>
    <string>{Path.home()}</string>
</dict>
</plist>
"""
    plist_path.write_text(content)
    import subprocess
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    print(f"Installed and started. Listening on http://{args.host}:{args.port}")


def _uninstall_macos():
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.hermes.openai-proxy.plist"
    import subprocess
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        print("Service uninstalled.")


def _status_macos():
    import subprocess
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "hermes.openai-proxy" in line:
            print(line)
            return
    print("not loaded")


# ---- Linux (systemd) ----

def _install_linux(args):
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_path = unit_dir / "hermes-openai-proxy.service"
    unit_dir.mkdir(parents=True, exist_ok=True)
    py = _service_python()
    content = f"""[Unit]
Description=Hermes OpenAI-Compatible Proxy
After=network.target

[Service]
Type=simple
ExecStart={py} -m hermes_openai_proxy --host {args.host} --port {args.port}
Restart=on-failure
RestartSec=5
StandardOutput=append:{Path.home()}/hermes-openai-proxy.log
StandardError=append:{Path.home()}/hermes-openai-proxy.err.log
WorkingDirectory={Path.home()}

[Install]
WantedBy=default.target
"""
    unit_path.write_text(content)
    import subprocess
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "hermes-openai-proxy.service"], check=True)
    print(f"Installed and started. Listening on http://{args.host}:{args.port}")


def _uninstall_linux():
    import subprocess
    subprocess.run(["systemctl", "--user", "disable", "--now", "hermes-openai-proxy.service"], capture_output=True)
    unit_path = Path.home() / ".config" / "systemd" / "user" / "hermes-openai-proxy.service"
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print("Service uninstalled.")


def _status_linux():
    import subprocess
    subprocess.run(["systemctl", "--user", "status", "hermes-openai-proxy.service"])


def main(argv=None):
    args = parse_args(argv)
    if args.install:
        install_service(args)
    elif args.uninstall:
        uninstall_service(args)
    elif args.status:
        status_service(args)
    else:
        run_server(args)


if __name__ == "__main__":
    main()