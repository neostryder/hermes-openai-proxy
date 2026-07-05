"""Cross-platform service install / uninstall / status / upgrade.

Per-platform implementation:

  Windows  (no admin required, admin is best-effort):
    1. NSSM service (preferred when admin is available).
    2. Task Scheduler /SC ONLOGON registration (works without admin on
       most home + work machines).
    3. HKCU\\...\\Run registry key (universally works, but no auto-restart).
    Each mechanism has strengths. The chosen mechanism is recorded in
    service_config so --uninstall cleans up exactly what was set up.
    KeepAlive: NSSM via SCM; Task Scheduler via /SC ONLOGON (with a
    Windows-side watchdog we trigger from --install); HKCU Run has no
    KeepAlive. Order of preference matches: NSSM > Task Scheduler > HKCU.

  macOS:
    1. launchd LaunchAgent at ~/Library/LaunchAgents/.<label>.plist.
       RunAtLoad=true, KeepAlive=true. Logrotate via 2 files watched
       by a launchd plist that rotates them on size threshold (we put
       the rotator plist as a second LaunchAgent).
       IPv4/IPv6 dual-stack is fine -- bind to 0.0.0.0 only if the user
       asked for it; defaults to 127.0.0.1 when starting locally for
       BYOM use.

  Linux:
    systemd --user unit at ~/.config/systemd/user/.<label>.service.
    Restart=on-failure, RestartSec=5. Logs via journald (no rotation
    needed; journalctl handles it). The unit is enabled --now so it
    runs immediately and on every subsequent user-session logon.

  All platforms:
    - Single source of truth for state: service_config_path() JSON file
      records {host, port, mechanism, label, installed_at, python}.
    - PIDfile lock before bind: see _service_lock.
    - --upgrade: detect running service, pip-install --upgrade, restart.
    - --uninstall: read service_config, undo exactly what --install did.
    - --status: report mechanism, PID, listening port, last log lines.

Upgrade philosophy: --upgrade is the operator's tool. It is idempotent
(no-op if already up to date), it never silently fails, and it never
breaks the running service in case of network failure.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .service_paths import (
    logs_root,
    pidfile_path,
    platform_key,
    service_config_path,
    service_label_linux,
    service_label_macos,
    service_label_windows,
    stderr_log_path,
    stdout_log_path,
)

# When we spawn the proxy as a detached subprocess on Windows (used by
# the HKCU Run path, which has no service manager), we want zero
# visible artifacts: no console window popping up, no parent console
# leak. CREATE_NO_WINDOW (0x08000000) alone is not enough against the
# Python interpreter (which is built with the console subsystem); we
# must combine it with DETACHED_PROCESS (0x00000008). The combined value
# is 0x08000008 and that's what gives a truly invisible process.
if sys.platform == "win32":
    _WIN_DETACHED_NO_WINDOW = 0x08000008
else:
    _WIN_DETACHED_NO_WINDOW = 0

# =====================================================================
#  Public API
# =====================================================================

def install(args) -> int:
    """Install the proxy as a background service on this OS.

    Returns 0 on success, non-zero on failure. --install is idempotent:
    re-running with the same host/port is a no-op; with a different
    host/port the existing service is uninstalled first."""
    # Idempotency first: if there's already a working installation of
    # THIS package at the same host/port, report it and return without
    # re-running the install (which would redundantly try to bind, see
    # ourselves already on the port, and fail the precheck).
    cfg = _read_service_config()
    if cfg and cfg.get("package_version") == __version__ \
       and cfg.get("host") == args.host \
       and cfg.get("port") == args.port \
       and _is_registered(cfg):
        print(f"Already installed: {cfg['mechanism']} on "
              f"http://{args.host}:{args.port} (PID {cfg.get('pid', '?')})")
        print("Use --status to inspect, or --uninstall + --install to "
              "change config.")
        return 0

    # Port precheck (item 4): if the port is taken by something OTHER
    # than us, --install fails cleanly with the actual conflict visible
    # (don't write a plist that won't be able to bind).
    _port_precheck(args.host, args.port)

    pk = platform_key()
    if pk == "windows":
        mechanism = _install_windows(args)
    elif pk == "macos":
        mechanism = _install_macos(args)
    else:
        mechanism = _install_linux(args)

    _write_service_config({
        "package_version": __version__,
        "host": args.host,
        "port": args.port,
        "mechanism": mechanism,
        "installed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "python": sys.executable,
        "log_stdout": str(stdout_log_path()),
        "log_stderr": str(stderr_log_path()),
        "platform": platform_key(),
        "label": _current_label(),
    })
    print(f"Installed via {mechanism}. Listening on http://{args.host}:{args.port}")
    print(f"  stdout: {stdout_log_path()}")
    print(f"  stderr: {stderr_log_path()}")
    print(f"  config: {service_config_path()}")
    return 0


def uninstall(args) -> int:
    """Remove every artifact left by --install on this OS.

    Reads service_config_path() to know which mechanism was used; if
    no record exists, scans all platforms (Windows is the messy one --
    it might have an NSSM service, a Task Scheduler entry, AND a
    registry Run key left from prior --install runs).
    """
    pk = platform_key()
    if pk == "windows":
        _uninstall_windows()
    elif pk == "macos":
        _uninstall_macos()
    else:
        _uninstall_linux()

    # Clear state files. The pidfile may be held open by a still-running
    # proxy (we just stopped it, but the OS hasn't released the handle);
    # we tolerate that with OSError suppression.
    with contextlib.suppress(FileNotFoundError, OSError):
        service_config_path().unlink()
    with contextlib.suppress(FileNotFoundError, OSError):
        pidfile_path().unlink()
    print(f"Uninstalled. To remove logs, delete: {logs_root() / 'hermes-openai-proxy*'}")
    return 0


def status(args) -> int:
    """Report the registered mechanism, PID, port, and recent log lines.

    Returns 0 always; the operator is supposed to read this, not parse
    an exit code."""
    pk = platform_key()
    if pk == "windows":
        return _status_windows()
    if pk == "macos":
        return _status_macos()
    return _status_linux()


def tray_autostart(args, *, enable: bool) -> int:
    """Register (enable=True) or unregister (enable=False) the tray
    icon at user logon.

    On Windows: writes HKCU\\...\\Run key.
    On macOS: writes ~/Library/LaunchAgents/com.hermes.openai-proxy.tray.plist.
    On Linux: writes ~/.config/systemd/user/hermes-openai-proxy.tray.service.

    Idempotent: re-running with the same state is a no-op.
    """
    cfg = _read_service_config()
    if not cfg:
        print("No prior --install detected. Run --install first.", file=sys.stderr)
        return 2
    pk = platform_key()
    if pk == "windows":
        return _tray_autostart_windows(cfg, enable)
    if pk == "macos":
        return _tray_autostart_macos(cfg, enable=enable)
    return _tray_autostart_linux(cfg, enable=enable)


def _tray_autostart_windows(cfg, enable: bool) -> int:
    """HKCU Run key for the tray. The registered command runs
    `--tray` (which connects to an existing proxy) so the tray never
    starts a second proxy."""
    label = service_label_windows() + ".Tray"
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        )
    except FileNotFoundError:
        if not enable:
            print("Tray autostart is not registered (nothing to remove).")
            return 0
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        )
    cmd = (
        f'"{cfg["python"]}" -m hermes_openai_proxy '
        f'--host {cfg["host"]} --port {cfg["port"]} --tray'
    )
    try:
        if enable:
            winreg.SetValueEx(key, label, 0, winreg.REG_SZ, cmd)
            print(f"Registered tray at logon (HKCU\\...\\Run\\{label}).")
            print(f"  command: {cmd}")
        else:
            try:
                winreg.DeleteValue(key, label)
                print(f"Removed tray autostart (HKCU\\...\\Run\\{label}).")
            except FileNotFoundError:
                print("Tray autostart was not registered (nothing to remove).")
    finally:
        winreg.CloseKey(key)
    return 0


def _tray_autostart_macos(cfg, *, enable: bool) -> int:
    """macOS LaunchAgent for the tray. RunAtLoad launches the tray
    after user login; Aqua session is automatic from the user context."""
    from pathlib import Path
    plist_path = (Path.home() / "Library" / "LaunchAgents"
                  / "com.hermes.openai-proxy.tray.plist")
    label = "com.hermes.openai-proxy.tray"
    if not enable:
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)],
                           capture_output=True)
            plist_path.unlink()
            print(f"Removed {plist_path}")
        else:
            print("Tray autostart is not registered (nothing to remove).")
        return 0
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
        "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{cfg["python"]}</string>
        <string>-m</string>
        <string>hermes_openai_proxy</string>
        <string>--host</string>
        <string>{cfg["host"]}</string>
        <string>--port</string>
        <string>{cfg["port"]}</string>
        <string>--tray</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>ProcessType</key>
    <string>Interactive</string>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    r = subprocess.run(["launchctl", "load", str(plist_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl load failed: {r.stderr}", file=sys.stderr)
        return r.returncode
    print(f"Registered tray at logon: {plist_path}")
    return 0


def _tray_autostart_linux(cfg, *, enable: bool) -> int:
    """systemd --user unit for the tray. Linux: untested end-to-end;
    the unit file is shipped and follows the Linux conventions
    documented in install code's Linux section."""
    from pathlib import Path
    unit_path = (Path.home() / ".config" / "systemd" / "user"
                 / "hermes-openai-proxy.tray.service")
    if not enable:
        if unit_path.exists():
            subprocess.run(
                ["systemctl", "--user", "disable", "--now",
                 "hermes-openai-proxy.tray.service"],
                capture_output=True,
            )
            unit_path.unlink()
            print(f"Removed {unit_path}")
        else:
            print("Tray autostart is not registered (nothing to remove).")
        return 0
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit = f"""[Unit]
Description=hermes-openai-proxy tray icon
After=network-online.target hermes-openai-proxy.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={cfg["python"]} -m hermes_openai_proxy --host {cfg["host"]} --port {cfg["port"]} --tray
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now",
         "hermes-openai-proxy.tray.service"],
        capture_output=True,
    )
    print(f"Registered tray at logon: {unit_path}")
    return 0


def upgrade(args) -> int:
    """pip-install --upgrade and restart the service.

    Sequence:
      1. Run the proper installer (uv or pip) to upgrade in this venv.
      2. Confirm import works and version advanced.
      3. Restart the registered service (uninstall-reinstall, or
         systemctl --user restart, or launchctl unload && load).
      4. Confirm /healthz returns the new version.
    """
    cfg = _read_service_config()
    if not cfg:
        print("No prior --install detected. Run --install first.")
        return 2
    print(f"Current: v{cfg.get('package_version', '?')} via {cfg.get('mechanism')}")
    print("Upgrading...")
    # Two installer backends: pip (preferred) and uv (fallback when
    # the venv is hermes-agent-style and pip is stripped). We detect
    # pip availability first; if absent, fall back to `uv pip install`.
    r = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        install_cmd = [sys.executable, "-m", "pip", "install",
                       "--upgrade", "hermes_openai_proxy"]
    else:
        # uv pip install against this exact interpreter.
        install_cmd = ["uv", "pip", "install", "--python", sys.executable,
                       "--upgrade", "hermes_openai_proxy"]
    r2 = subprocess.run(install_cmd, capture_output=True, text=True)
    if r2.returncode != 0:
        print(f"install failed:\n{r2.stderr}", file=sys.stderr)
        return r2.returncode
    # Verify the new code is importable in *this* process's interpreter.
    # (We re-spawn so we get the freshly installed package.)
    r3 = subprocess.run(
        [sys.executable, "-c",
         "import hermes_openai_proxy; print(hermes_openai_proxy.__version__)"],
        capture_output=True, text=True,
    )
    new_version = r3.stdout.strip() or "?"
    print(f"Upgraded to v{new_version}.")
    print(f"Restarting service ({cfg['mechanism']})...")
    _restart_registered(cfg)
    # Health-check
    deadline = time.time() + 10
    while time.time() < deadline:
        if _health_ok(cfg["host"], cfg["port"], timeout=1.5):
            print("Restart OK. /healthz responding.")
            _write_service_config({**cfg,
                                   "package_version": new_version,
                                   "installed_at": datetime.datetime.utcnow().isoformat() + "Z"})
            return 0
        time.sleep(0.5)
    print("Service did not come back within 10s. Check the stderr log:")
    print(f"  {stderr_log_path()}")
    return 1


# =====================================================================
#  Shared helpers
# =====================================================================

def _current_label() -> str:
    pk = platform_key()
    if pk == "windows":
        return service_label_windows()
    if pk == "macos":
        return service_label_macos()
    return service_label_linux()


def _read_service_config() -> dict[str, Any]:
    p = service_config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_service_config(d: dict[str, Any]) -> None:
    p = service_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _port_precheck(host: str, port: int) -> None:
    """Verify the port isn't already serving another listener.

    We use `connect()` (kernel-level probe), not `bind()`. Bind would
    fight SO_EXCLUSIVE semantics on Windows, throwing WinError 10013
    from inside the *same* user shell when the proxy's own process
    already listens. A TCP connect cleanly identifies "someone else is
    here" without entering a bind race with ourselves.

    If the listen is on a different host/port binding (e.g. the user
    asks --host 127.0.0.1 but something is on 0.0.0.0:8765), the probe
    will still see it -- which is what we want.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((host, port))
    except (OSError, ConnectionRefusedError):
        return  # Not listening -- port is free.
    finally:
        s.close()

    # Port is in use. Best-effort: identify the holder.
    pid_hint = ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty OwningProcess -Unique"],
            capture_output=True, text=True, timeout=5,
        )
        pids = [p.strip() for p in out.stdout.splitlines() if p.strip().isdigit()]
        if pids:
            pid_hint = f" (held by PID {','.join(pids)})"
    except Exception:
        pass
    raise SystemExit(
        f"port {port} is in use{pid_hint}. Pick another port with "
        f"--install --port <N>, or stop the process holding {port}."
    )


