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
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "setup"))
WORKBENCH_DIR = ROOT / "workbenches"
UI_DIR        = ROOT / "ui"

# ── Project helpers ───────────────────────────────────────────────────────────
try:
    from workbench import active_name, load_workbench
    from instruments import get_command, _resolve_family, _family_index, classify as _classify
    HELPERS_OK = True
except ImportError as _e:
    HELPERS_OK = False
    def active_name():          return None
    def load_workbench(n=None): raise RuntimeError("workbench helpers unavailable")
    def get_command(*a, **k):   raise KeyError
    def _family_index():        return {}
    def _resolve_family(f):     return f
    def _classify(idn):         return None

try:
    import pyvisa
    PYVISA_OK = True
except ImportError:
    PYVISA_OK = False

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO

# ── Flask / SocketIO ──────────────────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = "open-ee-workbench-key"
sio = SocketIO(flask_app, cors_allowed_origins="*", async_mode="threading",
               logger=False, engineio_logger=False)

# ── App state ─────────────────────────────────────────────────────────────────
_state: dict = {
    "workbench":   None,   # loaded workbench dict (with _unique list)
    "wb_name":     None,
    "rm":          None,   # pyvisa ResourceManager
    "resources":   {},     # resource_str -> pyvisa resource
    "families":    {},     # resource_str -> resolved family dict
    "connected":   False,
}
_lock     = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=8)
_poll_stop = threading.Event()


# ── SCPI helpers ──────────────────────────────────────────────────────────────
def _family_for(entry: dict):
    if not HELPERS_OK:
        return None
    fid = entry.get("family_id")
    if not fid:
        return None
    try:
        idx = _family_index()
        return _resolve_family(idx[fid]) if fid in idx else None
    except Exception:
        return None


def _run_steps(resource, steps: list) -> object:
    result = None
    for action, scpi in steps:
        if action == "write":
            resource.write(scpi)
        elif action == "query":
            result = resource.query(scpi).strip()
        elif action == "raw_query":
            resource.write(scpi)
            result = resource.read_raw()
    return result


def _op(resource, family, operation: str, **kwargs):
    if resource is None or family is None:
        return None
    try:
        steps = get_command(family, operation, **kwargs)
        result = _run_steps(resource, steps)
        writes = [s for a, s in steps if a in ("write", "query")]
        sio.emit("log", {"msg": "→  " + "  |  ".join(writes[:2])})
        return result
    except KeyError:
        sio.emit("log", {"msg": f"⚠  {operation!r} not supported on this instrument"})
        return None


def _log(msg: str):
    sio.emit("log", {"msg": msg})


# ── Workbench API ─────────────────────────────────────────────────────────────
@flask_app.route("/api/workbenches")
def api_workbenches():
    names = sorted(
        f.stem for f in WORKBENCH_DIR.glob("*.json")
        if f.name != "active.json" and f.exists()
    )
    return jsonify({"workbenches": names, "active": active_name()})


@flask_app.route("/api/workbench/<name>")
def api_load_workbench(name: str):
    try:
        wb = load_workbench(name)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    seen, unique = set(), []
    for instr in wb.get("instruments", []):
        key = (instr.get("serial", ""), instr.get("type", ""))
        if key not in seen:
            seen.add(key)
            unique.append(instr)
    wb["_unique"] = unique

    with _lock:
        _state["workbench"] = wb
        _state["wb_name"]   = name

    return jsonify(wb)


