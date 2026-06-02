#!/usr/bin/env python3
# nacho.works — PSU Interrupt
# Sets a PSU channel to V1, applies a timed voltage interrupt (channel off
# or a set V2), then restores to V3 (default V1). Logs V/I at the end of
# each phase. Supports sweeping the interrupt duration across multiple runs.
#
# When a scope is present in the workbench the test arms it in SINGLE mode
# on the falling edge before each interrupt and saves a screenshot after the
# capture completes. Trigger level, timebase, and horizontal position are
# calculated automatically to show the full T1→T2→T3 event on screen.
#
# Reads the active workbench to locate instruments by role; uses
# eewBackbone.json for the SCPI command set.
#
# Usage:
#   python psuInterrupt.py --v1 5.0
#   python psuInterrupt.py --v1 5.0 --v2 3.3 --t2 200
#   python psuInterrupt.py --v1 5.0 --t2-sweep 10 500 10
#   python psuInterrupt.py --v1 5.0 --t2-sweep 10 500 10 --total-time 2000
#
# Phase sequence per run:
#   1. Set V1, enable output, wait T1 ms, measure
#   2. Arm scope (SINGLE, falling edge) then trigger interrupt (channel off or V2)
#   3. Wait T2 ms, measure
#   4. Set V3 (default V1), enable output, wait T3 ms, measure, screenshot
#
# When --total-time is given T3 is derived as total-time − T1 − T2,
# keeping each cycle the same wall-clock length across a T2 sweep.
#
# robin.kerstens@uantwerpen.be

import argparse
import csv
import math
import os
import sys
import time
from datetime import datetime

import pyvisa

# workbench.py / eewBackbone.py live in ../core/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'core'))
# screenshot.py lives alongside this script
sys.path.insert(0, os.path.dirname(__file__))

from workbench import load_workbench, open_by_role
from eewBackbone import classify, get_command

try:
    from screenshot import get_screenshot
    _SCREENSHOT_OK = True
except ImportError:
    _SCREENSHOT_OK = False

# ─── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_CURRENT_LIMIT = 0.5   # A
DEFAULT_T1            = 500   # ms — settle time before interrupt
DEFAULT_T2            = 100   # ms — interrupt duration
DEFAULT_T3            = 500   # ms — settle time after restore
DEFAULT_OUTPUT_CSV    = 'psuInterrupt.csv'

# Wait after arming scope (`:SINGle`) before triggering the interrupt.
# Gives the scope firmware time to arm before the falling edge arrives.
ARM_DELAY_S = 0.15            # s

# Horizontal divisions — standard for all supported scope families.
SCOPE_DIVS  = 12

# Approximate serial + instrument processing overhead per measurement point.
# Used only to print a time estimate before the test starts.
_OVERHEAD_PER_POINT   = 0.25  # s  (6 PSU measurements + screenshot per cycle)
_SCREENSHOT_TIME      = 18.0  # s  conservative estimate for USB BMP screenshot


# ─── ms step generator ───────────────────────────────────────────────────────

def ms_steps(start: float, stop: float, step: float) -> list[float]:
    step = abs(step)
    n    = round(abs(stop - start) / step)
    sign = 1 if stop >= start else -1
    return [round(start + i * sign * step, 6) for i in range(n + 1)]


# ─── Oscilloscope timebase helpers ───────────────────────────────────────────

def _nice_time(s: float) -> float:
    """Round s up to the nearest standard oscilloscope time/div step (1-2-5 series)."""
    if s <= 0:
        return 1e-9
    exp = math.floor(math.log10(s))
    m   = s / 10 ** exp
    for step in (1, 2, 5, 10):
        if m <= step:
            return step * 10 ** exp
    return 10 * 10 ** exp


def _scope_write(scope, family: dict, op: str, **kw):
    """Send all write-type steps for an operation; silently skip unsupported ops."""
    for action, scpi in get_command(family, op, **kw):
        if action == 'write':
            scope.write(scpi)


