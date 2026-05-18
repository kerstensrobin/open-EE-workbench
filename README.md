# Open-EE-workbench
A non proprietary, cross-compatible VISA toolset for automating the modern day electronic engineering workbench.
Open-EE-workbench strives to implement a workflow that analyses your bench, and provides a set of standard test that work out of the box, regardless of brand(s) of your TME.
Relying hard on python libraries such as pyVISA and pyVISA-py, as well as programming manuals that are provided by all different manufaturers such as Keysight, Tektronix, Rohde&Shwarz, Rigol, Siglent, and more.

Very much a work in progress.

## Scripts
### nachoVisa.py
nachoVisa scans your local network and USB devices to check for available devices. It includes error diagnostics and an automatic udev-fix for Arch and Debian based devices.

### setWorkbench.py
setWorkbench configures all connected instruments to a known state defined by a JSON workflow file. It auto-discovers USB instruments and probes any Ethernet instruments listed in the config. Supported instruments: Keysight EDU36311A (power supply), EDU33211A (function generator), and a range of common oscilloscopes.

**Usage:**
```
python setWorkbench.py                        # apply workbench_config.json
python setWorkbench.py --config foo.json      # apply a specific workflow
python setWorkbench.py --reset-bench          # reset all instruments to safe defaults
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

`"hosts"` lists the IP addresses of any Ethernet instruments to probe (the pyvisa `@py` backend does not auto-discover LAN instruments). Multiple workflow files can coexist and are selected with `--config`. The `--reset-bench` flag bypasses the config and drives all instruments to a hardcoded safe state: PSU outputs off at 0 V / 500 mA, AWG set to 1 kHz 1 Vpp sine output off, scopes recalled to default setup.

Made with love by [nacho.works](www.nacho.works)
