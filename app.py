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


def _scope_enable_measures(scope_res, scope_fam, op_ch_pairs: list):
    """Enable Rigol-style measurement display items **once** before a measurement loop.

    Procedure:
      1. Clear all currently-shown items via 'measure_clear_all' (Rigol only;
         other families raise KeyError which is silently swallowed).
      2. Send only the *write* step of each measurement command to activate
         each item.  For Keysight-style scopes there are no write steps so
         both phases are effectively no-ops.

    op_ch_pairs: list of (operation_name, ch_int) tuples, e.g.
        [("measure_vpp", 1), ("measure_freq", 1), ...]
    """
    # Phase 1 — clear existing displayed items
    try:
        _run_steps(scope_res, get_command(scope_fam, "measure_clear_all"))
        time.sleep(0.1)
    except KeyError:
        pass          # command not in this family — fine
    except Exception as exc:
        _log(f"[scope] measure_clear_all: {exc}")

    # Phase 2 — enable each item (write step only)
    for op, ch in op_ch_pairs:
        try:
            steps = get_command(scope_fam, op, ch=ch)
            write_only = [(a, s) for a, s in steps if a == "write"]
            if write_only:
                _run_steps(scope_res, write_only)
        except KeyError:
            pass
        except Exception as exc:
            _log(f"[scope] enable {op} CH{ch}: {exc}")


def _scope_query_only(scope_res, scope_fam, op: str, ch: int):
    """Query a single scope measurement *without* re-sending its enable write.

    Call _scope_enable_measures() first, then use this inside the loop.
    Returns the float value, or None if unavailable / out-of-range.
    """
    try:
        steps = get_command(scope_fam, op, ch=ch)
        query_steps = [(a, s) for a, s in steps if a in ("query", "raw_query")]
        if not query_steps:
            return None
        raw = _run_steps(scope_res, query_steps)
        if raw is None:
            return None
        v = float(raw)
        return None if abs(v) > 1e30 else v
    except Exception:
        return None


def _write_only(scope_res, scope_fam, op: str, **kw):
    """Send only the write step(s) for an operation.
    Silent KeyError means the op is not supported on this scope family.
    """
    try:
        for action, scpi in get_command(scope_fam, op, **kw):
            if action == "write":
                scope_res.write(scpi)
    except KeyError:
        pass


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
                elif rstr.upper().startswith("ASRL"):
                    # USB-serial instrument (e.g. Keithley 2231A via Prolific)
                    res.baud_rate         = 9600
                    res.data_bits         = 8
                    res.stop_bits         = pyvisa.constants.StopBits.one
                    res.parity            = pyvisa.constants.Parity.none
                    res.read_termination  = "\n"
                    res.write_termination = "\n"
                idn = res.query("*IDN?").strip()
                raw_results[rstr] = (res, idn, None)
                _log(f"✓  {instr['model']}  →  {idn}")
                # Put serial instruments into remote-control mode right away
                if rstr.upper().startswith("ASRL") and HELPERS_OK:
                    fam = _family_for(instr)
                    if fam:
                        try:
                            for act, scpi in get_command(fam, "remote_mode"):
                                if act == "write":
                                    res.write(scpi)
                            _log(f"  → remote mode active")
                        except KeyError:
                            pass
                        except Exception as exc2:
                            _log(f"  ⚠ remote_mode: {exc2}")
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
        for rstr, r in _state["resources"].items():
            # Return serial instruments to local (front-panel) control
            if rstr.upper().startswith("ASRL") and HELPERS_OK:
                fam = _state["families"].get(rstr)
                if fam:
                    try:
                        for act, scpi in get_command(fam, "local_mode"):
                            if act == "write":
                                r.write(scpi)
                    except Exception:
                        pass
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


_SCOPE_MEASURES = {
    "measure_vpp":       ("Vpp",  "V p-p"),
    "measure_freq":      ("Freq", "Hz"),
    "measure_period":    ("Per",  "s"),
    "measure_vavg":      ("Vavg", "V"),
    "measure_vrms":      ("Vrms", "V"),
    "measure_vmax":      ("Vmax", "V"),
    "measure_vmin":      ("Vmin", "V"),
    "measure_risetime":  ("Rise", "s"),
    "measure_falltime":  ("Fall", "s"),
    "measure_dutycycle": ("Duty", "%"),
}


@flask_app.route("/api/scope/<cmd>", methods=["POST"])
def api_scope(cmd: str):
    if cmd not in ("run", "stop", "single", "autoscale"):
        return jsonify({"error": "unknown command"}), 400
    _executor.submit(lambda: _op(*_find_instrument("scope"), cmd))
    return jsonify({"status": "ok"})


@flask_app.route("/api/scope/measure", methods=["POST"])
def api_scope_measure():
    d    = request.json or {}
    meas = d.get("measurement", "measure_vpp")
    ch   = int(d.get("ch", 1))

    if meas not in _SCOPE_MEASURES:
        return jsonify({"error": f"unknown measurement {meas!r}"}), 400

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            sio.emit("scope_measurement", {"error": "No scope connected"}); return
        try:
            raw = _run_steps(res, get_command(fam, meas, ch=ch))
            val = float(raw) if raw is not None else None
            label, unit = _SCOPE_MEASURES[meas]
            sio.emit("scope_measurement",
                     {"measurement": meas, "label": label, "unit": unit,
                      "ch": ch, "value": val})
        except KeyError:
            sio.emit("scope_measurement",
                     {"error": f"{meas!r} not supported on this scope"})
        except Exception as exc:
            sio.emit("scope_measurement", {"error": str(exc)})

    _executor.submit(_do)
    return jsonify({"status": "ok"})