# ── Connection ────────────────────────────────────────────────────────────────
@flask_app.route("/api/connect", methods=["POST"])
def api_connect():
    if not PYVISA_OK:
        return jsonify({"error": "pyvisa not available — demo mode only"}), 503

    wb = _state.get("workbench")
    if not wb:
        return jsonify({"error": "No workbench loaded"}), 400

    def _do():
        rm = pyvisa.ResourceManager("@py")
        raw_results: dict = {}

        for instr in wb.get("_unique", []):
            rstr = instr.get("resource", "")
            if not rstr or rstr in raw_results:
                continue
            try:
                res = rm.open_resource(rstr)
                res.timeout = 8000
                if "SOCKET" in rstr.upper():
                    res.read_termination  = "\n"
                    res.write_termination = "\n"
                idn = res.query("*IDN?").strip()
                raw_results[rstr] = (res, idn, None)
                _log(f"✓  {instr['model']}  →  {idn}")
            except Exception as exc:
                raw_results[rstr] = (None, None, str(exc))
                _log(f"✗  {instr['model']}:  {exc}")

        families = {
            instr["resource"]: _family_for(instr)
            for instr in wb.get("_unique", [])
            if instr.get("resource")
        }

        with _lock:
            for r in _state["resources"].values():
                try: r.close()
                except: pass
            if _state["rm"]:
                try: _state["rm"].close()
                except: pass
            _state["rm"]        = rm
            _state["resources"] = {k: v[0] for k, v in raw_results.items() if v[0]}
            _state["families"]  = {k: v for k, v in families.items() if v}
            _state["connected"] = bool(_state["resources"])

        instruments_out = []
        for instr in wb.get("_unique", []):
            rstr = instr.get("resource", "")
            res, idn, err = raw_results.get(rstr, (None, None, "not attempted"))
            instruments_out.append({
                **instr,
                "ok":    res is not None,
                "idn":   idn,
                "error": err,
            })

        sio.emit("connection_result", {
            "connected":   _state["connected"],
            "instruments": instruments_out,
        })
        if _state["connected"]:
            _start_polling()

    _executor.submit(_do)
    return jsonify({"status": "connecting"})


@flask_app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    _poll_stop.set()
    with _lock:
        for r in _state["resources"].values():
            try: r.close()
            except: pass
        if _state["rm"]:
            try: _state["rm"].close()
            except: pass
        _state.update(rm=None, resources={}, connected=False)
    sio.emit("disconnected", {})
    return jsonify({"status": "disconnected"})


# ── Scope ─────────────────────────────────────────────────────────────────────
def _find_instrument(itype: str):
    """Return (resource, family) for the first instrument of given type."""
    wb = _state.get("workbench")
    if not wb:
        return None, None
    for instr in wb.get("_unique", []):
        if instr.get("type") == itype:
            rstr  = instr.get("resource", "")
            res   = _state["resources"].get(rstr)
            fam   = _state["families"].get(rstr)
            if res:
                return res, fam
    return None, None


@flask_app.route("/api/scope/<cmd>", methods=["POST"])
def api_scope(cmd: str):
    if cmd not in ("run", "stop", "single", "autoscale"):
        return jsonify({"error": "unknown command"}), 400
    _executor.submit(lambda: _op(*_find_instrument("scope"), cmd))
    return jsonify({"status": "ok"})


@flask_app.route("/api/scope/screenshot", methods=["POST"])
def api_screenshot():
    filename = (request.json or {}).get("filename", "screenshot")

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            sio.emit("screenshot_done", {"error": "No scope connected"}); return

        try:
            steps   = get_command(fam, "screenshot")
        except KeyError:
            sio.emit("screenshot_done",
                     {"error": "Screenshot not supported on this scope"}); return

        raw_idx = next((i for i, (a, _) in enumerate(steps) if a == "raw_query"), None)
        if raw_idx is None:
            sio.emit("screenshot_done", {"error": "No data-read step in screenshot command"}); return

        pre  = [(a, s) for a, s in steps[:raw_idx]     if a == "write"]
        cmd_ = steps[raw_idx][1]
        post = [(a, s) for a, s in steps[raw_idx + 1:] if a == "write"]

        orig_timeout  = res.timeout
        res.timeout   = 12_000
        is_usb        = res.resource_name.upper().startswith("USB")
        if is_usb:
            res.chunk_size = 4096

        for _, s in pre: res.write(s)
        time.sleep(1.2)
        res.write(cmd_)

        if is_usb:
            chunks = []
            while True:
                c = res.read_raw(); chunks.append(c)
                if len(c) < 4096: break
            data = b"".join(chunks)
        else:
            data = res.read_raw()

        for _, s in post: res.write(s)
        res.timeout = orig_timeout

        ext = ""
        for magic, e in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
            idx = data.find(magic)
            if idx != -1:
                data = data[idx:]; ext = e; break

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{filename}_{ts}{ext}"
        path = ROOT / name
        path.write_bytes(data)
        _log(f"Screenshot saved: {name}")
        sio.emit("screenshot_done", {"path": str(path), "filename": name})

    _executor.submit(_do)
    return jsonify({"status": "ok"})


