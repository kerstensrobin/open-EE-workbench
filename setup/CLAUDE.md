# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

Open-EE-workbench is a PyVISA-based automation framework for lab instruments (oscilloscopes, power supplies, AWGs, multimeters) from many vendors. All scripts run from `programs/` and require `pyvisa`, `pyvisa-py`, `pyusb`, and optionally `zeroconf`.

## Running scripts

All scripts are run directly with Python from `programs/`:

```bash
python nachoVisa.py                       # scan USB + LAN, save workbench
python nachoVisa.py --fix-udev            # write udev rules for USBTMC (Linux)
python nachoVisa.py --set-active NAME     # switch active workbench

python setWorkbench.py                    # apply workbench_config.json
python setWorkbench.py --set foo.json     # apply a specific config
python setWorkbench.py --reset-bench      # drive all instruments to safe defaults

python screenshot.py [file.png]           # capture oscilloscope screenshot
python acAnalysis.py                      # frequency sweep (reads acAnalysis.csv)
```

There are no test suites or build steps.

## Architecture

### Workbench files

`nachoVisa.py` saves discovered instruments as `../workbenches/<name>.json`. One is symlinked as `../workbenches/active.json`. Each entry records the VISA resource string, connection type, manufacturer/model/serial, `type` (scope/psu/etc.), `role` (scope/generator/psu/dmm), and `family_id` (key into `instruments.json`).

`workbench.py` provides helpers used by all other scripts:
- `load_workbench(name=None)` — loads the named or active workbench JSON
- `open_by_role(rm, wb, role)` — opens a PyVISA resource looked up by role

Role-based access means scripts work regardless of which port or IP an instrument is on.

### instruments.json / instruments.py

`instruments.json` is the vendor-neutral SCPI command database (57 families, 13 vendors). `instruments.py` loads it and exposes:
- `classify(idn)` — match IDN string → resolved family dict
- `get_command(family, operation)` — retrieve SCPI steps for an operation
- `resolve_command(cmd, **kwargs)` — expand `{ch}`, `{value}`, etc. placeholders

**Inheritance model:** families can declare `"inherits": "parent_family_id"` and `"overrides": {...}`. A `null` override removes an inherited command; new keys add extensions. `classify()` merges the chain so callers get a single flat dict.

**Command spec formats:**
- `"CMD {ch}"` — single write (string)
- `["CMD1", "CMD2"]` — sequential writes
- `{"write": "CMD {value}", "query": "CMD?"}` — settable+readable property
- `{"query": "CMD?"}` — read-only
- `{"raw_query": "CMD?"}` — binary read (screenshots)
- `{"note": "..."}` — documentation only

**Placeholders:** `{ch}`, `{value}`, `{voltage}`, `{current}`, `{freq}`, `{amp}`, `{offset}`, `{unit}`, `{func}`, `{coupling}`, `{slope}`.

### setWorkbench.py instrument handlers

`setWorkbench.py` has one dedicated handler function per supported instrument model (e.g. `configure_edu36311a`, `configure_scope`). Each handler receives the open PyVISA resource and the relevant block from the workflow JSON. To add support for a new instrument, add its IDN patterns to `instruments.json` and a handler function in `setWorkbench.py`.

### screenshot.py transport differences

USBTMC (USB) connections require reading in 4 KB chunks until a short packet signals EOF. TCPIP connections use a single `read_raw()`. The script detects connection type from the resource string prefix (`USB` vs `TCPIP`).
