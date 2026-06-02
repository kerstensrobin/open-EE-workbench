# Open-EE-workbench
A non-proprietary, cross-compatible VISA toolset for automating the modern electronic engineering workbench.
Open-EE-workbench analyses your bench and provides a standard set of workflows that work out of the box, regardless of the brand(s) of your test & measurement equipment.
Built on pyVISA and pyVISA-py, with SCPI dialect coverage sourced from manufacturer programming manuals across Keysight, Tektronix, Rohde & Schwarz, Rigol, Siglent, and more.

Very much a work in progress.

## Folder structure

```
open-EE-workbench/
├── documentation/       # Reference materials (not tracked — see note below)
├── workbenches/         # Saved workbench definitions (JSON)
├── core/               # Core tools and libraries
│   ├── nachoVisa.py
│   ├── setWorkbench.py
│   ├── eewBackbone.py
│   ├── eewBackbone.json
│   ├── workbench.py
│   └── workbench_config.json
└── scripts/             # Runnable one-off scripts
    ├── screenshot.py
    ├── acAnalysis.py
    ├── dcSweep.py
    ├── psuInterrupt.py
    ├── koradVisa.py
    ├── multimeter.py
    ├── powersupply.py
    └── waveformGenerator.py
```

## Setup

### Dependencies

```bash
pip install pyvisa pyvisa-py pyusb pyserial
```

`pyserial` is required for USB-to-serial instruments (power supplies and DMMs that connect via a USB-serial adapter such as the Prolific PL2303, FTDI FT232, Silabs CP210x, or CH340/CH341 chips). Without it, those instruments will not be discovered.

**Driver note — Prolific PL2303 (and clones):**
- **Linux** — the `pl2303` kernel module is included in all mainstream distributions; plug in the cable and `/dev/ttyUSBx` appears immediately. Ensure your user is in the `dialout` group (`sudo usermod -aG dialout $USER`; re-login required).
- **macOS** — Apple Silicon / macOS 12+ include a driver. Older macOS versions or clone chips (PL2303HXA, PL2303TA) may need the [Prolific macOS driver](https://www.prolific.com.tw/US/ShowProduct.aspx?p_id=229).
- **Windows** — Windows 10/11 include a driver for genuine PL2303 chips. Clone chips may require the [Prolific Windows driver](https://www.prolific.com.tw/US/ShowProduct.aspx?p_id=225) or the [CH340 driver](https://www.wch-ic.com/downloads/CH341SER_EXE.html) if the adapter uses a CH340.

### nachoVisa.py
Scans your local network and USB bus for VISA instruments, including USB-serial instruments. Includes dependency diagnostics and an automatic udev rule fix for Arch and Debian-based systems. After a successful scan it offers to save the result as a named **workbench** JSON file (see [Workbench files](#workbench-files) below).

```
python core/nachoVisa.py                           # scan USB + LAN
python core/nachoVisa.py --usb-only                # USB only
python core/nachoVisa.py --host 192.168.1.50       # probe a specific IP
python core/nachoVisa.py --subnet 192.168.1.0/24   # scan a subnet
python core/nachoVisa.py --save my_lab             # scan and save workbench without prompting
python core/nachoVisa.py --fix-udev                # write udev rules for detected USBTMC devices
python core/nachoVisa.py --debug                   # verbose output
```

### setWorkbench.py
Configures all connected instruments to a known state defined by a JSON workflow file. Auto-discovers USB instruments and probes any Ethernet instruments listed in the config. Supports Keysight EDU36311A (PSU), EDU33211A (AWG), and a range of common oscilloscopes.

```
python core/setWorkbench.py                        # apply workbench_config.json
python core/setWorkbench.py --set foo.json         # apply a specific workflow file
python core/setWorkbench.py --reset-bench          # reset all instruments to safe defaults
python core/setWorkbench.py --save-current NAME    # read current settings and save to NAME.json
```

**Workflow config (`core/workbench_config.json`):**
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

### eewBackbone.py / eewBackbone.json
A vendor-neutral SCPI abstraction layer. `eewBackbone.json` contains 68 instrument families spanning 374 IDN match patterns across 14 vendors. SCPI dialects are derived from vendor-provided programming manuals.

| Type | Families |
|---|---|
| Oscilloscope | 25 |
| AWG / Function generator | 14 |
| Power supply | 15 |
| Multimeter | 9 |
| SMU | 3 |
| Electronic load | 2 |

Vendors covered: AIM-TTI, BK Precision, Fluke, GW INSTEK, Hantek, Keithley, Keysight, Korad, OWON, Rigol, Rohde & Schwarz, Siglent, Tektronix, Teledyne.

`eewBackbone.py` loads the database and exposes `classify(idn)` and `resolve_command(cmd, **kw)` for use by other scripts.

## Scripts

- **screenshot.py** — Connects to the active workbench scope and captures a screenshot.
- **acAnalysis.py** — AC frequency sweep: steps a function generator through frequencies from a CSV and records Vpp on CH1/CH2 via the oscilloscope.
- **dcSweep.py** — Steps one or both PSU channels across a voltage range and logs measured V/I at each point. Supports 1-D and 2-D (bias + sweep) modes.
- **psuInterrupt.py** — Sets a PSU channel to V1, applies a timed interrupt (channel off or V2), then restores to V3. Logs V/I at the end of each phase. Supports sweeping the interrupt duration across multiple runs.
- **koradVisa.py**, **powersupply.py**, **multimeter.py**, **waveformGenerator.py** — Runnable examples for individual instrument classes.

## Workbench files

After scanning, `nachoVisa.py` asks whether to save the current bench. Workbench files are stored in `workbenches/<name>.json` and record each instrument's resource string, connection type, manufacturer/model/serial, and its **role** (`scope`, `generator`, `psu`, `dmm`). Scripts can load a workbench and bind to instruments by role rather than hard-coded resource strings, so they work regardless of which USB port or IP an instrument ends up on.

`nachoVisa.py` also reports which tests are ready to run given the roles present — e.g. `ac_frequency_sweep` requires a `scope` + `generator`, `psu_ramp_capture` requires a `scope` + `psu`.

## Note on documentation

The `documentation/` folder is used locally to store vendor programming manuals referenced during development. These files are not tracked in version control. The SCPI command definitions in `eewBackbone.json` are derived from those manuals but are expressed as independent, non-verbatim structured data.

---

Made with love by [nacho.works](www.nacho.works)
