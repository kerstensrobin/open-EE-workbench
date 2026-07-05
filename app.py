#!/usr/bin/env python3
"""
open-EE-workbench — web GUI
────────────────────────────────────────────────────────────────────
Flask + SocketIO backend served inside a PyWebView native window.

Usage
─────
    python app.py                 # native window (PyWebView)
    python app.py --browser       # open in system browser instead
    python app.py --port 5173     # different port
    python app.py gu128desk       # pre-load a workbench
"""

import argparse
import atexit
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT   = Path(__file__).parent
UI_DIR = ROOT / "ui"

# core/ and its siblings must be importable by bare module name
sys.path.insert(0, str(ROOT / "core"))

from core.browser import find_chrome

# ── PyVISA-Py USBTMC bug fix ──────────────────────────────────────────────────
# pyvisa-py ≤ 0.8.1: USBTMC.read() inner while uses `or` instead of `and`,
# causing it to call raw_read() without a new REQUEST when the device sends
# exactly wMaxPacketSize-byte chunks (e.g. Rigol DS1000Z :DISPlay:DATA?).
# Fix: replace `or` with `and` so the loop only continues when BOTH conditions
# are true: last USB packet was full-sized AND we still need more bytes.
def _apply_usbtmc_patch():
    """Replace PyVISA-Py USBTMC.read() with a faster direct-USB implementation.

    Two problems with the stock pyvisa-py ≤ 0.8.1 USBTMC.read():

    1. Bug: inner while loop uses 'or' instead of 'and' → hangs on wMaxPacketSize-
       aligned chunks (e.g. Rigol DS1000Z screenshots) → VI_ERROR_TMO.

    2. Performance: USBRaw.read() adds ~650 µs of Python overhead per 64-byte USB
       packet. For a 1.15 MB screenshot that's 18,001 calls × 650 µs ≈ 12 s of pure
       overhead. Direct pyusb endpoint reads cost ~51 µs/call → ~1 s total.

    Fix: read the USBTMC header from the first packet, then drain remaining packets
    directly from the bulk-IN endpoint, bypassing USBRaw.read() overhead.
    """
    try:
        from pyvisa_py.protocols import usbtmc as _usbtmc_mod
        from pyvisa_py.protocols.usbtmc import BulkInMessage, USBRaw, USBTMC
        import usb.core as _usb_core
    except ImportError:
        return

    def _patched_read(self, size):
        eom = False
        raw_write = USBRaw.write.__get__(self, USBTMC)
        received_message = bytearray()
        ep  = self.usb_recv_ep
        pkt = ep.wMaxPacketSize
        to  = self.timeout

        while not eom:
            received_transfer = bytearray()
            self._btag = (self._btag % 255) + 1
            req = BulkInMessage.build_array(self._btag, size, None)
            raw_write(req)
            try:
                resp = bytes(ep.read(pkt, to))
                if len(resp) < 12:
                    continue
                response = BulkInMessage.from_bytes(resp)
                received_transfer.extend(response.data)
                expected = response.transfer_size
                while len(received_transfer) < expected:
                    n = min(expected - len(received_transfer) + pkt, 65536)
                    received_transfer.extend(bytes(ep.read(n, to)))
            except (_usb_core.USBError, ValueError):
                self._abort_bulk_in(self._btag)
                raise
            eom = response.transfer_attributes & 1
            if not eom and len(received_transfer) >= size:
                eom = True
            received_message.extend(received_transfer[:expected])

        return bytes(received_message)

    _usbtmc_mod.USBTMC.read = _patched_read


# ── Flask + SocketIO ──────────────────────────────────────────────────────────
from flask import Flask, send_from_directory
from flask_socketio import SocketIO

flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = "open-ee-workbench-key"
sio = SocketIO(flask_app, cors_allowed_origins="*", async_mode="threading",
               logger=False, engineio_logger=False)

# ── Inject sio into shared state BEFORE importing route modules ───────────────
import core.shared as _shared
_shared.sio = sio

# Apply USBTMC patch now that we know if pyvisa is available
if _shared.PYVISA_OK:
    _apply_usbtmc_patch()

# ── Register blueprints ───────────────────────────────────────────────────────
from core.routes.connection   import bp as bp_connection
from core.routes.workbench    import bp as bp_workbench
from core.routes.instruments  import bp as bp_instruments
from core.routes.automation   import bp as bp_automation
from core.routes.system       import bp as bp_system

flask_app.register_blueprint(bp_connection)
flask_app.register_blueprint(bp_workbench)
flask_app.register_blueprint(bp_instruments)
flask_app.register_blueprint(bp_automation)
flask_app.register_blueprint(bp_system)


