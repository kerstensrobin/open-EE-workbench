"""
routes/connection.py — /api/connect and /api/disconnect blueprints.
"""
import json as _json
import time

from flask import Blueprint, jsonify, request

import core.shared as _sh
from core.backbone import get_command, _classify, HELPERS_OK, active_name, load_workbench
from core.demo import DemoResource
from core.helpers import (
    _find_instrument, _run_steps, _op, _log, _rlock,
    _start_polling, _write_ops_for_family, _family_for, LockedResource,
    _windows_driverless_hint,
)

bp = Blueprint("connection", __name__)


# ── Helper used only here ──────────────────────────────────────────────────────

def _safe_query(res, fam, op, **kwargs):
    """Run a backbone query; return stripped string or None on any failure."""
    try:
        steps  = get_command(fam, op, **kwargs)
        result = _run_steps(res, steps)
        return result.strip() if isinstance(result, str) else None
    except Exception:
        return None


def _find_resource_by_serial(rm, serial: str, cache: dict):
    """Recover a USB instrument's current VISA resource string by serial
    number when the one saved in the workbench no longer opens.

    VISA re-enumerates USB resource strings (bus/address, sometimes the
    embedded serial-derived suffix) across reboots or a different port —
    especially on Windows/NI-VISA — so a workbench saved in one session can
    point at a resource that no longer exists in the next, even though the
    same physical instrument is still plugged in and otherwise identical.
    `cache` is shared across one connect cycle so the live USB enumeration
    (which opens every USB resource once to read its IDN) only runs once no
    matter how many instruments need recovering.
    """
    if not serial or rm is None:
        return None
    if not cache.get("_built"):
        cache["_built"] = True
        try:
            from nachoVisa import discover_resources, query_identity, parse_idn
            live, _errs = discover_resources(rm)
        except Exception:
            return None
        for live_rstr in live:
            if not live_rstr.upper().startswith("USB"):
                continue
            try:
                inst = rm.open_resource(live_rstr)
                try:
                    idn = query_identity(inst, live_rstr)
                finally:
                    inst.close()
            except Exception:
                continue
            _, _, live_serial, _ = parse_idn(idn)
            if live_serial:
                cache[live_serial] = live_rstr
    return cache.get(serial)


# ── /api/connect ──────────────────────────────────────────────────────────────