@flask_app.route("/api/scope/screenshot", methods=["POST"])
def api_screenshot():
    filename = (request.json or {}).get("filename", "screenshot")

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            sio.emit("screenshot_done", {"error": "No scope connected"}); return

        try:
            steps = get_command(fam, "screenshot")
        except KeyError:
            sio.emit("screenshot_done",
                     {"error": "Screenshot not supported on this scope"}); return

        raw_idx = next((i for i, (a, _) in enumerate(steps) if a == "raw_query"), None)
        if raw_idx is None:
            sio.emit("screenshot_done",
                     {"error": "No data-read step in screenshot command"}); return

        pre  = [(a, s) for a, s in steps[:raw_idx]     if a == "write"]
        cmd_ = steps[raw_idx][1]
        post = [(a, s) for a, s in steps[raw_idx + 1:] if a == "write"]

        orig_timeout = res.timeout
        try:
            res.timeout = 20_000    # screenshots can take several seconds

            for _, s in pre:
                res.write(s)
            time.sleep(1.0)         # let scope compose the image before we ask

            res.write(cmd_)
            # pyvisa's read_raw() handles USBTMC end-of-transfer natively;
            # manual chunking is unreliable (hangs when data is a multiple of chunk_size).
            data = res.read_raw()

            for _, s in post:
                try: res.write(s)
                except Exception: pass

        except Exception as exc:
            _log(f"✗ Screenshot I/O error: {exc}")
            sio.emit("screenshot_done", {"error": str(exc)})
            return
        finally:
            try: res.timeout = orig_timeout
            except Exception: pass

        if not data:
            sio.emit("screenshot_done", {"error": "Scope returned empty data"}); return

        # Strip any leading SCPI header before the image magic bytes
        ext = ""
        for magic, e in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
            idx = data.find(magic)
            if idx != -1:
                data = data[idx:]; ext = e; break

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{filename}_{ts}{ext or '.bin'}"
        path = ROOT / name
        try:
            path.write_bytes(data)
        except Exception as exc:
            sio.emit("screenshot_done", {"error": f"Could not save file: {exc}"}); return

        _log(f"✓ Screenshot saved: {name}  ({len(data)} bytes)")
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
                serial_port_metadata, discover_serial_resources,
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

        # 2 — USB / standard VISA resources + USB-serial ports
        _emit("Querying USB & VISA resources…")
        resources, errs = discover_resources(rm)   # already includes serial via new helper
        errors.extend(errs)

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

        # Filter: keep USB-VISA, Ethernet, and USB-serial (ASRL from USB adapter)
        def _keep_resource(r: str) -> bool:
            ru = r.upper()
            if ru.startswith(("TCPIP", "USB")):
                return True
            if ru.startswith("ASRL"):
                dev = r[4:].replace("::INSTR", "").replace("::instr", "")
                return os.path.basename(dev).lower().startswith(
                    ("ttyusb", "ttyacm", "cu.usb", "tty.usb", "com")
                )
            return False

        resources = [r for r in resources if _keep_resource(r)]
        if usb_only:
            resources = [r for r in resources if not r.upper().startswith("ASRL")]

        # 4 — identify each resource
        _emit(f"Identifying {len(resources)} resource(s)…")
        for rstr in resources:
            inst = None
            try:
                inst = rm.open_resource(rstr)
                if rstr.upper().startswith("ASRL"):
                    inst.baud_rate         = 9600
                    inst.data_bits         = 8
                    inst.stop_bits         = pyvisa.constants.StopBits.one
                    inst.parity            = pyvisa.constants.Parity.none
                    inst.read_termination  = "\n"
                    inst.write_termination = "\n"
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


# ── Automation ────────────────────────────────────────────────────────────────
_auto_stop    = threading.Event()
_auto_running = False


