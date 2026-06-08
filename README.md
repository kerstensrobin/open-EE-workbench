# Open-EE-workbench

A non-proprietary, cross-compatible VISA toolset for automating the modern electronic engineering workbench.
Open-EE-workbench analyses your bench and provides a standard set of workflows that work out of the box, regardless of the brand(s) of your test & measurement equipment.
Built on PyVISA and PyVISA-py, with SCPI dialect coverage sourced from manufacturer programming manuals across Keysight, Tektronix, Rohde & Schwarz, Rigol, Siglent, and more.

> Forever a work in progress.

## Getting started

### 1. Install

```bash
git clone https://github.com/kerstensrobin/open-EE-workbench
cd open-EE-workbench
python install.py
```

`install.py` installs all required Python packages and (on Linux) creates a `.desktop` launcher so the app appears in your application menu.

**Required packages** (installed automatically):
`pyvisa`, `pyvisa-py`, `pyusb`, `pyserial`, `flask`, `flask-socketio`, `pywebview`

**Optional packages** (prompted during install):
- `zeroconf` — mDNS / LAN instrument discovery

### 2. Linux: USB permissions

On Linux, USBTMC instruments (scopes, AWGs, PSUs connected via USB) require a udev rule so PyVISA-py can access them without root:

```bash
python core/nachoVisa.py --fix-udev
```

Re-plug your USB instruments after running this. You only need to do it once.

### 3. Scan your bench

```bash
python core/nachoVisa.py
```

This scans USB and LAN for VISA instruments and saves the result as a named **workbench** file. Give it a name that describes your setup (e.g. `lab_desk`, `portable_rig`). The active workbench is used by all subsequent scripts and the GUI.

```bash
python core/nachoVisa.py --usb-only                # skip LAN scan
python core/nachoVisa.py --host 192.168.1.50       # probe a specific IP
python core/nachoVisa.py --subnet 192.168.1.0/24   # scan a subnet
python core/nachoVisa.py --save my_lab             # save without prompting
python core/nachoVisa.py --debug                   # verbose output
```

### 4. Launch the GUI

```bash
./open-eew                  # Linux / macOS — native window
./open-eew --browser        # open in system browser instead
./open-eew gu128desk        # pre-load a named workbench
open-eew.bat                # Windows
```

Or directly with Python from the project folder:

```bash
python app.py
python app.py --browser
```

The GUI shows a card for each instrument in the active workbench. From there you can control outputs, capture screenshots, and run the SCPI console.

Instruments that are not automatically recognised show an **Assign instrument…** button. Hover any recognised instrument card to reveal a **✎** edit button — both open a picker to manually assign a type, vendor, and family. The assignment is saved to the workbench file and takes effect immediately without reconnecting.

Two additional tabs are available:

- **Automation** — generic parametric tests: DC sweep (multi-channel, nested or simultaneous), PSU interrupt transient capture, AC frequency sweep, DMM logger, waveform analysis. Results are saved as CSV to `./results/`.
- **Plot Specific** — purpose-built transistor characterisation tests with live canvas plots:
  - *Static Characteristic (IV Curve)* — sweeps V_CE / V_DS, parameterised by base current (CC mode) or gate voltage. Plots a family of I_C / I_D curves.
  - *Transfer Characteristic* — sweeps V_BE / V_GS at a fixed collector/drain bias. Plots I_C / I_D vs gate/base voltage.
  - FET / BJT mode toggle; Y-axis fixed to 0 – I_limit during sweep for stable display; **⊡ Auto** button to fit to data after the run; scroll-to-zoom and drag-to-pan; PNG and CSV export to `./results/`.
