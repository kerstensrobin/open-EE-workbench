# Open-EE-workbench
A non-proprietary, cross-compatible VISA toolset for automating the modern electronic engineering workbench.
Open-EE-workbench analyses your bench and provides a standard set of workflows that work out of the box, regardless of the brand(s) of your test & measurement equipment.
Built on pyVISA and pyVISA-py, with SCPI dialect coverage sourced from manufacturer programming manuals across Keysight, Tektronix, Rohde & Schwarz, Rigol, Siglent, and more.

Very much a work in progress.

## Scripts

### nachoVisa.py
Scans your local network and USB bus for VISA instruments. Includes dependency diagnostics and an automatic udev rule fix for Arch and Debian-based systems. After a successful scan it offers to save the result as a named **workbench** JSON file (see [Workbench files](#workbench-files) below).

```
python nachoVisa.py                           # scan USB + LAN
python nachoVisa.py --usb-only                # USB only
python nachoVisa.py --host 192.168.1.50       # probe a specific IP
python nachoVisa.py --subnet 192.168.1.0/24   # scan a subnet
python nachoVisa.py --save my_lab             # scan and save workbench without prompting
python nachoVisa.py --fix-udev                # write udev rules for detected USBTMC devices
python nachoVisa.py --debug                   # verbose output
```

### setWorkbench.py
Configures all connected instruments to a known state defined by a JSON workflow file. Auto-discovers USB instruments and probes any Ethernet instruments listed in the config. Supports Keysight EDU36311A (PSU), EDU33211A (AWG), and a range of common oscilloscopes.

```
python setWorkbench.py                        # apply workbench_config.json
python setWorkbench.py --set foo.json         # apply a specific workflow file
python setWorkbench.py --reset-bench          # reset all instruments to safe defaults
python setWorkbench.py --save-current NAME    # read current settings and save to NAME.json
```

**Workflow config (`workbench_config.json`):**
```json
{
  "name": "Lab Ready",
  "hosts": ["192.168.1.100"],
  "instruments": {
    "edu36311a": {
      "outputs": [
        { "channel": 1, "voltage": 5.0, "current_limit": 0.5, "enabled": false }
      ]
    },
    "edu33211a": {
      "channels": [
        { "channel": 1, "function": "SIN", "frequency": 1000, "amplitude": 1.0,
          "amplitude_unit": "VPP", "offset": 0.0, "enabled": false }
      ]
    },
    "scope": { "reset": true }
  }
}
```

`"hosts"` lists IP addresses of Ethernet instruments to probe (the `@py` backend does not auto-discover LAN instruments). The `--reset-bench` flag bypasses the config and drives all instruments to a hardcoded safe state: PSU outputs off at 0 V / 500 mA, AWG set to 1 kHz 1 Vpp sine off, scopes recalled to default setup.

### instruments.py / instruments.json
A vendor-neutral SCPI abstraction layer. `instruments.json` contains 57 instrument families spanning 320 IDN match patterns across 13 vendors:

| Type | Families |
|---|---|
| Oscilloscope | 22 |
| AWG / Function generator | 12 |
| Power supply | 11 |
| Multimeter | 9 |
| SMU | 2 |
| Electronic load | 1 |

Vendors covered: AIM-TTI, Fluke, GW INSTEK, Hantek, Keithley, Keysight, Korad, OWON, Rigol, Rohde & Schwarz, Siglent, Tektronix.

`instruments.py` loads the database and exposes `classify(idn)` and `resolve_command(cmd, **kw)` for use by other scripts.

### Other scripts
- **acAnalysis.py** — AC frequency sweep: steps a function generator through frequencies from a CSV and records Vpp on CH1/CH2 via the oscilloscope.
- **screenshot.py** — Connects to a scope and captures a screenshot.
- **koradVisa.py**, **powersupply.py**, **multimeter.py**, **waveformGenerator.py** — Utility scripts for individual instrument classes.

## Workbench files

After scanning, `nachoVisa.py` asks whether to save the current bench. Workbench files are stored in `programs/workbenches/<name>.json` and record each instrument's resource string, connection type, manufacturer/model/serial, and its **role** (`scope`, `generator`, `psu`, `dmm`). Test scripts can load a workbench and bind to instruments by role rather than hard-coded resource strings, so they work regardless of which USB port or IP an instrument ends up on.

`nachoVisa.py` also reports which tests are ready to run given the roles present — e.g. `ac_frequency_sweep` requires a `scope` + `generator`, `psu_ramp_capture` requires a `scope` + `psu`.

---

Made with love by [nacho.works](www.nacho.works)
