"""
routes/workbench.py — workbench CRUD, scan, bench state, families, assign, info.
"""
import datetime
import json as _json
import os
import time
from pathlib import Path

from flask import Blueprint, jsonify, request

import core.shared as _sh
from core.backbone import (
    get_command, _classify, _family_index, _resolve_family, HELPERS_OK, active_name, load_workbench,
)
from core.helpers import (
    _find_instrument, _run_steps, _op, _log, _rlock,
    _family_for, _write_ops_for_family,
)

bp = Blueprint("workbench", __name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_ROOT       = Path(__file__).parent.parent
_STATES_DIR = _ROOT / "workbench_states"


def _states_dir() -> Path:
    _STATES_DIR.mkdir(exist_ok=True)
    return _STATES_DIR


# ── Local helpers ──────────────────────────────────────────────────────────────

def _safe_query(res, fam, op, **kwargs):
    """Run a backbone query; return stripped string or None on any failure."""
    try:
        steps  = get_command(fam, op, **kwargs)
        result = _run_steps(res, steps)
        return result.strip() if isinstance(result, str) else None
    except Exception:
        return None


def _capture_psu(res, fam, instr: dict) -> dict:
    outputs = []
    for ch in range(1, 5):
        v = _safe_query(res, fam, "set_voltage",       ch=ch, value="?")
        i = _safe_query(res, fam, "set_current_limit", ch=ch, value="?")
        if v is None and i is None:
            break
        try: v = round(float(v), 4) if v else 0.0
        except ValueError: v = 0.0
        try: i = round(float(i), 4) if i else 0.5
        except ValueError: i = 0.5
        outputs.append({"channel": ch, "voltage": v, "current_limit": i, "enabled": False})
    return {"type": "psu", "model": instr.get("model", ""), "outputs": outputs}


def _capture_awg(res, fam, instr: dict) -> dict:
    channels = []
    for ch in range(1, 3):
        func   = _safe_query(res, fam, "set_function",  ch=ch, func="?")
        freq   = _safe_query(res, fam, "set_frequency", ch=ch, freq="?")
        amp    = _safe_query(res, fam, "set_amplitude", ch=ch, amp="?")
        offset = _safe_query(res, fam, "set_offset",    ch=ch, offset="?")
        if func is None and freq is None:
            break
        try: freq   = round(float(freq),   6) if freq   else 1000.0
        except ValueError: freq = 1000.0
        try: amp    = round(float(amp),    6) if amp    else 1.0
        except ValueError: amp = 1.0
        try: offset = round(float(offset), 6) if offset else 0.0
        except ValueError: offset = 0.0
        channels.append({
            "channel":        ch,
            "function":       (func or "SIN").upper(),
            "frequency":      freq,
            "amplitude":      amp,
            "amplitude_unit": "VPP",
            "offset":         offset,
            "enabled":        False,
        })
    return {"type": "awg", "model": instr.get("model", ""), "channels": channels}


_TYPE_TO_ROLE = {
    "scope": "scope", "psu": "psu", "awg": "generator",
    "dmm": "dmm", "load": "load", "smu": "smu",
}


# ── Workbench list / load / rename / delete ────────────────────────────────────

@bp.route("/api/workbenches")
def api_workbenches():
    names = sorted(
        f.stem for f in _sh.WORKBENCH_DIR.glob("*.json")
        if f.name != "active.json" and f.exists()
    )
    return jsonify({"workbenches": names, "active": active_name()})


@bp.route("/api/workbench/<name>/rename", methods=["POST"])
def api_rename_workbench(name: str):
    import pathlib
    from workbench import _safe_name, WORKBENCH_DIR as WD
    new_name = _safe_name((request.json or {}).get("new_name", "").strip())
    if not new_name:
        return jsonify({"error": "new_name required"}), 400
    src = pathlib.Path(WD) / f"{_safe_name(name)}.json"
    dst = pathlib.Path(WD) / f"{new_name}.json"
    if not src.exists():
        return jsonify({"error": f"Workbench {name!r} not found"}), 404
    if dst.exists():
        return jsonify({"error": f"A workbench named {new_name!r} already exists"}), 409
    src.rename(dst)
    active_link = Path(WD) / "active.json"
    if active_link.is_symlink() and active_link.readlink() == Path(f"{_safe_name(name)}.json"):
        active_link.unlink()
        active_link.symlink_to(f"{new_name}.json")
    return jsonify({"status": "renamed", "name": new_name})


@bp.route("/api/workbench/<name>/delete", methods=["POST"])
def api_delete_workbench(name: str):
    from workbench import _safe_name, WORKBENCH_DIR as WD
    path = Path(WD) / f"{_safe_name(name)}.json"
    if not path.exists():
        return jsonify({"error": f"Workbench {name!r} not found"}), 404
    path.unlink()
    active_link = Path(WD) / "active.json"
    if active_link.is_symlink() and active_link.readlink() == Path(f"{_safe_name(name)}.json"):
        active_link.unlink()
    return jsonify({"status": "deleted"})


@bp.route("/api/workbench/<name>")
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
            family = _family_for(instr)
            instr["write_ops"] = _write_ops_for_family(family) if family else []
            unique.append(instr)
    wb["_unique"] = unique

    with _sh._lock:
        _sh._state["workbench"] = wb
        _sh._state["wb_name"]   = name

    return jsonify(wb)


# ── Scan ──────────────────────────────────────────────────────────────────────

@bp.route("/api/scan", methods=["POST"])
def api_scan():
    """Discover VISA instruments on USB + LAN; stream progress via SocketIO."""
    d        = request.json or {}
    usb_only = bool(d.get("usb_only", False))

    def _emit(msg, **kw):
        _sh.sio.emit("scan_progress", {"msg": msg, **kw})
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
            _sh.sio.emit("scan_result",
                         {"error": str(exc), "instruments": [], "errors": []})
            return

        instruments, errors = [], []

        # 1 — open VISA RM
        _emit("Opening VISA resource manager…")
        try:
            rm = open_resource_manager()
        except Exception as exc:
            _sh.sio.emit("scan_result",
                         {"error": str(exc), "instruments": [], "errors": []})
            return

        # 2 — USB / standard VISA resources + USB-serial ports
        _emit("Querying USB & VISA resources…")
        resources, errs = discover_resources(rm)
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

        # 4 — identify each resource
        _emit(f"Identifying {len(resources)} resource(s)…")
        for rstr in resources:
            inst = None
            try:
                inst = rm.open_resource(rstr)
                if rstr.upper().startswith("ASRL"):
                    inst.baud_rate         = 9600
                    inst.data_bits         = 8
                    inst.stop_bits         = _sh.pyvisa.constants.StopBits.one
                    inst.parity            = _sh.pyvisa.constants.Parity.none
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
                    "role":         (_TYPE_ROLE.get(family["type"], family["type"])
                                     if family else None),
                    "family_id":    family["id"] if family else None,
                }
                instruments.append(entry)
                _emit(f"✓  {model}  ({connection_type(rstr)})", instrument=entry)
            except Exception as exc:
                errors.append(f"{rstr}: {exc}")
                _emit(f"⚠  {rstr}: {exc}")
            finally:
                if inst is not None:
                    try: inst.close()
                    except: pass

        try: rm.close()
        except: pass

        _sh.sio.emit("scan_result", {
            "instruments": instruments,
            "errors":      [e for e in errors if e],
        })

    _sh._executor.submit(_do)
    return jsonify({"status": "scanning"})