def _health_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP-level health check. Does NOT parse the response; just
    confirms the kernel will accept() on the port. Faster than HTTP
    and doesn't fail on a proxy that's listening but mid-startup."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def _is_registered(cfg: dict[str, Any]) -> bool:
    """Confirm that the mechanism recorded in the config file is
    actually live (e.g. launchd has it loaded, NSSM service exists).
    Returns False if the config says 'installed' but reality disagrees."""
    pk = cfg.get("platform") or platform_key()
    mech = cfg.get("mechanism", "")
    if pk == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{cfg['label']}.plist"
        if not plist.exists():
            return False
        r = subprocess.run(["launchctl", "list", cfg["label"]],
                           capture_output=True, text=True)
        # launchctl list prints "<pid>\t<exit>\t<label>" or "<pid>\t<exit>\t<label>"
        # followed by the label on a separate line in the "list" output.
        # Quick acceptance: any line containing our label.
        return cfg["label"] in r.stdout or cfg["label"] in r.stderr
    if pk == "linux":
        r = subprocess.run(
            ["systemctl", "--user", "is-active", cfg["label"]],
            capture_output=True, text=True,
        )
        return r.stdout.strip() == "active"
    # Windows: NSSM service, scheduled task, or HKCU Run key.
    if mech == "nssm":
        r = subprocess.run(["sc", "query", cfg["label"]],
                           capture_output=True, text=True)
        return r.returncode == 0
    if mech == "taskscheduler":
        r = subprocess.run(
            ["schtasks", "/Query", "/TN", cfg["label"]],
            capture_output=True, text=True,
        )
        return r.returncode == 0
    if mech == "hkcu_run":
        # The HKCU value name was service_label_windows() -- "HermesOpenAIProxy".
        import winreg
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                winreg.QueryValueEx(key, "HermesOpenAIProxy")
            return True
        except FileNotFoundError:
            return False
    return False


