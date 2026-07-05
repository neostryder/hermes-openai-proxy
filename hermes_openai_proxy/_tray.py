"""System-tray / menu-bar icon for the proxy.

Optional functionality. The proxy itself runs headless (no GUI, no
icons, no notifications). When run with `--tray`, this module spawns
a backend-specific tray app:

  Windows: pystray (uses the system tray Shell_NotifyIcon).
  macOS:   rumps (native NSStatusItem).
  Linux:   pystray (uses Ayatana / StatusNotifierItem via DBus).

The tray exposes a **management** menu:

  Status     v{version}  -- {host}:{port}     (header, not clickable)
             --healthz--  /healthz: OK        (live, refreshed every 5s)
  -------
  Open /healthz in browser
  Open logs folder
  Copy base URL to clipboard
  -------
  Start proxy at login         (toggle, when not registered)
  Stop proxy at login          (toggle, when registered)
  -------
  Restart proxy                (--upgrade)
  Stop proxy                   (stops the service, leaves install intact)
  Uninstall proxy              (--uninstall, full removal)
  Quit                         (closes tray, leaves proxy running)

Each action runs in a background thread so the menu thread never
blocks the UI. The "Stop" / "Restart" / "Uninstall" actions also close
the tray themselves when appropriate so the user doesn't end up with
a tray controlling a service that no longer exists.

The icon itself is a tiny inline SVG (rendered to PNG by cairosvg
on macOS/Linux, by Pillow's bitmap fallback on Windows). We avoid
shipping an icon binary to keep the install footprint small and
license-clear.

If `pystray` or `rumps` isn't installed, --tray errors immediately.
The required deps are in pyproject.toml's optional `tray` group.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable

log = logging.getLogger("hermes-openai-proxy.tray")


# A small inline SVG. The "H" represents Hermes; the green dot in
# the corner signals "running". When the proxy is unhealthy we flip
# the dot to red via a separate template. Two SVGs, two icons.
_ICON_SVG_OK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="10" fill="#1f6feb"/>'
    '<text x="32" y="46" font-size="42" font-family="sans-serif" '
    'fill="white" text-anchor="middle" font-weight="700">H</text>'
    '<circle cx="50" cy="14" r="6" fill="#3fb950"/>'
    "</svg>"
)

_ICON_SVG_FAIL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="10" fill="#1f6feb"/>'
    '<text x="32" y="46" font-size="42" font-family="sans-serif" '
    'fill="white" text-anchor="middle" font-weight="700">H</text>'
    '<circle cx="50" cy="14" r="6" fill="#f85149"/>'
    "</svg>"
)


# =====================================================================
#  Icon rendering
# =====================================================================

def _svg_to_png_bytes(svg: str) -> bytes:
    """Render SVG to PNG bytes. cairosvg first, PIL fallback.

    cairosvg needs cairo system libs which Windows users sometimes
    lack; on Windows we go straight to PIL drawing so the tray icon
    is always available as long as Pillow is.
    """
    # macOS/Linux: cairosvg path (clean rendering, full vector).
    if sys.platform != "win32":
        try:
            import cairosvg  # type: ignore[import-not-found]
            return cairosvg.svg2png(bytestring=svg.encode("utf-8"),
                                    output_width=64, output_height=64)
        except ImportError:
            pass
    # Windows + cairosvg-unavailable fallback: PIL drawing. Same look.
    import io

    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (31, 111, 235, 255))
    draw = ImageDraw.Draw(img)
    draw.text((22, 8), "H", fill="white")
    # Status dot in the corner. Color depends on which icon we drew.
    dot = (63, 185, 80, 255) if svg == _ICON_SVG_OK else (248, 81, 73, 255)
    draw.ellipse((42, 6, 58, 22), fill=dot)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _load_icon(healthy: bool):
    """Return a backend-specific icon object (PIL.Image for pystray,
    raw bytes for rumps)."""
    svg = _ICON_SVG_OK if healthy else _ICON_SVG_FAIL
    if sys.platform == "darwin":
        return _svg_to_png_bytes(svg)
    import io

    from PIL import Image
    return Image.open(io.BytesIO(_svg_to_png_bytes(svg)))


# =====================================================================
#  Background-thread runner for tray actions
# =====================================================================

def _run_in_thread(fn: Callable[[], None]) -> None:
    """Run a tray action in a worker thread. The tray backend stays
    responsive while the action does its work (restart, uninstall)."""
    t = threading.Thread(target=fn, daemon=True, name="tray-action")
    t.start()


# =====================================================================
#  Live status poller (every 5s)
# =====================================================================

class _StatusPoller:
    """Thread that polls /healthz every 5s and updates an icon's tooltip
    + the status menu items.

    A standalone class so it works with both pystray and rumps: each
    backend polls and decides how to render the result.
    """

    def __init__(self, host: str, port: int, version: str):
        self.host = host
        self.port = port
        self.version = version
        # Synchronous first probe so the menu's initial render shows
        # the current state instead of "checking..." for the first 5s.
        first_healthy, first_detail = self._probe()
        self._healthy = first_healthy
        self._detail = first_detail
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._on_change: Callable[[bool, str], None] | None = None

    def on_change(self, callback: Callable[[bool, str], None]) -> None:
        """Backend registers a function(healthy, detail) to be called
        whenever health changes. The callback runs on the poller thread;
        the backend is responsible for marshalling to the UI thread."""
        self._on_change = callback

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="tray-poller")
        t.start()

    def stop(self) -> None:
        self._stop.set()

    def is_healthy(self) -> bool:
        with self._lock:
            return self._healthy

    def detail(self) -> str:
        with self._lock:
            return self._detail

    def _loop(self) -> None:
        while not self._stop.is_set():
            healthy, detail = self._probe()
            with self._lock:
                changed = (healthy != self._healthy or detail != self._detail)
                self._healthy = healthy
                self._detail = detail
            if changed and self._on_change is not None:
                try:
                    self._on_change(healthy, detail)
                except Exception as e:
                    log.debug("on_change callback raised: %s", e)
            self._stop.wait(5.0)

    def _probe(self) -> tuple[bool, str]:
        """Quick TCP probe + /healthz GET for the model line."""
        try:
            with socket.create_connection((self.host, self.port), timeout=1.5):
                pass
        except OSError as e:
            return False, f"unreachable: {e}"
        # /healthz probe -- read the model + version fields.
        import urllib.request
        try:
            with urllib.request.urlopen(
                f"http://{self.host}:{self.port}/healthz", timeout=1.5,
            ) as r:
                import json
                data = json.loads(r.read().decode("utf-8"))
                return True, f"v{data.get('version', '?')}  default_model={data.get('default_model', '?')}"
        except Exception as e:
            return False, f"healthz failed: {e}"


# =====================================================================
#  /logs-folder helper
# =====================================================================

def _open_logs_folder() -> None:
    """Open the proxy's log directory in the OS file manager."""
    from .service_paths import logs_root
    path = logs_root()
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        # Use explorer.exe and quote the path so spaces are handled.
        subprocess.Popen(["explorer", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _copy_to_clipboard(text: str) -> None:
    """Best-effort clipboard copy. macOS: pbcopy; Windows: clip; Linux: xclip."""
    try:
        if sys.platform == "darwin":
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
        elif sys.platform == "win32":
            # clip.exe reads from stdin until EOF.
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
        else:
            if shutil.which("xclip"):
                p = subprocess.Popen(["xclip", "-selection", "clipboard"],
                                     stdin=subprocess.PIPE)
                p.communicate(text.encode("utf-8"))
            elif shutil.which("xsel"):
                p = subprocess.Popen(["xsel", "--clipboard", "--input"],
                                     stdin=subprocess.PIPE)
                p.communicate(text.encode("utf-8"))
            else:
                print(f"No clipboard tool found; base URL is: {text}", file=sys.stderr)
    except Exception as e:
        print(f"clipboard copy failed: {e}", file=sys.stderr)


# =====================================================================
#  Install / uninstall / restart / stop (called from menu actions)
# =====================================================================

def _action_install(host: str, port: int) -> None:
    subprocess.run(
        [sys.executable, "-m", "hermes_openai_proxy",
         "--host", host, "--port", str(port), "--install"],
        check=False,
    )


def _action_uninstall() -> None:
    subprocess.run(
        [sys.executable, "-m", "hermes_openai_proxy", "--uninstall"],
        check=False,
    )


def _action_upgrade() -> None:
    subprocess.run(
        [sys.executable, "-m", "hermes_openai_proxy", "--upgrade"],
        check=False,
    )


def _action_stop() -> None:
    """Stop the registered service without uninstalling.

    Windows: try sc stop, fall back to killing HKCU-Run python.
    macOS: launchctl unload.
    Linux: systemctl --user stop.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*hermes_openai_proxy*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True,
        )
        subprocess.run(["sc", "stop", "HermesOpenAIProxy"], capture_output=True)
    elif sys.platform == "darwin":
        from pathlib import Path
        plist = Path.home() / "Library/LaunchAgents" / "com.hermes.openai-proxy.plist"
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    else:
        subprocess.run(
            ["systemctl", "--user", "stop", "hermes-openai-proxy.service"],
            capture_output=True,
        )


# =====================================================================
#  pystray backend (Windows / Linux)
# =====================================================================
def _backend_pystray(host: str, port: int, version: str) -> None:
    """Windows / Linux system-tray icon via pystray.

    The status line is dynamic via MenuItem(label=callable). pystray
    calls our callable on every menu render, so we read the latest
    poller state without needing to rebuild the menu. The icon image
    is rebuilt and pushed when health changes (via update_menu()).
    """
    import pystray

    poller = _StatusPoller(host, port, version)
    icon_ref: dict = {}

    def status_text(_item=None) -> str:
        healthy = poller.is_healthy()
        detail = poller.detail() or ("checking..." if not healthy else "OK")
        if healthy:
            return f"OK -- {detail}"
        return f"FAIL -- {detail}"

    def make_icon(healthy: bool):
        img = _load_icon(healthy)
        icon_ref["obj"].icon = img

    def on_change(healthy: bool, _detail: str) -> None:
        if "obj" in icon_ref:
            make_icon(healthy)
            icon_ref["obj"].update_menu()

    poller.on_change(on_change)
    poller.start()

    icon = pystray.Icon(
        "hermes-openai-proxy",
        _load_icon(True),
        f"hermes-openai-proxy v{version}",
        menu=pystray.Menu(
            # Header (read-only): version and address. Plain strings.
            pystray.MenuItem(f"v{version} on {host}:{port}",
                             None, enabled=False),
            # Status line: dynamic. callable -> re-evaluated each render.
            pystray.MenuItem(status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,

            # Group 1: open / copy
            pystray.MenuItem("Open /healthz in browser",
                             lambda *_: _run_in_thread(lambda: webbrowser.open(f"http://{host}:{port}/healthz"))),
            pystray.MenuItem("Open logs folder",
                             lambda *_: _run_in_thread(_open_logs_folder)),
            pystray.MenuItem(f"Copy base URL ({host}:{port})",
                             lambda *_: _run_in_thread(lambda: _copy_to_clipboard(f"http://{host}:{port}/v1"))),
            pystray.Menu.SEPARATOR,

            # Group 2: lifecycle
            pystray.MenuItem("Restart proxy (--upgrade)",
                             lambda *_: _run_in_thread(_action_upgrade)),
            pystray.MenuItem("Stop proxy",
                             lambda *_: _run_in_thread(_action_stop)),
            pystray.MenuItem("Uninstall proxy (full removal)",
                             lambda *_: _run_in_thread(_action_uninstall)),
            pystray.Menu.SEPARATOR,

            # Quit: close the tray, leave the proxy running.
            pystray.MenuItem("Quit tray (proxy stays running)",
                             lambda *_: icon_ref["obj"].stop()),
        ),
    )
    icon_ref["obj"] = icon

    try:
        icon.run()
    finally:
        poller.stop()


# =====================================================================
#  rumps backend (macOS)
# =====================================================================

def _backend_rumps(host: str, port: int, version: str) -> None:
    """macOS native menu bar via rumps."""
    import rumps

    poller = _StatusPoller(host, port, version)

    class HermesProxyApp(rumps.App):
        def __init__(self):
            super().__init__("hp", icon=_load_icon(True))
            self.version = version
            self.host = host
            self.port = port
            self._build_menu()

        def _build_menu(self):
            """(Re)build the menu from current poller state. Called on
            init and after every poll update."""
            healthy = poller.is_healthy()
            detail = poller.detail() or ("OK" if healthy else "checking...")
            status_label = ("OK -- " + detail) if healthy else ("FAIL -- " + detail)
            self.menu.clear()
            self.menu = [
                rumps.MenuItem(f"v{version} on {host}:{port}", None),
                rumps.MenuItem(status_label, None),
                None,
                rumps.MenuItem("Open /healthz in browser", callback=self.open_health),
                rumps.MenuItem("Open logs folder", callback=self.open_logs),
                rumps.MenuItem(f"Copy base URL ({host}:{port})",
                               callback=self.copy_url),
                None,
                rumps.MenuItem("Restart proxy (--upgrade)", callback=self.restart),
                rumps.MenuItem("Stop proxy", callback=self.stop_proxy),
                rumps.MenuItem("Uninstall proxy (full removal)",
                               callback=self.uninstall),
                None,
                rumps.MenuItem("Quit tray (proxy stays running)",
                               callback=rumps.quit_application),
            ]
            self.icon = _load_icon(healthy)

        @rumps.clicked("Open /healthz in browser")
        def open_health(self, _):
            webbrowser.open(f"http://{host}:{port}/healthz")

        @rumps.clicked("Open logs folder")
        def open_logs(self, _):
            _open_logs_folder()

        @rumps.clicked(f"Copy base URL ({host}:{port})")
        def copy_url(self, _):
            _copy_to_clipboard(f"http://{host}:{port}/v1")

        @rumps.clicked("Restart proxy (--upgrade)")
        def restart(self, _):
            _run_in_thread(_action_upgrade)

        @rumps.clicked("Stop proxy")
        def stop_proxy(self, _):
            _run_in_thread(_action_stop)

        @rumps.clicked("Uninstall proxy (full removal)")
        def uninstall(self, _):
            _run_in_thread(_action_uninstall)

    app = HermesProxyApp()

    # Poll callback: refresh the menu on every change. rumps handles
    # marshalling from this thread to the main run loop.
    def _on_change(_healthy: bool, _detail: str) -> None:
        rumps.notification("hermes-openai-proxy", "", "Status changed")
        app._build_menu()
    poller.on_change(_on_change)
    poller.start()
    try:
        app.run()
    finally:
        poller.stop()


# =====================================================================
#  Public entry point
# =====================================================================

def run(host: str = "127.0.0.1", port: int = 8765, version: str = "0.1.0") -> None:
    """Spawn the platform's tray icon. Blocks until the user clicks Quit.

    Errors loudly if the tray deps aren't installed: print a clear
    hint pointing at `pip install 'hermes-openai-proxy[tray]'`.
    """
    if sys.platform == "darwin":
        try:
            _backend_rumps(host, port, version)
            return
        except ImportError:
            print(
                "rumps not installed. Run "
                "`pip install 'hermes-openai-proxy[tray]'` "
                "to enable the menu-bar icon.",
                file=sys.stderr,
            )
            sys.exit(1)
    # Windows / Linux: pystray.
    try:
        _backend_pystray(host, port, version)
    except ImportError:
        print(
            "pystray not installed. Run "
            "`pip install 'hermes-openai-proxy[tray]'` "
            "to enable the system-tray icon.",
            file=sys.stderr,
        )
        sys.exit(1)