"""
routes/instruments.py — scope, PSU, AWG, DMM, and raw SCPI routes.
"""
import pathlib
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

import core.shared as _sh
from core.backbone import get_command
from core.helpers import (
    _find_instrument, _run_steps, _op, _log, _rlock,
    _scope_enable_measures, _scope_query_only,
)

bp = Blueprint("instruments", __name__)

# ── Scope measurement table ───────────────────────────────────────────────────
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

_SCOPE_SET_OPS = {
    "channel_on", "channel_off", "channel_scale", "channel_offset",
    "channel_coupling", "channel_probe", "channel_label",
    "timebase_scale", "timebase_position",
    "trigger_level", "trigger_slope", "trigger_source",
    "labels_on", "labels_off",
}

DMM_OPS = {
    "vdc":   "measure_vdc",   "vac":   "measure_vac",
    "idc":   "measure_idc",   "iac":   "measure_iac",
    "r":     "measure_resistance",     "r4w": "measure_fresistance",
    "freq":  "measure_frequency",      "cont": "measure_continuity",
    "diode": "measure_diode",          "cap":  "measure_capacitance",
}

PSU_LOGGER_OPS = {
    "psu_v": "measure_voltage",
    "psu_i": "measure_current",
    "psu_p": "measure_power",
}


def _res_for_interval(interval: float) -> str:
    """Return EDU34450A resolution keyword for a given measurement interval."""
    if interval < 0.5: return "FAST"
    if interval < 2.0: return "MED"
    return "SLOW"


# ── Scope ─────────────────────────────────────────────────────────────────────

@bp.route("/api/scope/<cmd>", methods=["POST"])
def api_scope(cmd: str):
    if cmd not in ("run", "stop", "single", "autoscale"):
        return jsonify({"error": "unknown command"}), 400
    _sh._executor.submit(lambda: _op(*_find_instrument("scope"), cmd, role="scope"))
    return jsonify({"status": "ok"})


@bp.route("/api/scope/measure", methods=["POST"])
def api_scope_measure():
    d    = request.json or {}
    meas = d.get("measurement", "measure_vpp")
    ch   = int(d.get("ch", 1))

    if meas not in _SCOPE_MEASURES:
        return jsonify({"error": f"unknown measurement {meas!r}"}), 400

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            _sh.sio.emit("scope_measurement", {"error": "No scope connected"}); return
        try:
            raw = _run_steps(res, get_command(fam, meas, ch=ch), role="scope")
            val = float(raw) if raw is not None else None
            label, unit = _SCOPE_MEASURES[meas]
            _sh.sio.emit("scope_measurement",
                         {"measurement": meas, "label": label, "unit": unit,
                          "ch": ch, "value": val})
        except KeyError:
            _sh.sio.emit("scope_measurement",
                         {"error": f"{meas!r} not supported on this scope"})
        except Exception as exc:
            _sh.sio.emit("scope_measurement", {"error": str(exc)})

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


@bp.route("/api/scope/measure_batch", methods=["POST"])
def api_scope_measure_batch():
    """Run a list of measurements sequentially.

    setup=True  → call _scope_enable_measures first (clears display items and
                  re-registers each one).  Required on first call and whenever
                  the measurement list changes or overflows the scope's on-screen
                  slot limit.
    setup=False → skip the enable step and only send the query for each item.
                  Safe as long as the scope still has those items registered.
    """
    d       = request.json or {}
    items   = d.get("measurements", [])   # [{op, ch}, ...]
    delay_s = max(0, min(int(d.get("delay_ms", 50)), 2000)) / 1000.0
    setup   = bool(d.get("setup", True))
    poll    = bool(d.get("poll", False))

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            _sh.sio.emit("log", {"msg": "⚠ No scope connected"})
            _sh.sio.emit("scope_measurement_batch_done", {})
            return

        valid = [(m.get("op", ""), int(m.get("ch", 1)))
                 for m in items if m.get("op", "") in _SCOPE_MEASURES]

        if setup:
            _scope_enable_measures(res, fam, valid)

        for idx, (op, ch) in enumerate(valid):
            if idx > 0 and delay_s > 0:
                time.sleep(delay_s)
            label, unit = _SCOPE_MEASURES[op]
            val = _scope_query_only(res, fam, op, ch, poll=poll)
            _sh.sio.emit("scope_measurement",
                         {"measurement": op, "label": label, "unit": unit,
                          "ch": ch, "value": val})

        _sh.sio.emit("scope_measurement_batch_done", {})

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


