# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Open-EE-workbench is a PyVISA-based automation framework for lab instruments (oscilloscopes, power supplies, AWGs, multimeters, electronic loads) from many vendors. The `core/` directory contains the shared library used by `app.py` and standalone CLI scripts.

## Running the app

Run from the project root (not from `core/`):

```bash
python app.py                         # web GUI
python core/nachoVisa.py              # scan USB + LAN, save workbench
python core/nachoVisa.py --fix-udev   # write udev rules for USBTMC (Linux)
python core/setWorkbench.py           # apply workbench_config.json
python core/setWorkbench.py --reset-bench
```

There are no test suites or build steps.

## Architecture

### eewBackbone.json / eewBackbone.py

`eewBackbone.json` is the vendor-neutral SCPI command database (73 families, 15 vendors). `eewBackbone.py` loads it and exposes:
- `classify(idn)` — match IDN string → resolved family dict (with full inheritance merged in)
- `get_command(family, operation, **kwargs)` — retrieve resolved `(action, scpi_string)` steps
- `resolve_command(cmd, **kwargs)` — expand `{ch}`, `{value}`, etc. placeholders

**Inheritance model:** families can declare `"inherits": "parent_family_id"` and `"overrides": {...}`. A `null` override removes an inherited command; a new key adds an extension. `classify()` merges the full chain so callers always get a single flat dict.

**Command spec formats:**
- `"CMD {ch}"` — single write (string)
- `["CMD1", "CMD2"]` — sequential writes
- `{"write": "CMD {value}", "query": "CMD?"}` — settable+readable property (both steps run on write; only query on read)
- `{"query": "CMD?"}` — read-only
- `{"raw_query": "CMD?"}` — binary read (screenshots)
- `{"note": "..."}` — documentation only, never executed

**Placeholders:** `{ch}`, `{value}`, `{voltage}`, `{current}`, `{freq}`, `{amp}`, `{offset}`, `{unit}`, `{func}`, `{coupling}`, `{slope}`, `{attenuation}`, `{label}`, `{text}`.

**Instrument types in the backbone:** `scope`, `psu`, `awg`, `dmm`, `load`, `smu`.

**Pattern matching:** `classify()` iterates families in JSON order and returns the first match where any pattern string appears as a substring of the IDN (case-insensitive). More-specific families must therefore appear before broader catch-all families of the same vendor (e.g. `korad_kel` before `korad_ka`).

### helpers.py

Shared helpers used by all route modules:

- `_find_instrument(itype)` — searches `_sh._state["workbench"]["_unique"]` for the first connected instrument of the given type; returns `(resource, family)` or `(None, None)`
- `_run_steps(resource, steps, role, poll)` — executes `(action, scpi_string)` steps sequentially; `write` calls `resource.write()`, `query` calls `resource.query()` and returns the result, `raw_query` calls `resource.read_raw()`
- `_op(res, fam, operation, role, **kwargs)` — calls `get_command` then `_run_steps`; emits `scpi_traffic` SocketIO events
- `_rlock(res)` — returns `res._visa_lock` (or a throwaway Lock if the resource has none)
- `_start_polling()` — launches a background executor task that polls all `psu` and `load` instruments in the workbench every 1.5 s, emitting `psu_reading` and `load_reading` SocketIO events
- `_family_for(entry)` — looks up a workbench entry's `family_id` in the backbone index

### workbench.py

Workbench file helpers used by app routes and CLI scripts:
- `load_workbench(name=None)` — loads the named or active workbench JSON from `workbenches/`
- `open_by_role(rm, wb, role)` — opens a PyVISA resource looked up by role string
- `set_active(name)` — makes `workbenches/active.json` a symlink to `<name>.json`
- `active_name()` — returns the name of the currently active workbench

### nachoVisa.py

Instrument discovery: scans USB (USBTMC + USB-serial via `pyserial`) and LAN (TCP port 5025 sweep + optional mDNS via `zeroconf`). Writes discovered instruments to `workbenches/<name>.json` and symlinks `active.json`.

### setWorkbench.py

Loads a workflow JSON (`workbench_config.json`) and drives all instruments to the specified state using backbone commands. `--reset-bench` drives everything to safe defaults.

### demo.py

`DemoResource` is a drop-in fake VISA resource used when a workbench has `DEMO::` resource strings (no real hardware needed). `write()` tracks set-points; `query()` dispatches to `_fake_value()` which returns type-appropriate noisy responses:
- `scope` — Vpp, Vmax, Vmin, Freq, Period, duty cycle, rise/fall times
- `psu` — voltage/current setpoints and measurements with slow sinusoidal drift
- `awg` — frequency and amplitude readback
- `dmm` — voltage/current/resistance with per-slot de-correlated noise
- `load` — simulates a discharging battery (voltage declines over time toward a floor)