# ── PSU ───────────────────────────────────────────────────────────────────────
@flask_app.route("/api/psu/set", methods=["POST"])
def api_psu_set():
    d  = request.json or {}
    ch = int(d.get("ch", 1))
    op_map = {"voltage": ("set_voltage", "value"),
              "current": ("set_current_limit", "value")}
    for key, (op, kw) in op_map.items():
        if key in d:
            val = f"{float(d[key]):.4f}"
            _executor.submit(lambda o=op, v=val: _op(*_find_instrument("psu"), o, ch=ch, **{kw: v}))
    return jsonify({"status": "ok"})


@flask_app.route("/api/psu/output", methods=["POST"])
def api_psu_output():
    d   = request.json or {}
    ch  = int(d.get("ch", 1))
    on  = bool(d.get("state", False))
    _executor.submit(lambda: _op(*_find_instrument("psu"), "output_on" if on else "output_off", ch=ch))
    return jsonify({"status": "ok"})


@flask_app.route("/api/psu/reset", methods=["POST"])
def api_psu_reset():
    _executor.submit(lambda: _op(*_find_instrument("psu"), "reset"))
    return jsonify({"status": "ok"})


# ── AWG ───────────────────────────────────────────────────────────────────────
@flask_app.route("/api/awg/apply", methods=["POST"])
def api_awg_apply():
    d  = request.json or {}
    ch = int(d.get("ch", 1))
    res, fam = _find_instrument("awg")

    def _do():
        ops = [
            ("function",  "set_function",   lambda v: {"func":   str(v)}),
            ("frequency", "set_frequency",  lambda v: {"freq":   f"{float(v):.6g}"}),
            ("amplitude", "set_amplitude",  lambda v: {"amp":    f"{float(v):.4f}"}),
            ("offset",    "set_offset",     lambda v: {"offset": f"{float(v):.4f}"}),
        ]
        for key, scpi_op, kw_fn in ops:
            if key in d:
                _op(res, fam, scpi_op, ch=ch, **kw_fn(d[key]))
        if "amplitude" in d:
            _op(res, fam, "set_amplitude_unit", ch=ch, unit="VPP")

    _executor.submit(_do)
    return jsonify({"status": "ok"})


@flask_app.route("/api/awg/output", methods=["POST"])
def api_awg_output():
    d  = request.json or {}
    ch = int(d.get("ch", 1))
    on = bool(d.get("state", False))
    _executor.submit(lambda: _op(*_find_instrument("awg"), "output_on" if on else "output_off", ch=ch))
    return jsonify({"status": "ok"})


@flask_app.route("/api/awg/reset", methods=["POST"])
def api_awg_reset():
    _executor.submit(lambda: _op(*_find_instrument("awg"), "reset"))
    return jsonify({"status": "ok"})


# ── DMM ───────────────────────────────────────────────────────────────────────
DMM_OPS = {
    "vdc":  "measure_vdc",  "vac":   "measure_vac",
    "idc":  "measure_idc",  "iac":   "measure_iac",
    "r":    "measure_resistance",    "r4w": "measure_fresistance",
    "freq": "measure_frequency",     "cont": "measure_continuity",
    "diode":"measure_diode",         "cap":  "measure_capacitance",
}


@flask_app.route("/api/dmm/measure", methods=["POST"])
def api_dmm_measure():
    mode = (request.json or {}).get("mode", "vdc")
    op   = DMM_OPS.get(mode)
    if not op:
        return jsonify({"error": f"unknown mode {mode!r}"}), 400

    def _do():
        res, fam = _find_instrument("dmm")
        if res is None or fam is None: return
        try:
            val = float(_run_steps(res, get_command(fam, op)) or "nan")
            sio.emit("dmm_reading", {"value": val, "mode": mode})
        except Exception as exc:
            _log(f"[dmm] {exc}")

    _executor.submit(_do)
    return jsonify({"status": "ok"})