@bp.route("/api/scope/screenshot", methods=["POST"])
def api_screenshot():
    d        = request.json or {}
    filename = d.get("filename", "screenshot")
    out_dir  = d.get("output_path", "").strip()

    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            _sh.sio.emit("screenshot_done", {"error": "No scope connected"}); return

        try:
            steps = get_command(fam, "screenshot")
        except KeyError:
            _sh.sio.emit("screenshot_done",
                         {"error": "Screenshot not supported on this scope"}); return

        raw_idx = next((i for i, (a, _) in enumerate(steps) if a == "raw_query"), None)
        if raw_idx is None:
            _sh.sio.emit("screenshot_done",
                         {"error": "No data-read step in screenshot command"}); return

        pre  = [(a, s) for a, s in steps[:raw_idx]     if a == "write"]
        cmd_ = steps[raw_idx][1]
        post = [(a, s) for a, s in steps[raw_idx + 1:] if a == "write"]

        data = None
        with _rlock(res):
            # Stop the scope before capturing so :DISPlay:DATA? returns immediately
            # instead of blocking until the next triggered acquisition.
            scope_stopped = False
            try:
                _run_steps(res, get_command(fam, "stop"), role="scope")
                scope_stopped = True
                time.sleep(0.1)     # let display latch the frozen frame
            except Exception:
                pass                # scope family has no stop cmd — proceed anyway

            orig_timeout = res.timeout
            try:
                res.timeout = 35_000    # BMP over USB takes ~16 s; 35 s gives margin

                for _, s in pre:
                    res.write(s)

                res.write(cmd_)
                time.sleep(0.1)         # let scope compose the image before REQUESTing data
                orig_chunk = getattr(res, 'chunk_size', None)
                try:
                    res.chunk_size = _sh._SCREENSHOT_CHUNK_SIZE
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
                _sh.sio.emit("screenshot_done", {"error": str(exc)})
                return
            finally:
                try: res.timeout = orig_timeout
                except Exception: pass
                # Always resume acquisition after the capture attempt
                if scope_stopped:
                    try:
                        _run_steps(res, get_command(fam, "run"), role="scope")
                    except Exception:
                        pass

        if not data:
            _sh.sio.emit("screenshot_done", {"error": "Scope returned empty data"}); return

        # Strip any leading SCPI header before the image magic bytes
        ext = ""
        for magic, e in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
            idx = data.find(magic)
            if idx != -1:
                data = data[idx:]; ext = e; break

        ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
        name     = f"{filename}_{ts}{ext or '.bin'}"
        _ROOT    = pathlib.Path(__file__).parent.parent
        save_dir = pathlib.Path(out_dir) if out_dir else _ROOT / "results"
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / name
        try:
            path.write_bytes(data)
        except Exception as exc:
            _sh.sio.emit("screenshot_done", {"error": f"Could not save file: {exc}"}); return

        _log(f"✓ Screenshot saved: {name}  ({len(data)} bytes)")
        _sh.sio.emit("screenshot_done", {"path": str(path), "filename": name})

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


@bp.route("/api/scope/set", methods=["POST"])
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
            _sh.sio.emit("log", {"msg": "⚠ No scope connected"}); return
        try:
            steps  = get_command(fam, op, **kw)
            _run_steps(res, steps, role="scope")
            writes = [s for a, s in steps if a in ("write", "query")]
            if writes:
                _sh.sio.emit("log", {"msg": "→  " + "  |  ".join(writes[:2])})
        except KeyError:
            _sh.sio.emit("log",
                         {"msg": f"⚠ scope: {op!r} not supported on this scope model"})
        except Exception as exc:
            _sh.sio.emit("log", {"msg": f"✗ scope {op}: {exc}"})

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


