#!/usr/bin/env python3
# nacho.works — Set Workbench
# Configures all connected instruments from a JSON workflow file,
# or resets them all to safe defaults with --reset-bench.
#
# Usage:
#   python setWorkbench.py                       # apply workbench_config.json
#   python setWorkbench.py --config foo.json     # apply a specific config file
#   python setWorkbench.py --reset-bench         # reset all instruments to safe defaults

import argparse
import itertools
import json
import os
import sys
import threading
import time

try:
    import pyvisa
except ImportError:
    print("Error: pyvisa is not installed. Run: pip install pyvisa pyvisa-py")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Logo + spinner
# ---------------------------------------------------------------------------

_SPINNER_FRAMES = ["|", "/", "-", "\\"]

_LOGO_LINES = [
    "                ####",
    "              #######",
    "             #########",
    "           ############",
    "          ##############",
    "         ################",
    "       ###################",
    "      #######     ####",
    "    ##########   ######  ##",
    "   ###############   #######",
    "  ###############     #######",
    "##############################",
    "    ##########################",
    "                  #############",
]
_LOGO_PAD    = 36
_BOX_INNER   = _LOGO_PAD + 42
_TITLE_ROW   = 4
_SUB_ROW     = 5
_SPINNER_ROW = 7
_ROWS_TO_SPINNER = len(_LOGO_LINES) - _SPINNER_ROW + 1

_subtitle = "instrument setup"


def _logo_line(idx: int, frame: str = " ", msg: str = "") -> str:
    logo = _LOGO_LINES[idx].ljust(_LOGO_PAD)
    if idx == _TITLE_ROW:
        right = "nacho.works"
    elif idx == _SUB_ROW:
        right = _subtitle
    elif idx == _SPINNER_ROW:
        right = f"{frame}  {msg}" if msg else ""
    else:
        right = ""
    inner = (logo + right).ljust(_BOX_INNER)
    return f"│{inner}│"


def _print_logo(frame: str = " ", msg: str = ""):
    print("┌" + "─" * _BOX_INNER + "┐")
    for i in range(len(_LOGO_LINES)):
        print(_logo_line(i, frame, msg))
    print("└" + "─" * _BOX_INNER + "┘")


class Spinner:
    def __init__(self, message: str = "Working"):
        self._message = message
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _write_spinner_row(self, frame: str, msg: str):
        line = _logo_line(_SPINNER_ROW, frame, msg)
        n = _ROWS_TO_SPINNER
        sys.stdout.write(f"\033[{n}A\r{line}\033[{n}B\r")
        sys.stdout.flush()

    def _spin(self):
        for frame in itertools.cycle(_SPINNER_FRAMES):
            if self._stop.is_set():
                break
            with self._lock:
                msg = self._message
            self._write_spinner_row(frame, msg)
            time.sleep(0.15)
        self._write_spinner_row(" ", "")

    def update(self, message: str):
        with self._lock:
            self._message = message

    def __enter__(self):
        _print_logo(frame=_SPINNER_FRAMES[0], msg=self._message)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join()
        print()


# ---------------------------------------------------------------------------
# Instrument classification
# ---------------------------------------------------------------------------

try:
    from instruments import classify as _db_classify
except ImportError:
    _db_classify = None

# Maps instruments.json family IDs to the handler keys used by APPLY/RESET_HANDLERS.
# Families not listed here fall back to their generic type (e.g. "scope").
_FAMILY_TO_HANDLER = {
    "keysight_edu36311a": "edu36311a",
    "keysight_e36300":    "edu36311a",
    "keysight_edu33211a": "edu33211a",
}


def classify(idn: str) -> str:
    if _db_classify is not None:
        family = _db_classify(idn)
        if family is not None:
            fid = family["id"]
            if fid in _FAMILY_TO_HANDLER:
                return _FAMILY_TO_HANDLER[fid]
            ftype = family.get("type", "unknown")
            if ftype in APPLY_HANDLERS:
                return ftype
            return "unknown"
    # fallback if instruments.py is unavailable
    u = idn.upper()
    if "EDU36311A" in u:
        return "edu36311a"
    if "EDU33211A" in u:
        return "edu33211a"
    for pat in ["DSOX", "EDUX", "INFINIIVISION", "MSO5074", "SDS1104X-E",
                "SDS1202X-E", "TBS1052B", "MSO24", "RTB2004"]:
        if pat.upper() in u:
            return "scope"
    return "unknown"


def serial_from_idn(idn: str) -> str:
    parts = [p.strip() for p in idn.split(",")]
    return parts[2] if len(parts) >= 3 else idn


def model_from_idn(idn: str) -> str:
    parts = [p.strip() for p in idn.split(",")]
    return parts[1] if len(parts) >= 2 else idn


_CONN_PRIORITY = {"USB": 0, "TCPIP": 1}


