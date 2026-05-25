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
import itertools
import json as _json
import os
import subprocess
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

# ── PyVISA-Py USBTMC bug fix ──────────────────────────────────────────────────
# pyvisa-py ≤ 0.8.1: USBTMC.read() inner while uses `or` instead of `and`,
# causing it to call raw_read() without a new REQUEST when the device sends
# exactly wMaxPacketSize-byte chunks (e.g. Rigol DS1000Z :DISPlay:DATA?).
# Fix: replace `or` with `and` so the loop only continues when BOTH conditions
# are true: last USB packet was full-sized AND we still need more bytes.
def _apply_usbtmc_patch():
    import math
    try:
        from pyvisa_py.protocols import usbtmc as _usbtmc_mod
        from pyvisa_py.protocols.usbtmc import BulkInMessage, USBRaw, USBTMC
        import usb.core as _usb_core
    except ImportError:
        return

    def _patched_read(self, size):
        usbtmc_header_size = 12
        eom = False
        raw_read  = USBRaw.read.__get__(self, USBTMC)
        raw_write = USBRaw.write.__get__(self, USBTMC)
        received_message = bytearray()
        while not eom:
            received_transfer = bytearray()
            self._btag = (self._btag % 255) + 1
            req = BulkInMessage.build_array(self._btag, size, None)
            raw_write(req)
            try:
                chunk_size = (
                    math.floor(
                        (size + usbtmc_header_size) / self.usb_recv_ep.wMaxPacketSize
                    ) + 1
                ) * self.usb_recv_ep.wMaxPacketSize
                resp = raw_read(chunk_size)
                if len(resp) < 12:
                    # ZLP / short response — scope not ready, outer loop retries.
                    continue
                response = BulkInMessage.from_bytes(resp)
                received_transfer.extend(response.data)
                if len(received_transfer) >= response.transfer_size:
                    eom = response.transfer_attributes & 1
                if not eom and len(received_transfer) >= size:
                    eom = True
                else:
                    while (
                        (len(resp) % self.usb_recv_ep.wMaxPacketSize) == 0
                        and len(received_transfer) < response.transfer_size  # AND (was OR)
                    ) and not eom:
                        chunk_size = (
                            math.floor(
                                (size - len(received_transfer))
                                / self.usb_recv_ep.wMaxPacketSize
                            ) + 1
                        ) * self.usb_recv_ep.wMaxPacketSize
                        resp = raw_read(chunk_size)
                        received_transfer.extend(resp)
                    if len(received_transfer) >= response.transfer_size:
                        eom = response.transfer_attributes & 1
                    if not eom and len(received_transfer) >= size:
                        eom = True
                received_message.extend(received_transfer[: response.transfer_size])
            except (_usb_core.USBError, ValueError):
                self._abort_bulk_in(self._btag)
                raise
        return bytes(received_message)

    _usbtmc_mod.USBTMC.read = _patched_read

if PYVISA_OK:
    _apply_usbtmc_patch()

# chunk size for binary screenshot reads — must exceed the image size so
# USBTMC.read() accumulates all chunks and returns the full image in one call.
_SCREENSHOT_CHUNK_SIZE = 2 * 1024 * 1024   # 2 MB

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
            # Use a large chunk_size so USBTMC.read() accumulates the full
            # binary payload (e.g. 1.15 MB BMP) before returning.
            orig_chunk = getattr(resource, 'chunk_size', None)
            try:
                resource.chunk_size = _SCREENSHOT_CHUNK_SIZE
                result = resource.read_raw()
            finally:
                if orig_chunk is not None:
                    try: resource.chunk_size = orig_chunk
                    except Exception: pass
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