def scope_setup(scope, scope_fam: dict, args, max_t2_ms: float, max_t3_ms: float):
    """Configure trigger and timebase once before the first run.

    Trigger: falling edge on scope_channel at (V1+V2)/2.
    Timebase: auto-sized to fit T1 + max_T2 + max_T3 across SCOPE_DIVS with 10%
    margin, with horizontal position placing the falling edge at T1 from the
    left edge of the screen.
    """
    sch     = args.scope_channel
    v1      = args.v1
    v2      = 0.0 if args.v2 is None else args.v2
    trig_lv = (v1 + v2) / 2.0

    total_s    = (args.t1 + max_t2_ms + max_t3_ms) / 1000.0 * 1.1
    t_per_div  = args.scope_scale if args.scope_scale else _nice_time(total_s / SCOPE_DIVS)
    win        = t_per_div * SCOPE_DIVS
    # Positive position = trigger appears to the LEFT of centre
    position   = win / 2.0 - args.t1 / 1000.0

    # Trigger configuration
    try:
        _scope_write(scope, scope_fam, 'trigger_mode')          # EDGE (Rigol has null here — skip)
    except KeyError:
        pass
    _scope_write(scope, scope_fam, 'trigger_source',  ch=sch)
    _scope_write(scope, scope_fam, 'trigger_slope',   slope='NEG')
    _scope_write(scope, scope_fam, 'trigger_level',   value=f'{trig_lv:.4f}')

    # Timebase
    _scope_write(scope, scope_fam, 'timebase_scale',    value=f'{t_per_div:.6e}')
    _scope_write(scope, scope_fam, 'timebase_position', value=f'{position:.6e}')

    return t_per_div


def scope_arm(scope, scope_fam: dict):
    _scope_write(scope, scope_fam, 'single')


# ─── Instrument helpers (PSU) ─────────────────────────────────────────────────

def _exec(inst, steps: list) -> float | None:
    result = None
    for action, scpi in steps:
        if action == 'write':
            inst.write(scpi)
        elif action == 'query':
            result = float(inst.query(scpi))
    return result


def psu_reset(inst, family: dict):
    _exec(inst, get_command(family, 'reset'))

def psu_set_voltage(inst, family: dict, ch: int, voltage: float):
    _exec(inst, get_command(family, 'set_voltage', ch=ch, value=voltage))

def psu_set_limit(inst, family: dict, ch: int, current: float):
    _exec(inst, get_command(family, 'set_current_limit', ch=ch, value=current))

def psu_output(inst, family: dict, ch: int, on: bool):
    _exec(inst, get_command(family, 'output_on' if on else 'output_off', ch=ch))

def psu_measure_v(inst, family: dict, ch: int) -> float:
    return _exec(inst, get_command(family, 'measure_voltage', ch=ch))

def psu_measure_i(inst, family: dict, ch: int) -> float:
    return _exec(inst, get_command(family, 'measure_current', ch=ch))


# ─── Argument parsing ─────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog='psuInterrupt.py',
        description='Apply a timed voltage interrupt to a PSU channel and log V/I per phase.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  Single interrupt — CH1 at 5 V, 100 ms channel-off, restore to 5 V:
    python psuInterrupt.py --v1 5.0

  Interrupt to a reduced voltage instead of channel-off:
    python psuInterrupt.py --v1 5.0 --v2 3.3 --t2 200

  Restore to a different voltage after the interrupt:
    python psuInterrupt.py --v1 5.0 --t2 100 --v3 4.8

  Sweep interrupt duration from 10 to 500 ms in 10 ms steps:
    python psuInterrupt.py --v1 5.0 --t2-sweep 10 500 10

  Sweep with fixed total cycle time (T3 shrinks as T2 grows):
    python psuInterrupt.py --v1 5.0 --t2-sweep 10 500 10 --total-time 2000

  Override scope timebase (e.g. 5 ms/div):
    python psuInterrupt.py --v1 5.0 --scope-scale 0.005