def conn_priority(resource: str) -> int:
    return _CONN_PRIORITY.get(resource.upper().split("0::")[0], 99)


def open_inst(rm, resource: str):
    inst = rm.open_resource(resource)
    inst.timeout = 5000
    inst.read_termination = "\n"
    inst.write_termination = "\n"
    return inst


# ---------------------------------------------------------------------------
# Apply handlers — configure from JSON
# ---------------------------------------------------------------------------

def _fmt_edu36311a(config: dict) -> str:
    parts = []
    for out in config.get("outputs", []):
        ch = out.get("channel", "?")
        v = out.get("voltage", 0)
        i = out.get("current_limit", 0.5)
        state = "on" if out.get("enabled", False) else "off"
        parts.append(f"CH{ch} {v}V/{i}A {state}")
    return "  |  ".join(parts) if parts else "no outputs configured"


def _fmt_edu33211a(config: dict) -> str:
    parts = []
    for ch_cfg in config.get("channels", []):
        ch = ch_cfg.get("channel", 1)
        func = ch_cfg.get("function", "SIN").upper()
        freq = ch_cfg.get("frequency", 1000)
        amp = ch_cfg.get("amplitude", 1.0)
        unit = ch_cfg.get("amplitude_unit", "VPP")
        offset = ch_cfg.get("offset", 0.0)
        state = "on" if ch_cfg.get("enabled", False) else "off"
        parts.append(f"CH{ch} {func} {freq}Hz {amp}{unit} {offset}V offset {state}")
    return "  |  ".join(parts) if parts else "no channels configured"


def _fmt_scope(config: dict) -> str:
    if config.get("reset", False):
        return "default setup (:SYSTem:PRESet)"
    return "no changes"


def apply_edu36311a(inst, config: dict):
    for out in config.get("outputs", []):
        ch = out["channel"]
        v = out.get("voltage", 0)
        i_lim = out.get("current_limit", 0.5)
        enabled = out.get("enabled", False)
        inst.write(f"VOLT {v},(@{ch})")
        inst.write(f"CURR {i_lim},(@{ch})")
        inst.write(f"OUTP {'ON' if enabled else 'OFF'},(@{ch})")


def apply_edu33211a(inst, config: dict):
    for ch_cfg in config.get("channels", []):
        ch = ch_cfg.get("channel", 1)
        func = ch_cfg.get("function", "SIN").upper()
        freq = ch_cfg.get("frequency", 1000)
        amp = ch_cfg.get("amplitude", 1.0)
        unit = ch_cfg.get("amplitude_unit", "VPP").upper()
        offset = ch_cfg.get("offset", 0.0)
        enabled = ch_cfg.get("enabled", False)
        inst.write(f"SOUR{ch}:VOLT:UNIT {unit}")
        inst.write(f"SOUR{ch}:APPL:{func} {freq},{amp},{offset}")
        inst.write(f"OUTP{ch} {'ON' if enabled else 'OFF'}")


def apply_scope(inst, config: dict):
    if config.get("reset", False):
        inst.timeout = 15000
        inst.write("*CLS")
        inst.write(":SYSTem:PRESet")
        inst.query("*OPC?")


APPLY_HANDLERS = {
    "edu36311a": (apply_edu36311a, _fmt_edu36311a),
    "edu33211a": (apply_edu33211a, _fmt_edu33211a),
    "scope":     (apply_scope,     _fmt_scope),
}


# ---------------------------------------------------------------------------
# Save handlers — read current settings from instruments
# ---------------------------------------------------------------------------

def save_edu36311a(inst) -> dict:
    outputs = []
    for ch in (1, 2, 3):
        v     = float(inst.query(f"VOLT? (@{ch})").strip())
        i_lim = float(inst.query(f"CURR? (@{ch})").strip())
        state = inst.query(f"OUTP? (@{ch})").strip()
        outputs.append({
            "channel":       ch,
            "voltage":       round(v, 4),
            "current_limit": round(i_lim, 4),
            "enabled":       state in ("1", "ON"),
        })
    return {"outputs": outputs}


def save_edu33211a(inst) -> dict:
    channels = []
    for ch in (1, 2):
        try:
            func   = inst.query(f"SOUR{ch}:FUNC?").strip()
            freq   = float(inst.query(f"SOUR{ch}:FREQ?").strip())
            amp    = float(inst.query(f"SOUR{ch}:VOLT?").strip())
            unit   = inst.query(f"SOUR{ch}:VOLT:UNIT?").strip()
            offset = float(inst.query(f"SOUR{ch}:VOLT:OFFS?").strip())
            state  = inst.query(f"OUTP{ch}?").strip()
            channels.append({
                "channel":        ch,
                "function":       func,
                "frequency":      round(freq, 6),
                "amplitude":      round(amp, 6),
                "amplitude_unit": unit,
                "offset":         round(offset, 6),
                "enabled":        state in ("1", "ON"),
            })
        except Exception:
            break  # single-channel device or channel out of range
    return {"channels": channels}