@flask_app.route("/api/scope/measure_batch", methods=["POST"])
def api_scope_measure_batch():
    """Run a list of measurements sequentially with a configurable inter-command delay."""
    d            = request.json or {}
    items        = d.get("measurements", [])   # [{op, ch}, ...]
    delay_s      = max(0, min(int(d.get("delay_ms", 50)), 2000)) / 1000.0

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            sio.emit("log", {"msg": "⚠ No scope connected"})
            sio.emit("scope_measurement_batch_done", {})
            return
        for idx, m in enumerate(items):
            if idx > 0 and delay_s > 0:
                time.sleep(delay_s)
            op = m.get("op", "")
            ch = int(m.get("ch", 1))
            if op not in _SCOPE_MEASURES:
                continue
            try:
                raw = _run_steps(res, get_command(fam, op, ch=ch))
                val = float(raw) if raw is not None else None
                label, unit = _SCOPE_MEASURES[op]
                sio.emit("scope_measurement",
                         {"measurement": op, "label": label, "unit": unit,
                          "ch": ch, "value": val})
            except KeyError:
                sio.emit("scope_measurement",
                         {"measurement": op, "ch": ch,
                          "error": f"{op!r} not supported on this scope"})
            except Exception as exc:
                sio.emit("scope_measurement",
                         {"measurement": op, "ch": ch, "error": str(exc)})
        sio.emit("scope_measurement_batch_done", {})

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

        # Stop the scope before capturing so :DISPlay:DATA? returns immediately
        # instead of blocking until the next triggered acquisition.
        scope_stopped = False
        try:
            _run_steps(res, get_command(fam, "stop"))
            scope_stopped = True
            time.sleep(0.2)     # let display latch the frozen frame
        except Exception:
            pass                # scope family has no stop cmd — proceed anyway

        orig_timeout = res.timeout
        try:
            res.timeout = 35_000    # BMP over USB takes ~16 s; 35 s gives margin

            for _, s in pre:
                res.write(s)

            res.write(cmd_)
            time.sleep(0.5)         # let scope compose the image before REQUESTing data
            # chunk_size is set inside _run_steps; here we call read_raw directly
            # so we must set it ourselves.
            orig_chunk = getattr(res, 'chunk_size', None)
            try:
                res.chunk_size = _SCREENSHOT_CHUNK_SIZE
                data = res.read_raw()
            finally:
                if orig_chunk is not None:
                    try: res.chunk_size = orig_chunk
                    except Exception: pass

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
            # Always resume acquisition after the capture attempt
            if scope_stopped:
                try:
                    _run_steps(res, get_command(fam, "run"))
                except Exception:
                    pass

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


# ── Scope controls ────────────────────────────────────────────────────────────
_SCOPE_SET_OPS = {
    "channel_on", "channel_off", "channel_scale", "channel_offset",
    "channel_coupling", "channel_probe", "channel_label",
    "timebase_scale", "timebase_position",
    "trigger_level", "trigger_slope", "trigger_source",
}