""")

    p.add_argument('--channel', type=int, default=1, metavar='N',
                   help='PSU channel to use (default 1)')
    p.add_argument('--v1', type=float, required=True, metavar='V',
                   help='Voltage in phase 1 (before interrupt)')
    p.add_argument('--t1', type=float, default=DEFAULT_T1, metavar='MS',
                   help=f'Settle time in phase 1 in ms (default {DEFAULT_T1})')
    p.add_argument('--v2', type=float, default=None, metavar='V',
                   help='Voltage during interrupt phase (default: channel off)')
    p.add_argument('--t2', type=float, default=DEFAULT_T2, metavar='MS',
                   help=f'Interrupt duration in ms (default {DEFAULT_T2}; ignored if --t2-sweep is given)')
    p.add_argument('--t2-sweep', nargs=3, type=float, metavar=('START', 'STOP', 'STEP'),
                   help='Run the test once per T2 value swept from START to STOP in STEP ms increments')
    p.add_argument('--v3', type=float, default=None, metavar='V',
                   help='Voltage after interrupt (default: same as --v1)')
    p.add_argument('--t3', type=float, default=DEFAULT_T3, metavar='MS',
                   help=f'Settle time after restore in ms (default {DEFAULT_T3}; overridden by --total-time)')
    p.add_argument('--total-time', type=float, default=None, metavar='MS',
                   help='Fixed total cycle time in ms; T3 = total-time − T1 − T2 (overrides --t3)')
    p.add_argument('--current-limit', type=float, default=DEFAULT_CURRENT_LIMIT, metavar='A',
                   help=f'Channel current limit in A (default {DEFAULT_CURRENT_LIMIT})')
    p.add_argument('--output', default=DEFAULT_OUTPUT_CSV, metavar='FILE',
                   help=f'Output CSV filename (default: {DEFAULT_OUTPUT_CSV})')

    # Scope options
    p.add_argument('--scope-channel', type=int, default=1, metavar='N',
                   help='Scope channel monitoring the PSU output (default 1)')
    p.add_argument('--scope-scale', type=float, default=None, metavar='S_PER_DIV',
                   help='Override auto-calculated time/div in seconds (default: auto-fit event)')
    p.add_argument('--no-scope', action='store_true',
                   help='Skip scope integration even if a scope is in the workbench')

    args = p.parse_args()

    if args.t2_sweep is not None:
        start, stop, step = args.t2_sweep
        if step == 0:
            p.error('--t2-sweep: STEP must not be zero.')
        if start <= 0 or stop <= 0:
            p.error('--t2-sweep: all values must be positive.')

    if args.total_time is not None:
        t2_max = max(args.t2_sweep[0], args.t2_sweep[1]) if args.t2_sweep else args.t2
        t3_min = args.total_time - args.t1 - t2_max
        if t3_min < 0:
            p.error(
                f'--total-time {args.total_time:.0f} ms is shorter than '
                f'T1 ({args.t1:.0f}) + max T2 ({t2_max:.0f}) = {args.t1 + t2_max:.0f} ms.'
            )

    return args


# ─── CSV ──────────────────────────────────────────────────────────────────────

CSV_HEADER = [
    'timestamp', 'run', 'ch',
    'v1_set_V', 't1_ms', 'v1_meas_V', 'v1_meas_A',
    'v2_set_V', 't2_ms', 'v2_meas_V', 'v2_meas_A',
    'v3_set_V', 't3_ms', 'v3_meas_V', 'v3_meas_A',
    'screenshot',
]


def _fmt(v) -> str:
    if v is None:
        return ''
    return f'{v:.6f}' if isinstance(v, float) else str(v)


# ─── Single cycle ─────────────────────────────────────────────────────────────

def run_cycle(psu, psu_fam: dict, scope, scope_fam, scope_idn: str,
              args, t2_ms: float, t3_ms: float,
              run_num: int, timestamp: str, shot_dir: str,
              writer, out_f) -> tuple:
    ch     = args.channel
    v3     = args.v3 if args.v3 is not None else args.v1
    ch_off = args.v2 is None
    v2_set = 0.0 if ch_off else args.v2

    # Phase 1 — establish V1 and wait for settle
    psu_set_voltage(psu, psu_fam, ch, args.v1)
    psu_output(psu, psu_fam, ch, True)
    time.sleep(args.t1 / 1000.0)
    v1_v = psu_measure_v(psu, psu_fam, ch)
    v1_i = psu_measure_i(psu, psu_fam, ch)

    # Arm scope on falling edge before triggering interrupt
    if scope is not None:
        scope_arm(scope, scope_fam)
        time.sleep(ARM_DELAY_S)

    # Phase 2 — interrupt (this falling edge triggers the scope)
    if ch_off:
        psu_output(psu, psu_fam, ch, False)
    else:
        psu_set_voltage(psu, psu_fam, ch, v2_set)
    time.sleep(t2_ms / 1000.0)
    v2_v = psu_measure_v(psu, psu_fam, ch)
    v2_i = psu_measure_i(psu, psu_fam, ch)

    # Phase 3 — restore (set voltage before enabling to avoid a transient)
    psu_set_voltage(psu, psu_fam, ch, v3)
    if ch_off:
        psu_output(psu, psu_fam, ch, True)
    time.sleep(t3_ms / 1000.0)
    v3_v = psu_measure_v(psu, psu_fam, ch)
    v3_i = psu_measure_i(psu, psu_fam, ch)

    # Screenshot — scope capture is complete by now (T2 + T3 elapsed since trigger)
    shot_path = ''
    if scope is not None and _SCREENSHOT_OK:
        ts_str   = datetime.now().strftime('%Y%m%d_%H%M%S')
        base     = os.path.join(shot_dir, f'psu_interrupt_run{run_num:03d}_t2_{t2_ms:.0f}ms_{ts_str}')
        try:
            get_screenshot(scope, scope_idn, base)
            # get_screenshot appends the detected extension; find the saved file
            for ext in ('.png', '.bmp', '.bin'):
                if os.path.isfile(base + ext):
                    shot_path = os.path.abspath(base + ext)
                    break
        except Exception as exc:
            print(f'[warn] Screenshot failed: {exc}')

    writer.writerow([
        timestamp, run_num, ch,
        _fmt(args.v1), _fmt(args.t1), _fmt(v1_v), _fmt(v1_i),
        '' if ch_off else _fmt(v2_set), _fmt(t2_ms), _fmt(v2_v), _fmt(v2_i),
        _fmt(v3), _fmt(t3_ms), _fmt(v3_v), _fmt(v3_i),
        shot_path,
    ])
    out_f.flush()

    return v1_v, v1_i, v2_v, v2_i, v3_v, v3_i


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    v3        = args.v3 if args.v3 is not None else args.v1
    ch_off    = args.v2 is None
    v2_label  = 'off' if ch_off else f'{args.v2:.3f} V'
    t2_list   = ms_steps(*args.t2_sweep) if args.t2_sweep else [args.t2]
    total_runs = len(t2_list)

    def t3_for(t2_ms):
        return args.total_time - args.t1 - t2_ms if args.total_time is not None else args.t3

    # ── Screenshots directory ──────────────────────────────────────────────────
    root     = os.path.join(os.path.dirname(__file__), '..')
    shot_dir = os.path.join(root, 'screenshots')
    os.makedirs(shot_dir, exist_ok=True)

    # ── Connect ────────────────────────────────────────────────────────────────
    rm  = pyvisa.ResourceManager()
    wb  = load_workbench()
    psu = open_by_role(rm, wb, 'psu')

    idn    = psu.query('*IDN?').strip()
    family = classify(idn)
    if family is None:
        print(f'[error] Unrecognised PSU IDN: {idn!r}')
        print('[error] Check eewBackbone.json has a matching pattern for this model.')
        psu.close(); rm.close(); sys.exit(1)

    # ── Optional scope ─────────────────────────────────────────────────────────
    scope     = None
    scope_fam = None
    scope_idn = ''

    if not args.no_scope and _SCREENSHOT_OK:
        try:
            scope     = open_by_role(rm, wb, 'scope')
            scope_idn = scope.query('*IDN?').strip()
            scope_fam = classify(scope_idn)
            if scope_fam is None:
                print(f'[warn] Scope IDN not recognised ({scope_idn!r}), skipping scope.')
                scope.close()
                scope = None
        except RuntimeError:
            pass  # no scope role in workbench
    elif args.no_scope:
        pass
    elif not _SCREENSHOT_OK:
        print('[warn] screenshot.py import failed — scope integration disabled.')

    # ── Scope setup ────────────────────────────────────────────────────────────
    t_per_div = None
    if scope is not None:
        max_t2 = max(t2_list)
        max_t3 = t3_for(max_t2)
        t_per_div = scope_setup(scope, scope_fam, args, max_t2, max_t3)

    # ── Summary ────────────────────────────────────────────────────────────────
    print('PSU Interrupt')
    print('─' * 60)
    print(f'[info] PSU         : {idn}')
    print(f'[info] Family      : {family["id"]}')
    print(f'[info] Channel     : CH{args.channel}')
    print(f'[info] V1          : {args.v1:.3f} V   T1 = {args.t1:.0f} ms')
    print(f'[info] Interrupt   : {v2_label}')

    if total_runs == 1:
        t3 = t3_for(t2_list[0])
        print(f'[info] T2          : {t2_list[0]:.1f} ms')
        print(f'[info] V3          : {v3:.3f} V   T3 = {t3:.0f} ms')
        est = (args.t1 + t2_list[0] + t3) / 1000.0 + 6 * _OVERHEAD_PER_POINT
        if scope is not None:
            est += _SCREENSHOT_TIME
        print(f'[info] Est.        : ~{est:.1f} s')
    else:
        t3_first = t3_for(t2_list[0])
        t3_last  = t3_for(t2_list[-1])
        print(f'[info] T2 sweep    : {t2_list[0]:.1f} → {t2_list[-1]:.1f} ms  ({total_runs} runs)')
        print(f'[info] V3          : {v3:.3f} V')
        if args.total_time is not None:
            print(f'[info] Total/cycle : {args.total_time:.0f} ms  '
                  f'(T3: {t3_first:.0f} → {t3_last:.0f} ms)')
        else:
            print(f'[info] T3          : {args.t3:.0f} ms')
        est = sum(
            (args.t1 + t2 + t3_for(t2)) / 1000.0 + 6 * _OVERHEAD_PER_POINT
            for t2 in t2_list
        )
        if scope is not None:
            est += total_runs * _SCREENSHOT_TIME
        print(f'[info] Est.        : ~{est:.0f} s  ({est / 60:.1f} min)')

    if scope is not None:
        print(f'[info] Scope       : {scope_idn}')
        print(f'[info] Scope CH    : CH{args.scope_channel}  '
              f'trig NEG @ {(args.v1 + (0.0 if ch_off else args.v2)) / 2:.3f} V  '
              f'scale {t_per_div * 1000:.3g} ms/div')
        print(f'[info] Screenshots : {os.path.abspath(shot_dir)}')

    print(f'[info] Output      : {os.path.abspath(args.output)}')
    print()

    # ── Initialise PSU ─────────────────────────────────────────────────────────
    psu_reset(psu, family)
    time.sleep(1.0)
    psu_set_limit(psu, family, args.channel, args.current_limit)
    psu_set_voltage(psu, family, args.channel, args.v1)
    psu_output(psu, family, args.channel, True)
    time.sleep(0.5)

    # ── Print table header ─────────────────────────────────────────────────────
    print(f'  {"Run":>4}  {"T2(ms)":>8}  │  '
          f'{"V1 meas":>10}  {"V1 I":>9}  │  '
          f'{"V2 meas":>10}  {"V2 I":>9}  │  '
          f'{"V3 meas":>10}  {"V3 I":>9}')
    print(f'  {"":>4}  {"":>8}  │  '
          f'{"(V)":>10}  {"(A)":>9}  │  '
          f'{"(V)":>10}  {"(A)":>9}  │  '
          f'{"(V)":>10}  {"(A)":>9}')
    sep = (f'  {"─"*4}  {"─"*8}  ┼  {"─"*10}  {"─"*9}  ┼  '
           f'{"─"*10}  {"─"*9}  ┼  {"─"*10}  {"─"*9}')
    print(sep)

    # ── Run ────────────────────────────────────────────────────────────────────
    write_header = not os.path.isfile(args.output)
    out_f  = open(args.output, 'a', newline='', encoding='utf-8')
    writer = csv.writer(out_f)
    if write_header:
        writer.writerow(CSV_HEADER)

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        for run_num, t2_ms in enumerate(t2_list, start=1):
            t3_ms = t3_for(t2_ms)
            v1_v, v1_i, v2_v, v2_i, v3_v, v3_i = run_cycle(
                psu, family, scope, scope_fam, scope_idn,
                args, t2_ms, t3_ms, run_num, timestamp, shot_dir,
                writer, out_f,
            )
            print(f'  {run_num:>4}  {t2_ms:>8.1f}  │  '
                  f'{v1_v:>10.5f}  {v1_i:>9.5f}  │  '
                  f'{v2_v:>10.5f}  {v2_i:>9.5f}  │  '
                  f'{v3_v:>10.5f}  {v3_i:>9.5f}')

    except KeyboardInterrupt:
        print('\n[info] Interrupted — returning output to 0 V.')

    finally:
        out_f.close()
        psu_set_voltage(psu, family, args.channel, 0.0)
        psu_output(psu, family, args.channel, False)
        psu.write('SYSTem:LOCal')
        psu.close()
        if scope is not None:
            scope.close()
        rm.close()

    print()
    print(f'[info] Results saved to {os.path.abspath(args.output)}')
    print('[info] Done.')


if __name__ == '__main__':
    main()
