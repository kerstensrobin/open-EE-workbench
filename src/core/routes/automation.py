"""
routes/automation.py — automation test runner routes.
"""
import itertools
import json as _json
import os
import subprocess
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

import core.shared as _sh
from core.backbone import get_command, HELPERS_OK
from core.helpers import (
    _find_instrument, _run_steps, _op, _log, _rlock,
    _scope_enable_measures, _scope_query_only, _start_polling,
    _extract_value_key,
)
from core.paths import today_output_dir
from core.routes.instruments import DMM_OPS, PSU_LOGGER_OPS, _res_for_interval

bp = Blueprint("automation", __name__)

_ROOT = Path(__file__).parent.parent

# ── Module-level automation control events ────────────────────────────────────
_auto_stop    = threading.Event()
_auto_pause   = threading.Event()   # set = paused; runners call _pause_point()
_auto_running = False


# ── Custom tests (saved from the Sandbox tab) ─────────────────────────────────
_CUSTOM_TESTS_DIR = Path(__file__).resolve().parents[2] / "custom_tests"


def _load_custom_tests() -> list:
    """Parse every saved custom test definition. Malformed files are skipped."""
    if not _CUSTOM_TESTS_DIR.is_dir():
        return []
    out = []
    for p in sorted(_CUSTOM_TESTS_DIR.glob("*.json")):
        try:
            out.append(_json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _load_custom_test(test_id: str):
    p = _CUSTOM_TESTS_DIR / f"{test_id}.json"
    if not p.is_file():
        return None
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _instrument_type_of(resource: str):
    """Look up the instrument type (psu/scope/dmm/...) for a resource string
    in the currently loaded workbench, so saved steps can be stored by role
    instead of a literal resource that may not exist on a future connection."""
    wb = _sh._state.get("workbench") or {}
    for instr in wb.get("_unique", []):
        if instr.get("resource") == resource:
            return instr.get("type")
    return None


def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^\w]+", "_", name.strip().lower()).strip("_")
    return s or "custom_test"


# ── Suggest tests ─────────────────────────────────────────────────────────────

def _suggest_tests() -> list:
    """Return tests available based on instruments that are *actually connected*.

    Using connected resources (not just workbench contents) means a PSU-only
    test (dc_sweep) appears even when a scope in the workbench is switched off.
    All known tests are always returned; unavailable ones have available=False
    so the frontend can show them greyed out even with no workbench selected.
    """
    _KNOWN = [
        {"id": "ac_frequency_sweep", "name": "AC Frequency Sweep",
         "description": "Sweep AWG frequency; scope measurements are optional",
         "requires": ["awg"]},
        {"id": "dc_sweep",           "name": "DC Sweep",
         "description": "Sweep voltage source(s) across N channels and record measurements",
         "requires": ["psu"]},
        {"id": "psu_interrupt",      "name": "PSU Interrupt",
         "description": "Interrupt a PSU channel for T2 ms and capture the transient response",
         "requires": ["psu"]},
        {"id": "dmm_logger",         "name": "DMM Logger",
         "description": "Log DMM measurements at a fixed interval",
         "requires": ["dmm"]},
        {"id": "psu_logger",         "name": "PSU Logger",
         "description": "Set PSU channel voltage(s) and log current draw / power in real time",
         "requires": ["psu"]},
        {"id": "battery_capacity",   "name": "Battery Capacity",
         "description": "Constant-current discharge test; records V/I/P and calculates mAh",
         "requires": ["load"]},
    ]
    wb = _sh._state.get("workbench")
    if not wb:
        # No workbench — return all tests as unavailable stubs so users can browse
        stubs = [{**kt, "available": False, "params": [], "columns": []} for kt in _KNOWN]
        stubs += [{
            "id": cdef["id"], "name": cdef.get("name", cdef["id"]),
            "description": cdef.get("description", ""),
            "requires": cdef.get("instrument_roles", []), "is_custom": True,
            "available": False, "params": [], "columns": [],
        } for cdef in _load_custom_tests()]
        return stubs
    connected = set(_sh._state.get("resources", {}).keys())
    types = {
        instr.get("type")
        for instr in wb.get("_unique", [])
        if instr.get("resource", "") in connected
    }
    tests = []

    if "awg" in types:
        has_scope = "scope" in types
        tests.append({
            "id":          "ac_frequency_sweep",
            "name":        "AC Frequency Sweep",
            "description": "Sweep AWG frequency, measure Vpp on scope CH1 and CH2" if has_scope
                           else "Sweep AWG frequency (connect scope to add Vpp measurements)",
            "requires":    ["awg"],
            "params": [
                {"id": "freq_start",  "label": "Start freq",  "unit": "Hz",  "default": 100,    "type": "number"},
                {"id": "freq_stop",   "label": "Stop freq",   "unit": "Hz",  "default": 100000, "type": "number"},
                {"id": "num_points",  "label": "Points",      "unit": "",    "default": 20,     "type": "number"},
                {"id": "amplitude",   "label": "Amplitude",   "unit": "Vpp", "default": 1.0,    "type": "number"},
                {"id": "settle_time", "label": "Settle",      "unit": "s",   "default": 0.2,    "type": "number"},
            ] + ([{
                "id": "scope_measures", "label": "Measure", "type": "multicheck",
                "default": "vpp_ch1,vpp_ch2,freq_ch1",
                "options": [
                    {"value": "vpp_ch1",  "label": "Vpp CH1"},
                    {"value": "vpp_ch2",  "label": "Vpp CH2"},
                    {"value": "freq_ch1", "label": "Freq CH1"},
                ],
            }] if has_scope else []),
            "columns": ["set_freq_Hz", "meas_freq_Hz", "vpp_ch1_V", "vpp_ch2_V"] if has_scope
                       else ["set_freq_Hz"],
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
        # Meter options: PSU built-in → DMMs → Scopes
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
            "custom_ui":   True,
            "sources":     source_opts,
            "meters":      i_meter_opts,
            "params":      [],
            "columns":     ["step", "measurement"],
        })

        has_scope = bool(scope_list)
        _psu_sel = (
            [{"id": "psu_resource", "label": "PSU", "unit": "", "type": "select",
              "default": source_opts[0]["value"],
              "options": source_opts}]
            if len(psu_list) > 1 else []
        )
        tests.append({
            "id":          "psu_interrupt",
            "name":        "PSU Interrupt",
            "description": "Interrupt a PSU channel for T2 ms and capture the transient response",
            "requires":    ["psu"],
            "params": _psu_sel + [
                {"id": "channel",       "label": "PSU channel",  "unit": "",    "default": 1,     "type": "number"},
                {"id": "v1",            "label": "V1",           "unit": "V",   "default": 5.0,   "type": "number"},
                {"id": "t1",            "label": "T1 settle",    "unit": "ms",  "default": 500,   "type": "number"},
                {"id": "v2_mode",       "label": "Interrupt state", "unit": "", "default": "off", "type": "select",
                 "options": [{"value": "off", "label": "PSU channel off"}, {"value": "voltage", "label": "Specific V2 voltage"}]},
                {"id": "v2_sweep",      "label": "Sweep V2",     "unit": "",    "default": "no",  "type": "select",
                 "options": ["no", "yes"]},
                {"id": "v2",            "label": "V2",           "unit": "V",   "default": 0.0,   "type": "number"},
                {"id": "v2_start",      "label": "V2 start",     "unit": "V",   "default": 0.0,   "type": "number"},
                {"id": "v2_stop",       "label": "V2 stop",      "unit": "V",   "default": 3.0,   "type": "number"},
                {"id": "v2_step",       "label": "V2 step",      "unit": "V",   "default": 0.5,   "type": "number"},
                {"id": "t2",            "label": "T2 duration",  "unit": "ms",  "default": 100,   "type": "number"},
                {"id": "t2_sweep",      "label": "Sweep T2",     "unit": "",    "default": "no",  "type": "select",
                 "options": ["no", "yes"]},
                {"id": "t2_start",      "label": "T2 start",     "unit": "ms",  "default": 10,    "type": "number"},
                {"id": "t2_stop",       "label": "T2 stop",      "unit": "ms",  "default": 500,   "type": "number"},
                {"id": "t2_step",       "label": "T2 step",      "unit": "ms",  "default": 10,    "type": "number"},
                {"id": "v3",            "label": "V3",           "unit": "V",   "default": 5.0,   "type": "number"},
                {"id": "t3",            "label": "T3 settle",    "unit": "ms",  "default": 500,   "type": "number"},
                {"id": "current_limit", "label": "I limit",      "unit": "A",   "default": 0.5,   "type": "number"},
                {"id": "scope_channel",      "label": "Scope CH",      "unit": "",   "default": 1,              "type": "number"},
                {"id": "scope_slope",        "label": "Trig slope",    "unit": "",   "default": "falling",      "type": "select",
                 "options": [{"value": "falling", "label": "Falling (NEG)"}, {"value": "rising", "label": "Rising (POS)"}]},
                {"id": "scope_scale",        "label": "Time/div",      "unit": "ms", "default": 0,              "type": "number"},
                {"id": "scope_horiz_pos",    "label": "H. offset",     "unit": "",   "default": "yes",          "type": "select",
                 "options": [{"value": "yes", "label": "Auto"}, {"value": "no", "label": "Off"}]},
                {"id": "scope_measurements", "label": "Measurements",  "unit": "",   "default": "vmax,vmin,nwidth", "type": "hidden"},
            ],
            "columns": ["run", "t2_ms",
                        "v1_meas_V", "v1_meas_A",
                        "v2_meas_V", "v2_meas_A",
                        "v3_meas_V", "v3_meas_A",
                        "screenshot"],
            "has_scope": has_scope,
        })

        # Prefer a user-set nickname over the bare model name so identical
        # PSU models can be told apart; fall back to a "#N" suffix if two
        # supplies still share the same name (no nickname set on either).
        psu_names = [i.get("nickname") or i.get("model", "PSU") for i in psu_list]
        name_counts = {}
        for n in psu_names:
            name_counts[n] = name_counts.get(n, 0) + 1
        name_seen = {}
        psu_channel_opts = []
        for psu_instr, base_name in zip(psu_list, psu_names):
            res_key = psu_instr.get("resource", "")
            model   = psu_instr.get("model", "PSU")
            num_ch  = _sh._state["psu_channels"].get(res_key, 1)
            if name_counts[base_name] > 1:
                name_seen[base_name] = name_seen.get(base_name, 0) + 1
                supply_label = f"{base_name} #{name_seen[base_name]}"
            else:
                supply_label = base_name
            for ch in range(1, num_ch + 1):
                ch_label = f"{supply_label} Ch{ch}" if num_ch > 1 else supply_label
                psu_channel_opts.append({
                    "resource": res_key, "label": ch_label,
                    "model": model, "ch": ch,
                })
        tests.append({
            "id":            "psu_logger",
            "name":          "PSU Logger",
            "description":   "Set PSU channel voltage(s) and log current draw / power in real time",
            "requires":      ["psu"],
            "custom_ui":     True,
            "psu_channels":  psu_channel_opts,
            "params":        [],
            "columns":       ["elapsed_s"],
        })

    if "dmm" in types or "psu" in types:
        all_instr_all  = wb.get("_unique", [])
        psu_list_l     = [i for i in all_instr_all if i.get("type") == "psu"]
        dmm_instr_opts = [
            {"resource": i["resource"], "label": i.get("model", f"DMM{n+1}"),
             "model": i.get("model", "DMM"), "itype": "dmm"}
            for n, i in enumerate(all_instr_all)
            if i.get("type") == "dmm" and i.get("resource")
        ]
        for psu_instr in psu_list_l:
            res_key  = psu_instr.get("resource", "")
            model    = psu_instr.get("model", "PSU")
            num_ch   = _sh._state["psu_channels"].get(res_key, 1)
            for ch in range(1, num_ch + 1):
                ch_label = f"{model} Ch{ch}" if num_ch > 1 else model
                dmm_instr_opts.append({
                    "resource": res_key, "label": ch_label,
                    "model": model,      "itype": "psu", "ch": ch,
                })
        tests.append({
            "id":               "dmm_logger",
            "name":             "DMM Logger",
            "description":      "Log measurements from DMMs and PSUs at a fixed interval",
            "requires":         ["dmm"],
            "custom_ui":        True,
            "dmm_instruments":  dmm_instr_opts,
            "params":           [],
            "columns":          ["elapsed_s"],
        })

    if "load" in types:
        tests.append({
            "id":          "battery_capacity",
            "name":        "Battery Capacity",
            "description": "Constant-current discharge; records V/I/P and calculates mAh",
            "requires":    ["load"],
            "params": [
                {"id": "current",       "label": "Discharge I",    "unit": "A",   "default": 1.0,  "type": "number"},
                {"id": "cutoff_v",      "label": "Cutoff voltage", "unit": "V",   "default": 3.0,  "type": "number"},
                {"id": "interval",      "label": "Poll interval",  "unit": "s",   "default": 5,    "type": "number"},
                {"id": "max_hours",     "label": "Max duration",   "unit": "h",   "default": 10,   "type": "number"},
            ],
            "columns": ["elapsed_s", "voltage_V", "current_A", "power_W", "capacity_mAh"],
        })

    # Append greyed-out stubs for any known test whose instruments aren't connected
    existing = {t["id"] for t in tests}
    for kt in _KNOWN:
        if kt["id"] not in existing:
            tests.append({**kt, "available": False, "params": [], "columns": []})

    # ── Custom tests saved from the Sandbox tab ───────────────────────────────
    all_instr = wb.get("_unique", [])
    connected_by_type = {}
    for instr in all_instr:
        if instr.get("resource", "") in connected:
            connected_by_type.setdefault(instr.get("type"), []).append(instr)

    for cdef in _load_custom_tests():
        roles = cdef.get("instrument_roles", [])
        avail = all(r in types for r in roles)
        cparams = list(cdef.get("params", []))
        if avail:
            for role in roles:
                candidates = connected_by_type.get(role, [])
                if len(candidates) > 1:
                    cparams.insert(0, {
                        "id": f"_role_{role}", "label": role.upper(), "unit": "",
                        "type": "select", "default": candidates[0]["resource"],
                        "options": [{"value": c["resource"], "label": c.get("model", role)}
                                    for c in candidates],
                    })
        tests.append({
            "id":          cdef["id"],
            "name":        cdef.get("name", cdef["id"]),
            "description": cdef.get("description", ""),
            "requires":    roles,
            "is_custom":   True,
            "available":   avail,
            "params":      cparams,
            "columns":     cdef.get("columns", []),
        })

    return tests


