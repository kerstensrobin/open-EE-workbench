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

→ See the **[Wiki](https://github.com/kerstensrobin/open-EE-workbench/wiki)** for the full walkthrough.

---

## What's inside

| | |
|---|---|
| **Workbench GUI** | Instrument cards with live controls for scope, PSU, AWG, and DMM. Save / load / reset instrument state snapshots. Unrecognised instruments can be manually assigned a family. |
| **Automation** | Parametric tests: DC sweep (single / simultaneous / nested), PSU interrupt transient, AC frequency sweep, DMM logger. |
| **Plot Specific** | Purpose-built tests that produce specific, well-defined plots — the kind that appear in a component datasheet. Currently: Static Characteristic (IV Curve) and Transfer Characteristic for BJT / FET. |
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
→ **[eewBackbone](https://github.com/kerstensrobin/open-EE-workbench/wiki/eewBackbone)**

---

## Wiki

| Page | |
|---|---|
| [eewBackbone](https://github.com/kerstensrobin/open-EE-workbench/wiki/eewBackbone) | SCPI database, adding instruments |
| [Workbench and Instruments](https://github.com/kerstensrobin/open-EE-workbench/wiki/Workbench-and-Instruments) | Workbench files, roles, instrument assignment |
| [CLI](https://github.com/kerstensrobin/open-EE-workbench/wiki/CLI) | nachoVisa.py, setWorkbench.py, standalone scripts |
| [GUI - Automation](https://github.com/kerstensrobin/open-EE-workbench/wiki/GUI---Automation) | Built-in parametric tests |
| [GUI - Plot Specific](https://github.com/kerstensrobin/open-EE-workbench/wiki/GUI---Plot-Specific) | Datasheet-style plots — IV Curve, Transfer Characteristic |
| [GUI - Sandbox](https://github.com/kerstensrobin/open-EE-workbench/wiki/GUI---Sandbox) | Custom test builder |
| [About Testing](https://github.com/kerstensrobin/open-EE-workbench/wiki/About-Testing) | Engineering context for every test |

---

Made with love by [nacho.works](https://nacho.works)
