"""
core/helpers.py — SCPI helper functions, instrument lookup, polling.
"""
import re
import threading
import time

import core.shared as _sh
from core.backbone import get_command, _family_index, _resolve_family, HELPERS_OK


# ── Value-key extraction ──────────────────────────────────────────────────────

def _extract_value_key(cmd_spec) -> str:
    """Return the first non-ch placeholder name from a command spec, or 'value'."""
    if isinstance(cmd_spec, str):
        keys = [k for k in re.findall(r'\{(\w+)\}', cmd_spec) if k != 'ch']
        return keys[0] if keys else 'value'
    if isinstance(cmd_spec, dict):
        src = cmd_spec.get('write') or next(iter(cmd_spec.values()), '')
        return _extract_value_key(src)
    if isinstance(cmd_spec, list):
        for item in cmd_spec:
            k = _extract_value_key(item)
            if k != 'value':
                return k
    return 'value'


def _write_ops_for_family(family: dict) -> list:
    """Return [{op, label, key}] for all set_* operations in a resolved family."""
    ops = []
    for op_name, cmd_spec in family.get('commands', {}).items():
        if not op_name.startswith('set_'):
            continue
        key   = _extract_value_key(cmd_spec)
        label = op_name[4:].replace('_', ' ').title()   # "set_frequency" → "Frequency"
        ops.append({'op': op_name, 'label': label, 'key': key})
    return ops


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


# ── SCPI execution helpers ────────────────────────────────────────────────────

def _run_steps(resource, steps: list, role: str = None, poll: bool = False) -> object:
    result = None
    for action, scpi in steps:
        if action == "write":
            resource.write(scpi)
            _sh.sio.emit("scpi_traffic", {"role": role, "cmd": scpi,
                                          "result": None, "poll": poll})
        elif action == "query":
            result = resource.query(scpi).strip()
            _sh.sio.emit("scpi_traffic", {"role": role, "cmd": scpi,
                                          "result": result, "poll": poll})
        elif action == "raw_query":
            resource.write(scpi)
            # Use a large chunk_size so USBTMC.read() accumulates the full
            # binary payload (e.g. 1.15 MB BMP) before returning.
            orig_chunk = getattr(resource, 'chunk_size', None)
            try:
                resource.chunk_size = _sh._SCREENSHOT_CHUNK_SIZE
                result = resource.read_raw()
            finally:
                if orig_chunk is not None:
                    try:
                        resource.chunk_size = orig_chunk
                    except Exception:
                        pass
            _sh.sio.emit("scpi_traffic", {"role": role, "cmd": scpi,
                                          "result": f"<{len(result)} bytes>",
                                          "poll": poll})
    return result


def _op(resource, family, operation: str, role: str = None, **kwargs):
    if resource is None or family is None:
        return None
    try:
        steps  = get_command(family, operation, **kwargs)
        result = _run_steps(resource, steps, role=role)
        writes = [s for a, s in steps if a in ("write", "query")]
        _sh.sio.emit("log", {"msg": "→  " + "  |  ".join(writes[:2])})
        return result
    except KeyError:
        _sh.sio.emit("log",
                     {"msg": f"⚠  {operation!r} not supported on this instrument"})
        return None


def _log(msg: str):
    _sh.sio.emit("log", {"msg": msg})


# ── Scope measurement helpers ─────────────────────────────────────────────────

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
        _run_steps(scope_res, get_command(scope_fam, "measure_clear_all"), role="scope")
        time.sleep(0.1)
    except KeyError:
        pass          # command not in this family — fine
    except Exception as exc:
        _log(f"[scope] measure_clear_all: {exc}")

    # Phase 2 — enable each item (write step only)
    for op, ch in op_ch_pairs:
        try:
            steps      = get_command(scope_fam, op, ch=ch)
            write_only = [(a, s) for a, s in steps if a == "write"]
            if write_only:
                _run_steps(scope_res, write_only, role="scope")
        except KeyError:
            pass
        except Exception as exc:
            _log(f"[scope] enable {op} CH{ch}: {exc}")


def _scope_query_only(scope_res, scope_fam, op: str, ch: int, poll: bool = False):
    """Query a single scope measurement *without* re-sending its enable write.

    Call _scope_enable_measures() first, then use this inside the loop.
    Returns the float value, or None if unavailable / out-of-range.
    """
    try:
        steps       = get_command(scope_fam, op, ch=ch)
        query_steps = [(a, s) for a, s in steps if a in ("query", "raw_query")]
        if not query_steps:
            return None
        raw = _run_steps(scope_res, query_steps, role="scope", poll=poll)
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


# ── Instrument lookup ─────────────────────────────────────────────────────────

def _find_instrument(itype: str):
    """Return (resource, family) for the first instrument of given type."""
    wb = _sh._state.get("workbench")
    if not wb:
        return None, None
    for instr in wb.get("_unique", []):
        if instr.get("type") == itype:
            rstr = instr.get("resource", "")
            res  = _sh._state["resources"].get(rstr)
            fam  = _sh._state["families"].get(rstr)
            if res:
                return res, fam
    return None, None


def _rlock(res):
    """Return the per-resource VISA lock (a no-op Lock if res has none)."""
    lock = getattr(res, "_visa_lock", None)
    if lock is None:
        lock = threading.Lock()
    return lock


# ── Background PSU polling ────────────────────────────────────────────────────

def _start_polling():
    _sh._poll_stop.clear()

    def _loop():
        _sh._poller_idle.clear()
        try:
            while not _sh._poll_stop.is_set() and _sh._state["connected"]:
                wb = _sh._state.get("workbench")
                if wb:
                    for instr in wb.get("_unique", []):
                        itype = instr.get("type")
                        rstr  = instr.get("resource", "")
                        res   = _sh._state["resources"].get(rstr)
                        fam   = _sh._state["families"].get(rstr)
                        if res is None or fam is None:
                            continue

                        if itype == "psu":
                            # Use the family's declared channel count as the ceiling.
                            # _psu_ch_cache[rstr] shrinks to the true count after the
                            # first poll on instruments whose family covers multiple
                            # models with different channel counts (e.g. DP811/DP832).
                            max_ch = _sh._psu_ch_cache.get(rstr, fam.get("channels", 4))
                            for ch in range(1, max_ch + 1):
                                if _sh._poll_stop.is_set():
                                    break
                                readings: dict = {}
                                for op, key in [("measure_voltage", "v"),
                                                ("measure_current", "i"),
                                                ("measure_power",   "p")]:
                                    try:
                                        r = _run_steps(res,
                                                       get_command(fam, op, ch=ch),
                                                       role="psu", poll=True)
                                        if r is not None:
                                            readings[key] = float(r)
                                    except Exception:
                                        pass
                                if readings:
                                    _sh.sio.emit("psu_reading", {"ch": ch, **readings})
                                elif ch > 1:
                                    _sh._psu_ch_cache[rstr] = ch - 1
                                    break

                        elif itype == "load":
                            if _sh._poll_stop.is_set():
                                break
                            readings: dict = {}
                            for op, key in [("measure_voltage", "v"),
                                            ("measure_current", "i"),
                                            ("measure_power",   "p")]:
                                try:
                                    r = _run_steps(res,
                                                   get_command(fam, op),
                                                   role="load", poll=True)
                                    if r is not None:
                                        readings[key] = float(
                                            str(r).strip().rstrip("VAW"))
                                except Exception:
                                    pass
                            if readings:
                                _sh.sio.emit("load_reading", readings)

                _sh._poll_stop.wait(timeout=1.5)
        finally:
            _sh._poller_idle.set()

    _sh._executor.submit(_loop)