- **Sandbox** — build custom tests with a column-based pipeline editor. See [Building custom tests](#building-custom-tests) below.

---

## Building custom tests

The **Sandbox** tab lets you design arbitrary test sequences without writing code. Tests are defined as a pipeline of **loop columns** followed by an **actions column**, matching the nested-loop structure of most EE characterisation work.

### Concepts

```
┌──────────────┬──────────────┬──────────────────────────────┐
│  Loop 1      │  Loop 2      │  Actions                     │
│  (outermost) │  (innermost) │  (run at every combination)  │
│              │              │                              │
│  Sweep V_CE  │  Sweep V_BE  │  [Set CH1 current]           │
│  1 → 5 V     │  0 → 0.8 V  │  [Wait 50 ms]                │
│  step 2 V    │  step 0.01 V │  [Measure I_C → "I_C"]       │
└──────────────┴──────────────┴──────────────────────────────┘
```

- **Loop columns** (left → right = outer → inner). Each loop sweeps one instrument parameter across a range. The total number of measurement rows = product of all loop sizes.
- **Actions** run once per combination of loop values. Actions execute top-to-bottom.

### Loop block fields

| Field | Description |
|---|---|
| Variable | Short name used to reference this loop's current value in Set actions (e.g. `v_ce`). |
| Label | Column header in the CSV output. |
| Instrument | Which instrument to control. Populated from the active workbench. Leave blank to use the first available PSU. |
| Channel | Instrument channel (1–4). |
| Parameter | `Voltage` (CV mode) or `Current (CC)` (PSU constant-current mode). |
| Start / Stop / Step | Sweep range. Step direction is inferred automatically. |
| Settle | Time to wait after setting the new value before executing actions. |
| I Limit / V Compliance | Current limit for voltage sweeps; compliance voltage for CC sweeps. |

### Action block types

**Set** — writes a value to an instrument at each loop step. The value field accepts a literal number (`0.001`) or a reference to a loop variable (`{v_ce}`).

**Measure** — reads one value from an instrument and records it as a named column in the output. Parameters: `Voltage (DC)`, `Current`, `Voltage (AC)`, `Resistance`, `Resistance (4W)`.

**Wait** — fixed settle delay in seconds.

**Wait for State** — polls a measurement repeatedly until a condition is met before allowing the loop to continue. Useful for environmental conditioning (climate chambers, thermal soak, etc.).

| Field | Description |
|---|---|
| Instrument / Channel / Parameter | What to read on each poll. |
| Condition | `≥`, `≤`, or `±` (within tolerance). |
| Target | The value to wait for. |
| Tolerance | Used with `±` condition only. |
| Poll every | Seconds between readings. |
| Timeout | Give up and continue after N seconds (0 = wait forever). Progress is logged each poll. |

### Cross-compatibility via eewBackbone

All instrument commands go through `core/eewBackbone.json` — the same SCPI abstraction layer used by all built-in tests. This means a Sandbox test you build on a Keysight PSU will run unchanged on a Rigol or Siglent PSU with the same parameter types, provided both families define the relevant command.

The mapping used for loop/action parameters:

| Parameter | SCPI operation |
|---|---|
| Voltage | `set_voltage` / `measure_voltage` |
| Current (CC) | `set_current_limit` + output cycle |
| Voltage (AC) | `measure_vac` |
| Resistance | `measure_r` |
| Resistance (4W) | `measure_r4w` |

To check which operations an instrument family supports, look up its `id` in `core/eewBackbone.json`. Any family that declares the required command key will work with that parameter type. Families that inherit from a parent (`"inherits": "parent_id"`) automatically get the parent's commands unless overridden.

### Save and load

Tests are exported as plain JSON (`💾 Save`) and reloaded with `📂 Load`. The JSON schema is intentionally simple:

```json
{
  "name": "IV at temperature",
  "loops": [
    { "var": "v_ce", "label": "V_CE", "instrument": "USB0::...", "ch": 1,
      "param": "voltage", "start": 0, "stop": 5, "step": 0.5,
      "settle": 0.1, "i_limit": 0.5 }
  ],
  "actions": [
    { "type": "wait_for", "instrument": "USB0::...", "ch": 1, "param": "voltage",
      "condition": ">=", "target": 25.0, "interval": 10, "timeout": 600,
      "label": "Wait 25°C" },
    { "type": "measure", "instrument": "USB0::...", "ch": 1,
      "param": "current", "label": "I_C", "samples": 3, "settle": 0.05 }
  ]
}
```

Results are auto-saved as CSV to `./results/` alongside all other test output.

---

## Workflow

The typical session looks like this:

```
scan bench  →  GUI or scripts  →  save results
```

**Switching workbenches** — if you have multiple setups saved:

```bash
python core/nachoVisa.py --set-active my_other_lab
```

**Applying a known instrument state** before a test session:

```bash
python core/setWorkbench.py                   # apply workbench_config.json
python core/setWorkbench.py --set foo.json    # apply a specific config
python core/setWorkbench.py --reset-bench     # drive all instruments to safe defaults
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

`"hosts"` lists IP addresses of Ethernet instruments (LAN instruments are not auto-discovered by the `@py` backend). `--reset-bench` bypasses the config and drives all instruments to a hardcoded safe state.

---

## CLI scripts

Scripts in `scripts/` are standalone — run them directly with Python.

| Script | What it does |
|---|---|
| `screenshot.py` | Capture a screenshot from the active workbench scope |
| `acAnalysis.py` | AC frequency sweep: step a generator through frequencies from a CSV, record Vpp on scope CH1/CH2 |
| `dcSweep.py` | Step one or both PSU channels across a voltage range; log V/I at each point |
| `psuInterrupt.py` | V1 → interrupt (off or V2) → V3 cycle; sweep interrupt duration and/or voltage across multiple runs |
| `waveformAnalysis.py` | Live waveform analysis: autoscale, measure freq/Vpp/risetime, save screenshot and CSV |

`scripts/cgb-US21x-equipment/` contains instrument-specific examples for the Keysight EDU lab kit (EDU33211A AWG, EDU34450A DMM, EDU36311A PSU) and the Korad KA3005P.

All scripts that use workbench role lookup accept `--workbench <name>` to override the active workbench.

---

## Workbench files

Saved in `workbenches/<name>.json`. Each entry records:

```json
{
  "resource": "USB0::...", "connection": "USB",
  "manufacturer": "Keysight", "model": "EDU36311A", "serial": "...",
  "type": "psu", "role": "psu", "family_id": "keysight_edu36311a"
}
```

`role` is how scripts and the GUI find instruments — `scope`, `psu`, `generator`, `dmm`. Scripts work regardless of which USB port or IP an instrument is on.

`workbenches/active.json` is a symlink to the current workbench.

---

## SCPI coverage — eewBackbone

`core/eewBackbone.json` is a vendor-neutral SCPI command database. `core/eewBackbone.py` loads it and is used by all scripts and the GUI backend.

| Type | Families |
|---|---|
| Oscilloscope | 25 |
| AWG / Function generator | 15 |
| Power supply | 15 |
| Multimeter | 9 |
| SMU | 3 |
| Electronic load | 2 |

**69 families total across 15 vendors:** AIM-TTI, Agilent, BK Precision, Fluke, GW INSTEK, Hantek, Keithley, Keysight, Korad, OWON, Rigol, Rohde & Schwarz, Siglent, Tektronix, Teledyne.

To add a new instrument: add an entry to `core/eewBackbone.json` with `id`, `type`, `patterns` (IDN substrings), and `commands`. No code changes needed — `classify()` picks it up automatically. Families can declare `"inherits": "parent_id"` to reuse a parent's command set with selective overrides.

---

## Driver notes

`pyserial` is required for USB-to-serial instruments (PSUs and DMMs that use a Prolific PL2303, FTDI FT232, Silabs CP210x, or CH340 adapter).

- **Linux** — the `pl2303` module is included in all mainstream distros. Add your user to the `dialout` group: `sudo usermod -aG dialout $USER` (re-login required).
- **macOS** — Apple Silicon / macOS 12+ include a driver. Older macOS or clone chips (PL2303HXA) may need the [Prolific macOS driver](https://www.prolific.com.tw/US/ShowProduct.aspx?p_id=229).
- **Windows** — Windows 10/11 include a driver for genuine PL2303 chips. Clone chips may need the [Prolific Windows driver](https://www.prolific.com.tw/US/ShowProduct.aspx?p_id=225) or the [CH340 driver](https://www.wch-ic.com/downloads/CH341SER_EXE.html).

---

## Note on documentation

The `documentation/` folder is used locally to store vendor programming manuals referenced during development. These files are not tracked in version control. The SCPI command definitions in `eewBackbone.json` are derived from those manuals but are expressed as independent, non-verbatim structured data.

---

Made with love by [nacho.works](https://nacho.works)