@bp.route("/api/scope/sync", methods=["POST"])
def api_scope_sync():
    """Query current scope settings (timebase, per-channel scale/coupling/probe/display)
    and emit a scope_state SocketIO event so the UI can update its controls."""
    def _do():
        res, fam = _find_instrument("scope")
        if res is None:
            _sh.sio.emit("log", {"msg": "⚠ scope sync: not connected"}); return

        state: dict = {"channels": []}

        with _rlock(res):
            # Timebase
            try:
                for act, scpi in get_command(fam, "timebase_scale", value=1):
                    if act == "query":
                        state["timebase_scale"] = float(res.query(scpi).strip())
            except Exception:
                pass

            # Per-channel: display, scale, coupling, probe  (query up to 4)
            for ch in range(1, 5):
                ch_state: dict = {"ch": ch}
                try:
                    ch_state["display"] = res.query(f":CHANnel{ch}:DISPlay?").strip() in ("1", "ON")
                except Exception:
                    pass
                for op, kw in [("channel_scale",    {"value": 1}),
                               ("channel_coupling", {"coupling": "DC"}),
                               ("channel_probe",    {"attenuation": 1})]:
                    try:
                        for act, scpi in get_command(fam, op, ch=ch, **kw):
                            if act == "query":
                                raw = res.query(scpi).strip()
                                if op == "channel_coupling":
                                    ch_state["coupling"] = raw.upper()
                                else:
                                    ch_state[op.split("_")[1]] = float(raw)
                    except Exception:
                        pass
                state["channels"].append(ch_state)

        _sh.sio.emit("scope_state", state)
        _log("↻ Scope settings synced")

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


# ── Raw SCPI ──────────────────────────────────────────────────────────────────

@bp.route("/api/scpi", methods=["POST"])
def api_scpi():
    """Send an arbitrary SCPI command to an instrument identified by role.
    If the command contains '?', queries and returns the result via scpi_result event."""
    d    = request.json or {}
    role = d.get("role", "scope")
    cmd  = d.get("cmd", "").strip()
    if not cmd:
        return jsonify({"error": "no command"}), 400

    def _do():
        res, _fam = _find_instrument(role)
        if res is None:
            _sh.sio.emit("scpi_result", {"role": role, "cmd": cmd,
                                         "error": f"{role} not connected"}); return
        try:
            with _rlock(res):
                if "?" in cmd:
                    result = res.query(cmd).strip()
                    _sh.sio.emit("scpi_result", {"role": role, "cmd": cmd, "result": result})
                    _sh.sio.emit("scpi_traffic", {"role": role, "cmd": cmd, "result": result})
                    _log(f"[SCPI/{role}] {cmd}  →  {result}")
                else:
                    res.write(cmd)
                    _sh.sio.emit("scpi_result", {"role": role, "cmd": cmd, "result": None})
                    _sh.sio.emit("scpi_traffic", {"role": role, "cmd": cmd, "result": None})
                    _log(f"[SCPI/{role}] {cmd}")
        except Exception as exc:
            _sh.sio.emit("scpi_result", {"role": role, "cmd": cmd, "error": str(exc)})
            _log(f"[SCPI/{role}] ✗ {cmd}: {exc}")

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


# ── PSU ───────────────────────────────────────────────────────────────────────

@bp.route("/api/psu/set", methods=["POST"])
def api_psu_set():
    d  = request.json or {}
    ch = int(d.get("ch", 1))
    op_map = {"voltage": ("set_voltage", "value"),
              "current": ("set_current_limit", "value")}
    for key, (op, kw) in op_map.items():
        if key in d:
            val = f"{float(d[key]):.4f}"
            _sh._executor.submit(
                lambda o=op, v=val: _op(*_find_instrument("psu"), o, role="psu",
                                        ch=ch, **{kw: v}))
    return jsonify({"status": "ok"})