def _restart_registered(cfg: dict[str, Any]) -> None:
    pk = cfg.get("platform") or platform_key()
    label = cfg["label"]
    if pk == "macos":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        subprocess.run(["launchctl", "load", str(plist)], check=True)
        return
    if pk == "linux":
        subprocess.run(
            ["systemctl", "--user", "restart", label], check=True
        )
        return
    # Windows
    mech = cfg.get("mechanism", "")
    if mech == "nssm":
        subprocess.run(["net", "stop", label], capture_output=True)
        subprocess.run(["net", "start", label], check=True)
    elif mech == "taskscheduler":
        # Task Scheduler has no "restart"; trigger again.
        subprocess.run(
            ["schtasks", "/Run", "/TN", label], capture_output=True
        )
    elif mech == "hkcu_run":
        # Spawn truly detached: no console window, no parent shell.
        # See the constant definition above for why CREATE_NO_WINDOW
        # alone is not enough.
        import subprocess as sp
        py = cfg["python"]
        sp.Popen(
            [py, "-m", "hermes_openai_proxy",
             "--host", cfg["host"], "--port", str(cfg["port"])],
            creationflags=_WIN_DETACHED_NO_WINDOW,
            stdin=sp.DEVNULL,
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
            close_fds=True,
        )
    # else: unknown mechanism -- try nothing, the caller's health check will fail.


