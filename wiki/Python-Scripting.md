# Python Scripting

The **Python** tab gives you a live Python environment running inside the open-EEW server process. You can write arbitrary Python, run it with **Ctrl+Enter** or **▶ Run**, and optionally use the open-EEW infrastructure — connected instruments, SCPI helpers, workbench state — without any setup.

---

## Quick start

1. Open the **Python** tab.
2. Write your script in the editor.
3. Press **Ctrl+Enter** (or **Shift+Enter**, or click **▶ Run**).
4. Output appears in the panel below the editor.
5. Use **■ Stop** to interrupt a running script.

No `import` needed for the built-ins listed below — they are pre-injected into every script's namespace.

---

## Available names (no import needed)

| Name | Type | Description |
|------|------|-------------|
| `print` | function | Prints to the output panel |
| `time` | module | Standard `time` module |
| `json` | module | Standard `json` module |
| `math` | module | Standard `math` module |
| `Path` | class | `pathlib.Path` |
| `pyvisa` | module | PyVISA (available when installed) |
| `resources` | dict | `{ resource_str: pyvisa_resource }` — open VISA handles |
| `families` | dict | `{ resource_str: resolved_family_dict }` — eewBackbone families |
| `wb` | dict or None | Active workbench JSON, or `None` if none loaded |
| `sio` | SocketIO | Server-side SocketIO handle (emit events to the UI) |
| `_find_instrument` | function | Find a connected instrument by type |
| `_op` | function | Send a named SCPI operation |
| `_run_steps` | function | Execute a raw step list |
| `_log` | function | Emit a message to the app log panel |
| `get_command` | function | Resolve a named operation from the backbone database |

---

## Working without hardware

The scripting environment works standalone. You can use any Python built-in or do pure computation without instruments connected:

```python
import numpy as np   # if installed in the venv

freqs = [10, 100, 1000, 10000]
for f in freqs:
    print(f"{f} Hz  →  {20 * math.log10(f / 10):.1f} dB")
```

You can also `import` anything installed in the open-EEW virtual environment.

---

## Accessing instruments

### Find an instrument by type

```python
psu, fam = _find_instrument('psu')    # 'scope', 'dmm', 'awg', 'load'
if psu is None:
    print("No PSU connected")
```

`_find_instrument(type)` returns `(resource, family_id)` for the first connected instrument of that type, or `(None, None)` if none is found.

### Send a raw SCPI command

```python
psu, fam = _find_instrument('psu')
if psu:
    psu.write('OUTP ON')
    voltage = psu.query('MEAS:VOLT?')
    print(f"Voltage: {voltage.strip()} V")
```

`resource` is a standard PyVISA resource — `.write()`, `.query()`, `.read()`, `.read_raw()` all work normally.

### Use a named operation from the backbone

Named operations abstract over instrument families so the same call works across different hardware:

```python
psu, fam = _find_instrument('psu')
if psu:
    _op(psu, fam, 'set_voltage', ch=1, value='3.3')
    _op(psu, fam, 'output_on',   ch=1)
    v = _op(psu, fam, 'voltage',  ch=1)
    i = _op(psu, fam, 'current',  ch=1)
    print(f"V={v}  I={i}")
```

`_op(resource, family, operation, **kwargs)` resolves the operation name against the instrument's family in `eewBackbone.json`, executes the SCPI steps, and returns the last query result (or `None` for write-only operations).

To see what operations a family supports:

```python
psu, fam = _find_instrument('psu')
cmds = get_command.__module__  # just to check it loaded
print(list(fam.get('commands', {}).keys()) if fam else "no family")
```

---

## Iterating over all instruments

```python
for res_str, res in resources.items():
    fam = families.get(res_str)
    print(res_str, "→", fam.get('id') if fam else "unknown family")
```

---

## Reading the active workbench

```python
if wb:
    for instr in wb.get('_unique', []):
        print(instr['type'], instr.get('model'), instr.get('resource'))
else:
    print("No workbench loaded")
```

---

## Emitting to the app log panel

`_log(msg)` sends a message to the shared log panel on the right (visible on all tabs):

```python
_log("⚠ Compliance limit reached")
```

To send custom events to the frontend (advanced):

```python
sio.emit('log', {'msg': 'hello from script', 'cls': 'ok'})
```

---

## Saving results

Use `Path` and the standard file API to write results. The output folder configured in the navbar is not automatically injected, but you can read it from `wb`:

```python
import csv

output_dir = Path("results")
output_dir.mkdir(exist_ok=True)

rows = [(1, 3.301), (2, 3.298), (3, 3.305)]
with open(output_dir / "measurements.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["sample", "voltage_V"])
    w.writerows(rows)

print(f"Saved {len(rows)} rows to {output_dir / 'measurements.csv'}")
```

---

## Example: voltage sweep

```python
psu, fam = _find_instrument('psu')
scope, sfam = _find_instrument('scope')

if not psu:
    print("No PSU found"); raise SystemExit

_op(psu, fam, 'output_on', ch=1)

results = []
for v_set in [1.0, 1.5, 2.0, 2.5, 3.0, 3.3]:
    _op(psu, fam, 'set_voltage', ch=1, value=str(v_set))
    time.sleep(0.1)
    v_meas = float(_op(psu, fam, 'voltage', ch=1) or 0)
    i_meas = float(_op(psu, fam, 'current', ch=1) or 0)
    results.append((v_set, v_meas, i_meas))
    print(f"Set {v_set:.2f} V  →  {v_meas:.4f} V  {i_meas*1000:.2f} mA")

_op(psu, fam, 'output_off', ch=1)

print("\nDone.")
```

---

## Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| Ctrl+Enter | Run script |
| Shift+Enter | Run script |
| Tab | Insert 4 spaces |

---

## Notes

- Scripts run in a background thread. The UI stays responsive while a script runs.
- **■ Stop** sends a `KeyboardInterrupt` to the running thread. Long-blocking VISA operations may not respond immediately.
- Each run starts with a fresh namespace — variables do not persist between runs.
- Standard `import` works for anything installed in the open-EEW virtual environment (`pip install` into `.venv/`).