@bp.route("/api/psu/output", methods=["POST"])
def api_psu_output():
    d  = request.json or {}
    ch = int(d.get("ch", 1))
    on = bool(d.get("state", False))
    _sh._executor.submit(
        lambda: _op(*_find_instrument("psu"),
                    "output_on" if on else "output_off", role="psu", ch=ch))
    return jsonify({"status": "ok"})


@bp.route("/api/psu/reset", methods=["POST"])
def api_psu_reset():
    _sh._executor.submit(lambda: _op(*_find_instrument("psu"), "reset", role="psu"))
    return jsonify({"status": "ok"})


# ── AWG ───────────────────────────────────────────────────────────────────────

@bp.route("/api/awg/apply", methods=["POST"])
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
                _op(res, fam, scpi_op, role="awg", ch=ch, **kw_fn(d[key]))
        if "amplitude" in d:
            _op(res, fam, "set_amplitude_unit", role="awg", ch=ch, unit="VPP")

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


@bp.route("/api/awg/output", methods=["POST"])
def api_awg_output():
    d  = request.json or {}
    ch = int(d.get("ch", 1))
    on = bool(d.get("state", False))
    _sh._executor.submit(
        lambda: _op(*_find_instrument("awg"),
                    "output_on" if on else "output_off", role="awg", ch=ch))
    return jsonify({"status": "ok"})


@bp.route("/api/awg/read_state", methods=["POST"])
def api_awg_read_state():
    d  = request.json or {}
    ch = int(d.get("ch", 1))

    def _do():
        res, fam = _find_instrument("awg")
        if res is None or fam is None:
            return {}

        cmds = fam.get("commands", {})

        # Normalize voltage unit to VPP so amplitude queries return Vpp.
        # GWinstek (and some others) use VOLT:UNIT; APPL? ignores VOLT:UNIT
        # changes so those families must null apply_query and use AMPL? instead.
        amp_unit_spec = cmds.get("set_amplitude_unit")
        if isinstance(amp_unit_spec, dict) and "write" in amp_unit_spec and "query" in amp_unit_spec:
            # Query first; only write if unit is not already VPP.
            try:
                unit_q = amp_unit_spec["query"].format(ch=ch)
                with _rlock(res):
                    cur_unit = _run_steps(res, [("query", unit_q)], role="awg")
                if not cur_unit or cur_unit.strip().upper() != "VPP":
                    unit_w = amp_unit_spec["write"].format(ch=ch, unit="VPP")
                    with _rlock(res):
                        _run_steps(res, [("write", unit_w)], role="awg")
            except Exception:
                pass
        elif isinstance(amp_unit_spec, str) and "{unit}" in amp_unit_spec:
            try:
                write_str = amp_unit_spec.format(ch=ch, unit="VPP")
                with _rlock(res):
                    _run_steps(res, [("write", write_str)], role="awg")
            except Exception:
                pass

        # Prefer APPL? when available: returns func, freq, amp in one shot.
        appl_spec = cmds.get("apply_query")
        if isinstance(appl_spec, dict) and "query" in appl_spec:
            try:
                q_str = appl_spec["query"].format(ch=ch)
                with _rlock(res):
                    raw = _run_steps(res, [("query", q_str)], role="awg")
                if raw:
                    raw = raw.strip()
                    # Response: "SIN +5.000E+03,+3.0000E+00,-2.5000E+00"
                    head, _, tail = raw.partition(" ")
                    vals = [v.strip() for v in tail.split(",")]
                    out = {"function": head.strip()}
                    for dest, idx in [("frequency", 0), ("amplitude", 1), ("offset", 2)]:
                        if idx < len(vals):
                            try:
                                out[dest] = str(float(vals[idx]))
                            except ValueError:
                                pass
                    # GWinstek omits offset from APPL? when it is 0 V;
                    # always query DCO? separately to guarantee a value.
                    off_spec = cmds.get("set_offset")
                    if isinstance(off_spec, dict) and "query" in off_spec:
                        try:
                            oq = off_spec["query"].format(ch=ch)
                            with _rlock(res):
                                oval = _run_steps(res, [("query", oq)], role="awg")
                            if oval is not None:
                                out["offset"] = oval.strip()
                        except Exception:
                            pass
                    return out
            except Exception:
                pass

        # Fall back to individual queries (one round-trip per parameter).
        # GWinstek AMPL? returns peak amplitude (Vpp/2), not Vpp. Detect this by
        # the presence of a queryable set_amplitude_unit spec (dict with both
        # write and query), which is currently only added for gwinstek_afg.
        amp_is_peak = isinstance(amp_unit_spec, dict) and "query" in amp_unit_spec
        out = {}
        for key, op in [("function", "set_function"), ("frequency", "set_frequency"),
                        ("amplitude", "set_amplitude"), ("offset", "set_offset")]:
            try:
                spec = cmds.get(op)
                if not isinstance(spec, dict) or "query" not in spec:
                    continue
                q_str = spec["query"].format(ch=ch)
                with _rlock(res):
                    val = _run_steps(res, [("query", q_str)], role="awg")
                if val is not None:
                    v = val.strip()
                    if key == "amplitude" and amp_is_peak:
                        try:
                            v = str(float(v) * 2)
                        except ValueError:
                            pass
                    out[key] = v
            except Exception:
                pass
        return out

    result = _sh._executor.submit(_do).result(timeout=5)
    return jsonify(result)