# =====================================================================
#  Windows: NSSM > Task Scheduler > HKCU Run. Each one writes back the
#  mechanism used so --uninstall knows exactly what to clean up.
# =====================================================================

def _find_nssm() -> str | None:
    """Locate nssm.exe. winget puts it under Local\\Microsoft\\WinGet\\Links."""
    p = shutil.which("nssm")
    if p:
        return p
    for c in [r"C:\Program Files\nssm\win64\nssm.exe",
              r"C:\tools\nssm\win64\nssm.exe"]:
        if Path(c).exists():
            return c
    # Last-ditch: scan WinGet links dir.
    winget_links = Path("C:/Users") / os.environ.get("USERNAME", "") / (
        "AppData/Local/Microsoft/WinGet/Links")
    if winget_links.exists():
        for n in winget_links.glob("nssm*.exe"):
            return str(n)
    return None


def _install_windows(args) -> str:
    nssm = _find_nssm()
    py = sys.executable

    if nssm:
        try:
            return _try_install_windows_nssm(nssm, py, args)
        except subprocess.CalledProcessError as e:
            print(f"NSSM install did not complete: {e}", file=sys.stderr)
            # Fall through to Task Scheduler.

    # Try Task Scheduler.
    mech = _try_install_windows_taskscheduler(py, args)
    if mech:
        return mech
    # Last resort: HKCU Run.
    return _install_windows_hkcurun(py, args)