SAVE_HANDLERS = {
    "edu36311a": save_edu36311a,
    "edu33211a": save_edu33211a,
}


# ---------------------------------------------------------------------------
# Reset handlers — safe defaults
# ---------------------------------------------------------------------------

def reset_scope(inst):
    inst.timeout = 15000
    inst.write("*CLS")
    inst.write(":SYSTem:PRESet")
    inst.query("*OPC?")
    inst.write(":CHANnel2:DISPlay OFF")


def reset_edu36311a(inst):
    inst.write("*RST")
    inst.write("*CLS")
    time.sleep(1.0)
    inst.write("VOLT 0,(@1,2,3)")
    inst.write("CURR 0.5,(@1,2,3)")
    inst.write("OUTP OFF,(@1,2,3)")


def reset_edu33211a(inst):
    inst.write("*RST")
    inst.write("*CLS")
    time.sleep(0.5)
    inst.write("SOUR1:VOLT:UNIT VPP")
    inst.write("SOUR1:APPL:SIN 1E3,1,0")
    inst.write("OUTP1 OFF")


RESET_HANDLERS = {
    "scope":     (reset_scope,     "oscilloscope → factory default setup, CH2 off"),
    "edu36311a": (reset_edu36311a, "EDU36311A → all outputs 0 V / 500 mA / off"),
    "edu33211a": (reset_edu33211a, "EDU33211A → CH1 1 kHz / 1 Vpp sine / 0 V offset / off"),
}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _candidate_lan_resources(host: str) -> tuple:
    return (
        f"TCPIP0::{host}::inst0::INSTR",
        f"TCPIP0::{host}::hislip0::INSTR",
        f"TCPIP0::{host}::5025::SOCKET",
    )


def _probe_resource(rm, resource: str) -> tuple[str, str] | None:
    """Try to open resource and query IDN. Returns (resource, idn) or None."""
    inst = None
    try:
        inst = open_inst(rm, resource)
        idn = inst.query("*IDN?").strip()
        return resource, idn
    except Exception:
        return None
    finally:
        if inst is not None:
            try:
                inst.close()
            except Exception:
                pass


def discover(rm, spinner: Spinner, extra_hosts: list | None = None) -> dict:
    """Return {serial: (resource, idn)} for all reachable instruments.

    USB/USBTMC devices are found via list_resources(). Ethernet instruments
    are NOT auto-discovered by the @py backend, so extra_hosts must list
    their IPs explicitly (sourced from the 'hosts' key in the config JSON).
    """
    found = {}

    def _register(resource: str, idn: str):
        serial = serial_from_idn(idn)
        if serial not in found or conn_priority(resource) < conn_priority(found[serial][0]):
            found[serial] = (resource, idn)

    # USB auto-discovery via VISA resource manager
    try:
        usb_resources = [
            r for r in rm.list_resources()
            if r.upper().startswith("USB") and not r.upper().endswith("::RAW")
        ]
    except Exception:
        usb_resources = []

    for resource in usb_resources:
        spinner.update(f"Probing {resource}")
        result = _probe_resource(rm, resource)
        if result:
            _register(*result)

    # Ethernet instruments — probe explicit host IPs from config.
    # The @py backend does not auto-discover LAN instruments via list_resources().
    for host in (extra_hosts or []):
        spinner.update(f"Probing {host}")
        for resource in _candidate_lan_resources(host):
            result = _probe_resource(rm, resource)
            if result:
                _register(*result)
                break  # found it on this host, skip remaining candidates

    return found


# ---------------------------------------------------------------------------
# Run modes
# ---------------------------------------------------------------------------

def run_setup(rm, discovered: dict, workflow: dict, spinner: Spinner) -> list:
    instruments_cfg = workflow.get("instruments", {})
    results = []

    for serial, (resource, idn) in discovered.items():
        model = model_from_idn(idn)
        kind = classify(idn)
        spinner.update(f"Configuring {model}")

        if kind not in APPLY_HANDLERS:
            results.append((model, resource, False, "unknown instrument type — skipped"))
            continue

        inst_cfg = instruments_cfg.get(kind)
        if inst_cfg is None:
            results.append((model, resource, None, "not in config — skipped"))
            continue

        apply_fn, fmt_fn = APPLY_HANDLERS[kind]
        description = fmt_fn(inst_cfg)
        inst = None
        try:
            inst = open_inst(rm, resource)
            apply_fn(inst, inst_cfg)
            results.append((model, resource, True, description))
        except Exception as exc:
            results.append((model, resource, False, str(exc)))
        finally:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass

    return results