# ── Background polling ────────────────────────────────────────────────────────
def _start_polling():
    _poll_stop.clear()

    def _loop():
        while not _poll_stop.is_set() and _state["connected"]:
            wb = _state.get("workbench")
            if wb:
                for instr in wb.get("_unique", []):
                    if instr.get("type") != "psu":
                        continue
                    rstr = instr.get("resource", "")
                    res  = _state["resources"].get(rstr)
                    fam  = _state["families"].get(rstr)
                    if res is None or fam is None:
                        continue
                    for ch in range(1, 5):
                        readings: dict = {}
                        for op, key in [("measure_voltage", "v"),
                                        ("measure_current", "i"),
                                        ("measure_power",   "p")]:
                            try:
                                r = _run_steps(res, get_command(fam, op, ch=ch))
                                if r is not None:
                                    readings[key] = float(r)
                            except Exception:
                                pass
                        if readings:
                            sio.emit("psu_reading", {"ch": ch, **readings})
            _poll_stop.wait(timeout=1.5)

    _executor.submit(_loop)


# ── Scan ─────────────────────────────────────────────────────────────────────
@flask_app.route("/api/scan", methods=["POST"])
def api_scan():
    """Discover VISA instruments on USB + LAN; stream progress via SocketIO."""
    d        = request.json or {}
    usb_only = bool(d.get("usb_only", False))

    def _emit(msg, **kw):
        sio.emit("scan_progress", {"msg": msg, **kw})
        _log(msg)

    def _do():
        try:
            from nachoVisa import (
                open_resource_manager, discover_resources,
                discover_lan_hosts, probe_lan_resources,
                query_identity, parse_idn, connection_type,
                serial_port_metadata,
            )
        except ImportError as exc:
            sio.emit("scan_result", {"error": str(exc), "instruments": [], "errors": []})
            return

        instruments, errors = [], []

        # 1 — open VISA RM
        _emit("Opening VISA resource manager…")
        try:
            rm = open_resource_manager("@py")
        except Exception as exc:
            sio.emit("scan_result", {"error": str(exc), "instruments": [], "errors": []})
            return

        # 2 — USB / standard VISA resources
        _emit("Querying USB & VISA resources…")
        resources, errs = discover_resources(rm)
        errors.extend(errs)
        serial_meta = serial_port_metadata()

        # 3 — LAN scan
        if not usb_only:
            _emit("Scanning LAN for instruments (this may take ~10 s)…")
            lan_hosts, lan_notes = discover_lan_hosts(
                hosts=[], subnets=[], timeout=0.35,
                max_hosts=256, workers=64,
            )
            errors.extend(lan_notes)
            if lan_hosts:
                _emit(f"Probing {len(lan_hosts)} LAN host(s)…")
                lan_res, lan_errs = probe_lan_resources(rm, lan_hosts)
                resources = sorted(set(resources) | set(lan_res))
                errors.extend(lan_errs)

        # filter to USB/TCPIP only
        resources = [r for r in resources if r.upper().startswith(("TCPIP", "USB"))]
        if usb_only:
            resources = [r for r in resources if r.upper().startswith("USB")]

        # 4 — identify each resource
        _emit(f"Identifying {len(resources)} resource(s)…")
        for rstr in resources:
            inst = None
            try:
                inst = rm.open_resource(rstr)
                idn  = query_identity(inst, rstr)
                mfr, model, serial, fw = parse_idn(idn)
                family = _classify(idn) if HELPERS_OK else None
                _TYPE_ROLE = {"awg": "generator"}
                entry = {
                    "resource":     rstr,
                    "connection":   connection_type(rstr),
                    "manufacturer": mfr,
                    "model":        model,
                    "serial":       serial,
                    "firmware":     fw,
                    "idn":          idn,
                    "type":         family["type"] if family else None,
                    "role":         _TYPE_ROLE.get(family["type"], family["type"]) if family else None,
                    "family_id":    family["id"]   if family else None,
                }
                instruments.append(entry)
                _emit(f"✓  {model}  ({connection_type(rstr)})",
                      instrument=entry)
            except Exception as exc:
                errors.append(f"{rstr}: {exc}")
                _emit(f"⚠  {rstr}: {exc}")
            finally:
                if inst is not None:
                    try: inst.close()
                    except: pass

        try: rm.close()
        except: pass

        sio.emit("scan_result", {
            "instruments": instruments,
            "errors":      [e for e in errors if e],
        })

    _executor.submit(_do)
    return jsonify({"status": "scanning"})