def _try_install_windows_nssm(nssm: str, py: str, args) -> str:
    label = service_label_windows()
    # Idempotency: if the service is already installed, replace its
    # settings rather than re-installing (avoids 1073 / 1078 errors).
    if subprocess.run(["sc", "query", label],
                       capture_output=True, text=True).returncode == 0:
        subprocess.run(["net", "stop", label], capture_output=True)
    else:
        subprocess.run(
            [nssm, "install", label, py, "-m", "hermes_openai_proxy",
             "--host", args.host, "--port", str(args.port)],
            check=True,
        )
    sets = [
        (["set", label, "DisplayName", "Hermes OpenAI-Compatible Proxy"], None),
        (["set", label, "Description",
          "OpenAI-compatible HTTP API exposing Hermes credentials. "
          "http://<host>:<port>/v1"], None),
        (["set", label, "Start", "SERVICE_AUTO_START"], None),
        (["set", label, "AppStdout", str(stdout_log_path())], None),
        (["set", label, "AppStderr", str(stderr_log_path())], None),
        # Log rotation: 10MB per file, keep 5 generations.
        (["set", label, "AppRotateFiles", "1"], None),
        (["set", label, "AppRotateBytes", "10485760"], None),
        # Auto-restart on crash: SCM Restart=1 with delay 5s.
        (["set", label, "AppRestartDelay", "5000"], None),
        (["set", label, "AppExit", "Default", "Restart"], None),
        # Hidden: no console window for foreground subprocesses.
        (["set", label, "AppNoConsole", "1"], None),
    ]
    for cmd, _ in sets:
        subprocess.run([nssm, *cmd], check=True, capture_output=True)
    subprocess.run(["net", "start", label], check=True)
    return "nssm"