@bp.route("/api/connect", methods=["POST"])
def api_connect():
    wb = _sh._state.get("workbench")
    if not wb:
        return jsonify({"error": "No workbench loaded"}), 400

    # Allow connecting a demo workbench even without pyvisa installed
    is_demo = all(
        instr.get("resource", "").upper().startswith("DEMO::")
        for instr in wb.get("_unique", [])
    )
    if not _sh.PYVISA_OK and not is_demo:
        return jsonify({"error": "pyvisa not available — demo mode only"}), 503

    def _do():
        rm            = None
        raw_results: dict = {}
        demo_slot_ctr = {}   # instr_type -> count (for per-slot noise de-correlation)
        serial_cache: dict = {}   # lazy serial → live-resource map, see _find_resource_by_serial

        for instr in wb.get("_unique", []):
            rstr = instr.get("resource", "")
            if not rstr or rstr in raw_results:
                continue

            if rstr.upper().startswith("DEMO::"):
                # Create a DemoResource — no real hardware needed
                itype = instr.get("type", "unknown")
                slot  = demo_slot_ctr.get(itype, 0)
                demo_slot_ctr[itype] = slot + 1
                idn   = instr.get("idn", f"DEMO,{instr.get('model','DEMO')},SN000000,1.0")
                res   = DemoResource(rstr, idn, itype, slot)
                raw_results[rstr] = (res, idn, None)
                _log(f"✓  {instr['model']}  →  {idn}  [demo]")
                continue

            try:
                if rm is None:
                    from nachoVisa import open_resource_manager
                    rm = open_resource_manager()
                res = LockedResource(rm.open_resource(rstr))   # serialise concurrent VISA ops
                res.timeout    = 8000
                if "SOCKET" in rstr.upper():
                    res.read_termination  = "\n"
                    res.write_termination = "\n"
                elif rstr.upper().startswith("ASRL"):
                    # USB-serial instrument (e.g. Keithley 2231A via Prolific)
                    res.baud_rate         = 9600
                    res.data_bits         = 8
                    res.stop_bits         = _sh.pyvisa.constants.StopBits.one
                    res.parity            = _sh.pyvisa.constants.Parity.none
                    res.read_termination  = "\n"
                    res.write_termination = "\n"
                try:
                    res.clear()   # flush stale I/O buffers left from a prior session
                except Exception:
                    pass
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
                            _log("  → remote mode active")
                        except KeyError:
                            pass
                        except Exception as exc2:
                            _log(f"  ⚠ remote_mode: {exc2}")
            except Exception as exc:
                recovered  = False
                live_rstr  = _find_resource_by_serial(rm, instr.get("serial"), serial_cache)
                if live_rstr and live_rstr != rstr:
                    try:
                        res = LockedResource(rm.open_resource(live_rstr))
                        res.timeout = 8000
                        try:
                            res.clear()   # flush stale I/O buffers left from a prior session
                        except Exception:
                            pass
                        idn = res.query("*IDN?").strip()
                        raw_results[rstr] = (res, idn, None)
                        _log(f"✓  {instr['model']}  →  {idn}  "
                             f"(resource moved: {rstr} → {live_rstr})")
                        recovered = True
                    except Exception as exc2:
                        exc = exc2
                if not recovered:
                    msg = str(exc)
                    hint = _windows_driverless_hint(rstr)
                    if hint:
                        msg = f"{msg} — {hint}"
                    raw_results[rstr] = (None, None, msg)
                    _log(f"✗  {instr['model']}:  {msg}")

        families = {}
        for instr in wb.get("_unique", []):
            rstr = instr.get("resource", "")
            if not rstr:
                continue
            _, idn, _ = raw_results.get(rstr, (None, None, None))
            # Prefer live-IDN classification over the stored family_id so stale
            # workbench entries (created before a new family was added) get the
            # right commands automatically.
            fam = _classify(idn) if idn else None
            if fam is None:
                fam = _family_for(instr)
            families[rstr] = fam

        with _sh._lock:
            for r in _sh._state["resources"].values():
                try: r.close()
                except: pass
            if _sh._state["rm"]:
                try: _sh._state["rm"].close()
                except: pass
            _sh._state["rm"]        = rm
            _sh._state["resources"] = {k: v[0] for k, v in raw_results.items() if v[0]}
            _sh._state["families"]  = {k: v for k, v in families.items() if v}
            _sh._state["connected"] = bool(_sh._state["resources"])

        # Probe PSU channel counts once here so _suggest_tests() can read from
        # _state without making live VISA queries on every automation tab refresh.
        psu_ch_cache = {}
        for instr in wb.get("_unique", []):
            if instr.get("type") != "psu":
                continue
            rstr    = instr.get("resource", "")
            psu_res = _sh._state["resources"].get(rstr)
            psu_fam = _sh._state["families"].get(rstr)
            if not psu_res or not psu_fam:
                continue
            num_ch = 0
            for ch in range(1, 5):
                try:
                    r = _safe_query(psu_res, psu_fam, "measure_voltage", ch=ch)
                    if r is None:
                        break
                    num_ch = ch
                except Exception:
                    break
            psu_ch_cache[rstr] = max(num_ch, 1)
        with _sh._lock:
            _sh._state["psu_channels"] = psu_ch_cache

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

        _sh.sio.emit("connection_result", {
            "connected":   _sh._state["connected"],
            "instruments": instruments_out,
        })
        if _sh._state["connected"] and _sh._polling_enabled:
            _start_polling()

    _sh._executor.submit(_do)
    return jsonify({"status": "connecting"})


# ── /api/polling/stop and /api/polling/resume ─────────────────────────────────

@bp.route("/api/polling/stop", methods=["POST"])
def api_polling_stop():
    _sh._polling_enabled = False
    _sh._poll_stop.set()
    return jsonify({"status": "stopped"})


@bp.route("/api/polling/resume", methods=["POST"])
def api_polling_resume():
    _sh._polling_enabled = True
    if _sh._state.get("connected"):
        _start_polling()
    return jsonify({"status": "resumed"})


# ── /api/disconnect ───────────────────────────────────────────────────────────

def _disconnect_all(close_rm: bool = True):
    """Tear down every connected instrument. Shared by /api/disconnect and the
    Python console's resource-takeover guard (core/routes/system.py), which
    passes close_rm=False: pyvisa shares one ResourceManager per backend, so
    closing it would also kill the session the script is about to use."""
    _sh._poll_stop.set()
    with _sh._lock:
        for rstr, r in _sh._state["resources"].items():
            # Return all instruments to local (front-panel) control before closing
            if HELPERS_OK:
                fam = _sh._state["families"].get(rstr)
                if fam:
                    try:
                        for act, scpi in get_command(fam, "local_mode"):
                            if act == "write":
                                r.write(scpi)
                    except Exception:
                        pass
            try: r.close()
            except: pass
        if close_rm and _sh._state["rm"]:
            try: _sh._state["rm"].close()
            except: pass
        _sh._state.update(rm=None, resources={}, psu_channels={}, connected=False)
        _sh._psu_ch_cache.clear()
    _sh.sio.emit("disconnected", {})


@bp.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    _disconnect_all()
    return jsonify({"status": "disconnected"})
