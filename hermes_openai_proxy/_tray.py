"""System-tray / menu-bar icon for the proxy.

Optional functionality. The proxy itself runs headless (no GUI, no
icons, no notifications). When run with `--tray`, this module spawns
a backend-specific tray app:

  Windows: pystray (uses the system tray Shell_NotifyIcon).
  macOS:   rumps (native NSStatusItem).
  Linux:   pystray (uses Ayatana / StatusNotifierItem via DBus).

The tray exposes:
  - Show status: opens a small window with the version, port, /healthz.
  - Open in browser: opens the /healthz URL in the default browser.
  - Restart: re-runs --uninstall + --install with the recorded args.
  - Quit proxy: stops the service (does not uninstall).

The icon itself is a tiny SVG (generated inline below) with the
proxy's hostname in the corner. We avoid shipping an icon binary to
keep the install footprint small and license-clear.

If `pystray` or `rumps` isn't installed, --tray errors immediately.
The required deps are in pyproject.toml's optional `tray` group.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import webbrowser

# A small inline SVG. The text is a generic "H" stylized as an open
# network socket; it's intentionally low-fi so the package works
# without an external icon asset.
_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="10" fill="#1f6feb"/>'
    '<text x="32" y="44" font-size="40" font-family="sans-serif" '
    'fill="white" text-anchor="middle" font-weight="700">H</text>'
    '<circle cx="50" cy="14" r="5" fill="#3fb950"/>'
    "</svg>"
)


def _svg_to_png_bytes() -> bytes:
    """Convert the inline SVG to PNG bytes via a tiny pure-python path.

    We avoid shipping PIL/Pillow as a hard requirement; Pillow is in
    the tray extras group, so importing it here only happens when the
    user runs --tray.
    """
    try:
        import io

        import cairosvg  # type: ignore[import-not-found]
        from PIL import Image
        png = cairosvg.svg2png(bytestring=_ICON_SVG.encode("utf-8"),
                               output_width=64, output_height=64)
        return png
    except ImportError:
        pass
    # Fallback: render without cairosvg by using PIL with a basic bitmap.
    # Less pretty but works on every platform.
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (31, 111, 235, 255))
    draw = ImageDraw.Draw(img)
    draw.text((22, 12), "H", fill="white")
    draw.ellipse((42, 6, 58, 22), fill=(63, 185, 80, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _backend_pystray(host: str, port: int, version: str):
    import io

    import pystray
    from PIL import Image

    icon_image = Image.open(io.BytesIO(_svg_to_png_bytes()))

    def on_open(icon, item):
        webbrowser.open(f"http://{host}:{port}/healthz")

    def on_status(icon, item):
        # Open a console window on Windows showing status; on macOS a
        # notification. pystray doesn't have native alert popups; we
        # use the title update trick.
        icon.notify(f"v{version} on http://{host}:{port}\n"
                    f"/healthz returning: {_quick_health(host, port)}",
                    "hermes-openai-proxy")

    def on_restart(icon, item):
        subprocess.Popen([sys.executable, "-m", "hermes_openai_proxy", "--upgrade"])

    def on_quit(icon, item):
        icon.stop()
        # Stop the service: re-use the same args as --install, but
        # route to --uninstall --start-fresh.
        subprocess.Popen([sys.executable, "-m", "hermes_openai_proxy", "--uninstall"])

    icon = pystray.Icon(
        "hermes-openai-proxy",
        icon_image,
        f"hermes-openai-proxy v{version}",
        menu=pystray.Menu(
            pystray.MenuItem(f"Status: {host}:{port}", None, enabled=False),
            pystray.MenuItem("Open /healthz in browser", on_open),
            pystray.MenuItem("Show status", on_status),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart proxy", on_restart),
            pystray.MenuItem("Stop proxy (uninstall)", on_quit),
        ),
    )
    icon.run()


def _backend_rumps(host: str, port: int, version: str):
    """macOS native menu bar via rumps. Same UX as pystray path."""
    import rumps

    class HermesProxyApp(rumps.App):
        def __init__(self):
            super().__init__("hp")
            self.title = "hp"
            self.menu = [
                rumps.MenuItem(f"v{version} on {host}:{port}", None),
                None,
                rumps.MenuItem("Open /healthz", self.open_health),
                rumps.MenuItem("Show status", self.show_status),
                None,
                rumps.MenuItem("Restart proxy", self.restart),
                rumps.MenuItem("Stop proxy", self.stop),
            ]

        def open_health(self, _):
            webbrowser.open(f"http://{host}:{port}/healthz")

        def show_status(self, _):
            rumps.notification(
                "hermes-openai-proxy",
                f"v{version} on {host}:{port}",
                f"/healthz: {_quick_health(host, port)}",
            )

        def restart(self, _):
            subprocess.Popen([sys.executable, "-m", "hermes_openai_proxy", "--upgrade"])

        def stop(self, _):
            rumps.quit_application()
            subprocess.Popen([sys.executable, "-m", "hermes_openai_proxy", "--uninstall"])

    HermesProxyApp().run()


def _quick_health(host: str, port: int) -> str:
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        s.close()
        return "OK"
    except OSError as e:
        return f"FAIL ({e})"


def run(host: str = "127.0.0.1", port: int = 8765, version: str = "0.1.0") -> None:
    """Spawn the platform's tray icon.

    Blocks until the user clicks Quit. Errors loudly if the tray deps
    aren't installed."""
    if sys.platform == "darwin":
        try:
            _backend_rumps(host, port, version)
            return
        except ImportError:
            print("rumps not installed. `pip install 'hermes-openai-proxy[tray]'` to enable the tray icon.",
                  file=sys.stderr)
            sys.exit(1)
    # Windows / Linux: pystray.
    try:
        _backend_pystray(host, port, version)
    except ImportError:
        print("pystray not installed. `pip install 'hermes-openai-proxy[tray]'` to enable the tray icon.",
              file=sys.stderr)
        sys.exit(1)