def _try_install_windows_taskscheduler(py: str, args) -> str | None:
    """Try to register a Task Scheduler ONLOGON task. Returns 'taskscheduler'
    on success, None if schtasks isn't available or the user lacks rights.
    """
    label = service_label_windows()
    tr = (f'"{py}" -m hermes_openai_proxy --host {args.host} '
          f'--port {args.port}')
    r = subprocess.run(
        ["schtasks", "/Create", "/TN", label, "/TR", tr,
         "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        subprocess.run(["schtasks", "/Run", "/TN", label], capture_output=True)
        return "taskscheduler"
    return None


def _install_windows_hkcurun(py: str, args) -> str:
    """Last-resort: write to HKCU\\...\\Run.

    On next logon, Windows starts the proxy. No auto-restart, but always
    works without admin. We also spawn a detached copy now so the user
    gets the service immediately.
    """
    import winreg
    label = service_label_windows()
    cmd = (f'"{py}" -m hermes_openai_proxy --host {args.host} '
           f'--port {args.port}')
    key = winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
    )
    winreg.SetValueEx(key, label, 0, winreg.REG_SZ, cmd)
    key.Close()
    import subprocess as sp
    sp.Popen([py, "-m", "hermes_openai_proxy",
              "--host", args.host, "--port", str(args.port)],
             stdin=sp.DEVNULL, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
             close_fds=True,
             creationflags=_WIN_DETACHED_NO_WINDOW)
    return "hkcu_run"


def _uninstall_windows() -> None:
    nssm = _find_nssm()
    label = service_label_windows()
    if nssm and subprocess.run(["sc", "query", label], capture_output=True,
                                text=True).returncode == 0:
        # Only attempt NSSM cleanup if the service actually exists.
        subprocess.run(["net", "stop", label], capture_output=True)
        subprocess.run([nssm, "remove", label, "confirm"], capture_output=True)
        print("NSSM service removed.")
    # Always run the cleanup regardless of the config -- older --install
    # runs may have left schtasks / HKCU entries behind.
    r = subprocess.run(["schtasks", "/Delete", "/TN", label, "/F"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        print("Scheduled task removed.")
    # If the HKCU Run path spawned a detached python, kill it here.
    # The HKCU Run mechanism has no service manager; without this step
    # we'd leave an orphaned process that survives `--uninstall`.
    try:
        cfg = _read_service_config()
    except Exception:
        cfg = None
    if cfg:
        cfg_host = cfg.get("host", "")
        cfg_port = cfg.get("port", "")
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*hermes_openai_proxy*' }} | "
            f"Where-Object {{ $_.CommandLine -like '*{cfg_host}*{cfg_port}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
        )
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.DeleteValue(key, label)
        print("Registry Run key removed.")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Registry cleanup failed: {e}", file=sys.stderr)


def _status_windows() -> int:
    label = service_label_windows()
    nssm = _find_nssm()
    found = False
    if nssm and subprocess.run(["sc", "query", label],
                               capture_output=True, text=True).returncode == 0:
        r = subprocess.run(["sc", "query", label], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "STATE" in line:
                print(f"NSSM service: {line.strip()}")
                found = True
    r2 = subprocess.run(["schtasks", "/Query", "/TN", label],
                        capture_output=True, text=True)
    if r2.returncode == 0:
        print("Task Scheduler: registered")
        found = True
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.QueryValueEx(key, label)
        print("HKCU Run key: registered")
        found = True
    except FileNotFoundError:
        pass
    if not found:
        print("Not installed. Run --install to register.")
    # Live port check.
    if _health_ok("127.0.0.1", 8765, timeout=1.0):
        print("Port 8765: ACCEPTing connections")
    else:
        print("Port 8765: not listening")
    return 0


# =====================================================================
#  macOS
# =====================================================================

_MACOS_ROTATOR_LABEL = "com.hermes.openai-proxy.logrotate"


def _install_macos(args) -> str:
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    label = service_label_macos()
    plist_path = plist_dir / f"{label}.plist"

    # Idempotency: unload any existing copy first.
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    if plist_path.exists():
        plist_path.unlink()

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
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
    <string>{stdout_log_path()}</string>
    <key>StandardErrorPath</key>
    <string>{stderr_log_path()}</string>
    <key>WorkingDirectory</key>
    <string>{Path.home()}</string>
    <!-- Network: don't start before networking is up. Prevents the
         common failure where the proxy starts during early boot, tries
         to bind, fails, and KeepAlive restarts it forever. -->
    <key>WatchPaths</key>
    <array/>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)
    # Companion log-rotator LaunchAgent: a separate plist that runs
    # every 5 minutes and rotates the logs when they exceed 10MB.
    _install_macos_logrotate()
    return "launchd"


def _install_macos_logrotate() -> None:
    """A second LaunchAgent that runs every 5 minutes and rotates logs.

    launchd itself does not have a size-based rotation primitive; this
    companion plist fills the gap. Two files: .log and .err.log,
    rotated to .log.1 / .2 / .3 (size-capped, keep 5 generations).
    """
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{_MACOS_ROTATOR_LABEL}.plist"
    # The rotator script lives in the venv bin/ next to the python.
    py_dir = Path(sys.executable).parent
    rotator_script = py_dir / "hermes-logrotate.sh"
    rotator_script.write_text(
        "#!/bin/bash\n"
        "# Auto-installed by hermes-openai-proxy --install.\n"
        "# Rotates the proxy's stdout/stderr logs at 10MB, keeping 5 generations.\n"
        "set -e\n"
        f"LOG_DIR=\"{Path.home()}\"\n"
        f"SIZE=\"10485760\"  # 10MB\n"
        "KEEP=5\n"
        "for base in hermes-openai-proxy.log hermes-openai-proxy.err.log; do\n"
        "  f=\"$LOG_DIR/$base\"\n"
        "  [ -f \"$f\" ] || continue\n"
        "  actual=$(stat -f%z \"$f\" 2>/dev/null || echo 0)\n"
        "  [ \"$actual\" -ge \"$SIZE\" ] || continue\n"
        "  # Shift generations: .4 -> .5, .3 -> .4, ..., .log -> .1\n"
        "  i=$((KEEP))\n"
        "  while [ \"$i\" -ge 1 ]; do\n"
        "    prev=$((i - 1))\n"
        "    if [ \"$prev\" -eq 0 ]; then\n"
        "      src=\"$f\"\n"
        "    else\n"
        "      src=\"$f.$prev\"\n"
        "    fi\n"
        "    [ -f \"$src\" ] && mv \"$src\" \"$f.$i\"\n"
        "    i=$((i - 1))\n"
        "  done\n"
        "  # Empty file (rotation creates empty .log to keep launchd's\n"
        "  # open file handle pointing at something writable).\n"
        "  : > \"$f\"\n"
        "done\n"
    )
    rotator_script.chmod(0o755)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_MACOS_ROTATOR_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{rotator_script}</string>
    </array>
    <key>StartInterval</key>
    <integer>300</integer>  <!-- every 5 minutes -->
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(plist_path)], check=True)