@bp.route("/api/scan/save", methods=["POST"])
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


@bp.route("/api/info")
def api_info():
    return jsonify({"nachovisa_path": str(_ROOT / "core" / "nachoVisa.py")})


# ── Bench state save / load / delete / reset ──────────────────────────────────

@bp.route("/api/bench/states")
def api_bench_states():
    states = []
    for p in sorted(_states_dir().glob("*.json")):
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            states.append({
                "name":     p.stem,
                "saved_at": data.get("saved_at", ""),
                "summary":  data.get("summary", ""),
            })
        except Exception:
            pass
    return jsonify({"states": states})


@bp.route("/api/bench/state/save", methods=["POST"])
def api_bench_state_save():
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    wb = _sh._state.get("workbench")
    if not wb:
        return jsonify({"error": "No workbench loaded"}), 400

    instruments    = {}
    summary_parts  = []
    for instr in wb.get("_unique", []):
        rstr = instr.get("resource", "")
        res  = _sh._state["resources"].get(rstr)
        fam  = _sh._state["families"].get(rstr)
        if not res or not fam:
            continue
        itype = instr.get("type", "")
        if itype == "psu":
            instruments[rstr] = _capture_psu(res, fam, instr)
            summary_parts.append(f"PSU {instr.get('model','')}")
        elif itype == "awg":
            instruments[rstr] = _capture_awg(res, fam, instr)
            summary_parts.append(f"AWG {instr.get('model','')}")

    if not instruments:
        return jsonify({"error": "No supported instruments connected (PSU / AWG)"}), 400

    payload = {
        "name":        name,
        "saved_at":    datetime.datetime.now().isoformat(timespec="seconds"),
        "summary":     ", ".join(summary_parts),
        "instruments": instruments,
    }
    path = _states_dir() / f"{name}.json"
    path.write_text(_json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _log(f"✓ Bench state saved: {name}")
    return jsonify({"status": "saved", "name": name})


@bp.route("/api/bench/state/load", methods=["POST"])
def api_bench_state_load():
    d    = request.json or {}
    name = (d.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400

    path = _states_dir() / f"{name}.json"
    if not path.exists():
        return jsonify({"error": f"State {name!r} not found"}), 404

    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    applied, skipped = [], []
    for rstr, cfg in payload.get("instruments", {}).items():
        res = _sh._state["resources"].get(rstr)
        fam = _sh._state["families"].get(rstr)
        if not res or not fam:
            skipped.append(rstr)
            continue
        itype = cfg.get("type", "")
        try:
            if itype == "psu":
                for out in cfg.get("outputs", []):
                    ch = out["channel"]
                    _run_steps(res, get_command(fam, "set_voltage",
                        ch=ch, value=f"{out['voltage']:.4f}"), role="psu")
                    _run_steps(res, get_command(fam, "set_current_limit",
                        ch=ch, value=f"{out['current_limit']:.4f}"), role="psu")
                    cmd = "output_on" if out.get("enabled") else "output_off"
                    try: _run_steps(res, get_command(fam, cmd, ch=ch), role="psu")
                    except KeyError: pass
                applied.append(rstr)
            elif itype == "awg":
                for ch_cfg in cfg.get("channels", []):
                    ch = ch_cfg["channel"]
                    try:
                        _run_steps(res, get_command(fam, "apply",
                            ch=ch, func=ch_cfg.get("function", "SIN"),
                            freq=ch_cfg.get("frequency", 1000),
                            amp=ch_cfg.get("amplitude", 1.0),
                            offset=ch_cfg.get("offset", 0.0)), role="awg")
                    except KeyError:
                        # apply not available — set params individually
                        for op, kw in [
                            ("set_function",  {"func":   ch_cfg.get("function", "SIN")}),
                            ("set_frequency", {"freq":   ch_cfg.get("frequency", 1000)}),
                            ("set_amplitude", {"amp":    ch_cfg.get("amplitude", 1.0)}),
                            ("set_offset",    {"offset": ch_cfg.get("offset", 0.0)}),
                        ]:
                            try: _run_steps(res, get_command(fam, op, ch=ch, **kw), role="awg")
                            except KeyError: pass
                    cmd = "output_on" if ch_cfg.get("enabled") else "output_off"
                    try: _run_steps(res, get_command(fam, cmd, ch=ch), role="awg")
                    except KeyError: pass
                applied.append(rstr)
        except Exception as exc:
            _log(f"⚠ state load {rstr}: {exc}")
            skipped.append(rstr)

    _log(f"✓ Bench state loaded: {name} ({len(applied)} applied, {len(skipped)} skipped)")
    return jsonify({"status": "loaded", "applied": len(applied), "skipped": len(skipped)})


@bp.route("/api/bench/state/delete", methods=["POST"])
def api_bench_state_delete():
    name = ((request.json or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    path = _states_dir() / f"{name}.json"
    if path.exists():
        path.unlink()
    return jsonify({"status": "deleted"})


@bp.route("/api/bench/reset", methods=["POST"])
def api_bench_reset():
    wb = _sh._state.get("workbench")
    if not wb:
        return jsonify({"error": "No workbench loaded"}), 400
    count = 0
    for instr in wb.get("_unique", []):
        rstr  = instr.get("resource", "")
        res   = _sh._state["resources"].get(rstr)
        fam   = _sh._state["families"].get(rstr)
        if not res or not fam:
            continue
        itype = instr.get("type", "")
        try:
            if itype == "psu":
                for ch in range(1, 5):
                    try:
                        _run_steps(res, get_command(fam, "set_voltage",       ch=ch, value="0.0"))
                        _run_steps(res, get_command(fam, "set_current_limit", ch=ch, value="0.1"))
                        _run_steps(res, get_command(fam, "output_off",        ch=ch))
                    except KeyError:
                        break
                count += 1
            elif itype == "awg":
                for ch in range(1, 3):
                    try:
                        _run_steps(res, get_command(fam, "output_off", ch=ch))
                    except KeyError:
                        break
                try: _run_steps(res, get_command(fam, "reset"))
                except KeyError: pass
                count += 1
        except Exception as exc:
            _log(f"⚠ reset {rstr}: {exc}")
    _log(f"✓ Bench reset ({count} instrument(s))")
    return jsonify({"status": "reset", "count": count})


# ── Families + family assignment ───────────────────────────────────────────────

@bp.route("/api/families")
def api_families():
    try:
        from eewBackbone import _load
        families = [
            {
                "id":     f["id"],
                "vendor": f.get("vendor", "Unknown"),
                "series": f.get("series", f["id"]),
                "type":   f.get("type", "unknown"),
            }
            for f in _load()["families"]
        ]
        return jsonify({"families": families})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/workbench/assign", methods=["POST"])
def api_assign_family():
    d         = request.json or {}
    resource  = (d.get("resource") or "").strip()
    family_id = (d.get("family_id") or "").strip()
    if not resource or not family_id:
        return jsonify({"error": "resource and family_id required"}), 400
    try:
        idx = _family_index()
        if family_id not in idx:
            return jsonify({"error": f"Unknown family: {family_id}"}), 404
        raw_fam  = idx[family_id]
        new_type = raw_fam.get("type", "unknown")
        new_role = (d.get("role") or "").strip() or _TYPE_TO_ROLE.get(new_type, new_type)

        wb = _sh._state.get("workbench")
        if not wb:
            return jsonify({"error": "No workbench loaded"}), 400

        updated = None
        for instr in wb.get("instruments", []):
            if instr.get("resource") == resource:
                instr.update(family_id=family_id, type=new_type, role=new_role)
                updated = instr
        for instr in wb.get("_unique", []):
            if instr.get("resource") == resource:
                instr.update(family_id=family_id, type=new_type, role=new_role)

        if updated is None:
            return jsonify({"error": "Instrument not found in workbench"}), 404

        # Resolve and cache the family immediately so controls work without reconnect
        resolved = _resolve_family(raw_fam)
        with _sh._lock:
            _sh._state["families"][resource] = resolved

        # Persist to workbench file
        wb_name = _sh._state.get("wb_name")
        if wb_name:
            from workbench import WORKBENCH_DIR, _safe_name
            path = os.path.join(WORKBENCH_DIR, f"{_safe_name(wb_name)}.json")
            payload = {k: v for k, v in wb.items() if not k.startswith("_")}
            with open(path, "w", encoding="utf-8") as f:
                _json.dump(payload, f, indent=2, ensure_ascii=False)

        return jsonify({"status": "ok", "instrument": updated})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
