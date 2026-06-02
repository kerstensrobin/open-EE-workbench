# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Open-EE-workbench is a PyVISA-based automation framework for lab instruments (oscilloscopes, power supplies, AWGs, multimeters) covering 68 instrument families across 14 vendors. It has two front-ends and a shared core library.

## Running the app

```bash
python app.py                   # web GUI — native window (PyWebView)
python app.py --browser         # web GUI — system browser
python app.py --port 5173       # different port
python app.py gu128desk         # pre-load a named workbench

python gui.py                   # Tkinter GUI (alternate front-end)
python gui.py --demo            # no hardware required

python core/nachoVisa.py        # scan USB + LAN, save workbench
python core/nachoVisa.py --fix-udev   # write udev rules for USBTMC (Linux)

python core/setWorkbench.py           # apply workbench_config.json
python core/setWorkbench.py --reset-bench
```

There are no test suites or build steps. Scripts in `scripts/` are standalone and run directly with Python.

## Dependencies

```bash
pip install pyvisa pyvisa-py pyusb pyserial flask flask-socketio pywebview
# Optional:
pip install zeroconf          # mDNS / LAN instrument discovery
pip install cairosvg pillow   # SVG logo rendering in Tkinter GUI
```

## Architecture

### Two front-ends, one core

**`app.py`** — primary front-end. Flask + SocketIO server (`/api/*` REST + SocketIO events) rendered inside a PyWebView native window. The web UI lives in `ui/index.html`. All instrument I/O is dispatched to a `ThreadPoolExecutor` (8 workers); results are pushed to the browser via SocketIO events (`connection_result`, `psu_reading`, `scope_measurement`, `automation_row`, etc.). A `threading.Lock` per resource (`res._visa_lock`) serialises concurrent USB access.

**`gui.py`** — alternate Tkinter front-end. Same core imports, card-per-instrument layout (`ScopeCard`, `PSUCard`, `AWGCard`, `DMMCard`). Uses `app._run_async(fn, callback)` pattern — background thread, callback dispatched on the Tk main thread via `after(0, ...)`.

### Core library (`core/`)

**`eewBackbone.json` + `eewBackbone.py`** — the central SCPI abstraction layer. The JSON file defines 68 instrument families with IDN match patterns and command specs. `eewBackbone.py` exposes:
- `classify(idn)` → resolved family dict or `None`
- `get_command(family, operation, **kwargs)` → `list[(action, scpi_string)]`  
  where `action` ∈ `{"write", "query", "raw_query"}`

Families support inheritance: `"inherits": "parent_id"` + `"overrides": {...}`. A `null` override removes a parent command; new keys extend it. `classify()` always returns the fully-merged dict.

Command spec formats in the JSON:
- `"CMD {ch}"` — single write
- `["CMD1", {"write": "CMD2 {value}", "query": "CMD2?"}]` — sequential steps
- `{"query": "CMD?"}` — read-only
- `{"raw_query": "CMD?"}` — binary read (screenshots via `read_raw()`)

Placeholder variables: `{ch}`, `{value}`, `{freq}`, `{amp}`, `{offset}`, `{unit}`, `{func}`, `{coupling}`, `{slope}`.

**`workbench.py`** — workbench file helpers:
- `load_workbench(name=None)` — loads named or active workbench from `workbenches/`
- `open_by_role(rm, wb, role)` — opens a PyVISA resource by role
- `set_active(name)` — makes `workbenches/active.json` a symlink to `<name>.json`
- `active_name()` — returns the current active workbench name

**`nachoVisa.py`** — instrument discovery: scans USB (USBTMC + USB-serial via `pyserial`) and LAN (TCP port 5025 sweep + optional mDNS). Writes discovered instruments to `workbenches/<name>.json` and sets `active.json`.

**`setWorkbench.py`** — loads a workflow JSON (`workbench_config.json`) and drives all instruments to the specified state. `--reset-bench` drives everything to safe defaults.

### Workbench files

Stored in `workbenches/<name>.json`. Each instrument entry records:
```json
{ "resource": "USB0::...", "connection": "USB", "manufacturer": "...",
  "model": "...", "serial": "...", "type": "scope", "role": "scope",
  "family_id": "rigol_ds1000z" }
```
`type` and `role` are the key fields scripts use. `family_id` keys into `eewBackbone.json` for SCPI dispatch. `workbenches/active.json` is a symlink to the current workbench.

### USBTMC patch in `app.py`

`app.py` monkeypatches `pyvisa_py.protocols.usbtmc.USBTMC.read` at startup (`_apply_usbtmc_patch()`). This fixes two issues in pyvisa-py ≤ 0.8.1: a bug where `or` instead of `and` caused hangs on wMaxPacketSize-aligned USB chunks (e.g. Rigol scope screenshots), and a ~12 s performance issue from Python overhead per 64-byte USB packet. The patch reads USBTMC headers from the first packet then drains remaining data directly from the bulk-IN endpoint.

### Adding a new instrument family

1. Add a new entry to `core/eewBackbone.json` — set `id`, `type`, `patterns` (IDN substrings), and `commands`. Use `"inherits"` if it's a variant of an existing family.
2. No code changes needed; `classify()` picks it up automatically.

### Adding a new automation test

Tests are defined inline in `app.py::_suggest_tests()`. Each test dict describes `id`, `name`, `requires` (instrument types), `params` (UI form fields), and `columns` (CSV output). The runner function (e.g. `_run_ac_sweep`, `_run_dc_sweep`) is a closure inside `api_automation_run()`.