# ── Simple automation control routes ──────────────────────────────────────────

@bp.route("/api/automation/tests")
def api_automation_tests():
    return jsonify({"tests": _suggest_tests()})


@bp.route("/api/automation/custom-tests", methods=["POST"])
def api_save_custom_test():
    """Save a Sandbox-designed sequence as a reusable test in the Automation tab.

    Steps are stored with instrument *roles* (psu/scope/dmm/...) instead of the
    literal resource selected in Sandbox, so the test still resolves correctly
    against a different workbench/connection later.
    """
    d     = request.json or {}
    name  = (d.get("name") or "").strip()
    steps = d.get("steps", [])
    loops = d.get("loops", [])
    params = d.get("params", [])

    if not name:
        return jsonify({"error": "name is required"}), 400
    if not any(s.get("type") == "measure" for s in steps):
        return jsonify({"error": "Add at least one Measure step"}), 400

    roles = set()
    baked_steps = []
    for s in steps:
        s2 = dict(s)
        resource = s2.get("instrument")
        if resource:
            role = _instrument_type_of(resource)
            if role:
                roles.add(role)
                s2["instrument"] = role
        baked_steps.append(s2)

    columns = [s.get("label") or f"meas_{i+1}"
               for i, s in enumerate(steps) if s.get("type") == "measure"]

    base_id = _slugify(name)
    test_id = base_id
    n = 2
    while (_CUSTOM_TESTS_DIR / f"{test_id}.json").exists():
        test_id = f"{base_id}_{n}"
        n += 1

    _CUSTOM_TESTS_DIR.mkdir(parents=True, exist_ok=True)
    defn = {
        "id":               test_id,
        "name":             name,
        "description":      (d.get("description") or "").strip(),
        "created":          datetime.now().isoformat(timespec="seconds"),
        "params":           params,
        "instrument_roles": sorted(roles),
        "columns":          columns,
        "steps":            baked_steps,
        "loops":            loops,
    }
    (_CUSTOM_TESTS_DIR / f"{test_id}.json").write_text(
        _json.dumps(defn, indent=2), encoding="utf-8")
    return jsonify({"id": test_id})


@bp.route("/api/automation/custom-tests/<test_id>", methods=["DELETE"])
def api_delete_custom_test(test_id):
    p = _CUSTOM_TESTS_DIR / f"{test_id}.json"
    if not p.is_file():
        return jsonify({"error": "not found"}), 404
    p.unlink()
    return jsonify({"status": "deleted"})


@bp.route("/api/automation/stop", methods=["POST"])
def api_automation_stop():
    global _auto_running
    _auto_pause.clear()   # unblock any paused runner so it can see _auto_stop
    _auto_stop.set()
    _auto_running = False
    return jsonify({"status": "stopping"})


@bp.route("/api/automation/pause", methods=["POST"])
def api_automation_pause():
    _auto_pause.set()
    _sh.sio.emit("automation_paused", {})
    return jsonify({"status": "paused"})


@bp.route("/api/automation/resume", methods=["POST"])
def api_automation_resume():
    _auto_pause.clear()
    _sh.sio.emit("automation_resumed", {})
    return jsonify({"status": "running"})


# ── Main automation run route ──────────────────────────────────────────────────