# ── Static file routes ────────────────────────────────────────────────────────
@flask_app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@flask_app.route("/ui/<path:p>")
def ui_static(p):
    """Serve any file from the ui/ directory (e.g. socket.io.min.js)."""
    return send_from_directory(UI_DIR, p)


@flask_app.route("/assets/<path:p>")
def gui_assets(p):
    return send_from_directory(ROOT / "ui" / "gui_assets", p)


@flask_app.route("/favicon.ico")
@flask_app.route("/favicon.png")
def favicon():
    return send_from_directory(ROOT / "ui" / "gui_assets", "favicon-32.png",
                               mimetype="image/png")


@flask_app.route("/manifest.json")
def manifest():
    from flask import jsonify as _j
    return _j({
        "name":             "open-EE-workbench",
        "short_name":       "EEW",
        "display":          "standalone",
        "background_color": "#0d0e11",
        "theme_color":      "#231040",
        "icons": [
            {"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


# ── SocketIO ──────────────────────────────────────────────────────────────────
@sio.on("connect")
def _on_connect():
    from core.helpers import _log
    _log("[system] UI connected")


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _cleanup():
    """Close all open VISA connections. Safe to call more than once."""
    _shared._poll_stop.set()
    with _shared._lock:
        resources = list(_shared._state.get("resources", {}).values())
        rm        = _shared._state.get("rm")
        _shared._state["resources"] = {}
        _shared._state["rm"]        = None
        _shared._state["connected"] = False
    for r in resources:
        try:
            r.close()
        except Exception:
            pass
    if rm:
        try:
            rm.close()
        except Exception:
            pass


atexit.register(_cleanup)   # covers Ctrl-C, sys.exit(), and normal process end


# ── Entry point ───────────────────────────────────────────────────────────────

def _open_chrome_app(url: str, width: int = 1300, height: int = 840) -> subprocess.Popen:
    """Open url in Chromium app-mode (no address bar / tabs)."""
    chrome = find_chrome()
    if not chrome:
        raise FileNotFoundError("No Chromium-family browser found")
    return subprocess.Popen([
        chrome,
        f"--app={url}",
        f"--window-size={width},{height}",
        "--class=open-EE-workbench",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-extensions",
        "--password-store=basic",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _open_qtwebkit(url: str, width: int = 1300, height: int = 840):
    """Open url in a frameless PyQt5 QtWebKit window (no Chromium required)."""
    import sys as _sys
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtWebKitWidgets import QWebView
    from PyQt5.QtCore import QUrl

    qapp = QApplication(_sys.argv)
    view = QWebView()
    view.setWindowTitle("open-EE-workbench")
    view.resize(width, height)
    view.load(QUrl(url))
    view.show()
    _sys.exit(qapp.exec_())


def _run_server(port: int):
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    sio.run(flask_app, host="127.0.0.1", port=port,
            debug=False, use_reloader=False, log_output=False,
            allow_unsafe_werkzeug=True)


def main():
    ap = argparse.ArgumentParser(description="open-EE-workbench web GUI")
    ap.add_argument("workbench", nargs="?", default=None)
    ap.add_argument("--browser", action="store_true",
                    help="Open in system browser instead of native window")
    ap.add_argument("--port", type=int, default=5173)
    args = ap.parse_args()

    t = threading.Thread(target=_run_server, args=(args.port,), daemon=True)
    t.start()
    time.sleep(0.9)      # give Flask time to bind

    url = f"http://127.0.0.1:{args.port}"
    if args.workbench:
        url += f"?wb={args.workbench}"

    if args.browser:
        import webbrowser
        webbrowser.open(url)
        try:
            t.join()
        finally:
            _cleanup()
        return

    # ── Standalone window — try backends in order ─────────────────────────────
    # 1. Chrome / Brave --app mode: best rendering, real Chromium, no browser UI
    try:
        proc = _open_chrome_app(url)
        started = time.monotonic()
        proc.wait()   # block until user closes the window
        # If a Chrome/Brave/Edge instance is already running elsewhere, --app
        # just hands the request to that instance's single-instance mediator
        # and exits almost immediately — the window ends up owned by the
        # pre-existing process, not `proc`. Treating that as "window closed"
        # would kill the server the moment it starts. Only tear down if this
        # really was a standalone process that ran for a while.
        if time.monotonic() - started < 2:
            try:
                t.join()
            finally:
                _cleanup()
        else:
            _cleanup()
        return
    except FileNotFoundError:
        pass

    # 2. PyQt5 + QtWebKit: pure Qt, no Chromium sandbox issues
    try:
        from PyQt5.QtWebKitWidgets import QWebView  # noqa: F401 — check availability
        _open_qtwebkit(url)   # calls sys.exit() → atexit fires _cleanup
        return
    except ImportError:
        pass

    # 3. Last resort: system browser
    import webbrowser
    webbrowser.open(url)
    try:
        t.join()
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