@flask_app.route("/api/scope/set", methods=["POST"])
def api_scope_set():
    """Send a single write-type scope command (channel or timebase setting)."""
    d  = request.json or {}
    op = d.get("op", "")
    kw = {k: v for k, v in d.items() if k != "op"}
    if op not in _SCOPE_SET_OPS:
        return jsonify({"error": f"unknown op {op!r}"}), 400

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            sio.emit("log", {"msg": "⚠ No scope connected"}); return
        try:
            _run_steps(res, get_command(fam, op, **kw))
        except KeyError:
            sio.emit("log", {"msg": f"⚠ scope: {op!r} not supported on this scope model"})
        except Exception as exc:
            sio.emit("log", {"msg": f"✗ scope {op}: {exc}"})

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
        # usb_only skips LAN scanning (done above) but must keep USB-serial
        # (ASRL) resources — they are USB devices too.  _keep_resource already
        # filtered ASRL down to ttyUSB/ttyACM/COM ports, so nothing extra needed.

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
    """Return tests available based on instruments that are *actually connected*.

    Using connected resources (not just workbench contents) means a PSU-only
    test (dc_sweep) appears even when a scope in the workbench is switched off.
    """
    wb = _state.get("workbench")
    if not wb:
        return []
    connected = set(_state.get("resources", {}).keys())
    types = {
        instr.get("type")
        for instr in wb.get("_unique", [])
        if instr.get("resource", "") in connected
    }
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
        all_instr   = wb.get("_unique", [])
        psu_list    = [i for i in all_instr if i.get("type") == "psu"]
        dmm_list    = [i for i in all_instr if i.get("type") == "dmm"]
        scope_list  = [i for i in all_instr if i.get("type") == "scope"]

        # Source options: one entry per PSU in the workbench
        source_opts = [
            {"value": i["resource"], "label": i.get("model", "PSU")}
            for i in psu_list if i.get("resource")
        ]
        # Meter options: PSU built-in → DMMs → Scopes (type tag lets frontend pick right measure list)
        i_meter_opts = [{"value": "psu", "label": "PSU (built-in)", "itype": "psu"}] + [
            {"value": i["resource"], "label": f"DMM · {i.get('model', 'DMM')}", "itype": "dmm"}
            for i in dmm_list if i.get("resource")
        ] + [
            {"value": i["resource"], "label": f"Scope · {i.get('model', 'Scope')}", "itype": "scope"}
            for i in scope_list if i.get("resource")
        ]

        tests.append({
            "id":          "dc_sweep",
            "name":        "DC Sweep",
            "description": "Sweep voltage source(s) across N channels and record measurements",
            "requires":    ["psu"],
            "custom_ui":   True,          # rendered by buildDcSweepCard() in the frontend
            "sources":     source_opts,   # [{value, label}] — available voltage sources
            "meters":      i_meter_opts,  # [{value, label, itype}] — available measurement instruments
            "params":      [],            # param collection handled by the custom card UI
            "columns":     ["step", "measurement"],   # overridden at runtime by the runner
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


@flask_app.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    """Open a native OS folder-picker dialog and return the chosen path."""
    try:
        import tkinter as _tk
        from tkinter import filedialog as _fd
        root = _tk.Tk()
        root.withdraw()          # hide the empty Tk window
        root.attributes("-topmost", True)
        initial = (request.json or {}).get("initial", str(Path.home()))
        path = _fd.askdirectory(parent=root, title="Select output folder",
                                initialdir=initial)
        root.destroy()
        if path:
            return jsonify({"path": path})
        return jsonify({"path": None})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@flask_app.route("/api/automation/run", methods=["POST"])
def api_automation_run():
    global _auto_running
    if _auto_running:
        return jsonify({"error": "A test is already running"}), 409

    d       = request.json or {}
    test_id = d.get("test_id")
    params  = d.get("params", {})
    # Resolve output directory: expand ~ and make absolute
    raw_out = (d.get("output_path") or "").strip()
    out_dir = Path(os.path.expanduser(raw_out)).resolve() if raw_out else Path.cwd()

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
        # Auto-save CSV to output directory
        csv_path = None
        if rows and columns and not error:
            try:
                ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
                out_dir.mkdir(parents=True, exist_ok=True)
                csv_path = out_dir / f"{test_id}_{ts}.csv"
                with open(csv_path, "w", newline="") as f:
                    import csv as _csv
                    w = _csv.writer(f)
                    w.writerow(columns)
                    w.writerows(rows)
                _log(f"[auto] saved CSV → {csv_path}")
            except Exception as exc:
                _log(f"[auto] CSV save failed: {exc}")
        sio.emit("automation_done", {
            "test_id": test_id, "columns": columns,
            "rows": rows, "error": error,
            "csv_path": str(csv_path) if csv_path else None,
        })
        # Resume PSU polling now that automation has released the resources
        if _state.get("connected"):
            _start_polling()

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
        num_ch     = max(1, int(params.get("num_channels", 1)))
        sweep_mode = str(params.get("sweep_mode", "simultaneous"))

        # ── Parse measurement / action items ─────────────────────────────────
        # Each item: {kind, instrument?, measure?, samples?, settle?, label?,
        #             action?, script?}
        raw_meas = params.get("measurements", "[]")
        try:
            meas_items = _json.loads(raw_meas) if isinstance(raw_meas, str) else []
        except Exception:
            meas_items = []
        # Default: single voltage readout from PSU
        if not meas_items:
            meas_items = [{"kind": "measurement", "instrument": "psu",
                           "measure": "voltage", "samples": 1, "settle": 0.1}]

        # ── Per-channel configs ──────────────────────────────────────────────
        ch_cfgs = []
        for n in range(num_ch):
            ch_cfgs.append({
                "resource": str(params.get(f"ch{n}_resource", "")),
                "ch":       max(1, int(params.get(f"ch{n}_ch",      n + 1))),
                "v_start":  float(params.get(f"ch{n}_v_start", 0)),
                "v_step":   abs(float(params.get(f"ch{n}_v_step",  0.1))) or 0.1,
                "v_stop":   float(params.get(f"ch{n}_v_stop",  5)),
                "i_limit":  float(params.get(f"ch{n}_i_limit", 0.5)),
                "settle":   float(params.get(f"ch{n}_settle",  0.1)),
            })

        # ── Resolve per-channel instrument handles ───────────────────────────
        ch_handles = []
        for n, cfg in enumerate(ch_cfgs):
            res = _state["resources"].get(cfg["resource"])
            fam = _state["families"].get(cfg["resource"])
            if res is None:
                res, fam = _find_instrument("psu")
            if res is None:
                _done([], [], f"CH{n + 1} voltage source not connected"); return
            ch_handles.append({"res": res, "fam": fam, "cfg": cfg})

        # ── Shared helpers ────────────────────────────────────────────────────
        def _safe(r):
            try:
                v = float(r)
                return None if abs(v) > 1e30 else round(v, 6)
            except Exception:
                return None

        def _make_vols(cfg):
            n = round(abs(cfg["v_stop"] - cfg["v_start"]) / cfg["v_step"])
            s = 1 if cfg["v_stop"] >= cfg["v_start"] else -1
            return [round(cfg["v_start"] + k * s * cfg["v_step"], 10)
                    for k in range(n + 1)]

        def _set_v(handle, v):
            _run_steps(handle["res"], get_command(
                handle["fam"], "set_voltage",
                ch=handle["cfg"]["ch"], value=f"{v:.6f}"))

        def _setup_ch(handle):
            _run_steps(handle["res"], get_command(
                handle["fam"], "set_current_limit",
                ch=handle["cfg"]["ch"], value=f"{handle['cfg']['i_limit']:.4f}"))
            _run_steps(handle["res"], get_command(
                handle["fam"], "output_on", ch=handle["cfg"]["ch"]))

        def _teardown_ch(handle):
            try:
                _run_steps(handle["res"], get_command(
                    handle["fam"], "set_voltage",
                    ch=handle["cfg"]["ch"], value="0.0"))
                _run_steps(handle["res"], get_command(
                    handle["fam"], "output_off", ch=handle["cfg"]["ch"]))
            except Exception:
                pass

        def _emit_row(row, cols, progress):
            sio.emit("automation_row", {"test_id": test_id, "row": row,
                                        "columns": cols, "progress": progress})

        # ── Per-item execution ────────────────────────────────────────────────
        # Maps measurement type → (PSU SCPI op, DMM SCPI op, Scope SCPI op, auto column abbr)
        _MEAS_MAP = {
            "voltage":   ("measure_voltage", "measure_vdc",      None,                    "V"),
            "current":   ("measure_current", "measure_idc",      None,                    "I"),
            "vac":       (None,              "measure_vac",      None,                    "Vac"),
            "r":         (None,              "measure_r",        None,                    "R"),
            "r4w":       (None,              "measure_r4w",      None,                    "R4w"),
            # Scope measurements
            "vpp":       (None,              None,               "measure_vpp",           "Vpp"),
            "vrms":      (None,              None,               "measure_vrms",          "Vrms"),
            "freq":      (None,              None,               "measure_freq",          "Freq"),
            "duty":      (None,              None,               "measure_dutycycle",     "Duty"),
            "rise":      (None,              None,               "measure_risetime",      "Rise"),
            "fall":      (None,              None,               "measure_falltime",      "Fall"),
            "overshoot": (None,              None,               "measure_overshoot",     "Ovs"),
            "period":    (None,              None,               "measure_period",        "Per"),
        }

        def _auto_label(item, idx):
            lbl = (item.get("label") or "").strip()
            if lbl:
                return lbl
            kind = item.get("kind", "measurement")
            if kind == "measurement":
                abbr = _MEAS_MAP.get(item.get("measure", "voltage"),
                                     ("", "", "", "val"))[3]
            elif item.get("action") == "screenshot":
                abbr = "img"
            else:
                abbr = "script"
            return f"{abbr}_{idx + 1}"

        def _exec_item(item, step_ctx):
            """Execute one measurement/action item; return its value."""
            kind = item.get("kind", "measurement")

            # ── Measurement ──────────────────────────────────────────────────
            if kind == "measurement":
                instr_id = item.get("instrument", "psu")
                measure  = item.get("measure",    "voltage")
                samples  = max(1, int(item.get("samples", 1)))
                settle   = float(item.get("settle", 0.1))
                instr_ch = max(1, int(item.get("instr_ch", 1) or 1))

                use_ext = instr_id not in ("psu", "")
                if use_ext:
                    res = _state["resources"].get(instr_id)
                    fam = _state["families"].get(instr_id)
                    # Determine instrument type from workbench for correct dispatch
                    wb_instr = next(
                        (i for i in (_state.get("workbench") or {}).get("_unique", [])
                         if i.get("resource") == instr_id), None)
                    instr_type = (wb_instr.get("type", "dmm") if wb_instr else "dmm")
                else:
                    res = ch_handles[0]["res"]
                    fam = ch_handles[0]["fam"]
                    instr_type = "psu"

                if res is None:
                    return None

                psu_op, dmm_op, scope_op, _ = _MEAS_MAP.get(
                    measure, ("measure_voltage", "measure_vdc", None, "V"))

                time.sleep(settle)

                def _once():
                    try:
                        if instr_type == "scope":
                            if scope_op is None:
                                return None
                            # Use query-only path (no re-enable write needed for one-off reads)
                            return _scope_query_only(res, fam, scope_op, ch=instr_ch)
                        elif use_ext:
                            if dmm_op is None:
                                return None
                            return _safe(_run_steps(res, get_command(fam, dmm_op)))
                        elif psu_op:
                            return _safe(_run_steps(res, get_command(
                                fam, psu_op, ch=instr_ch)))
                        return None
                    except Exception:
                        return None

                vals = [_once() for _ in range(samples)]
                nums = [v for v in vals if v is not None]
                return round(sum(nums) / len(nums), 6) if nums else None

            # ── Action ───────────────────────────────────────────────────────
            elif kind == "action":
                action = item.get("action", "screenshot")

                if action == "screenshot":
                    scope_res, scope_fam = _find_instrument("scope")
                    if scope_res is None:
                        _log("[auto] capture: no scope connected"); return None
                    data = b""
                    try:
                        steps   = get_command(scope_fam, "screenshot")
                        raw_idx = next(
                            (i for i, (a, _) in enumerate(steps) if a == "raw_query"),
                            None)
                        if raw_idx is None:
                            _log("[auto] capture: no raw_query step in screenshot command")
                            return None
                        pre   = [s for a, s in steps[:raw_idx]     if a == "write"]
                        cmd_  = steps[raw_idx][1]
                        post  = [s for a, s in steps[raw_idx + 1:] if a == "write"]

                        # STOP the scope before capturing.
                        # In run/waiting-for-trigger state some InfiniiVision
                        # firmware versions hold :DISPlay:DATA? until the next
                        # complete acquisition — which may never arrive (e.g.
                        # DUT is off at 0 V).  :STOP freezes the display and
                        # guarantees an immediate response.
                        scope_stopped = False
                        try:
                            _run_steps(scope_res,
                                       get_command(scope_fam, "stop"))
                            scope_stopped = True
                            time.sleep(0.2)   # let scope latch the frozen frame
                        except Exception:
                            pass              # scope family has no stop cmd — proceed anyway

                        orig_timeout = scope_res.timeout
                        try:
                            scope_res.timeout = 20_000  # screenshots can take several seconds
                            for s in pre:
                                scope_res.write(s)
                            time.sleep(0.5)             # let scope compose the image
                            scope_res.write(cmd_)
                            data = scope_res.read_raw()
                            for s in post:
                                try: scope_res.write(s)
                                except Exception: pass
                        finally:
                            try: scope_res.timeout = orig_timeout
                            except Exception: pass
                            # Always resume acquisition after the capture attempt
                            if scope_stopped:
                                try:
                                    _run_steps(scope_res,
                                               get_command(scope_fam, "run"))
                                except Exception:
                                    pass

                        if not data:
                            _log("[auto] capture: scope returned empty data")
                            return None

                        # Strip leading SCPI binary block header (#NXXXXXXX...)
                        # before the actual image magic bytes
                        ext = ".bin"
                        for magic, e in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
                            idx = data.find(magic)
                            if idx != -1:
                                data = data[idx:]; ext = e; break

                        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
                        step  = step_ctx.get("step", 0)
                        fname = f"sweep_{step:04d}_{ts}{ext}"
                        fpath = out_dir / "screenshots" / fname
                        fpath.parent.mkdir(parents=True, exist_ok=True)
                        fpath.write_bytes(data)
                        _log(f"[auto] capture → {fpath}  ({len(data)} bytes)")
                        return str(fpath)
                    except Exception as exc:
                        _log(f"[auto] capture error: {exc}")
                    return None

                elif action == "scpi":
                    rstr    = (item.get("scpi_instrument") or "").strip()
                    command = (item.get("scpi_command")    or "").strip()
                    settle  = float(item.get("scpi_settle", 0.1) or 0)
                    if not command:
                        return None
                    # Resolve instrument: by resource string, or fall back to first connected
                    res = _state["resources"].get(rstr)
                    if res is None and rstr:
                        _log(f"[auto] SCPI query: instrument '{rstr}' not connected")
                        return None
                    if res is None:
                        # pick any connected instrument
                        res = next(iter(_state["resources"].values()), None)
                    if res is None:
                        return None
                    try:
                        if settle > 0:
                            time.sleep(settle)
                        raw = res.query(command).strip()
                        try:
                            return round(float(raw), 6)
                        except ValueError:
                            return raw
                    except Exception as exc:
                        _log(f"[auto] SCPI query '{command}': {exc}")
                    return None

                elif action == "script":
                    script = (item.get("script") or "").strip()
                    if not script:
                        return None
                    try:
                        env = {
                            **os.environ,
                            **{f"SWEEP_{k.upper()}": str(v)
                               for k, v in step_ctx.items()},
                        }
                        result = subprocess.run(
                            [script], env=env,
                            capture_output=True, text=True, timeout=30,
                        )
                        out = (result.stdout or "").strip().split("\n")[0]
                        if out:
                            try:
                                return round(float(out), 6)
                            except ValueError:
                                return out
                    except Exception as exc:
                        _log(f"[auto] script '{script}': {exc}")
                    return None

            return None

        def _exec_all(step_ctx):
            """Run every measurement/action item; return list of values.

            Items with a 'trigger' field other than 'every' are skipped
            unless the matching loop level changed this step.
            'every'  → always execute (default)
            'inner'  → only when the inner loop ticks (always true in nested)
            'outer'  → only when the outer loop changes
            'mid'    → only when any mid-level loop changes
            'midN'   → only when mid level N changes  (mid1, mid2)
            """
            level_changed = step_ctx.get("_level_changed", "inner")
            results = []
            for item in meas_items:
                trigger = (item.get("trigger") or "every").strip().lower()
                if trigger != "every" and trigger != level_changed:
                    # For broad triggers: 'outer' fires when outer changed;
                    # 'mid' fires for any mid* level.
                    if not (trigger == "mid" and level_changed.startswith("mid")):
                        results.append(None)
                        continue
                results.append(_exec_item(item, step_ctx))
            return results

        # ── Build result columns ──────────────────────────────────────────────
        meas_cols = [_auto_label(item, i) for i, item in enumerate(meas_items)]

        rows = []

        # ════════════════════════════════════════════════════════════════════
        # SIMULTANEOUS (or single channel)
        # Every channel follows its own range; step count = the longest range.
        # Shorter ranges clamp at their v_stop for remaining steps.
        # ════════════════════════════════════════════════════════════════════
        if sweep_mode == "simultaneous" or num_ch == 1:
            vols_all = [_make_vols(h["cfg"]) for h in ch_handles]
            total    = max(len(v) for v in vols_all)

            if num_ch == 1:
                cols = ["v_set_V"] + meas_cols
            else:
                cols = [f"ch{n + 1}_v_set_V" for n in range(num_ch)] + meas_cols

            _emit_progress(f"DC Sweep — {num_ch} ch simultaneous: {total} steps")
            try:
                for h in ch_handles:
                    _setup_ch(h)
            except Exception as exc:
                _done([], cols, f"Setup failed: {exc}"); return

            for k in range(total):
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break
                try:
                    v_sets = {}
                    for n, h in enumerate(ch_handles):
                        v = vols_all[n][min(k, len(vols_all[n]) - 1)]
                        _set_v(h, v)
                        v_sets[f"ch{n + 1}"] = round(v, 6)
                    time.sleep(ch_cfgs[0]["settle"])

                    step_ctx = {"step": k + 1, **v_sets}
                    meas_vals = _exec_all(step_ctx)

                    if num_ch == 1:
                        row = [v_sets["ch1"]] + meas_vals
                    else:
                        row = [v_sets[f"ch{n + 1}"] for n in range(num_ch)] + meas_vals

                    rows.append(row)
                    _emit_row(row, cols, (k + 1) / total)
                except Exception as exc:
                    _log(f"[auto] step {k + 1}: {exc}")

            for h in ch_handles:
                _teardown_ch(h)

        # ════════════════════════════════════════════════════════════════════
        # NESTED: CH1 = inner (fastest loop), CHN = outer (slowest loop).
        # Generalised to 2–4 channels using itertools.product.
        # Naming: inner / mid / mid1,mid2 / outer  (by channel index)
        # ════════════════════════════════════════════════════════════════════
        elif sweep_mode == "nested" and num_ch >= 2:

            def _nest_name(n):
                """n=0 is inner (fastest), n=num_ch-1 is outer (slowest)."""
                if n == 0:             return "inner"
                if n == num_ch - 1:   return "outer"
                mid_n = n             # 1, 2, …
                if num_ch == 3:       return "mid"
                return f"mid{mid_n}"  # mid1, mid2 for 4-ch

            # Volumes per channel, index 0 = inner
            vols_per_ch = [_make_vols(h["cfg"]) for h in ch_handles]
            # For itertools.product we iterate outermost first → reversed
            vols_outer_first   = list(reversed(vols_per_ch))
            handles_outer_first = list(reversed(ch_handles))

            total = 1
            for v in vols_per_ch:
                total *= len(v)

            # Column order: outermost voltage first, innermost last
            # e.g. 3-ch: ["v_outer_V", "v_mid_V", "v_inner_V"]
            cols = [f"v_{_nest_name(n)}_V"
                    for n in reversed(range(num_ch))] + meas_cols

            shape_str = "×".join(str(len(v)) for v in vols_outer_first)
            _emit_progress(
                f"DC Sweep — nested {num_ch}-ch  "
                f"{shape_str} = {total} pts")
            try:
                for h in ch_handles:
                    _setup_ch(h)
            except Exception as exc:
                _done([], cols, f"Setup failed: {exc}"); return

            step       = 0
            prev_combo = None

            for combo in itertools.product(*vols_outer_first):
                # combo = (v_outer, [v_mid…], v_inner)
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break

                # Determine which level changed since last step
                if prev_combo is None:
                    first_changed = 0        # everything is new
                else:
                    first_changed = next(
                        (i for i, (a, b) in enumerate(zip(combo, prev_combo))
                         if a != b),
                        num_ch - 1)

                # Set only channels that changed; settle all but innermost
                for i in range(first_changed, num_ch):
                    h = handles_outer_first[i]
                    _set_v(h, combo[i])
                    if i < num_ch - 1:          # not the innermost
                        time.sleep(h["cfg"]["settle"])

                prev_combo = combo
                step += 1

                # step_ctx: ch1 = inner voltage, chN = outer voltage
                step_ctx = {"step": step}
                for i, v in enumerate(reversed(combo)):   # i=0 → inner
                    step_ctx[f"ch{i + 1}"] = round(v, 6)

                # level_changed: 0 = outer changed, num_ch-1 = only inner changed
                # expressed as the *name* of the level that changed most
                step_ctx["_level_changed"] = _nest_name(num_ch - 1 - first_changed)

                meas_vals = _exec_all(step_ctx)
                row       = [round(v, 6) for v in combo] + meas_vals
                rows.append(row)
                _emit_row(row, cols, step / total)

                # Announce whenever an outer level completes its inner pass
                if first_changed == 0 and step > 1:
                    # inner just ticked — no announcement needed
                    pass
                elif first_changed < num_ch - 1:
                    changed_name = _nest_name(num_ch - 1 - first_changed)
                    changed_v    = combo[first_changed]
                    _emit_progress(
                        f"  {changed_name} = {changed_v:.4g} V — sweeping inner…")

            for h in ch_handles:
                _teardown_ch(h)

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

    # Stop the PSU polling loop so automation has exclusive access to resources.
    # PyVISA resource objects are not thread-safe; concurrent poller + runner
    # calls produce "Invalid session handle" errors.  _done() will restart it.
    _poll_stop.set()

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