@bp.route("/api/automation/run", methods=["POST"])
def api_automation_run():
    global _auto_running
    if _auto_running:
        return jsonify({"error": "A test is already running"}), 409

    d       = request.json or {}
    test_id = d.get("test_id")
    params  = d.get("params", {})
    # Resolve output directory: today's dated folder, under a custom base if given
    raw_out = (d.get("output_path") or "").strip()
    out_dir = today_output_dir(raw_out or None)

    if not test_id:
        return jsonify({"error": "test_id required"}), 400
    if not _sh._state.get("connected"):
        return jsonify({"error": "Not connected to instruments"}), 400

    _auto_stop.clear()
    _auto_pause.clear()
    _auto_running = True

    def _emit_progress(msg: str):
        _sh.sio.emit("automation_progress", {"test_id": test_id, "msg": msg})
        _log(f"[auto] {msg}")

    def _psu_outputs_off(handles):
        """Turn off the output of every PSU channel handle."""
        for h in handles:
            try:
                for _act, scpi in get_command(h["fam"], "output_off", ch=h["ch"]):
                    if _act == "write":
                        h["res"].write(scpi)
            except Exception:
                pass

    def _psu_local_mode(handles):
        """Restore front-panel control on all PSU handles."""
        seen = set()
        for h in handles:
            res = h["res"]
            if id(res) in seen:
                continue
            seen.add(id(res))
            # Serial (ASRL) instruments — e.g. the Keithley 2231A via its
            # Prolific USB adapter — need an explicit SYSTem:REMote before
            # they'll accept commands again (sent once at connect time; see
            # connection.py). Unlike GPIB/USBTMC, SYSTem:LOCal over ASRL
            # fully drops the instrument out of remote and nothing re-arms
            # it, so the app loses control until the user reconnects. Skip
            # relinquishing remote control for these so later tests keep
            # working; the trade-off is the front panel stays locked out
            # until the next connect/disconnect.
            if str(getattr(res, "resource_name", "")).upper().startswith("ASRL"):
                continue
            try:
                for _act, scpi in get_command(h["fam"], "local_mode"):
                    if _act == "write":
                        res.write(scpi)
            except Exception:
                pass
            # Note: res.clear() (viClear) is intentionally omitted — on some
            # USB instruments (e.g. Keithley 2200) it triggers endpoint
            # re-enumeration and invalidates the PyVISA session for the rest
            # of the process lifetime.  The USBTMC read patch already handles
            # stuck buffers without needing a device-level clear.

    def _done(rows, columns, error=None):
        global _auto_running
        _auto_running = False
        # Auto-save CSV to output directory
        csv_path = None
        if rows and columns and not error:
            try:
                ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
                csv_path = out_dir / f"{test_id}_{ts}.csv"
                with open(csv_path, "w", newline="") as f:
                    import csv as _csv
                    w = _csv.writer(f)
                    w.writerow(columns)
                    w.writerows(rows)
                _log(f"[auto] saved CSV → {csv_path}")
            except Exception as exc:
                _log(f"[auto] CSV save failed: {exc}")
        _sh.sio.emit("automation_done", {
            "test_id":  test_id, "columns": columns,
            "rows":     rows,    "error":   error,
            "csv_path": str(csv_path) if csv_path else None,
        })
        # Resume PSU polling only if user is on the Workbench tab
        if _sh._state.get("connected") and _sh._polling_enabled:
            _start_polling()

    def _pause_point():
        """Block here between run-steps while paused; returns immediately if stopped."""
        while _auto_pause.is_set() and not _auto_stop.is_set():
            time.sleep(0.05)

    def _run_ac_sweep():
        freq_start  = float(params.get("freq_start",  100))
        freq_stop   = float(params.get("freq_stop",   100000))
        n_pts       = max(2, int(params.get("num_points", 20)))
        amplitude   = float(params.get("amplitude",   1.0))
        settle_time = float(params.get("settle_time", 0.2))

        awg_res,   awg_fam   = _find_instrument("awg")
        scope_res, scope_fam = _find_instrument("scope")
        if awg_res is None:
            _done([], [], "AWG not connected"); return

        use_scope = scope_res is not None
        sel_raw = params.get("scope_measures", "vpp_ch1,vpp_ch2,freq_ch1") if use_scope else ""
        sel = set(s.strip() for s in sel_raw.split(",") if s.strip())

        cols = ["set_freq_Hz"]
        if use_scope:
            if "freq_ch1" in sel: cols.append("meas_freq_Hz")
            if "vpp_ch1"  in sel: cols.append("vpp_ch1_V")
            if "vpp_ch2"  in sel: cols.append("vpp_ch2_V")
        rows = []

        # logarithmically spaced frequencies
        import math
        freqs = [freq_start * (freq_stop / freq_start) ** (i / (n_pts - 1))
                 for i in range(n_pts)]

        # Configure AWG: sine wave, fixed amplitude
        try:
            _op(awg_res, awg_fam, "set_function",       role="awg", ch=1, func="SIN")
            _op(awg_res, awg_fam, "set_amplitude",      role="awg", ch=1, amp=f"{amplitude:.4f}")
            _op(awg_res, awg_fam, "set_amplitude_unit", role="awg", ch=1, unit="VPP")
            _op(awg_res, awg_fam, "output_on",          role="awg", ch=1)
        except Exception as exc:
            _done([], cols, f"AWG setup failed: {exc}"); return

        if use_scope and sel:
            enable_pairs = []
            if "vpp_ch1"  in sel: enable_pairs.append(("measure_vpp",  1))
            if "vpp_ch2"  in sel: enable_pairs.append(("measure_vpp",  2))
            if "freq_ch1" in sel: enable_pairs.append(("measure_freq", 1))
            _scope_enable_measures(scope_res, scope_fam, enable_pairs)

        suffix = "" if use_scope else " (no scope — set frequency only)"
        _emit_progress(
            f"Sweep: {n_pts} pts  {freq_start:.0f}–{freq_stop:.0f} Hz  {amplitude} Vpp  settle={settle_time}s{suffix}")

        for i, freq in enumerate(freqs):
            if _auto_stop.is_set():
                _emit_progress("Stopped by user"); break
            try:
                _op(awg_res, awg_fam, "set_frequency", role="awg", ch=1, freq=f"{freq:.6g}")
                time.sleep(settle_time)
                row = [round(freq, 3)]
                if use_scope:
                    def _r(v): return round(v, 6) if v is not None else None
                    if "freq_ch1" in sel:
                        row.append(_r(_scope_query_only(scope_res, scope_fam, "measure_freq", ch=1)))
                    if "vpp_ch1" in sel:
                        row.append(_r(_scope_query_only(scope_res, scope_fam, "measure_vpp",  ch=1)))
                    if "vpp_ch2" in sel:
                        row.append(_r(_scope_query_only(scope_res, scope_fam, "measure_vpp",  ch=2)))
                rows.append(row)
                _sh.sio.emit("automation_row", {"test_id": test_id, "row": row,
                                                "columns": cols,
                                                "progress": (i + 1) / n_pts})
            except Exception as exc:
                _log(f"[auto] step {i+1} error: {exc}")

        try: _op(awg_res, awg_fam, "output_off", role="awg", ch=1)
        except Exception: pass
        _done(rows, cols)

    def _run_dc_sweep():
        num_ch     = max(1, int(params.get("num_channels", 1)))
        sweep_mode = str(params.get("sweep_mode", "simultaneous"))

        # ── Parse measurement / action items ─────────────────────────────────
        raw_meas = params.get("measurements", "[]")
        try:
            meas_items = _json.loads(raw_meas) if isinstance(raw_meas, str) else []
        except Exception:
            meas_items = []
        if not meas_items:
            meas_items = [{"kind": "measurement", "instrument": "psu",
                           "measure": "voltage", "samples": 1, "settle": 0.1}]

        # ── Per-channel configs ──────────────────────────────────────────────
        ch_cfgs = []
        for n in range(num_ch):
            ch_cfgs.append({
                "resource":     str(params.get(f"ch{n}_resource", "")),
                "ch":           max(1, int(params.get(f"ch{n}_ch",      n + 1))),
                "v_start":      float(params.get(f"ch{n}_v_start", 0)),
                "v_step":       abs(float(params.get(f"ch{n}_v_step",  0.1))) or 0.1,
                "v_stop":       float(params.get(f"ch{n}_v_stop",  5)),
                "v_list":       str(params.get(f"ch{n}_v_list", "")).strip(),
                "i_limit":      float(params.get(f"ch{n}_i_limit", 0.5)),
                "settle":       float(params.get(f"ch{n}_settle",  0.1)),
                "sweep_type":   str(params.get(f"ch{n}_sweep_type", "voltage")),
                "v_compliance": float(params.get(f"ch{n}_v_compliance", 5.0)),
            })

        # ── Resolve per-channel instrument handles ───────────────────────────
        ch_handles = []
        for n, cfg in enumerate(ch_cfgs):
            res = _sh._state["resources"].get(cfg["resource"])
            fam = _sh._state["families"].get(cfg["resource"])
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
            if cfg.get("v_list"):
                return [float(x) for x in cfg["v_list"].split(",") if x.strip() != ""]
            n = round(abs(cfg["v_stop"] - cfg["v_start"]) / cfg["v_step"])
            s = 1 if cfg["v_stop"] >= cfg["v_start"] else -1
            return [round(cfg["v_start"] + k * s * cfg["v_step"], 10)
                    for k in range(n + 1)]

        def _set_v(handle, v):
            cfg = handle["cfg"]
            if cfg["sweep_type"] == "current":
                try:
                    _run_steps(handle["res"], get_command(
                        handle["fam"], "output_off", ch=cfg["ch"]))
                except Exception:
                    pass
                if v == 0:
                    return
                try:
                    _run_steps(handle["res"], get_command(
                        handle["fam"], "set_current_limit",
                        ch=cfg["ch"], value=f"{v:.6f}"))
                    _run_steps(handle["res"], get_command(
                        handle["fam"], "output_on", ch=cfg["ch"]))
                except Exception as exc:
                    _log(f"[auto] CC set_v({v:.6f} A) failed: {exc}")
            else:
                _run_steps(handle["res"], get_command(
                    handle["fam"], "set_voltage",
                    ch=cfg["ch"], value=f"{v:.6f}"))

        def _setup_ch(handle):
            cfg = handle["cfg"]
            if cfg["sweep_type"] == "current":
                _run_steps(handle["res"], get_command(
                    handle["fam"], "set_voltage",
                    ch=cfg["ch"], value=f"{cfg['v_compliance']:.4f}"))
            else:
                _run_steps(handle["res"], get_command(
                    handle["fam"], "set_current_limit",
                    ch=cfg["ch"], value=f"{cfg['i_limit']:.4f}"))
                _run_steps(handle["res"], get_command(
                    handle["fam"], "output_on", ch=cfg["ch"]))

        def _teardown_ch(handle):
            try:
                cfg = handle["cfg"]
                if cfg["sweep_type"] == "current":
                    _run_steps(handle["res"], get_command(
                        handle["fam"], "set_current_limit",
                        ch=cfg["ch"], value="0.0"))
                else:
                    _run_steps(handle["res"], get_command(
                        handle["fam"], "set_voltage",
                        ch=cfg["ch"], value="0.0"))
                _run_steps(handle["res"], get_command(
                    handle["fam"], "output_off", ch=cfg["ch"]))
            except Exception:
                pass

        def _emit_row(row, cols, progress):
            _sh.sio.emit("automation_row", {"test_id": test_id, "row": row,
                                            "columns": cols, "progress": progress})

        # ── Per-item execution ────────────────────────────────────────────────
        _MEAS_MAP = {
            "voltage":   ("measure_voltage", "measure_vdc",      None,                "V"),
            "current":   ("measure_current", "measure_idc",      None,                "I"),
            "vac":       (None,              "measure_vac",      None,                "Vac"),
            "r":         (None,              "measure_r",        None,                "R"),
            "r4w":       (None,              "measure_r4w",      None,                "R4w"),
            "vpp":       (None,              None,               "measure_vpp",       "Vpp"),
            "vrms":      (None,              None,               "measure_vrms",      "Vrms"),
            "freq":      (None,              None,               "measure_freq",      "Freq"),
            "duty":      (None,              None,               "measure_dutycycle", "Duty"),
            "rise":      (None,              None,               "measure_risetime",  "Rise"),
            "fall":      (None,              None,               "measure_falltime",  "Fall"),
            "overshoot": (None,              None,               "measure_overshoot", "Ovs"),
            "period":    (None,              None,               "measure_period",    "Per"),
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

                use_ext = instr_id not in ("psu", "psu_outer", "")
                if use_ext:
                    res = _sh._state["resources"].get(instr_id)
                    fam = _sh._state["families"].get(instr_id)
                    wb_instr = next(
                        (i for i in (_sh._state.get("workbench") or {}).get("_unique", [])
                         if i.get("resource") == instr_id), None)
                    instr_type = (wb_instr.get("type", "dmm") if wb_instr else "dmm")
                elif instr_id == "psu_outer" and len(ch_handles) > 1:
                    res = ch_handles[1]["res"]
                    fam = ch_handles[1]["fam"]
                    instr_type = "psu"
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

                        scope_stopped = False
                        try:
                            _run_steps(scope_res, get_command(scope_fam, "stop"))
                            scope_stopped = True
                            time.sleep(0.1)
                        except Exception:
                            pass

                        orig_timeout = scope_res.timeout
                        orig_chunk   = getattr(scope_res, "chunk_size", None)
                        try:
                            scope_res.timeout    = 20_000
                            scope_res.chunk_size = _sh._SCREENSHOT_CHUNK_SIZE
                            for s in pre:
                                scope_res.write(s)
                            scope_res.write(cmd_)
                            time.sleep(0.1)
                            data = scope_res.read_raw()
                            for s in post:
                                try: scope_res.write(s)
                                except Exception: pass
                        finally:
                            try: scope_res.timeout = orig_timeout
                            except Exception: pass
                            if orig_chunk is not None:
                                try: scope_res.chunk_size = orig_chunk
                                except Exception: pass
                            if scope_stopped:
                                try:
                                    _run_steps(scope_res, get_command(scope_fam, "run"))
                                except Exception:
                                    pass

                        if not data:
                            _log("[auto] capture: scope returned empty data")
                            return None

                        ext = ".bin"
                        for magic, e in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
                            idx = data.find(magic)
                            if idx != -1:
                                data = data[idx:]; ext = e; break

                        ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
                        step   = step_ctx.get("step", 0)
                        prefix = (item.get("filename_prefix") or "sweep").strip() or "sweep"
                        fname  = f"{prefix}_{step:04d}_{ts}{ext}"
                        fpath  = out_dir / fname
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
                    res = _sh._state["resources"].get(rstr)
                    if res is None and rstr:
                        _log(f"[auto] SCPI query: instrument '{rstr}' not connected")
                        return None
                    if res is None:
                        res = next(iter(_sh._state["resources"].values()), None)
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
            """Run every measurement/action item; return list of values."""
            level_changed = step_ctx.get("_level_changed", "inner")
            results = []
            for item in meas_items:
                trigger = (item.get("trigger") or "every").strip().lower()
                if trigger != "every" and trigger != level_changed:
                    if not (trigger == "mid" and level_changed.startswith("mid")):
                        results.append(None)
                        continue
                results.append(_exec_item(item, step_ctx))
            return results

        # ── Build result columns ──────────────────────────────────────────────
        meas_cols = [_auto_label(item, i) for i, item in enumerate(meas_items)]
        rows = []

        # ════ SIMULTANEOUS (or single channel) ═══════════════════════════════
        if sweep_mode == "simultaneous" or num_ch == 1:
            vols_all = [_make_vols(h["cfg"]) for h in ch_handles]
            total    = max(len(v) for v in vols_all)

            def _set_col(n):
                return "i_set_A" if ch_cfgs[n]["sweep_type"] == "current" else "v_set_V"

            if num_ch == 1:
                cols = [_set_col(0)] + meas_cols
            else:
                cols = [f"ch{n + 1}_{_set_col(n)}" for n in range(num_ch)] + meas_cols

            _emit_progress(f"DC Sweep — {num_ch} ch simultaneous: {total} steps")
            try:
                for h in ch_handles:
                    _setup_ch(h)
            except Exception as exc:
                _done([], cols, f"Setup failed: {exc}"); return

            try:
                for k in range(total):
                    _pause_point()
                    if _auto_stop.is_set():
                        _emit_progress("Stopped by user"); break
                    try:
                        v_sets = {}
                        for n, h in enumerate(ch_handles):
                            v = vols_all[n][min(k, len(vols_all[n]) - 1)]
                            _set_v(h, v)
                            v_sets[f"ch{n + 1}"] = round(v, 6)
                        time.sleep(ch_cfgs[0]["settle"])

                        step_ctx  = {"step": k + 1, **v_sets}
                        meas_vals = _exec_all(step_ctx)

                        if num_ch == 1:
                            row = [v_sets["ch1"]] + meas_vals
                        else:
                            row = [v_sets[f"ch{n + 1}"] for n in range(num_ch)] + meas_vals

                        rows.append(row)
                        _emit_row(row, cols, (k + 1) / total)
                    except Exception as exc:
                        _log(f"[auto] step {k + 1}: {exc}")
            finally:
                for h in ch_handles:
                    _teardown_ch(h)
                _psu_local_mode(ch_handles)

        # ════ NESTED ═════════════════════════════════════════════════════════
        elif sweep_mode == "nested" and num_ch >= 2:

            def _nest_name(n):
                """n=0 is inner (fastest), n=num_ch-1 is outer (slowest)."""
                if n == 0:             return "inner"
                if n == num_ch - 1:   return "outer"
                mid_n = n
                if num_ch == 3:       return "mid"
                return f"mid{mid_n}"

            vols_per_ch         = [_make_vols(h["cfg"]) for h in ch_handles]
            vols_outer_first    = list(reversed(vols_per_ch))
            handles_outer_first = list(reversed(ch_handles))

            total = 1
            for v in vols_per_ch:
                total *= len(v)

            def _nest_col(n):
                pfx = "i" if ch_cfgs[n]["sweep_type"] == "current" else "v"
                sfx = "A" if ch_cfgs[n]["sweep_type"] == "current" else "V"
                return f"{pfx}_{_nest_name(n)}_{sfx}"

            cols = [_nest_col(n) for n in reversed(range(num_ch))] + meas_cols

            shape_str = "×".join(str(len(v)) for v in vols_outer_first)
            _emit_progress(
                f"DC Sweep — nested {num_ch}-ch  {shape_str} = {total} pts")
            try:
                for h in ch_handles:
                    _setup_ch(h)
            except Exception as exc:
                _done([], cols, f"Setup failed: {exc}"); return

            step       = 0
            prev_combo = None

            try:
                for combo in itertools.product(*vols_outer_first):
                    _pause_point()
                    if _auto_stop.is_set():
                        _emit_progress("Stopped by user"); break

                    if prev_combo is None:
                        first_changed = 0
                    else:
                        first_changed = next(
                            (i for i, (a, b) in enumerate(zip(combo, prev_combo))
                             if a != b),
                            num_ch - 1)

                    for i in range(first_changed, num_ch):
                        h = handles_outer_first[i]
                        _set_v(h, combo[i])
                        time.sleep(h["cfg"]["settle"])

                    prev_combo = combo
                    step += 1

                    step_ctx = {"step": step}
                    for i, v in enumerate(reversed(combo)):
                        step_ctx[f"ch{i + 1}"] = round(v, 6)

                    step_ctx["_level_changed"] = _nest_name(num_ch - 1 - first_changed)

                    try:
                        meas_vals = _exec_all(step_ctx)
                    except Exception as exc:
                        _log(f"[auto] step {step}: {exc}")
                        meas_vals = [None] * len(meas_items)
                    row = [round(v, 6) for v in combo] + meas_vals
                    rows.append(row)
                    _emit_row(row, cols, step / total)

                    if first_changed == 0 and step > 1:
                        pass
                    elif first_changed < num_ch - 1:
                        changed_name = _nest_name(num_ch - 1 - first_changed)
                        changed_v    = combo[first_changed]
                        _emit_progress(
                            f"  {changed_name} = {changed_v:.4g} V — sweeping inner…")
            finally:
                for h in ch_handles:
                    _teardown_ch(h)
                _psu_local_mode(ch_handles)

        else:
            _done([], [], f"Unknown sweep_mode {sweep_mode!r}"); return

        _done(rows, cols)

    def _run_dmm_logger():
        interval = float(params.get("interval", 1.0))
        duration = float(params.get("duration", 60.0))
        infinite = duration <= 0

        res_str   = str(params.get("dmm_resources", "")).strip()
        mode_str  = str(params.get("dmm_modes",     "vdc")).strip()
        label_str = str(params.get("dmm_labels",    "")).strip()
        itype_str = str(params.get("dmm_itypes",    "")).strip()
        ch_str    = str(params.get("dmm_channels",  "")).strip()

        resources = [r.strip() for r in res_str.split("|")   if r.strip()]
        modes     = [m.strip() for m in mode_str.split("|")  if m.strip()]
        labels    = [l.strip() for l in label_str.split("|") if l.strip()]
        itypes    = [t.strip() for t in itype_str.split("|") if t.strip()]
        ch_nums   = [int(c)    for c in ch_str.split("|")    if c.strip()]

        if not resources:
            wb_fb = _sh._state.get("workbench", {})
            for instr in wb_fb.get("_unique", []):
                if instr.get("type") == "dmm" and _sh._state["resources"].get(
                        instr.get("resource", "")):
                    resources = [instr["resource"]]
                    break
            if not resources:
                _done([], [], "DMM not connected"); return
            modes   = [str(params.get("mode", "vdc"))]
            labels  = ["DMM"]
            itypes  = ["dmm"]
            ch_nums = [1]

        handles = []
        for i, res_name in enumerate(resources):
            mode  = modes[i]  if i < len(modes)  else "vdc"
            label = labels[i] if i < len(labels) else f"CH{i+1}"
            itype = itypes[i] if i < len(itypes) else "dmm"
            ch    = ch_nums[i] if i < len(ch_nums) else 1
            op    = PSU_LOGGER_OPS.get(mode) if itype == "psu" else DMM_OPS.get(mode)
            if not op:
                continue
            res = _sh._state["resources"].get(res_name)
            fam = _sh._state["families"].get(res_name)
            if res is None or fam is None:
                continue
            handles.append({"res": res, "fam": fam, "op": op,
                             "label": label, "ch": ch, "itype": itype,
                             "mode_key": mode})

        if not handles:
            _done([], [], "No valid channels configured"); return

        cols       = ["elapsed_s"] + [h["label"] for h in handles]
        rows       = []
        n_ch       = len(handles)
        resolution = _res_for_interval(interval)
        n_desc     = "∞" if infinite else f"{duration:.3g}s"
        _emit_progress(
            f"DMM logger: {n_desc} @ {interval:.2g}s interval, "
            f"{n_ch} channel{'s' if n_ch > 1 else ''}, res={resolution}"
        )

        t0 = time.time()
        i  = 0
        while True:
            iter_t = time.time()
            if _auto_stop.is_set():
                _emit_progress("Stopped by user"); break
            vals = []
            for h in handles:
                try:
                    res_kw = resolution if h["itype"] == "dmm" else "SLOW"
                    raw    = _run_steps(h["res"],
                                        get_command(h["fam"], h["op"],
                                                    ch=h["ch"], resolution=res_kw))
                    val = round(float(raw), 8) if raw is not None else None
                except Exception as exc:
                    _log(f"[auto] DMM sample {i+1} {h['label']}: {exc}")
                    val = None
                vals.append(val)
            elapsed = round(time.time() - t0, 3)
            row     = [elapsed] + vals
            rows.append(row)
            progress = -1 if infinite else min(elapsed / duration, 1.0)
            _sh.sio.emit("automation_row", {
                "test_id": test_id, "row": row, "columns": cols,
                "progress": progress,
            })
            i += 1
            if not infinite and (time.time() - t0) >= duration:
                break
            remaining = interval - (time.time() - iter_t)
            deadline  = time.time() + remaining
            while time.time() < deadline:
                if _auto_stop.is_set():
                    break
                time.sleep(min(0.05, deadline - time.time()))

        _done(rows, cols)

    def _run_psu_logger():
        interval = float(params.get("interval", 0.5))
        duration = float(params.get("duration", 0.0))
        infinite = duration <= 0
        action   = str(params.get("action", "set_and_run"))
        do_set   = action in ("set", "set_and_run")
        do_run   = action in ("run", "set_and_run")
        keep_power_on = bool(params.get("keep_power_on", False))

        res_str   = str(params.get("psu_resources", "")).strip()
        label_str = str(params.get("psu_labels",    "")).strip()
        ch_str    = str(params.get("psu_channels",  "")).strip()
        v_str     = str(params.get("psu_voltages",  "")).strip()
        ilim_str  = str(params.get("psu_ilimits",   "")).strip()

        resources = [r.strip() for r in res_str.split("|") if r.strip()]
        labels    = [l.strip() for l in label_str.split("|")]
        ch_nums   = [int(c) if c.strip() else 1 for c in ch_str.split("|")]
        voltages  = [v.strip() for v in v_str.split("|")]
        ilimits   = [i.strip() for i in ilim_str.split("|")]

        if not resources:
            _done([], [], "No PSU channels configured"); return

        handles = []
        for i, res_name in enumerate(resources):
            res = _sh._state["resources"].get(res_name)
            fam = _sh._state["families"].get(res_name)
            if res is None or fam is None:
                continue
            handles.append({
                "res": res, "fam": fam,
                "ch":    ch_nums[i]  if i < len(ch_nums)  else 1,
                "label": labels[i]   if i < len(labels)   else f"CH{i+1}",
                "v":     voltages[i] if i < len(voltages) else "",
                "ilim":  ilimits[i]  if i < len(ilimits)  else "",
            })

        if not handles:
            _done([], [], "No valid channels configured"); return

        # ── Apply voltage / current-limit setpoints and enable outputs ──────────
        if do_set:
            for h in handles:
                try:
                    if h["ilim"]:
                        _run_steps(h["res"], get_command(
                            h["fam"], "set_current_limit", ch=h["ch"], value=h["ilim"]))
                    if h["v"]:
                        _run_steps(h["res"], get_command(
                            h["fam"], "set_voltage", ch=h["ch"], value=h["v"]))
                        _run_steps(h["res"], get_command(
                            h["fam"], "output_on", ch=h["ch"]))
                except Exception as exc:
                    _emit_progress(f"  ⚠ {h['label']} setpoint: {exc}")

        if not do_run:
            _emit_progress(f"Setpoints applied to {len(handles)} channel(s)")
            _done([], [])
            return

        cols = ["elapsed_s"]
        for h in handles:
            cols += [f"{h['label']}_V", f"{h['label']}_I", f"{h['label']}_P"]
        rows   = []
        n_ch   = len(handles)
        n_desc = "∞" if infinite else f"{duration:.3g}s"
        _emit_progress(
            f"PSU logger: {n_desc} @ {interval:.2g}s interval, "
            f"{n_ch} channel{'s' if n_ch > 1 else ''}"
        )

        t0 = time.time()
        try:
            while True:
                iter_t = time.time()
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break
                vals = []
                for h in handles:
                    v = i_ = None
                    try:
                        raw_v = _run_steps(h["res"], get_command(
                            h["fam"], "measure_voltage", ch=h["ch"]))
                        v = round(float(raw_v), 8) if raw_v is not None else None
                    except Exception as exc:
                        _log(f"[auto] PSU logger V {h['label']}: {exc}")
                    try:
                        raw_i = _run_steps(h["res"], get_command(
                            h["fam"], "measure_current", ch=h["ch"]))
                        i_ = round(float(raw_i), 8) if raw_i is not None else None
                    except Exception as exc:
                        _log(f"[auto] PSU logger I {h['label']}: {exc}")
                    # Power = V × I — not queried, since not every PSU family
                    # exposes a MEAS:POW? command and the product is exact anyway.
                    p = round(v * i_, 8) if v is not None and i_ is not None else None
                    vals += [v, i_, p]
                elapsed = round(time.time() - t0, 3)
                row     = [elapsed] + vals
                rows.append(row)
                progress = -1 if infinite else min(elapsed / duration, 1.0)
                _sh.sio.emit("automation_row", {
                    "test_id": test_id, "row": row, "columns": cols,
                    "progress": progress,
                })
                if not infinite and (time.time() - t0) >= duration:
                    break
                remaining = interval - (time.time() - iter_t)
                deadline  = time.time() + remaining
                while time.time() < deadline:
                    if _auto_stop.is_set():
                        break
                    time.sleep(min(0.05, deadline - time.time()))
        finally:
            if not keep_power_on:
                _psu_outputs_off(handles)
            _psu_local_mode(handles)

        _done(rows, cols)

    def _run_psu_interrupt():
        import math as _math

        ch            = max(1, int(params.get("channel",       1)))
        v1            = float(params.get("v1",                 5.0))
        t1_ms         = float(params.get("t1",                 500))
        v2_mode       = str(params.get("v2_mode",              "off"))
        v2            = float(params.get("v2",  0.0)) if v2_mode != "off" else None
        do_v2_sweep   = (v2_mode != "off" and
                         str(params.get("v2_sweep", "no")).lower() == "yes")
        v2_start_val  = float(params.get("v2_start",           0.0))
        v2_stop_val   = float(params.get("v2_stop",            3.0))
        v2_step_val   = max(0.001, float(params.get("v2_step", 0.5)))
        t2_single     = float(params.get("t2",                 100))
        do_sweep      = str(params.get("t2_sweep",    "no")).lower() == "yes"
        t2_start      = float(params.get("t2_start",           10))
        t2_stop       = float(params.get("t2_stop",            500))
        t2_step       = max(1.0, float(params.get("t2_step",   10)))
        v3            = float(params.get("v3", v1))
        t3_ms_default = float(params.get("t3",                 500))
        total_time    = float(params.get("total_time",         0))
        i_limit       = float(params.get("current_limit",      0.5))
        scope_ch         = max(1, int(params.get("scope_channel",      1)))
        scope_scale      = float(params.get("scope_scale",            0) or 0)
        scope_slope      = str(params.get("scope_slope",      "falling")).lower()
        slope_scpi       = "POS" if scope_slope == "rising" else "NEG"
        scope_horiz_pos  = str(params.get("scope_horiz_pos",    "yes")).lower() == "yes"
        scope_meas_str   = str(params.get("scope_measurements", "vmax,vmin,nwidth"))

        _MEAS_MAP = {
            "vmax":   ("measure_vmax",     "scope_vmax_V"),
            "vmin":   ("measure_vmin",     "scope_vmin_V"),
            "nwidth": ("measure_nwidth",   "scope_nwidth_s"),
            "pwidth": ("measure_pwidth",   "scope_pwidth_s"),
            "vpp":    ("measure_vpp",      "scope_vpp_V"),
            "vrms":   ("measure_vrms",     "scope_vrms_V"),
            "rise":   ("measure_risetime", "scope_rise_s"),
            "fall":   ("measure_falltime", "scope_fall_s"),
            "duty":   ("measure_dutycycle","scope_duty_pct"),
        }
        selected_meas = [m.strip() for m in scope_meas_str.split(",")
                         if m.strip() in _MEAS_MAP]

        def _ms_steps(start, stop, step):
            step = abs(step)
            n    = round(abs(stop - start) / step)
            sign = 1 if stop >= start else -1
            return [round(start + i * sign * step, 6) for i in range(n + 1)]

        t2_list  = _ms_steps(t2_start, t2_stop, t2_step) if do_sweep else [t2_single]
        if do_v2_sweep:
            v2_list = _ms_steps(v2_start_val, v2_stop_val, v2_step_val)
        elif v2 is not None:
            v2_list = [v2]
        else:
            v2_list = [None]

        def _t3(t2):
            if total_time > 0:
                return max(0.0, total_time - t1_ms - t2)
            return t3_ms_default

        def _nice_time(s):
            if s <= 0:
                return 1e-3
            exp = _math.floor(_math.log10(s))
            m   = s / 10 ** exp
            for step in (1, 2, 5, 10):
                if m <= step:
                    return step * 10 ** exp
            return 10 * 10 ** exp

        psu_rstr = params.get("psu_resource", "")
        if psu_rstr and psu_rstr in _sh._state["resources"]:
            psu_res = _sh._state["resources"][psu_rstr]
            psu_fam = _sh._state["families"].get(psu_rstr)
        else:
            psu_res, psu_fam = _find_instrument("psu")
        if psu_res is None:
            _done([], [], "PSU not connected"); return

        scope_res, scope_fam = _find_instrument("scope")

        # ── Scope setup ───────────────────────────────────────────────────────
        scope_win = 0.0
        if scope_res is not None and scope_fam is not None:
            v2_val  = min((x for x in v2_list if x is not None), default=0.0)
            trig_lv = (v1 + v2_val) / 2.0
            max_t2  = max(t2_list)
            max_t3  = _t3(max_t2)
            total_s = (t1_ms + max_t2 + max_t3) / 1000.0 * 1.1
            tpd     = scope_scale if scope_scale > 0 else _nice_time(total_s / 12)
            scope_win = tpd * 12
            position  = scope_win / 2.0 - t1_ms / 1000.0

            _all_v = [v1, v3, 0.0]
            if v2_mode == "voltage":
                _all_v += [x for x in v2_list if x is not None]
            v_lo     = min(_all_v)
            v_hi     = max(_all_v)
            v_span   = max(v_hi - v_lo, 0.1)
            v_center = (v_hi + v_lo) / 2.0
            _VDIV    = [1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3,
                        0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0]
            vdiv = next((v for v in _VDIV if v >= v_span / 6.0), _VDIV[-1])

            try:
                def _sw(op, **kw):
                    for act, scpi in get_command(scope_fam, op, **kw):
                        if act == "write":
                            scope_res.write(scpi)

                try: _sw("trigger_mode")
                except KeyError: pass
                _sw("trigger_source",  ch=scope_ch)
                _sw("trigger_slope",   slope=slope_scpi)
                _sw("trigger_level",   value=f"{trig_lv:.4f}")
                _sw("timebase_scale",  value=f"{tpd:.6e}")
                if scope_horiz_pos:
                    _sw("timebase_position", value=f"{position:.6e}")
                try: _sw("channel_scale",  ch=scope_ch, value=f"{vdiv:.6e}")
                except KeyError: pass
                try: _sw("channel_offset", ch=scope_ch, value=f"{v_center:.4f}")
                except KeyError: pass
                _scope_enable_measures(
                    scope_res, scope_fam,
                    [(_MEAS_MAP[m][0], scope_ch) for m in selected_meas],
                )
                _emit_progress(
                    f"Scope configured: trig {slope_scpi} @ {trig_lv:.3f} V, "
                    f"{tpd * 1000:.3g} ms/div on CH{scope_ch}, "
                    f"window {scope_win * 1000:.0f} ms"
                    + (f", offset {position * 1000:.1f} ms" if scope_horiz_pos else "") +
                    f" | CH{scope_ch} {vdiv * 1000:.4g} mV/div, centre {v_center:.3f} V"
                    + (f" | meas: {', '.join(selected_meas)}" if selected_meas else "")
                )
            except Exception as exc:
                _emit_progress(
                    f"[warn] Scope setup failed: {exc} — continuing without scope")
                scope_res = None

        # ── PSU init ──────────────────────────────────────────────────────────
        try:
            _run_steps(psu_res, get_command(psu_fam, "reset"))
            time.sleep(1.0)
            _run_steps(psu_res, get_command(psu_fam, "set_current_limit",
                                            ch=ch, value=f"{i_limit:.4f}"))
            _run_steps(psu_res, get_command(psu_fam, "set_voltage",
                                            ch=ch, value=f"{v1:.6f}"))
            _run_steps(psu_res, get_command(psu_fam, "output_on", ch=ch))
            time.sleep(0.5)
        except Exception as exc:
            _done([], [], f"PSU init failed: {exc}"); return

        total_runs = len(t2_list) * len(v2_list)
        _emit_progress(
            f"Starting {total_runs} run(s): "
            f"V1={v1:.3f} V, interrupt="
            f"{'off' if v2 is None else 'voltage sweep' if do_v2_sweep else f'{v2:.3f} V'}, "
            f"V3={v3:.3f} V"
        )

        cols = ["run", "t2_ms"]
        if v2_mode == "voltage":
            cols.append("v2_set_V")
        cols += ["v1_meas_V", "v1_meas_A",
                 "v2_meas_V", "v2_meas_A",
                 "v3_meas_V", "v3_meas_A"]
        if scope_res is not None:
            cols += [_MEAS_MAP[m][1] for m in selected_meas]
        cols.append("screenshot")
        rows = []

        def _take_screenshot_app(run_num, t2_ms, run_v2=None):
            if scope_res is None:
                return ''
            try:
                steps = get_command(scope_fam, "screenshot")
            except KeyError:
                return ''
            raw_idx = next(
                (i for i, (a, _) in enumerate(steps) if a == "raw_query"), None)
            if raw_idx is None:
                return ''
            pre  = [(a, s) for a, s in steps[:raw_idx]     if a == "write"]
            cmd_ = steps[raw_idx][1]
            post = [(a, s) for a, s in steps[raw_idx + 1:] if a == "write"]

            with _rlock(scope_res):
                scope_stopped = False
                try:
                    _run_steps(scope_res, get_command(scope_fam, "stop"))
                    scope_stopped = True
                    time.sleep(0.1)
                except Exception:
                    pass
                orig_to = scope_res.timeout
                try:
                    scope_res.timeout = 35_000
                    for _, s in pre:
                        scope_res.write(s)
                    scope_res.write(cmd_)
                    time.sleep(0.1)
                    orig_chunk = getattr(scope_res, 'chunk_size', None)
                    try:
                        scope_res.chunk_size = _sh._SCREENSHOT_CHUNK_SIZE
                        data = scope_res.read_raw()
                    finally:
                        if orig_chunk is not None:
                            try: scope_res.chunk_size = orig_chunk
                            except Exception: pass
                    for _, s in post:
                        try: scope_res.write(s)
                        except Exception: pass
                except Exception as exc:
                    _log(f"[psu_interrupt] screenshot error: {exc}")
                    return ''
                finally:
                    try: scope_res.timeout = orig_to
                    except Exception: pass
                    if scope_stopped:
                        try: _run_steps(scope_res, get_command(scope_fam, "run"))
                        except Exception: pass

            ext = ''
            for magic, e in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
                idx = data.find(magic)
                if idx != -1:
                    data = data[idx:]; ext = e; break

            ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            v2tag  = (f"_v2_{run_v2:.2f}V"
                      if (run_v2 is not None and do_v2_sweep) else "")
            fname  = (f"psu_interrupt_run{run_num:03d}_t2_{t2_ms:.0f}ms"
                      f"{v2tag}_{ts_str}{ext or '.bin'}")
            try:
                fpath = out_dir / fname
                fpath.write_bytes(data)
                return str(fpath)
            except Exception as exc:
                _log(f"[psu_interrupt] screenshot save error: {exc}")
                return ''

        all_runs = list(itertools.product(v2_list, t2_list))
        try:
            for run_num, (run_v2, t2_ms) in enumerate(all_runs, start=1):
                _pause_point()
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break

                t3_ms  = _t3(t2_ms)
                ch_off = run_v2 is None

                scope_arm_t = 0.0
                if scope_res is not None:
                    try:
                        run_trig_lv = (v1 + (run_v2 if run_v2 is not None else 0.0)) / 2.0
                        for act, scpi in get_command(scope_fam, "trigger_level",
                                                     value=f"{run_trig_lv:.4f}"):
                            if act == "write":
                                scope_res.write(scpi)
                    except Exception:
                        pass
                    try:
                        for act, scpi in get_command(scope_fam, "single"):
                            if act == "write":
                                scope_res.write(scpi)
                        scope_arm_t = time.monotonic()
                    except Exception:
                        pass

                _run_steps(psu_res,
                           get_command(psu_fam, "set_voltage", ch=ch, value=f"{v1:.6f}"))
                _run_steps(psu_res, get_command(psu_fam, "output_on",   ch=ch))
                v1_v = float(
                    _run_steps(psu_res,
                               get_command(psu_fam, "measure_voltage", ch=ch)) or 0)
                v1_i = float(
                    _run_steps(psu_res,
                               get_command(psu_fam, "measure_current", ch=ch)) or 0)
                time.sleep(t1_ms / 1000.0)

                if ch_off:
                    _run_steps(psu_res, get_command(psu_fam, "output_off", ch=ch))
                else:
                    _run_steps(psu_res, get_command(psu_fam, "set_voltage",
                                                    ch=ch, value=f"{run_v2:.6f}"))
                time.sleep(t2_ms / 1000.0)
                v2_v = run_v2 if run_v2 is not None else 0.0
                v2_i = None

                _run_steps(psu_res, get_command(psu_fam, "set_voltage",
                                                ch=ch, value=f"{v3:.6f}"))
                if ch_off:
                    _run_steps(psu_res, get_command(psu_fam, "output_on", ch=ch))
                time.sleep(t3_ms / 1000.0)
                v3_v = float(
                    _run_steps(psu_res,
                               get_command(psu_fam, "measure_voltage", ch=ch)) or 0)
                v3_i = float(
                    _run_steps(psu_res,
                               get_command(psu_fam, "measure_current", ch=ch)) or 0)

                if scope_arm_t > 0 and scope_win > 0:
                    gap = (scope_arm_t + scope_win + 1.5) - time.monotonic()
                    if gap > 0:
                        time.sleep(gap)

                shot = _take_screenshot_app(run_num, t2_ms, run_v2)

                scope_meas_vals = {}
                if scope_res is not None:
                    for _mkey in selected_meas:
                        _op_name, _ = _MEAS_MAP[_mkey]
                        _v = _scope_query_only(scope_res, scope_fam, _op_name,
                                               ch=scope_ch)
                        scope_meas_vals[_mkey] = (round(_v, 9)
                                                   if _v is not None else None)

                row = [run_num, round(t2_ms, 3)]
                if v2_mode == "voltage":
                    row.append(round(run_v2, 6) if run_v2 is not None else None)
                row += [round(v1_v, 6), round(v1_i, 6),
                        round(v2_v, 6), round(v2_i, 6) if v2_i is not None else None,
                        round(v3_v, 6), round(v3_i, 6)]
                row += [scope_meas_vals.get(m) for m in selected_meas]
                row.append(shot)
                rows.append(row)
                _sh.sio.emit("automation_row", {
                    "test_id": test_id, "row": row, "columns": cols,
                    "progress": run_num / len(all_runs),
                })
                _emit_progress(
                    f"Run {run_num}/{len(all_runs)}  T2={t2_ms:.1f} ms"
                    + (f"  V2={run_v2:.3f} V" if run_v2 is not None else "")
                    + f"  V1={v1_v:.4f} V  V3={v3_v:.4f} V"
                    + "".join(
                        f"  {_MEAS_MAP[m][1]}={scope_meas_vals[m]:.4g}"
                        for m in selected_meas if scope_meas_vals.get(m) is not None
                    )
                    + (f"  → {os.path.basename(shot)}" if shot else "")
                )

        except Exception as exc:
            _log(f"[psu_interrupt] error: {exc}")

        finally:
            try:
                _run_steps(psu_res, get_command(psu_fam, "set_voltage",
                                                ch=ch, value="0.0"))
                _run_steps(psu_res, get_command(psu_fam, "output_off",  ch=ch))
            except Exception:
                pass
            _psu_local_mode([{"res": psu_res, "fam": psu_fam}])

        _done(rows, cols)

    # ── Sandbox runner ────────────────────────────────────────────────────────
    def _run_sandbox():
        """Execute a user-designed sequence from the Sandbox tab."""
        try:
            sb = _json.loads(params.get("sandbox_json", "{}"))
        except Exception as exc:
            _done([], [], f"Invalid sandbox JSON: {exc}"); return

        if "steps" in sb:
            _run_sandbox_sequential(sb); return

        # ── Legacy nested-sweep model ────────────────────────────────
        loops   = sb.get("loops",   [])
        actions = sb.get("actions", [])
        if not any(ac.get("type") == "measure" for ac in actions):
            _done([], [], "Add at least one Measure action"); return

        loop_cols = [lp.get("label") or lp.get("var") or f"loop_{i+1}"
                     for i, lp in enumerate(loops)]
        meas_cols = [ac.get("label") or f"meas_{i+1}"
                     for ac in actions if ac.get("type") == "measure"]
        cols = loop_cols + meas_cols

        def _get_h(rstr, fallback="psu"):
            res = _sh._state["resources"].get(rstr)
            fam = _sh._state["families"].get(rstr)
            if res is None:
                res, fam = _find_instrument(fallback)
            return res, fam

        loop_h = []
        for lp in loops:
            res, fam = _get_h(lp.get("instrument", ""))
            if res is None:
                _done([], cols, f"No instrument for loop '{lp.get('label','')}'"); return
            loop_h.append({"res": res, "fam": fam, "lp": lp})

        act_h = [{"res": _get_h(ac.get("instrument", ""))[0],
                  "fam": _get_h(ac.get("instrument", ""))[1],
                  "ac": ac} for ac in actions]

        def _mk_range(lp):
            a, b = float(lp.get("start", 0)), float(lp.get("stop", 1))
            s    = abs(float(lp.get("step", 0.1))) or 0.1
            n, d = round(abs(b - a) / s), (1 if b >= a else -1)
            return [round(a + k * d * s, 10) for k in range(n + 1)]

        ranges = [_mk_range(lp) for lp in loops] or [()]
        total  = max(1, 1)
        for r in ranges:
            if r: total *= len(r)

        _MEAS_OPS = {
            "voltage": "measure_voltage", "current": "measure_current",
            "vac": "measure_vac", "r": "measure_r", "r4w": "measure_r4w",
            "celsius": "measure_celsius", "temperature": "measure_temperature",
            "pressure": "measure_pressure", "field": "measure_field",
            "power": "measure_power", "frequency": "measure_frequency",
            "x": "measure_x", "y": "measure_y",
        }

        def _sf(v):
            try: f = float(v); return None if abs(f) > 1e30 else round(f, 6)
            except Exception: return None

        # Setup
        try:
            for h in loop_h:
                lp, ch = h["lp"], int(h["lp"].get("ch", 1))
                if lp.get("param") == "current":
                    _run_steps(h["res"], get_command(h["fam"], "set_voltage",
                        ch=ch, value=f"{float(lp.get('v_compliance', 5.0)):.4f}"))
                else:
                    _run_steps(h["res"], get_command(h["fam"], "set_current_limit",
                        ch=ch, value=f"{float(lp.get('i_limit', 0.5)):.4f}"))
                    _run_steps(h["res"], get_command(h["fam"], "output_on", ch=ch))
        except Exception as exc:
            _done([], cols, f"Setup failed: {exc}"); return

        def _exec_action(ac, res, fam, var_map):
            """Execute a single non-measure action block (set / wait / wait_for)."""
            t = ac.get("type", "")
            if t == "wait":
                time.sleep(float(ac.get("duration", 0.1)))
            elif t == "set" and res:
                ch, par = int(ac.get("ch", 1)), ac.get("param", "voltage")
                settle  = float(ac.get("settle", 0))
                if ac.get("mode") == "sweep":
                    a, b = float(ac.get("start", 0)), float(ac.get("stop", 1))
                    s    = abs(float(ac.get("step", 0.1))) or 0.1
                    n, d = round(abs(b - a) / s), (1 if b >= a else -1)
                    vals = [round(a + k * d * s, 10) for k in range(n + 1)]
                else:
                    raw = str(ac.get("value", "0"))
                    for var, val in var_map.items():
                        raw = raw.replace("{" + var + "}", str(val))
                    try:    vals = [float(raw)]
                    except Exception: vals = []
                for v in vals:
                    try:
                        if par == "set_current_limit":
                            try: _run_steps(res, get_command(fam, "output_off", ch=ch))
                            except Exception: pass
                            _run_steps(res, get_command(fam, "set_current_limit",
                                ch=ch, value=f"{float(v):.6f}"))
                            _run_steps(res, get_command(fam, "output_on", ch=ch))
                        else:
                            cmd_spec = fam.get("commands", {}).get(par)
                            kw       = _extract_value_key(cmd_spec) if cmd_spec else "value"
                            try:    fmt = f"{float(v):.6g}"
                            except (ValueError, TypeError): fmt = str(v).upper()
                            _run_steps(res, get_command(fam, par, ch=ch, **{kw: fmt}))
                        if settle > 0: time.sleep(settle)
                    except Exception as exc:
                        _log(f"[sandbox] set: {exc}")
            elif t == "wait_for" and res:
                op       = _MEAS_OPS.get(ac.get("param", "voltage"), "measure_voltage")
                ch       = int(ac.get("ch", 1))
                target   = float(ac.get("target", 0))
                tol      = abs(float(ac.get("tolerance", 0.5)))
                cond     = ac.get("condition", ">=")
                interval = max(0.5, float(ac.get("interval", 5)))
                timeout  = float(ac.get("timeout", 0))
                t_start  = time.time()
                _emit_progress(f"[sandbox] waiting for {ac.get('label','condition')}…")
                while not _auto_stop.is_set():
                    try:
                        v = _sf(_run_steps(res, get_command(fam, op, ch=ch)))
                        if v is not None:
                            met = ((cond == ">="     and v >= target) or
                                   (cond == "<="     and v <= target) or
                                   (cond == "within" and abs(v - target) <= tol))
                            _log(f"[sandbox] wait_for: {v:.4g} (need {cond} {target})"
                                 + (" ✓" if met else ""))
                            if met: break
                    except Exception as exc:
                        _log(f"[sandbox] wait_for: {exc}")
                    if timeout > 0 and (time.time() - t_start) >= timeout:
                        _log(f"[sandbox] wait_for: timed out after {timeout}s"); break
                    slept = 0.0
                    while slept < interval and not _auto_stop.is_set():
                        time.sleep(0.5); slept += 0.5

        # Resolve step-action handles per loop
        loop_sa_h = []
        for lp in loops:
            sas = []
            for sa in lp.get("step_actions", []):
                res2, fam2 = _get_h(sa.get("instrument", ""))
                sas.append({"res": res2, "fam": fam2, "ac": sa})
            loop_sa_h.append(sas)

        def _emit_row(row, cols_, progress):
            _sh.sio.emit("automation_row", {"test_id": test_id, "row": row,
                                            "columns": cols_, "progress": progress})

        rows, step, prev_combo = [], 0, None
        try:
            for combo in itertools.product(*ranges):
                if _auto_stop.is_set():
                    _emit_progress("Stopped by user"); break
                _pause_point()
                var_map = {lp.get("var", f"loop{i+1}"): combo[i]
                           for i, lp in enumerate(loops)}

                changed = (list(range(len(loops))) if prev_combo is None
                           else [i for i, (a, b) in enumerate(zip(combo, prev_combo))
                                 if a != b])
                for i in changed:
                    h = loop_h[i]
                    lp, v, ch = h["lp"], combo[i], int(h["lp"].get("ch", 1))
                    par = lp.get("param", "voltage")
                    try:
                        if par == "current":
                            try:
                                _run_steps(h["res"],
                                           get_command(h["fam"], "output_off", ch=ch))
                            except Exception: pass
                            _run_steps(h["res"],
                                       get_command(h["fam"], "set_current_limit",
                                                   ch=ch, value=f"{v:.6f}"))
                            _run_steps(h["res"],
                                       get_command(h["fam"], "output_on", ch=ch))
                        else:
                            _run_steps(h["res"],
                                       get_command(h["fam"], "set_voltage",
                                                   ch=ch, value=f"{v:.6f}"))
                    except Exception as exc:
                        _log(f"[sandbox] loop {i}: {exc}")
                    s = float(lp.get("settle", 0.1))
                    if s > 0: time.sleep(s)
                    for sa_h in loop_sa_h[i]:
                        _exec_action(sa_h["ac"], sa_h["res"], sa_h["fam"], var_map)

                prev_combo = combo
                meas_vals  = []
                for h in act_h:
                    ac, res, fam = h["ac"], h["res"], h["fam"]
                    if ac.get("type") == "measure":
                        val = None
                        if res:
                            op  = _MEAS_OPS.get(ac.get("param", "voltage"), "measure_voltage")
                            ch  = int(ac.get("ch", 1))
                            s   = float(ac.get("settle", 0))
                            n   = max(1, int(ac.get("samples", 1)))
                            if s > 0: time.sleep(s)
                            try:
                                vs = [_sf(_run_steps(res, get_command(fam, op, ch=ch)))
                                      for _ in range(n)]
                                ns = [v for v in vs if v is not None]
                                val = round(sum(ns) / len(ns), 6) if ns else None
                            except Exception as exc:
                                _log(f"[sandbox] measure: {exc}")
                        meas_vals.append(val)
                    else:
                        _exec_action(ac, res, fam, var_map)

                step += 1
                row = list(combo) + meas_vals
                rows.append(row)
                _emit_row(row, cols, step / total)
        finally:
            for h in loop_h:
                lp, ch = h["lp"], int(h["lp"].get("ch", 1))
                try:
                    if lp.get("param") == "current":
                        _run_steps(h["res"], get_command(h["fam"], "set_current_limit",
                            ch=ch, value="0.0"))
                    else:
                        _run_steps(h["res"], get_command(h["fam"], "set_voltage",
                            ch=ch, value="0.0"))
                    _run_steps(h["res"], get_command(h["fam"], "output_off", ch=ch))
                except Exception: pass
            _psu_local_mode(loop_h)
        _done(rows, cols)

    def _run_sandbox_sequential(sb):
        """Execute the new steps-based sandbox format."""
        steps = sb.get("steps", [])
        # Nested loops are common (an inner loop wrapping the tail of an outer
        # loop's range, so both share the same end_id). When that happens the
        # innermost loop must finish a full cycle before the outer one advances,
        # so sort loops with a later start_id (the more deeply nested one) first —
        # that's the order they need to be checked in at a shared end step.
        loops_cfg = sorted(sb.get("loops", []), key=lambda l: -l.get("start_id", 0))

        meas_cols = [s.get("label") or f"meas_{i+1}"
                     for i, s in enumerate(steps) if s.get("type") == "measure"]
        if not meas_cols:
            _done([], [], "Add at least one Measure step"); return

        def _get_h(rstr):
            res = _sh._state["resources"].get(rstr)
            fam = _sh._state["families"].get(rstr)
            if res is None:
                res, fam = _find_instrument("dmm")
            return res, fam

        step_by_id = {s["id"]: (i, s) for i, s in enumerate(steps)}
        handles    = {s["id"]: _get_h(s.get("instrument", "")) for s in steps}

        _MEAS_OPS = {
            "voltage": "measure_voltage", "current": "measure_current",
            "vac": "measure_vac", "r": "measure_r", "r4w": "measure_r4w",
            "celsius": "measure_celsius", "temperature": "measure_temperature",
            "pressure": "measure_pressure", "field": "measure_field",
            "power": "measure_power", "frequency": "measure_frequency",
            "x": "measure_x", "y": "measure_y",
        }

        def _sf(v):
            try: f = float(v); return None if abs(f) > 1e30 else round(f, 6)
            except Exception: return None

        def _apply_vmap(raw, var_map):
            """Substitute {sweep}-style loop variables into a step field's raw value."""
            s = str(raw)
            for var, val in var_map.items():
                s = s.replace("{" + var + "}", str(val))
            return s

        rows, meas_row = [], {}
        loop_counters  = {l["id"]: 0 for l in loops_cfg}
        loop_sweep_val = {}   # lc_id -> current swept float value
        step_idx, safety = 0, 200_000

        def _sweep_values(cond):
            n  = max(2, int(cond.get("steps", 11)))
            a  = float(cond.get("start", 0))
            b  = float(cond.get("stop",  1))
            lg = bool(cond.get("log", False))
            if lg and a > 0 and b > 0:
                import math as _math
                return [round(a * (b / a) ** (i / (n - 1)), 9) for i in range(n)]
            return [round(a + (b - a) * i / (n - 1), 9) for i in range(n)]

        # pre-seed first sweep value for each sweep loop so iteration 0 sees it
        for _lc in loops_cfg:
            if _lc.get("condition", {}).get("type") == "sweep":
                _sv = _sweep_values(_lc["condition"])
                if _sv:
                    loop_sweep_val[_lc["id"]] = _sv[0]
                    _lc["condition"].setdefault("var", "sweep")

        def _flush_row():
            vals = [meas_row.get(c) for c in meas_cols]
            rows.append(vals)
            _sh.sio.emit("automation_row", {
                "test_id": test_id, "row": vals, "columns": meas_cols,
                "progress": -1,
            })
            meas_row.clear()

        # We need _exec_action here too; re-define minimally for sequential mode
        def _exec_action_seq(s, res, fam, var_map):
            _MEAS_OPS_L = {
                "voltage": "measure_voltage", "current": "measure_current",
                "vac": "measure_vac", "r": "measure_r", "r4w": "measure_r4w",
                "celsius": "measure_celsius", "temperature": "measure_temperature",
                "pressure": "measure_pressure", "field": "measure_field",
                "power": "measure_power", "frequency": "measure_frequency",
                "x": "measure_x", "y": "measure_y",
            }
            t = s.get("type", "")
            if t == "wait":
                time.sleep(float(_apply_vmap(s.get("duration", 0), var_map)))
            elif t == "set" and res:
                ch, par = int(s.get("ch", 1)), s.get("param", "voltage")
                settle  = float(s.get("settle", 0))
                raw     = _apply_vmap(s.get("value", "0"), var_map)
                try:
                    v = float(raw)
                    _run_steps(res, get_command(fam, par, ch=ch, value=f"{v:.6f}"))
                    if settle > 0: time.sleep(settle)
                except Exception as exc:
                    _log(f"[sandbox-seq] set: {exc}")
            elif t == "wait_for" and res:
                op      = _MEAS_OPS_L.get(s.get("param", "voltage"), "measure_voltage")
                ch      = int(s.get("ch", 1))
                target  = float(_apply_vmap(s.get("target", 0), var_map))
                tol     = abs(float(_apply_vmap(s.get("tolerance", 0.5), var_map)))
                cond    = s.get("condition", ">=")
                intv    = max(0.5, float(_apply_vmap(s.get("interval", 5), var_map)))
                timeout = float(_apply_vmap(s.get("timeout", 0), var_map))
                t_start = time.time()
                while not _auto_stop.is_set():
                    try:
                        v = _sf(_run_steps(res, get_command(fam, op, ch=ch)))
                        if v is not None:
                            met = ((cond == ">="     and v >= target) or
                                   (cond == "<="     and v <= target) or
                                   (cond == "within" and abs(v - target) <= tol))
                            if met: break
                    except Exception: pass
                    if timeout > 0 and (time.time() - t_start) >= timeout: break
                    slept = 0.0
                    while slept < intv and not _auto_stop.is_set():
                        time.sleep(0.5); slept += 0.5

        while step_idx < len(steps) and safety > 0:
            safety -= 1
            if _auto_stop.is_set(): break
            _pause_point()

            s   = steps[step_idx]
            sid = s.get("id")
            res, fam = handles.get(sid, (None, None))
            t   = s.get("type", "")

            # build var_map from all active sweep loops (supports nesting) — lets
            # "set", "wait" and "wait_for" steps reference {sweep}-style loop vars
            vmap = {}
            for lc in loops_cfg:
                c = lc.get("condition", {})
                if c.get("type") == "sweep" and lc["id"] in loop_sweep_val:
                    vmap[c.get("var", "sweep")] = loop_sweep_val[lc["id"]]

            if t == "wait":
                time.sleep(float(_apply_vmap(s.get("duration", 0), vmap)))
            elif t in ("set", "wait_for") and res:
                _exec_action_seq(s, res, fam, vmap)
            elif t == "measure" and res:
                op     = _MEAS_OPS.get(s.get("param", "voltage"), "measure_voltage")
                ch     = int(s.get("ch", 1))
                settle = float(s.get("settle", 0))
                n      = max(1, int(s.get("samples", 1)))
                if settle > 0: time.sleep(settle)
                try:
                    vs  = [_sf(_run_steps(res, get_command(fam, op, ch=ch)))
                           for _ in range(n)]
                    ns  = [v for v in vs if v is not None]
                    val = round(sum(ns) / len(ns), 6) if ns else None
                except Exception: val = None
                lbl = s.get("label") or f"meas_{step_idx+1}"
                meas_row[lbl] = val
            elif t == "screenshot":
                pass  # TODO

            step_idx += 1

            for lc in loops_cfg:
                if lc.get("end_id") != sid: continue
                cond  = lc.get("condition", {})
                ctype = cond.get("type", "count")
                lc_id = lc["id"]

                should_loop = False
                if ctype == "count":
                    loop_counters[lc_id] += 1
                    if loop_counters[lc_id] < int(cond.get("count", 1)):
                        should_loop = True
                    else:
                        loop_counters[lc_id] = 0
                elif ctype == "sweep":
                    vals = _sweep_values(cond)
                    loop_counters[lc_id] += 1
                    if loop_counters[lc_id] < len(vals):
                        loop_sweep_val[lc_id] = vals[loop_counters[lc_id]]
                        should_loop = True
                    else:
                        loop_counters[lc_id] = 0
                        # Reset to the first sweep value (not remove it) so a
                        # nested loop's {var} still resolves correctly the next
                        # time an outer loop re-enters its range.
                        loop_sweep_val[lc_id] = vals[0]
                elif ctype in ("until", "while") and res:
                    op_key = _MEAS_OPS.get(cond.get("param", "voltage"), "measure_voltage")
                    u_ch   = int(cond.get("ch", 1))
                    try:
                        v    = _sf(_run_steps(res, get_command(fam, op_key, ch=u_ch)))
                        op_s = cond.get("op", ">=")
                        tgt  = float(cond.get("target", 0))
                        met  = ((op_s == ">=" and v is not None and v >= tgt) or
                                (op_s == "<=" and v is not None and v <= tgt))
                        should_loop = not met if ctype == "until" else met
                    except Exception:
                        should_loop = True

                if should_loop:
                    if meas_row: _flush_row()
                    si = step_by_id.get(lc.get("start_id"), (None,))[0]
                    if si is not None:
                        step_idx = si
                    break

        if meas_row: _flush_row()
        _done(rows, meas_cols)

    # ── Battery Capacity (load) ───────────────────────────────────────────────
    # Ported from BatteryCapacity.py by Gert Lauritsen
    # https://github.com/gert-lauritsen/KE103  (MIT licence, used with permission)
    def _run_battery_capacity():
        load_i   = float(params.get("current",   1.0))
        cutoff_v = float(params.get("cutoff_v",  3.0))
        interval = float(params.get("interval",  5.0))
        max_s    = float(params.get("max_hours", 10)) * 3600

        res, fam = _find_instrument("load")
        if res is None:
            _done([], [], "Electronic load not connected"); return

        cols = ["elapsed_s", "voltage_V", "current_A", "power_W", "capacity_mAh"]
        rows = []

        try:
            with _rlock(res):
                _run_steps(res, get_command(fam, "set_mode", func="CURR"), role="load")
                _run_steps(res, get_command(fam, "set_current", value=f"{load_i:.4f}"),
                           role="load")
                _run_steps(res, get_command(fam, "input_on"), role="load")
        except Exception as exc:
            _done([], [], f"load init: {exc}"); return

        _emit_progress(f"Discharge started: {load_i} A, cutoff {cutoff_v} V")
        start = time.time()

        try:
            while not _auto_stop.is_set():
                _pause_point()
                if _auto_stop.is_set(): break

                elapsed = time.time() - start
                if elapsed > max_s:
                    _emit_progress("Max duration reached — stopping test"); break

                v_raw, i_raw, p_raw = None, None, None
                try:
                    with _rlock(res):
                        v_r = _run_steps(res, get_command(fam, "measure_voltage"),
                                         role="load")
                        i_r = _run_steps(res, get_command(fam, "measure_current"),
                                         role="load")
                        p_r = _run_steps(res, get_command(fam, "measure_power"),
                                         role="load")
                    v_raw = float(str(v_r).strip().rstrip("V"))
                    i_raw = float(str(i_r).strip().rstrip("A"))
                    p_raw = float(str(p_r).strip().rstrip("W"))
                except Exception as exc:
                    _emit_progress(f"Measurement error: {exc}"); break

                cap_mah = (load_i * elapsed / 3600) * 1000
                row = [round(elapsed, 2), round(v_raw, 4), round(i_raw, 4),
                       round(p_raw, 4), round(cap_mah, 2)]
                rows.append(row)
                _sh.sio.emit("load_reading", {"v": v_raw, "i": i_raw, "p": p_raw})
                _sh.sio.emit("automation_row", {
                    "test_id": test_id, "row": row, "columns": cols, "progress": -1,
                    "cutoff_v": cutoff_v,
                })
                _emit_progress(
                    f"t={elapsed/3600:.3f} h  V={v_raw:.3f} V  "
                    f"I={i_raw:.3f} A  cap={cap_mah:.1f} mAh")

                if v_raw <= cutoff_v:
                    _emit_progress(f"Cutoff reached ({v_raw:.3f} V ≤ {cutoff_v} V) — done")
                    break

                _auto_stop.wait(timeout=interval)
        finally:
            try:
                with _rlock(res):
                    _run_steps(res, get_command(fam, "input_off"), role="load")
            except Exception:
                pass

        if rows:
            total_s   = rows[-1][0]
            final_mah = rows[-1][4]
            _emit_progress(
                f"Test complete — {total_s/3600:.3f} h elapsed, "
                f"{final_mah:.1f} mAh measured")
        _done(rows, cols)

    # ── Custom test (saved from Sandbox) ──────────────────────────────────────
    def _run_custom_test(defn):
        """Resolve instrument roles + bake declared params into a saved sandbox
        sequence, then run it through the same engine as the Sandbox tab."""
        wb_l   = _sh._state.get("workbench") or {}
        unique = wb_l.get("_unique", [])

        def _resolve_role(role):
            override = params.get(f"_role_{role}")
            if override:
                return override
            for instr in unique:
                if instr.get("type") == role and instr.get("resource") in _sh._state["resources"]:
                    return instr["resource"]
            return None

        role_resource = {}
        for role in defn.get("instrument_roles", []):
            rstr = _resolve_role(role)
            if rstr is None:
                _done([], [], f"No connected instrument for role '{role}'"); return
            role_resource[role] = rstr

        declared = {p["id"]: params.get(p["id"], p.get("default"))
                    for p in defn.get("params", []) if not p["id"].startswith("_role_")}

        def _bake(raw):
            s = str(raw)
            for k, v in declared.items():
                s = s.replace("{" + k + "}", str(v))
            return s

        import copy
        steps = copy.deepcopy(defn.get("steps", []))
        loops = copy.deepcopy(defn.get("loops", []))
        _TEMPLATE_FIELDS = ("value", "duration", "target", "tolerance", "interval", "timeout")
        for s in steps:
            role = s.get("instrument", "")
            if role in role_resource:
                s["instrument"] = role_resource[role]
            for f in _TEMPLATE_FIELDS:
                if isinstance(s.get(f), str):
                    s[f] = _bake(s[f])

        _run_sandbox_sequential({"steps": steps, "loops": loops})

    runners = {
        "ac_frequency_sweep": _run_ac_sweep,
        "dc_sweep":           _run_dc_sweep,
        "iv_curve":           _run_dc_sweep,   # Plot Specific: Static Characteristic
        "transfer":           _run_dc_sweep,   # Plot Specific: Transfer Characteristic
        "hfe_gain":           _run_dc_sweep,   # Plot Specific: Current Gain (hFE)
        "vce_sat":            _run_dc_sweep,   # Plot Specific: Collector Saturation Voltage
        "rds_on":             _run_dc_sweep,   # Plot Specific: On-Resistance (R_DS(on))
        "vgs_th":             _run_dc_sweep,   # Plot Specific: Gate Threshold Voltage spot check
        "dmm_logger":         _run_dmm_logger,
        "psu_logger":         _run_psu_logger,
        "psu_interrupt":      _run_psu_interrupt,
        "sandbox":            _run_sandbox,
        "battery_capacity":   _run_battery_capacity,
    }
    runner = runners.get(test_id)
    if runner is None:
        custom_defn = _load_custom_test(test_id)
        if custom_defn is not None:
            runner = lambda defn=custom_defn: _run_custom_test(defn)
    if runner is None:
        _auto_running = False
        return jsonify({"error": f"Unknown test: {test_id!r}"}), 400

    # Stop the PSU polling loop so automation has exclusive access to resources.
    _sh._poll_stop.set()
    _sh._poller_idle.wait(timeout=5.0)

    def _safe_runner():
        try:
            runner()
        except Exception as exc:
            _log(f"[auto/{test_id}] unhandled exception: {exc}")
            _done([], [], str(exc))

    _sh._executor.submit(_safe_runner)
    return jsonify({"status": "running", "test_id": test_id})