# ── DMM ───────────────────────────────────────────────────────────────────────

@bp.route("/api/dmm/measure", methods=["POST"])
def api_dmm_measure():
    d    = request.json or {}
    mode = d.get("mode", "vdc")
    poll = bool(d.get("poll", False))
    op   = DMM_OPS.get(mode)
    if not op:
        return jsonify({"error": f"unknown mode {mode!r}"}), 400

    def _do():
        res, fam = _find_instrument("dmm")
        if res is None or fam is None: return
        try:
            raw = (_run_steps(res, get_command(fam, op), role="dmm", poll=poll) or "nan")
            # Some DMMs (e.g. OWON) append unit characters; strip them before float()
            val = float(str(raw).strip().rstrip("VAΩFHzohm°C°F°K%dBm"))
            _sh.sio.emit("dmm_reading", {"value": val, "mode": mode})
        except Exception as exc:
            _log(f"[dmm] {exc}")

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})


# ── Electronic Load ───────────────────────────────────────────────────────────

@bp.route("/api/eload/mode", methods=["POST"])
def api_eload_mode():
    d    = request.json or {}
    mode = d.get("mode", "CURR").upper()   # CURR / VOLT / RES / POW
    _sh._executor.submit(
        lambda: _op(*_find_instrument("eload"), "set_mode", role="eload", func=mode))
    return jsonify({"status": "ok"})


@bp.route("/api/eload/value", methods=["POST"])
def api_eload_value():
    d   = request.json or {}
    op  = d.get("op", "set_current")       # set_current / set_voltage / set_resistance / set_power
    val = d.get("value")
    if val is None:
        return jsonify({"error": "value required"}), 400
    _sh._executor.submit(
        lambda: _op(*_find_instrument("eload"), op, role="eload", value=str(val)))
    return jsonify({"status": "ok"})


@bp.route("/api/eload/input", methods=["POST"])
def api_eload_input():
    d  = request.json or {}
    on = bool(d.get("state", False))
    _sh._executor.submit(
        lambda: _op(*_find_instrument("eload"),
                    "input_on" if on else "input_off", role="eload"))
    return jsonify({"status": "ok"})


@bp.route("/api/eload/measure", methods=["POST"])
def api_eload_measure():
    def _do():
        res, fam = _find_instrument("eload")
        if res is None or fam is None: return
        readings = {}
        for op, key in [("measure_voltage", "v"),
                        ("measure_current", "i"),
                        ("measure_power",   "p")]:
            try:
                r = _run_steps(res, get_command(fam, op), role="eload")
                if r is not None:
                    readings[key] = float(str(r).strip().rstrip("VAW"))
            except Exception:
                pass
        if readings:
            _sh.sio.emit("eload_reading", readings)

    _sh._executor.submit(_do)
    return jsonify({"status": "ok"})