@flask_app.route("/api/scan/save", methods=["POST"])
def api_scan_save():
    d           = request.json or {}
    name        = (d.get("name") or "").strip()
    instruments = d.get("instruments") or []

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not instruments:
        return jsonify({"error": "no instruments to save"}), 400

    try:
        from nachoVisa import save_workbench, _write_active_unsaved
        path = save_workbench(name, instruments)
        _write_active_unsaved(instruments)       # also set as active
        _log(f"✓ Workbench saved: {name}")
        return jsonify({"status": "saved", "name": name, "path": path})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Graceful shutdown ─────────────────────────────────────────────────────────
def _cleanup():
    """Close all open VISA connections. Safe to call more than once."""
    _poll_stop.set()
    with _lock:
        resources = list(_state.get("resources", {}).values())
        rm        = _state.get("rm")
        _state["resources"]  = {}
        _state["rm"]         = None
        _state["connected"]  = False
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


# ── Static files ──────────────────────────────────────────────────────────────
@flask_app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")

@flask_app.route("/ui/<path:p>")
def ui_static(p):
    """Serve any file from the ui/ directory (e.g. socket.io.min.js)."""
    return send_from_directory(UI_DIR, p)

@flask_app.route("/assets/<path:p>")
def gui_assets(p):
    return send_from_directory(ROOT / "gui_assets", p)

@flask_app.route("/favicon.ico")
@flask_app.route("/favicon.png")
def favicon():
    return send_from_directory(ROOT / "gui_assets", "favicon-32.png",
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
    _log("[system] UI connected")


# ── Entry point ───────────────────────────────────────────────────────────────
def _run_server(port: int):
    sio.run(flask_app, host="127.0.0.1", port=port,
            debug=False, use_reloader=False, log_output=False,
            allow_unsafe_werkzeug=True)


import shutil
import subprocess

# Chromium-family browsers that support --app= standalone-window mode
_CHROME_CANDIDATES = [
    "google-chrome-stable", "google-chrome",
    "chromium-browser", "chromium",
    "brave-browser", "brave",
    "microsoft-edge-stable", "microsoft-edge",
]


def _find_chrome() -> str | None:
    """Return the first available Chromium-family browser binary, or None."""
    for name in _CHROME_CANDIDATES:
        p = shutil.which(name)
        if p:
            return p
    return None


def _open_chrome_app(url: str, width: int = 1300, height: int = 840) -> subprocess.Popen:
    """Open url in Chromium app-mode (no address bar / tabs)."""
    chrome = _find_chrome()
    if not chrome:
        raise FileNotFoundError("No Chromium-family browser found")
    return subprocess.Popen([
        chrome,
        f"--app={url}",
        f"--window-size={width},{height}",
        # Set WM_CLASS so GNOME Shell matches this window to our .desktop file
        # and shows the nacho icon in the taskbar instead of the Chrome icon.
        "--class=open-EE-workbench",
        "--no-default-browser-check",
        "--no-first-run",
        "--disable-extensions",
    ])


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


def main():
    ap = argparse.ArgumentParser(description="open-EE-workbench web GUI")
    ap.add_argument("workbench", nargs="?", default=None)
    ap.add_argument("--browser",  action="store_true",
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
        print(f"[app] opened in Chrome app-mode (pid {proc.pid})")
        proc.wait()   # block until user closes the window
        _cleanup()
        return
    except FileNotFoundError:
        pass

    # 2. PyQt5 + QtWebKit: pure Qt, no Chromium sandbox issues
    try:
        from PyQt5.QtWebKitWidgets import QWebView  # noqa: F401 — check availability
        print("[app] opening with PyQt5 + QtWebKit")
        _open_qtwebkit(url)   # calls sys.exit() → atexit fires _cleanup
        return
    except ImportError:
        pass

    # 3. Last resort: system browser
    import webbrowser
    print(f"[app] no standalone window available — opening browser: {url}")
    webbrowser.open(url)
    try:
        t.join()
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
