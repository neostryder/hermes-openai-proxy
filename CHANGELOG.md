# Changelog

All notable changes to hermes-openai-proxy are documented here. Versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0a3] - 2026-07-05

### Added

- **`--tray-autostart` / `--tray-stop-autostart`**: register (or
  remove) the tray icon at user logon. The registered command runs
  `--tray` only -- it connects to an existing proxy, never starts
  a second one. Cross-platform:
    - **Windows**: HKCU\Software\Microsoft\Windows\CurrentVersion\Run
      key `HermesOpenAIProxy.Tray`.
    - **macOS**: LaunchAgent
      `~/Library/LaunchAgents/com.hermes.openai-proxy.tray.plist`
      with `RunAtLoad=true` and `ProcessType=Interactive` so the
      tray lives in the user's Aqua session.
    - **Linux**: systemd --user unit
      `hermes-openai-proxy.tray.service`, installed via
      `systemctl --user enable --now`. **Linux: untested end-to-end.**
- **`--tray` semantic change**: `--tray` alone no longer tries to
  start a second proxy. It connects to an existing proxy on
  `--host:port` and errors out with a clear hint if nothing is
  listening. New `--start-proxy` flag pairs with `--tray` to start
  a foreground proxy alongside the tray in the same terminal
  (handy for development; production users run the proxy via
  `--install` and just `--tray` for the icon).

### Fixed

- **`--tray` second-proxy collision**. Previously, `--tray` would
  take the single-instance lock and EADDRINUSE-fail with
  exit code 78 against a service-installed proxy, leaving the user
  no clear path to get an icon. Now `--tray` is a pure client of
  the proxy; the lock is not consulted.

### Changed

- The README "Verified on" table gains a row noting that the tray
  is opt-in (never auto-starts with the service). Recommend running
  `python -m hermes_openai_proxy --tray-autostart` once after
  `--install` to register the tray for logon.

[Unreleased]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.2.0a3...HEAD
[0.2.0a3]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.2.0a2...v0.2.0a3

## [0.2.0a2] - 2026-07-05

### Added

- **Tray icon with management menus** (Windows + macOS, opt-in via
  `pip install 'hermes-openai-proxy[tray]'` + `python -m hermes_openai_proxy
  --tray`). The icon stays in the system tray / menu bar; the menu
  exposes:
    - Header with version and listening address
    - Live status line (refreshed every 5 s; first render after a
      synchronous probe so the menu never shows "checking...")
    - **Open /healthz in browser**
    - **Open logs folder** (reveals the proxy log directory in the
      OS file manager)
    - **Copy base URL to clipboard** (pbcopy on macOS, clip.exe on
      Windows, xclip/xsel on Linux)
    - **Restart proxy** (runs `python -m hermes_openai_proxy
      --upgrade` in a worker thread, returns immediately to the
      tray)
    - **Stop proxy** (Windows: stops the NSSM service OR kills the
      HKCU-Run python via CIM; macOS: launchctl unload; Linux:
      systemctl --user stop). The service registration stays intact.
    - **Uninstall proxy** (full removal: plist / sc / systemd + log
      files left to user)
    - **Quit tray** (closes the menu-bar icon but leaves the proxy
      running)
  Linux ships the same UI via pystray (status-notifier-item over DBus)
  but is **untested** end-to-end -- the tray code path is the same
  on Windows + Linux, but the Linux test base is still empty.

### Changed

- **Tray icon: dynamic status text** via `MenuItem(text=callable)`
  rather than rebuilding the menu on every poll. pystray re-evaluates
  the callable on every menu render, so the status line is always
  live without thread-marshalling overhead.
- **README** gains a "Verified on" table that explicitly lists
  Windows 10 and macOS 16 Tahoe as tested, and Linux as
  implemented-but-untested. The `Linux: untested` banner is also in
  the install code's docstring and is the source of truth for the
  status.

### Notes

- The `--tray` flag is **opt-in**: it never auto-starts with the
  service. To get the tray icon after `--install`, run
  `python -m hermes_openai_proxy --tray` from a desktop session
  (it's a GUI app; it can't run inside a headless service).
- pystray + rumps + Pillow + (cairosvg on macOS/Linux) live in the
  `tray` optional group; the proxy stays headless without them.

