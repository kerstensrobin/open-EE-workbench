# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Open-EE-workbench is a PyVISA-based automation framework for lab instruments (oscilloscopes, power supplies, AWGs, multimeters, electronic loads) covering 73 instrument families across 15 vendors. It has a web GUI front-end and a shared core library.

## Running the app

```bash
python app.py                   # web GUI — native window (PyWebView)
python app.py --browser         # web GUI — system browser
python app.py --port 5173       # different port
python app.py gu128desk         # pre-load a named workbench

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
```

## Architecture

**`app.py`** — Flask + SocketIO server (`/api/*` REST + SocketIO events) rendered inside a PyWebView native window. The web UI lives in `ui/index.html`. All instrument I/O is dispatched to a `ThreadPoolExecutor` (8 workers); results are pushed to the browser via SocketIO events (`connection_result`, `psu_reading`, `load_reading`, `scope_measurement`, `automation_row`, etc.). A `threading.Lock` per resource (`res._visa_lock`) serialises concurrent USB access.

### Routes (`routes/`)

API logic is split across five blueprints registered in `app.py`:

- **`routes/connection.py`** — `POST /api/connect`, `POST /api/disconnect`. Opens PyVISA resources, creates `DemoResource` objects for workbenches with `DEMO::` resource strings, populates `_sh._state["resources"]` and `["families"]`, then calls `_start_polling()`.
- **`routes/workbench.py`** — workbench CRUD, instrument assignment, bench state save/load.
- **`routes/instruments.py`** — per-type control routes: `/api/scope/*`, `/api/psu/*`, `/api/awg/*`, `/api/dmm/*`, `/api/eload/*`, and raw SCPI (`/api/scpi`).
- **`routes/automation.py`** — `GET /api/automation/tests`, `POST /api/automation/run`, pause/stop/resume. Contains `_suggest_tests()` and all runner functions.
- **`routes/system.py`** — misc system routes (bench state, plot image save, etc.).

### Core library (`core/`)

**`eewBackbone.json` + `eewBackbone.py`** — the central SCPI abstraction layer. The JSON file defines 73 instrument families with IDN match patterns and command specs. `eewBackbone.py` exposes:
- `classify(idn)` → resolved family dict or `None`
- `get_command(family, operation, **kwargs)` → `list[(action, scpi_string)]`  
  where `action` ∈ `{"write", "query", "raw_query"}`

Families support inheritance: `"inherits": "parent_id"` + `"overrides": {...}`. A `null` override removes a parent command; new keys extend it. `classify()` always returns the fully-merged dict.

Command spec formats in the JSON:
- `"CMD {ch}"` — single write
- `["CMD1", {"write": "CMD2 {value}", "query": "CMD2?"}]` — sequential steps
- `{"query": "CMD?"}` — read-only
- `{"raw_query": "CMD?"}` — binary read (screenshots via `read_raw()`)

Placeholder variables: `{ch}`, `{value}`, `{freq}`, `{amp}`, `{offset}`, `{unit}`, `{func}`, `{coupling}`, `{slope}`, `{attenuation}`, `{label}`, `{text}`.

**`helpers.py`** — shared SCPI helpers used by all routes:
- `_find_instrument(itype)` → `(resource, family)` for the first connected instrument of that type
- `_run_steps(resource, steps, role, poll)` → executes write/query steps, returns last query result
- `_op(res, fam, operation, role, **kwargs)` → convenience wrapper around `get_command` + `_run_steps`
- `_start_polling()` — background thread that polls PSU channels and load instruments every 1.5 s and emits `psu_reading` / `load_reading` SocketIO events

**`workbench.py`** — workbench file helpers:
- `load_workbench(name=None)` — loads named or active workbench from `workbenches/`
- `open_by_role(rm, wb, role)` — opens a PyVISA resource by role
- `set_active(name)` — makes `workbenches/active.json` a symlink to `<name>.json`
- `active_name()` — returns the current active workbench name

**`nachoVisa.py`** — instrument discovery: scans USB (USBTMC + USB-serial via `pyserial`) and LAN (TCP port 5025 sweep + optional mDNS). Writes discovered instruments to `workbenches/<name>.json` and sets `active.json`.

**`demo.py`** — `DemoResource`: drop-in fake VISA resource for offline testing. `write()` tracks set-points; `query()` returns semi-realistic noisy values per instrument type (scope, psu, awg, dmm, load). Used automatically when a workbench has `DEMO::` resource strings.

**`setWorkbench.py`** — loads a workflow JSON (`workbench_config.json`) and drives all instruments to the specified state. `--reset-bench` drives everything to safe defaults.

### Instrument types

`type` in the workbench JSON and `eewBackbone.json` determines which card is rendered and which routes handle the instrument:

| type | Card | Poll event | Routes |
|---|---|---|---|
| `scope` | Scope card | — | `/api/scope/*` |
| `psu` | PSU card | `psu_reading` | `/api/psu/*` |
| `awg` | AWG card | — | `/api/awg/*` |
| `dmm` | DMM card | — | `/api/dmm/*` |
| `load` | Electronic Load card | `load_reading` | `/api/eload/*` |
| `smu` | Generic card | — | (manual SCPI only) |

### Workbench files

Stored in `workbenches/<name>.json`. Each instrument entry records:
```json
{ "resource": "USB0::...", "connection": "USB", "manufacturer": "...",
  "model": "...", "serial": "...", "type": "load", "role": "load",
  "family_id": "korad_kel" }
```
`type` and `role` are the key fields. `family_id` keys into `eewBackbone.json` for SCPI dispatch. `workbenches/active.json` is a symlink to the current workbench. `workbenches/demo.json` is version-controlled and provides the demo workbench (scope + PSU + AWG + 2× DMM + load).

### USBTMC patch in `app.py`

`app.py` monkeypatches `pyvisa_py.protocols.usbtmc.USBTMC.read` at startup (`_apply_usbtmc_patch()`). This fixes two issues in pyvisa-py ≤ 0.8.1: a bug where `or` instead of `and` caused hangs on wMaxPacketSize-aligned USB chunks (e.g. Rigol scope screenshots), and a ~12 s performance issue from Python overhead per 64-byte USB packet. The patch reads USBTMC headers from the first packet then drains remaining data directly from the bulk-IN endpoint.

### Adding a new instrument family

1. Add a new entry to `core/eewBackbone.json` — set `id`, `type`, `patterns` (IDN substrings), and `commands`. Use `"inherits"` if it's a variant of an existing family.
2. No code changes needed; `classify()` picks it up automatically.
3. If the `type` is a new value not already handled (e.g. not scope/psu/awg/dmm/load), the instrument will appear as a generic card with a manual SCPI input only.

### Adding a new automation test

1. Add the test stub to `_KNOWN` in `routes/automation.py::_suggest_tests()`.
2. Add a fully-described entry (with `params` and `columns`) to the type-conditional block in `_suggest_tests()`.
3. Write a `_run_<test_id>()` closure inside `api_automation_run()` and register it in the `runners` dict at the bottom of that function.