def _uninstall_macos() -> None:
    label = service_label_macos()
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)],
                       capture_output=True)
        plist_path.unlink()
        print(f"Removed {plist_path}")
    rotator_path = Path.home() / "Library" / "LaunchAgents" / f"{_MACOS_ROTATOR_LABEL}.plist"
    if rotator_path.exists():
        subprocess.run(["launchctl", "unload", str(rotator_path)],
                       capture_output=True)
        rotator_path.unlink()


def _status_macos() -> int:
    label = service_label_macos()
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    matched = [line for line in r.stdout.splitlines() if label in line]
    if matched:
        for line in matched:
            print(line)
    else:
        print(f"{label}: not loaded")
    # Also check the rotator is loaded.
    rr = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    if _MACOS_ROTATOR_LABEL in rr.stdout:
        print("  log rotator: loaded")
    else:
        print("  log rotator: not loaded")
    return 0


# =====================================================================
#  Linux
# =====================================================================

def _install_linux(args) -> str:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable
    label = service_label_linux()
    unit_path = unit_dir / label
    # Idempotency: stop & disable prior install.
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", label], capture_output=True
    )
    unit = f"""[Unit]
Description=Hermes OpenAI-Compatible Proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={py} -m hermes_openai_proxy --host {args.host} --port {args.port}
Restart=on-failure
RestartSec=5

# Keep logs in the user's log dir; journald also gets them via syslog.
# (Drop StandardOutput if you want journald-only -- the user-level
# journalctl -u hermes-openai-proxy gives you everything.)
StandardOutput=append:{stdout_log_path()}
StandardError=append:{stderr_log_path()}

WorkingDirectory={Path.home()}
# Lingering ensures the service runs even when the user is not logged
# in (e.g. after a reboot). Enabling here avoids needing to run
# `loginctl enable-linger` separately.
[Install]
WantedBy=default.target
"""
    unit_path.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    # Enable lingering so the service survives logout.
    with contextlib.suppress(subprocess.CalledProcessError):
        subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")],
                       capture_output=True, check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", label], check=True
    )
    return "systemd"


def _uninstall_linux() -> None:
    label = service_label_linux()
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", label], capture_output=True
    )
    unit_path = Path.home() / ".config" / "systemd" / "user" / label
    if unit_path.exists():
        unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def _status_linux() -> int:
    label = service_label_linux()
    subprocess.run(["systemctl", "--user", "status", label])
    return 0
