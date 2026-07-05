"""All file system locations used by the proxy on each platform.

Pure path computation -- no IO. Tests and installer code call into this
so there is exactly one source of truth for "where does the service
install put things on a given OS."

Locations:
  - logs_root:       where stdout/stderr land for the running service.
                     Each platform has its own convention; user-level when
                     possible (no admin).
  - service_config:  a small JSON file recording which install mechanism
                     was actually used (NSSM, Task Scheduler, HKCU Run on
                     Windows; launchd on macOS; systemd on Linux). The
                     uninstaller reads this to know what to clean up.
  - pidfile:         cross-platform single-instance lock. The service
                     acquires a non-blocking exclusive file lock on
                     start; a second proxy attempts to bind the port
                     and exits cleanly.
  - service_label:   the platform-native identifier (Windows service
                     name, launchd Label, systemd unit name).
"""

from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """Top-level project directory (the git repo checkout or the in-place
    install location). Used for log file placement when no user-home is
    available (e.g. Windows service running as SYSTEM)."""
    # The repo lives at <root>/hermes_openai_proxy/service_paths.py.
    # Caller passes an alternate location when running from a wheel.
    return Path(__file__).resolve().parent.parent


def user_home() -> Path:
    """User home directory. Path.home() with a Windows fallback."""
    p = Path.home()
    if str(p) == "~" or not p.exists():
        # We're running as a service account (SYSTEM) with no real home.
        # Fall back to a system-wide, admin-writable location.
        if sys.platform == "win32":
            return Path("C:/ProgramData/hermes-openai-proxy")
        return Path("/var/log/hermes-openai-proxy")
    return p


# ---------- logs ----------
def logs_root() -> Path:
    """Directory where the service writes stdout/stderr files."""
    if sys.platform == "win32":
        return user_home()  # %USERPROFILE% on user-level, ProgramData as fallback
    return user_home()


def stdout_log_path() -> Path:
    return logs_root() / "hermes-openai-proxy.log"


def stderr_log_path() -> Path:
    return logs_root() / "hermes-openai-proxy.err.log"


# ---------- service config (records which mechanism was used) ----------
def service_config_path() -> Path:
    """JSON file recording: host, port, mechanism, label, install timestamp.

    Created on first --install, read by --uninstall / --upgrade / --status
    so we don't have to guess which mechanism is in play.
    """
    if sys.platform == "win32":
        return Path.home() / "hermes-openai-proxy" / "service.json"
    if sys.platform == "darwin":
        return user_home() / "Library" / "Application Support" / "hermes-openai-proxy" / "service.json"
    # Linux
    return user_home() / ".config" / "hermes-openai-proxy" / "service.json"


# ---------- single-instance lock ----------
def pidfile_path() -> Path:
    """Cross-platform single-instance lock. Acquired with an exclusive
    non-blocking flock on Linux/macOS; on Windows we use msvcrt locking
    on an open file handle. Either way, a second proxy that tries to
    bind the same port discovers the lock and exits cleanly."""
    if sys.platform == "win32":
        return Path.home() / "hermes-openai-proxy" / "proxy.pid"
    # macOS / Linux: /tmp is shared but wiped on reboot; user-scoped is
    # safer for BYOM localhost-only use.
    return Path("/tmp/hermes-openai-proxy.pid")


# ---------- service labels (per platform) ----------
def service_label_macos() -> str:
    return "com.hermes.openai-proxy"


def service_label_linux() -> str:
    return "hermes-openai-proxy.service"


def service_label_windows() -> str:
    """Display name and registry service-name. NSSM uses the latter as
    the SCM service key."""
    return "HermesOpenAIProxy"


# ---------- service-platform constants (per platform, used by installer) ----------
def platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"