def run_reset(rm, discovered: dict, spinner: Spinner) -> list:
    results = []

    for serial, (resource, idn) in discovered.items():
        model = model_from_idn(idn)
        kind = classify(idn)
        spinner.update(f"Resetting {model}")

        if kind not in RESET_HANDLERS:
            results.append((model, resource, False, "unknown instrument type — skipped"))
            continue

        reset_fn, description = RESET_HANDLERS[kind]
        inst = None
        try:
            inst = open_inst(rm, resource)
            reset_fn(inst)
            results.append((model, resource, True, description))
        except Exception as exc:
            results.append((model, resource, False, str(exc)))
        finally:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass

    return results


def run_save(rm, discovered: dict, spinner: Spinner) -> tuple[dict, list]:
    """Query current settings from all instruments; return (config_dict, results)."""
    instruments_config = {}
    results = []

    for serial, (resource, idn) in discovered.items():
        model = model_from_idn(idn)
        kind  = classify(idn)
        spinner.update(f"Reading {model}")

        if kind not in SAVE_HANDLERS:
            results.append((model, resource, None, f"save not supported for '{kind}' — skipped"))
            continue

        save_fn = SAVE_HANDLERS[kind]
        inst = None
        try:
            inst = open_inst(rm, resource)
            config = save_fn(inst)
            instruments_config[kind] = config
            results.append((model, resource, True, f"settings read OK"))
        except Exception as exc:
            results.append((model, resource, False, str(exc)))
        finally:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass

    return instruments_config, results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _subtitle

    parser = argparse.ArgumentParser(
        description="Configure workbench instruments from a JSON workflow file."
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        default=None,
        help="Workflow config JSON file (default: workbench_config.json next to this script).",
    )
    parser.add_argument(
        "--reset-bench",
        action="store_true",
        help="Reset all instruments to safe defaults instead of applying a config.",
    )
    parser.add_argument(
        "--save-current",
        metavar="NAME",
        help="Read current instrument settings and write them to <NAME>.json.",
    )
    args = parser.parse_args()

    workflow = None
    extra_hosts = []
    default_config = os.path.join(os.path.dirname(__file__), "workbench_config.json")

    if args.save_current:
        _subtitle = f"saving: {args.save_current}"
        try:
            with open(default_config) as f:
                extra_hosts = json.load(f).get("hosts", [])
        except Exception:
            pass
    elif args.reset_bench:
        _subtitle = "workbench reset"
        # Load hosts from default config even in reset mode so LAN instruments are reached.
        try:
            with open(default_config) as f:
                extra_hosts = json.load(f).get("hosts", [])
        except Exception:
            pass
    else:
        config_path = args.config or default_config
        try:
            with open(config_path) as f:
                workflow = json.load(f)
        except FileNotFoundError:
            print(f"Config file not found: {config_path}")
            sys.exit(1)
        except json.JSONDecodeError as exc:
            print(f"Invalid JSON in {config_path}: {exc}")
            sys.exit(1)
        _subtitle = f"workflow: {workflow.get('name', os.path.basename(config_path))}"
        extra_hosts = workflow.get("hosts", [])

    try:
        rm = pyvisa.ResourceManager("@py")
    except Exception as exc:
        print(f"Error opening VISA resource manager: {exc}")
        sys.exit(1)

    instruments_config = {}

    with Spinner("Scanning instruments") as spinner:
        discovered = discover(rm, spinner, extra_hosts)

        if not discovered:
            pass  # handled after spinner exits
        elif args.save_current:
            instruments_config, results = run_save(rm, discovered, spinner)
        elif args.reset_bench:
            results = run_reset(rm, discovered, spinner)
        else:
            results = run_setup(rm, discovered, workflow, spinner)

    rm.close()

    if not discovered:
        print("No VISA instruments found.")
        return

    if args.save_current:
        outfile = f"{args.save_current}.json"
        output = {
            "name": args.save_current,
            "description": f"Saved from connected instruments — {time.strftime('%Y-%m-%d %H:%M')}",
            "instruments": instruments_config,
        }
        if extra_hosts:
            output["hosts"] = extra_hosts
        with open(outfile, "w") as f:
            json.dump(output, f, indent=2)
        print(f"Saved to {os.path.abspath(outfile)}")
        print()
    elif workflow:
        print(f"Workflow : {workflow.get('name', '—')}")
        if workflow.get("description"):
            print(f"          {workflow['description']}")
        print()

    ok = skipped = failed = 0
    for model, resource, success, message in results:
        marker = "✓" if success is True else ("–" if success is None else "✗")
        print(f"  {marker}  {model}")
        print(f"       {message}")
        print(f"       {resource}")
        print()
        if success is True:
            ok += 1
        elif success is None:
            skipped += 1
        else:
            failed += 1

    print("=" * 50)
    print(f"Done: {ok} configured, {skipped} skipped, {failed} failed.")


if __name__ == "__main__":
    main()