def _suggest_tests() -> list:
    """Return tests available for the current workbench based on instrument types present."""
    wb = _state.get("workbench")
    if not wb:
        return []
    types = {instr.get("type") for instr in wb.get("_unique", [])}
    tests = []

    if "awg" in types and "scope" in types:
        tests.append({
            "id":          "ac_frequency_sweep",
            "name":        "AC Frequency Sweep",
            "description": "Sweep AWG frequency, measure Vpp on scope CH1 and CH2",
            "requires":    ["awg", "scope"],
            "params": [
                {"id": "freq_start",  "label": "Start freq",  "unit": "Hz",  "default": 100,    "type": "number"},
                {"id": "freq_stop",   "label": "Stop freq",   "unit": "Hz",  "default": 100000, "type": "number"},
                {"id": "num_points",  "label": "Points",      "unit": "",    "default": 20,     "type": "number"},
                {"id": "amplitude",   "label": "Amplitude",   "unit": "Vpp", "default": 1.0,    "type": "number"},
                {"id": "settle_time", "label": "Settle",      "unit": "s",   "default": 0.2,    "type": "number"},
            ],
            "columns": ["set_freq_Hz", "meas_freq_Hz", "vpp_ch1_V", "vpp_ch2_V"],
        })

    if "psu" in types:
        all_instr  = wb.get("_unique", [])
        psu_list   = [i for i in all_instr if i.get("type") == "psu"]
        dmm_list   = [i for i in all_instr if i.get("type") == "dmm"]

        # Source options: one entry per PSU in the workbench
        source_opts = [
            {"value": i["resource"], "label": i.get("model", "PSU")}
            for i in psu_list if i.get("resource")
        ]
        # Current-meter options: PSU built-in, then any DMMs
        i_meter_opts = [{"value": "psu", "label": "PSU (built-in)"}] + [
            {"value": i["resource"], "label": f"DMM · {i.get('model', 'DMM')}"}
            for i in dmm_list if i.get("resource")
        ]

        tests.append({
            "id":          "dc_sweep",
            "name":        "DC Sweep",
            "description": "Sweep PSU voltage(s) and measure current — single, simultaneous, or nested",
            "requires":    ["psu"],
            "params": [
                # ── Instrument selectors ──────────────────────────────────────────
                {"id": "source",      "label": "V source",  "type": "select",
                 "options": source_opts,
                 "default": source_opts[0]["value"] if source_opts else ""},
                # ── Sweep mode ────────────────────────────────────────────────────
                {"id": "sweep_mode",  "label": "Mode",      "type": "select",
                 "options": [
                     {"value": "single",       "label": "Single channel"},
                     {"value": "simultaneous", "label": "Simultaneous (same range)"},
                     {"value": "nested",       "label": "Nested (CH1 inner, CH2 outer)"},
                 ], "default": "single"},
                # ── CH1 (primary / inner) ─────────────────────────────────────────
                {"id": "source_ch",   "label": "CH1",       "type": "select",
                 "options": ["1", "2", "3", "4"], "default": "1"},
                {"id": "v_start",     "label": "V1 start",  "unit": "V",  "default": 0,   "type": "number"},
                {"id": "v_stop",      "label": "V1 stop",   "unit": "V",  "default": 5,   "type": "number"},
                {"id": "v_step",      "label": "V1 step",   "unit": "V",  "default": 0.1, "type": "number"},
                {"id": "i_limit",     "label": "I1 limit",  "unit": "A",  "default": 0.5, "type": "number"},
                # ── CH2 (simultaneous partner / outer for nested) ─────────────────
                {"id": "source_ch2",  "label": "CH2",       "type": "select",
                 "options": ["none", "1", "2", "3", "4"], "default": "none"},
                {"id": "v2_start",    "label": "V2 start",  "unit": "V",  "default": 0,   "type": "number"},
                {"id": "v2_stop",     "label": "V2 stop",   "unit": "V",  "default": 3,   "type": "number"},
                {"id": "v2_step",     "label": "V2 step",   "unit": "V",  "default": 1,   "type": "number"},
                {"id": "i2_limit",    "label": "I2 limit",  "unit": "A",  "default": 0.5, "type": "number"},
                # ── Measurement & timing ──────────────────────────────────────────
                {"id": "i_meter",     "label": "I meter",   "type": "select",
                 "options": i_meter_opts, "default": "psu"},
                {"id": "settle_time", "label": "Settle",    "unit": "s",  "default": 0.1, "type": "number"},
            ],
            # Columns are overridden at runtime by the runner based on sweep_mode;
            # this placeholder is used only for the initial table header.
            "columns": ["v_set_V", "v_meas_V", "i_meas_A"],
        })

    if "dmm" in types:
        tests.append({
            "id":          "dmm_logger",
            "name":        "DMM Logger",
            "description": "Log DMM measurements at a fixed interval",
            "requires":    ["dmm"],
            "params": [
                {"id": "mode",        "label": "Mode",        "unit": "",    "default": "vdc", "type": "select",
                 "options": ["vdc", "vac", "idc", "iac", "r", "r4w", "freq", "cap"]},
                {"id": "interval",    "label": "Interval",    "unit": "s",   "default": 1.0,   "type": "number"},
                {"id": "num_samples", "label": "Samples",     "unit": "",    "default": 60,    "type": "number"},
            ],
            "columns": ["elapsed_s", "value"],
        })

    if "scope" in types:
        tests.append({
            "id":          "waveform_analysis",
            "name":        "Waveform Analysis",
            "description": "Log all measurements from a scope channel over time",
            "requires":    ["scope"],
            "params": [
                {"id": "ch",          "label": "Channel",   "unit": "",   "default": "1",   "type": "select",
                 "options": ["1", "2", "3", "4"]},
                {"id": "autoscale",   "label": "Autoscale", "unit": "",   "default": "yes", "type": "select",
                 "options": ["yes", "no"]},
                {"id": "num_samples", "label": "Samples",   "unit": "",   "default": 10,    "type": "number"},
                {"id": "interval",    "label": "Interval",  "unit": "s",  "default": 0.5,   "type": "number"},
            ],
            "columns": ["phase", "sample", "freq_Hz", "vpp_V", "vrms_V",
                        "duty_pct", "rise_s", "fall_s", "overshoot_pct"],
        })
        tests.append({
            "id":          "harmonic_analysis",
            "name":        "Harmonic Analysis",
            "description": "Crest factor, THD estimate, and optional CH1→CH2 gain",
            "requires":    ["scope"],
            "params": [
                {"id": "ch_signal",   "label": "Signal CH", "unit": "",   "default": "1",    "type": "select",
                 "options": ["1", "2", "3", "4"]},
                {"id": "ch_ref",      "label": "Ref CH",    "unit": "",   "default": "none", "type": "select",
                 "options": ["none", "1", "2", "3", "4"]},
                {"id": "autoscale",   "label": "Autoscale", "unit": "",   "default": "yes",  "type": "select",
                 "options": ["yes", "no"]},
                {"id": "num_samples", "label": "Samples",   "unit": "",   "default": 20,     "type": "number"},
                {"id": "interval",    "label": "Interval",  "unit": "s",  "default": 0.5,    "type": "number"},
            ],
            "columns": ["sample", "freq_Hz", "vpp_V", "vrms_V", "crest_factor", "thd_est_pct",
                        "ref_vpp_V", "gain_dB"],
        })

    return tests


@flask_app.route("/api/automation/tests")
def api_automation_tests():
    return jsonify({"tests": _suggest_tests()})


@flask_app.route("/api/automation/stop", methods=["POST"])
def api_automation_stop():
    global _auto_running
    _auto_stop.set()
    _auto_running = False
    return jsonify({"status": "stopping"})


