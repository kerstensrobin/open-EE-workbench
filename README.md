# Open-EE-workbench

A non-proprietary, cross-compatible VISA toolset for automating the modern electronic engineering workbench.
Works out of the box with any combination of supported instruments — no vendor lock-in.

Built on PyVISA and PyVISA-py with SCPI dialect coverage across **69 instrument families** from **15 vendors**: Keysight, Rigol, Rohde & Schwarz, Tektronix, Siglent, Agilent, and more.

> Forever a work in progress.

📖 **[Full documentation on the Wiki](https://github.com/kerstensrobin/open-EE-workbench/wiki)**

---

## Quick start

```bash
git clone https://github.com/kerstensrobin/open-EE-workbench
cd open-EE-workbench
python install.py             # install deps + optional desktop launcher
python core/nachoVisa.py      # scan bench, save workbench
./open-eew                    # launch GUI  (open-eew.bat on Windows)
```

On Linux, USB instruments need a one-time udev rule:
```bash
python core/nachoVisa.py --fix-udev   # then re-plug USB instruments
```

→ See the **[Getting Started](https://github.com/kerstensrobin/open-EE-workbench/wiki/Getting-Started)** wiki page for the full walkthrough.

---

## What's inside

| | |
|---|---|
| **Workbench GUI** | Instrument cards with live controls for scope, PSU, AWG, and DMM. Save / load / reset instrument state snapshots. Unrecognised instruments can be manually assigned a family. |
| **Automation** | Parametric tests: DC sweep (single / simultaneous / nested), PSU interrupt transient, AC frequency sweep, DMM logger. |
| **Plot Specific** | BJT / FET characterisation — Static Characteristic (IV Curve) and Transfer Characteristic with live canvas plots. |
| **Sandbox** | Build arbitrary test sequences with a no-code column-based pipeline editor. |
| **CLI scripts** | Standalone Python scripts for screenshots, sweeps, and waveform analysis. |

---

## SCPI coverage

| Type | Families |
|---|---|
| Oscilloscope | 25 |
| AWG / Function generator | 15 |
| Power supply | 15 |
| Multimeter | 9 |
| SMU | 3 |
| Electronic load | 2 |

To add a new instrument, add an entry to `core/eewBackbone.json` — no code changes needed.
→ **[eewBackbone Reference](https://github.com/kerstensrobin/open-EE-workbench/wiki/eewBackbone-Reference)**

---

## Wiki

| Page | |
|---|---|
| [Getting Started](https://github.com/kerstensrobin/open-EE-workbench/wiki/Getting-Started) | Install, USB permissions, scan, launch |
| [Workbench and Instruments](https://github.com/kerstensrobin/open-EE-workbench/wiki/Workbench-and-Instruments) | Workbench files, roles, instrument assignment |
| [Automation Tests](https://github.com/kerstensrobin/open-EE-workbench/wiki/Automation-Tests) | Built-in parametric tests |
| [Plot Specific](https://github.com/kerstensrobin/open-EE-workbench/wiki/Plot-Specific) | BJT / FET characterisation |
| [Sandbox](https://github.com/kerstensrobin/open-EE-workbench/wiki/Sandbox) | Custom test builder |
| [eewBackbone Reference](https://github.com/kerstensrobin/open-EE-workbench/wiki/eewBackbone-Reference) | SCPI database, adding instruments |
| [CLI Scripts](https://github.com/kerstensrobin/open-EE-workbench/wiki/CLI-Scripts) | nachoVisa.py, setWorkbench.py, standalone scripts |

---

Made with love by [nacho.works](https://nacho.works)