[Unreleased]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.2.0a2...HEAD
[0.2.0a2]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.2.0a1...v0.2.0a2

## [0.2.0a1] - 2026-07-05

### Added

- **`--upgrade` subcommand.** Atomically `pip install --upgrade` (or
  `uv pip install --upgrade` on hermes-agent-style venvs without pip)
  followed by service restart and `/healthz` re-validation. Idempotent
  and safe: never destroys a running service on a network failure.
- **Single-instance lock** (`hermes_openai_proxy/_service_lock.py`):
  a stdlib-only cross-platform PID-file lock. Two proxes on the same
  host fail cleanly with `EX_CONFIG` (exit code 78) and a message
  pointing at `--status` instead of colliding with `EADDRINUSE`.
- **`--tray` system-tray / menu-bar icon** (optional, requires
  `pip install 'hermes-openai-proxy[tray]'`). Provides open-/healthz,
  status, restart, and stop from the menu. The proxy itself stays
  headless; the tray is one extra thread that does not block startup.
- **Port precheck** in `--install`. Before writing the plist / sc
  service / systemd unit, `--install` does a TCP-level `connect()` to
  detect a listener at the target port. If the port is occupied by
  another process, `--install` exits with the holding PID and a hint
  to pick `--port <N>`.
- **macOS log-rotation LaunchAgent.** A companion launchd plist runs
  the bundled `hermes-logrotate.sh` every 5 minutes, rotating
  `hermes-openai-proxy.log` and `.err.log` at 10 MB, keeping 5
  generations. Closes the macOS gap vs Windows NSSM's
  `AppRotateBytes`.

### Fixed

- **HKCU Run install leaving a CMD window visible.** The detached
  spawn now uses `CREATE_NO_WINDOW | DETACHED_PROCESS` (0x08000008),
  plus `stdin/stdout/stderr=DEVNULL, close_fds=True`. The previous
  `DETACHED_PROCESS` alone was insufficient against Python's
  console-subsystem binary, which left a CMD window on the user's
  desktop. Verified by checking `MainWindowHandle == 0` post-install.
- **`--uninstall` left orphaned HKCU-Run python processes.** On the
  HKCU Run path there's no service manager, so uninstall now kills
  any `python.exe` whose command line contains the recorded
  `(host, port)` before unlinking state files.
- **`--uninstall` could fail with `PermissionError` on the pidfile**
  when the proxy was still bound. Tolerant unlink with
  `(FileNotFoundError, OSError)` suppression.
- **`--install` rejected itself as a port collision** when re-run
  against an already-running proxy (the bind-based precheck fired
  inside the same user shell). Idempotency is now checked BEFORE the
  port precheck so a second `--install` is a clean no-op.

### Changed

- **Service install code restructured.** `hermes_openai_proxy/_service.py`
  now contains `install / uninstall / status / upgrade` as a coherent
  module, with a separate `service_paths.py` for cross-platform
  filesystem paths and `_service_lock.py` for the PID lock. The
  `__main__.py` CLI is a thin wrapper over these. Windows-cleanup
  code now sweeps ALL three mechanisms (NSSM + schtasks + HKCU Run)
  instead of guessing which is active.
- **Linux systemd unit** adds `Wants=network-online.target` and
  enables `loginctl enable-linger` so the service survives logout
  and starts after boot.
- **Operating-system classifiers** in `pyproject.toml` split into
  the three specific platforms instead of `OS Independent`, since
  the install code is now genuinely per-platform.

### Notes

- **Tested**: Windows 10 (Eru, neost; HKCU Run + NSSM fallback),
  macOS 16 Tahoe (gamemaster, rpgm; launchd + logrotate plist).
- **Not tested**: Linux. The `systemd --user` unit is implemented
  and uses `enable-linger` for headless operation, but lacks
  end-to-end field use. The `Linux: untested` warning is explicit in
  the install code's docstring and in the README "Verified on" table;
  a CI test on a Linux VM is tracked as a follow-up.

[Unreleased]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.2.0a1...HEAD
[0.2.0a1]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.1.0...v0.2.0a1

## [0.1.0] - 2026-07-05

### Fixed
