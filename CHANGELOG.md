# Changelog

All notable changes to hermes-openai-proxy are documented here. Versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  end-to-end field use until Nate runs it. The `Linux: untested`
  warning is explicit in the install code's docstring.

[Unreleased]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.2.0a1...HEAD
[0.2.0a1]: https://github.com/neostryder/hermes-openai-proxy/compare/v0.1.0...v0.2.0a1

## [0.1.0] - 2026-07-05

### Fixed