@flask_app.route("/api/automation/run", methods=["POST"])
def api_automation_run():
    global _auto_running
    if _auto_running:
        return jsonify({"error": "A test is already running"}), 409

    d       = request.json or {}
    test_id = d.get("test_id")
    params  = d.get("params", {})

    if not test_id:
        return jsonify({"error": "test_id required"}), 400
    if not _state.get("connected"):
        return jsonify({"error": "Not connected to instruments"}), 400

    _auto_stop.clear()
    _auto_running = True

    def _emit_progress(msg: str):
        sio.emit("automation_progress", {"test_id": test_id, "msg": msg})
        _log(f"[auto] {msg}")

    def _done(rows, columns, error=None):
        global _auto_running
        _auto_running = False
        sio.emit("automation_done", {"test_id": test_id, "columns": columns,
                                     "rows": rows, "error": error})

    def _run_ac_sweep():
        freq_start  = float(params.get("freq_start",  100))
        freq_stop   = float(params.get("freq_stop",   100000))
        n_pts       = max(2, int(params.get("num_points", 20)))
        amplitude   = float(params.get("amplitude",   1.0))
        settle_time = float(params.get("settle_time", 0.2))

        awg_res,   awg_fam   = _find_instrument("awg")
        scope_res, scope_fam = _find_instrument("scope")
        if awg_res is None or scope_res is None:
            _done([], [], "AWG or scope not connected"); return

        cols = ["set_freq_Hz", "meas_freq_Hz", "vpp_ch1_V", "vpp_ch2_V"]
        rows = []

        # logarithmically spaced frequencies
        import math
        freqs = [freq_start * (freq_stop / freq_start) ** (i / (n_pts - 1)) for i in range(n_pts)]

        # Configure AWG: sine wave, fixed amplitude
        try:
            _run_steps(awg_res, get_command(awg_fam, "set_function",       ch=1, func="SIN"))
            _run_steps(awg_res, get_command(awg_fam, "set_amplitude",      ch=1, amp=f"{amplitude:.4f}"))
            _run_steps(awg_res, get_command(awg_fam, "set_amplitude_unit", ch=1, unit="VPP"))
            _run_steps(awg_res, get_command(awg_fam, "output_on",          ch=1))
        except Exception as exc:
            _done([], cols, f"AWG setup failed: {exc}"); return

        _emit_progress(f"Sweep started: {n_pts} points, {freq_start:.0f}–{freq_stop:.0f} Hz, {amplitude} Vpp")

        for i, freq in enumerate(freqs):
            if _auto_stop.is_set():
                _emit_progress("Stopped by user"); break
            try:
                _run_steps(awg_res, get_command(awg_fam, "set_frequency", ch=1, freq=f"{freq:.6g}"))
                time.sleep(settle_time)
                raw_vpp1 = _run_steps(scope_res, get_command(scope_fam, "measure_vpp",  ch=1))
                raw_vpp2 = _run_steps(scope_res, get_command(scope_fam, "measure_vpp",  ch=2))
                raw_freq = _run_steps(scope_res, get_command(scope_fam, "measure_freq", ch=1))

                def _safe(r):
                    try:
                        v = float(r)
                        return None if abs(v) > 1e30 else round(v, 6)
                    except Exception:
                        return None

                row = [round(freq, 3), _safe(raw_freq), _safe(raw_vpp1), _safe(raw_vpp2)]
                rows.append(row)
                sio.emit("automation_row", {"test_id": test_id, "row": row, "columns": cols,
                                            "progress": (i + 1) / n_pts})
            except Exception as exc:
                _log(f"[auto] step {i+1} error: {exc}")

        try: _run_steps(awg_res, get_command(awg_fam, "output_off", ch=1))
        except Exception: pass
        _done(rows, cols)

    def _run_dc_sweep():
        source_rstr = str(params.get("source",      ""))
        sweep_mode  = str(params.get("sweep_mode",  "single"))
        ch1         = int(params.get("source_ch",   1))
        ch2_raw     = str(params.get("source_ch2",  "none"))
        ch2         = None if ch2_raw in ("none", "") else int(ch2_raw)
        v_start     = float(params.get("v_start",   0))
        v_stop      = float(params.get("v_stop",    5))
        v_step      = abs(float(params.get("v_step", 0.1))) or 0.1
        i_limit     = float(params.get("i_limit",   0.5))
        v2_start    = float(params.get("v2_start",  0))
        v2_stop     = float(params.get("v2_stop",   3))
        v2_step     = abs(float(params.get("v2_step", 1.0))) or 1.0
        i2_limit    = float(params.get("i2_limit",  0.5))
        i_meter_id  = str(params.get("i_meter",     "psu"))
        settle_time = float(params.get("settle_time", 0.1))

        # single if ch2 not chosen regardless of mode selector
        if ch2 is None:
            sweep_mode = "single"

        # ── Resolve voltage-source instrument ────────────────────────────────
        psu_res = _state["resources"].get(source_rstr)
        psu_fam = _state["families"].get(source_rstr)
        if psu_res is None:
            psu_res, psu_fam = _find_instrument("psu")   # fallback
        if psu_res is None:
            _done([], [], "Voltage source not connected"); return

        # ── Resolve current-meter ────────────────────────────────────────────
        use_dmm = (i_meter_id not in ("psu", ""))
        if use_dmm:
            imtr_res = _state["resources"].get(i_meter_id)
            imtr_fam = _state["families"].get(i_meter_id)
            if imtr_res is None:
                _done([], [], f"Current meter not connected ({i_meter_id})"); return
        else:
            imtr_res, imtr_fam = psu_res, psu_fam

        def _safe(r):
            try:
                v = float(r)
                return None if abs(v) > 1e30 else round(v, 6)
            except Exception:
                return None

        def _meas_v(ch):
            try:
                return _safe(_run_steps(psu_res, get_command(psu_fam, "measure_voltage", ch=ch)))
            except Exception:
                return None

        def _meas_i(ch):
            try:
                if use_dmm:
                    return _safe(_run_steps(imtr_res, get_command(imtr_fam, "measure_idc")))
                return _safe(_run_steps(imtr_res, get_command(imtr_fam, "measure_current", ch=ch)))
            except Exception:
                return None

        def _set_v(ch, v):
            _run_steps(psu_res, get_command(psu_fam, "set_voltage", ch=ch, value=f"{v:.6f}"))

        def _emit_row(row, cols, progress):
            rows.append(row)
            sio.emit("automation_row", {"test_id": test_id, "row": row,
                                        "columns": cols, "progress": progress})

        def _setup_ch(ch, ilim):
            _run_steps(psu_res, get_command(psu_fam, "set_current_limit",
                                            ch=ch, value=f"{ilim:.4f}"))
            _run_steps(psu_res, get_command(psu_fam, "output_on", ch=ch))

        def _teardown_ch(ch):
            try:
                _run_steps(psu_res, get_command(psu_fam, "set_voltage", ch=ch, value="0.0"))
                _run_steps(psu_res, get_command(psu_fam, "output_off", ch=ch))
            except Exception:
                pass

        rows = []

        # ── Build inner voltage array (CH1 / only channel) ──────────────────
        n1 = round(abs(v_stop - v_start) / v_step)
        s1 = 1 if v_stop >= v_start else -1
        vols1 = [round(v_start + k * s1 * v_step, 10) for k in range(n1 + 1)]

        # ════════════════════════════════════════════════════════════════════
        # MODE: SINGLE
        # ════════════════════════════════════════════════════════════════════
        if sweep_mode == "single":
            cols = ["v_set_V", "v_meas_V", "i_meas_A"]
            _emit_progress(f"DC Sweep — single CH{ch1}: {len(vols1)} pts")
            try:
                _setup_ch(ch1, i_limit)
            except Exception as exc:
                _done([], cols, f"PSU setup failed: {exc}"); return

            for k, v in enumerate(vols1):
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break
                try:
                    _set_v(ch1, v)
                    time.sleep(settle_time)
                    _emit_row([round(v, 6), _meas_v(ch1), _meas_i(ch1)],
                              cols, (k + 1) / len(vols1))
                except Exception as exc:
                    _log(f"[auto] step {k+1}: {exc}")

            _teardown_ch(ch1)

        # ════════════════════════════════════════════════════════════════════
        # MODE: SIMULTANEOUS — both channels ramp together with the same range
        # ════════════════════════════════════════════════════════════════════
        elif sweep_mode == "simultaneous":
            cols = ["v_set_V",
                    f"v_meas_ch{ch1}_V", f"i_meas_ch{ch1}_A",
                    f"v_meas_ch{ch2}_V", f"i_meas_ch{ch2}_A"]
            _emit_progress(f"DC Sweep — simultaneous CH{ch1}+CH{ch2}: {len(vols1)} pts")
            try:
                _setup_ch(ch1, i_limit)
                _setup_ch(ch2, i2_limit)
            except Exception as exc:
                _done([], cols, f"PSU setup failed: {exc}"); return

            for k, v in enumerate(vols1):
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break
                try:
                    _set_v(ch1, v)
                    _set_v(ch2, v)
                    time.sleep(settle_time)
                    _emit_row([round(v, 6),
                               _meas_v(ch1), _meas_i(ch1),
                               _meas_v(ch2), _meas_i(ch2)],
                              cols, (k + 1) / len(vols1))
                except Exception as exc:
                    _log(f"[auto] step {k+1}: {exc}")

            _teardown_ch(ch1)
            _teardown_ch(ch2)

        # ════════════════════════════════════════════════════════════════════
        # MODE: NESTED — CH1 (inner) sweeps full range per step of CH2 (outer)
        # e.g. family of IV curves: CH2 = Vgs bias, CH1 = Vds sweep
        # ════════════════════════════════════════════════════════════════════
        elif sweep_mode == "nested":
            n2 = round(abs(v2_stop - v2_start) / v2_step)
            s2 = 1 if v2_stop >= v2_start else -1
            vols2 = [round(v2_start + k * s2 * v2_step, 10) for k in range(n2 + 1)]
            total = len(vols2) * len(vols1)

            cols = [f"v_ch{ch2}_set_V",    f"v_ch{ch1}_set_V",
                    f"v_ch{ch2}_meas_V",   f"v_ch{ch1}_meas_V",
                    f"i_ch{ch1}_meas_A"]
            _emit_progress(
                f"DC Sweep — nested CH{ch2}(outer)×CH{ch1}(inner): "
                f"{len(vols2)}×{len(vols1)} = {total} pts"
            )
            try:
                _setup_ch(ch2, i2_limit)   # outer first so it's stable
                _setup_ch(ch1, i_limit)
            except Exception as exc:
                _done([], cols, f"PSU setup failed: {exc}"); return

            step_num = 0
            for j, v2 in enumerate(vols2):
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break
                _set_v(ch2, v2)
                _emit_progress(f"  CH{ch2} = {v2:.4g} V — sweeping CH{ch1}…")
                time.sleep(settle_time)   # outer settle before inner sweep

                for k, v1 in enumerate(vols1):
                    if _auto_stop.is_set():
                        break
                    try:
                        _set_v(ch1, v1)
                        time.sleep(settle_time)
                        step_num += 1
                        _emit_row([round(v2, 6), round(v1, 6),
                                   _meas_v(ch2), _meas_v(ch1), _meas_i(ch1)],
                                  cols, step_num / total)
                    except Exception as exc:
                        _log(f"[auto] outer {j+1} inner {k+1}: {exc}")

            _teardown_ch(ch1)
            _teardown_ch(ch2)

        else:
            _done([], [], f"Unknown sweep_mode {sweep_mode!r}"); return

        _done(rows, cols)

    def _run_dmm_logger():
        mode        = str(params.get("mode",       "vdc"))
        interval    = float(params.get("interval",  1.0))
        num_samples = int(params.get("num_samples", 60))

        dmm_res, dmm_fam = _find_instrument("dmm")
        if dmm_res is None:
            _done([], [], "DMM not connected"); return

        op = DMM_OPS.get(mode)
        if not op:
            _done([], [], f"Unknown mode {mode!r}"); return

        cols = ["elapsed_s", "value"]
        rows = []
        _emit_progress(f"DMM logger: {num_samples} × {mode} at {interval}s interval")

        t0 = time.time()
        for i in range(num_samples):
            if _auto_stop.is_set():
                _emit_progress("Stopped by user"); break
            try:
                raw = _run_steps(dmm_res, get_command(dmm_fam, op))
                val = round(float(raw), 8) if raw is not None else None
                elapsed = round(time.time() - t0, 3)
                row = [elapsed, val]
                rows.append(row)
                sio.emit("automation_row", {"test_id": test_id, "row": row, "columns": cols,
                                            "progress": (i + 1) / num_samples})
            except Exception as exc:
                _log(f"[auto] sample {i+1} error: {exc}")
            _auto_stop.wait(timeout=interval)

        _done(rows, cols)

    def _scope_autoscale_and_rescale(scope_res, scope_fam, ch: int):
        """Scope setup:
        0. Clear measurement items so re-runs are clean
        1. Ensure scope is running (RUN)
        2. :AUToscale — let scope find initial ballpark (wait 4 s)
        3. Vertical rescale — Vpp / 6  V/div, offset 0
        4. Trigger — EDGE mode, selected channel, rising, level 0 V
        5. Timebase — ~3 cycles across 10 divisions
        """
        # ── 0. Clear displayed measurements ────────────────────────────
        _scope_enable_measures(scope_res, scope_fam, [])

        # ── 1. Make sure scope is running ───────────────────────────────
        try:
            _write_only(scope_res, scope_fam, "run")
            _auto_stop.wait(timeout=0.3)
        except Exception as exc:
            _emit_progress(f"  ⚠ run: {exc}")

        # ── 2. Autoscale ─────────────────────────────────────────────────
        try:
            _write_only(scope_res, scope_fam, "autoscale")
            _emit_progress("  Autoscaling… (waiting 4 s)")
            _auto_stop.wait(timeout=4.0)
            _emit_progress("  Autoscale done")
        except Exception as exc:
            _emit_progress(f"  ⚠ autoscale: {exc}")

        # ── 3. Vertical rescale ───────────────────────────────────────────
        try:
            raw = _run_steps(scope_res, get_command(scope_fam, "measure_vpp", ch=ch))
            vpp = float(raw) if raw is not None else None
            if vpp and 0 < abs(vpp) < 1e30:
                vscale = abs(vpp) / 6.0
                _write_only(scope_res, scope_fam, "channel_scale",  ch=ch, value=f"{vscale:.4e}")
                _write_only(scope_res, scope_fam, "channel_offset", ch=ch, value="0")
                _emit_progress(f"  CH{ch} scale → {vscale:.3e} V/div  (Vpp = {vpp:.3g} V)")
            else:
                _emit_progress(f"  ⚠ Vpp unreadable ({raw!r}) — vertical rescale skipped")
        except Exception as exc:
            _emit_progress(f"  ⚠ vertical rescale: {exc}")

        # ── 4. Trigger ───────────────────────────────────────────────────
        try:
            _write_only(scope_res, scope_fam, "trigger_mode")          # Keysight only; Rigol: no-op
            _write_only(scope_res, scope_fam, "trigger_source", ch=ch)
            _write_only(scope_res, scope_fam, "trigger_slope",  slope="POSitive")
            _write_only(scope_res, scope_fam, "trigger_level",  value="0")
            _emit_progress(f"  Trigger → CH{ch} rising edge at 0 V")
        except Exception as exc:
            _emit_progress(f"  ⚠ trigger: {exc}")

        # ── 5. Timebase: 3 cycles across 10 divisions ────────────────────
        try:
            raw = _run_steps(scope_res, get_command(scope_fam, "measure_freq", ch=ch))
            freq = float(raw) if raw is not None else None
            if freq and 0 < freq < 1e30:
                tscale = (1.0 / freq) * 3.0 / 10.0
                _write_only(scope_res, scope_fam, "timebase_scale", value=f"{tscale:.6e}")
                _emit_progress(f"  Timebase → {tscale:.3e} s/div  ({freq:.4g} Hz, 3 cycles)")
            else:
                _emit_progress(f"  ⚠ freq unreadable ({raw!r}) — timebase adjust skipped")
        except Exception as exc:
            _emit_progress(f"  ⚠ timebase: {exc}")

        _auto_stop.wait(timeout=0.5)

    def _run_waveform_analysis():
        ch           = int(params.get("ch", 1))
        num_samples  = int(params.get("num_samples", 10))
        interval     = float(params.get("interval", 0.5))
        do_autoscale = str(params.get("autoscale", "yes")).lower() != "no"

        scope_res, scope_fam = _find_instrument("scope")
        if scope_res is None:
            _done([], [], "Scope not connected"); return

        cols = ["phase", "sample",
                "freq_Hz", "vpp_V", "vrms_V",
                "duty_pct", "rise_s", "fall_s", "overshoot_pct"]
        rows = []

        def _r(v, n=6):
            return round(v, n) if v is not None else None

        def _emit_row(phase, sample, freq=None, vpp=None, vrms=None,
                      duty=None, rise=None, fall=None, overshoot=None, progress=0.0):
            row = [phase, sample,
                   _r(freq, 3), _r(vpp), _r(vrms),
                   _r(duty, 3), _r(rise, 9), _r(fall, 9), _r(overshoot, 3)]
            rows.append(row)
            sio.emit("automation_row", {"test_id": test_id, "row": row,
                                        "columns": cols, "progress": progress})

        # ── Phase 1: Setup + overview snapshot ───────────────────────────
        _emit_progress("Phase 1 — Setup")
        if do_autoscale:
            _scope_autoscale_and_rescale(scope_res, scope_fam, ch)
        if _auto_stop.is_set():
            _done(rows, cols); return

        _emit_progress("Phase 1 — Overview snapshot")
        _scope_enable_measures(scope_res, scope_fam, [
            ("measure_freq",      ch), ("measure_vpp",       ch),
            ("measure_vrms",      ch), ("measure_dutycycle", ch),
            ("measure_risetime",  ch), ("measure_falltime",  ch),
            ("measure_overshoot", ch),
        ])
        _auto_stop.wait(timeout=0.5)   # let scope compute after clear+enable
        freq = _scope_query_only(scope_res, scope_fam, "measure_freq",      ch=ch)
        vpp  = _scope_query_only(scope_res, scope_fam, "measure_vpp",       ch=ch)
        vrms = _scope_query_only(scope_res, scope_fam, "measure_vrms",      ch=ch)
        duty = _scope_query_only(scope_res, scope_fam, "measure_dutycycle", ch=ch)
        # Rise/fall at the 3-cycle timebase are often 0 or 9.9e37 on Rigol
        # (scope can't resolve the edge at this zoom) — treat both as None.
        def _vt(v):
            return None if (v is None or v <= 0 or abs(v) > 1e30) else v
        rise = _vt(_scope_query_only(scope_res, scope_fam, "measure_risetime",  ch=ch))
        fall = _vt(_scope_query_only(scope_res, scope_fam, "measure_falltime",  ch=ch))
        over = _scope_query_only(scope_res, scope_fam, "measure_overshoot", ch=ch)
        _emit_row("overview", 1, freq=freq, vpp=vpp, vrms=vrms,
                  duty=duty, rise=rise, fall=fall, overshoot=over, progress=0.15)
        _emit_progress(
            f"  {freq:.4g} Hz  Vpp={vpp:.4g} V  rise={rise:.3e} s"
            if all(v is not None for v in [freq, vpp, rise])
            else f"  freq={freq!r}  Vpp={vpp!r}  rise={rise!r}"
        )

        # ── Phase 2: Edge zoom ────────────────────────────────────────────
        if _auto_stop.is_set():
            _done(rows, cols); return

        # Choose edge zoom timebase.
        # Prefer measured rise time (1 rise-time / div).
        # Fall back to 1/20th of period — fine enough to resolve edges down
        # to ~1/200th of a period (e.g. 50 ns/div for a 1 MHz signal).
        if rise and 0 < rise < 1e30:
            edge_tscale = rise
            edge_pos    = rise * 2
            _emit_progress(f"Phase 2 — Edge zoom  (rise = {rise:.3e} s → {edge_tscale:.3e} s/div)")
        elif freq and 0 < freq < 1e30:
            edge_tscale = (1.0 / freq) / 20.0
            edge_pos    = 0.0
            _emit_progress(f"Phase 2 — Edge zoom  (rise unmeasurable at overview; freq-fallback → {edge_tscale:.3e} s/div)")
        else:
            edge_tscale = None
            _emit_progress("Phase 2 — Skipped (no signal data)")

        if edge_tscale is not None:
            def _valid_timing(v):
                """Return v only if it is a physically plausible timing value.
                Rigol returns 9.9e37 when unmeasurable, and sometimes 0 when
                the measurement hasn't settled yet — both are treated as None."""
                return None if (v is None or v <= 0 or abs(v) > 1e30) else v

            def _zoom_measure_rise(tscale, pos):
                """Zoom to tscale, lock on rising edge, return (rise, overshoot)."""
                _write_only(scope_res, scope_fam, "timebase_scale",    value=f"{tscale:.6e}")
                _write_only(scope_res, scope_fam, "timebase_position", value=f"{pos:.6e}")
                _write_only(scope_res, scope_fam, "trigger_slope", slope="POSitive")
                _auto_stop.wait(timeout=1.5)   # scope re-acquires at new timebase
                _scope_enable_measures(scope_res, scope_fam, [
                    ("measure_risetime",  ch), ("measure_overshoot", ch),
                ])
                _auto_stop.wait(timeout=0.5)   # scope computes after clear+enable
                return (
                    _valid_timing(_scope_query_only(scope_res, scope_fam, "measure_risetime",  ch=ch)),
                    _scope_query_only(scope_res, scope_fam, "measure_overshoot", ch=ch),
                )

            # ── Rising edge ──────────────────────────────────────────────────
            rise2, over2 = _zoom_measure_rise(edge_tscale, edge_pos)

            # If rise still unmeasurable (timebase too coarse), retry 10× finer
            if rise2 is None and edge_tscale > 1e-9:
                finer = edge_tscale / 10.0
                _emit_progress(f"  rise N/A — retrying at {finer:.3e} s/div")
                rise2, over2 = _zoom_measure_rise(finer, 0.0)
                if rise2 is not None:
                    edge_tscale = finer

            _emit_progress(
                f"  Rising edge:  rise={rise2:.3e} s  overshoot={over2:.2f}%"
                if rise2 is not None else f"  Rising edge:  rise=N/A  overshoot={over2!r}"
            )
            _auto_stop.wait(timeout=1.5)   # hold on rising-edge view

            # ── Falling edge ─────────────────────────────────────────────────
            _emit_progress("  Switching to falling-edge trigger…")
            _write_only(scope_res, scope_fam, "trigger_slope",    slope="NEGative")
            _write_only(scope_res, scope_fam, "timebase_position", value="0")
            _auto_stop.wait(timeout=1.5)   # scope re-acquires on falling edge
            _scope_enable_measures(scope_res, scope_fam, [("measure_falltime", ch)])
            _auto_stop.wait(timeout=0.5)
            fall2 = _valid_timing(_scope_query_only(scope_res, scope_fam, "measure_falltime", ch=ch))
            _emit_progress(
                f"  Falling edge: fall={fall2:.3e} s"
                if fall2 is not None else "  Falling edge: fall=N/A"
            )
            _auto_stop.wait(timeout=1.5)   # hold on falling-edge view

            # Restore rising-edge trigger before continuing
            _write_only(scope_res, scope_fam, "trigger_slope", slope="POSitive")

            _emit_row("edge_zoom", 1, rise=rise2, fall=fall2, overshoot=over2, progress=0.30)

            # Zoom back out: 3 cycles
            if freq and 0 < freq < 1e30:
                tscale = (1.0 / freq) * 3.0 / 10.0
                _write_only(scope_res, scope_fam, "timebase_scale",    value=f"{tscale:.6e}")
                _write_only(scope_res, scope_fam, "timebase_position", value="0")
                _emit_progress(f"  Back to overview: {tscale:.3e} s/div (3 cycles)")

        # ── Phase 3: Frequency stability ──────────────────────────────────
        if _auto_stop.is_set():
            _done(rows, cols); return

        _emit_progress(f"Phase 3 — Frequency stability ({num_samples} samples)")
        _scope_enable_measures(scope_res, scope_fam, [
            ("measure_freq", ch), ("measure_vpp", ch),
        ])
        _auto_stop.wait(timeout=0.5)   # let scope compute first sample
        last_vpp = vpp
        for i in range(num_samples):
            if _auto_stop.is_set():
                _emit_progress("Stopped by user"); break

            f = _scope_query_only(scope_res, scope_fam, "measure_freq", ch=ch)
            v = _scope_query_only(scope_res, scope_fam, "measure_vpp",  ch=ch)

            # Dynamic V/div: rescale if amplitude changed > 20 %
            if v and last_vpp and abs(v - last_vpp) / last_vpp > 0.20:
                _write_only(scope_res, scope_fam, "channel_scale",  ch=ch, value=f"{abs(v)/6:.4e}")
                _write_only(scope_res, scope_fam, "channel_offset", ch=ch, value="0")
                last_vpp = v

            _emit_row("stability", i + 1, freq=f, vpp=v,
                      progress=0.35 + (i + 1) / num_samples * 0.65)
            if i < num_samples - 1:
                _auto_stop.wait(timeout=interval)

        _done(rows, cols)

    def _run_harmonic_analysis():
        import math as _math
        ch_signal    = int(params.get("ch_signal", 1))
        ch_ref_raw   = str(params.get("ch_ref", "none")).strip()
        ch_ref       = None if ch_ref_raw in ("none", "0", "", str(ch_signal)) else int(ch_ref_raw)
        num_samples  = int(params.get("num_samples", 20))
        interval     = float(params.get("interval", 0.5))
        do_autoscale = str(params.get("autoscale", "yes")).lower() != "no"

        scope_res, scope_fam = _find_instrument("scope")
        if scope_res is None:
            _done([], [], "Scope not connected"); return

        cols = ["sample", "freq_Hz", "vpp_V", "vrms_V", "crest_factor",
                "thd_est_pct", "ref_vpp_V", "gain_dB"]

        ref_desc = f", gain vs CH{ch_ref}" if ch_ref else ""
        _emit_progress(f"Harmonic analysis: {num_samples} samples from CH{ch_signal}{ref_desc}")

        if do_autoscale:
            _scope_autoscale_and_rescale(scope_res, scope_fam, ch_signal)

        # Enable all needed measurement items once (prevents Rigol "existed item!" beeps)
        ops_to_enable = [
            ("measure_freq", ch_signal),
            ("measure_vpp",  ch_signal),
            ("measure_vrms", ch_signal),
        ]
        if ch_ref is not None:
            ops_to_enable.append(("measure_vpp", ch_ref))
        _scope_enable_measures(scope_res, scope_fam, ops_to_enable)

        def _safe(op, ch_):
            return _scope_query_only(scope_res, scope_fam, op, ch_)

        rows = []
        for i in range(num_samples):
            if _auto_stop.is_set():
                _emit_progress("Stopped by user"); break

            freq = _safe("measure_freq",  ch_signal)
            vpp  = _safe("measure_vpp",   ch_signal)
            vrms = _safe("measure_vrms",  ch_signal)

            # Crest factor = Vpeak / Vrms  (pure sine → √2 ≈ 1.414)
            # THD estimate: assume fundamental Vrms ≈ Vpp/(2√2)
            # THD% = √(Vrms² − Vrms_fund²) / Vrms_fund × 100
            #
            # Guard: if Vrms > Vpeak the scope is likely DC-coupled or bandwidth-
            # limited — crest < 1 is physically impossible, mark both as None.
            crest = thd = None
            if vpp is not None and vrms is not None and vrms > 0:
                vpeak = vpp / 2.0
                if vrms > vpeak:
                    # Physically impossible for an AC signal — skip derived values
                    _log(f"[auto] sample {i+1}: Vrms {vrms:.4g}V > Vpeak {vpeak:.4g}V "
                         f"— check scope coupling/bandwidth")
                else:
                    crest         = round(vpeak / vrms, 4)
                    vrms_fund_est = vpeak / _math.sqrt(2)
                    if vrms >= vrms_fund_est:
                        thd = round(_math.sqrt(max(vrms**2 - vrms_fund_est**2, 0))
                                    / vrms_fund_est * 100, 2)
                    else:
                        thd = 0.0

            ref_vpp = gain_db = None
            if ch_ref is not None:
                ref_vpp = _safe("measure_vpp", ch_ref)
                if vpp is not None and ref_vpp and ref_vpp > 0:
                    gain_db = round(20 * _math.log10(vpp / ref_vpp), 3)

            row = [
                i + 1,
                round(freq, 3)    if freq    is not None else None,
                round(vpp, 6)     if vpp     is not None else None,
                round(vrms, 6)    if vrms    is not None else None,
                crest, thd,
                round(ref_vpp, 6) if ref_vpp is not None else None,
                gain_db,
            ]
            rows.append(row)
            sio.emit("automation_row", {"test_id": test_id, "row": row, "columns": cols,
                                        "progress": (i + 1) / num_samples})
            if i < num_samples - 1:
                _auto_stop.wait(timeout=interval)

        _done(rows, cols)

    runners = {
        "ac_frequency_sweep": _run_ac_sweep,
        "dc_sweep":           _run_dc_sweep,
        "dmm_logger":         _run_dmm_logger,
        "waveform_analysis":  _run_waveform_analysis,
        "harmonic_analysis":  _run_harmonic_analysis,
    }
    runner = runners.get(test_id)
    if runner is None:
        _auto_running = False
        return jsonify({"error": f"Unknown test: {test_id!r}"}), 400

    _executor.submit(runner)
    return jsonify({"status": "running", "test_id": test_id})


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
