#!/usr/bin/env python3
# nacho.works — IV Curve Analysis
# Sweeps one or both PSU channels over a voltage range and logs the
# measured voltage and current at each operating point.
#
# Reads the active workbench to locate the PSU by role; uses
# instruments.json for the SCPI command set.
#
# Usage:
#   python IVCurveAnalysis.py --ch1-sweep START STOP STEP
#   python IVCurveAnalysis.py --ch2-sweep START STOP STEP
#   python IVCurveAnalysis.py --ch1-sweep 0 10 0.1 --ch2-sweep 0 5 1
#
# When both channels are given, a 2-D sweep is performed:
# CH2 is the outer (bias) loop, CH1 is the inner (swept) loop —
# the same convention used for transistor I-V family curves.
#
# robin.kerstens@uantwerpen.be

import argparse
import csv
import os
import sys
import time
from datetime import datetime

import pyvisa

# instruments.py and workbench.py live in ../setup/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'setup'))
from workbench import load_workbench, open_by_role
from instruments import classify, get_command

# ─── Defaults ────────────────────────────────────────────────────────────────

# Current limit applied to each swept channel before the sweep starts.
# 0.5 A is a conservative safe default; raise it explicitly if your DUT needs more.
DEFAULT_CURRENT_LIMIT = 0.5   # A

# Time to wait after each voltage step before measuring.
# The Keithley 2231A settles quickly, but your DUT may need longer.
DEFAULT_SETTLE_TIME   = 0.10  # s

DEFAULT_OUTPUT_CSV    = 'ivCurveAnalysis.csv'

# Approximate serial + instrument processing overhead per measurement point.
# Used only to print a time estimate before the sweep starts.
_OVERHEAD_PER_POINT   = 0.25  # s


# ─── Voltage step generator ───────────────────────────────────────────────────

def voltage_steps(start: float, stop: float, step: float) -> list[float]:
    """Return evenly spaced voltages from start to stop (inclusive).

    Uses integer counting internally to avoid floating-point accumulation
    that would cause e.g. 3.0000000000000004 instead of 3.0.
    Handles both ascending and descending sweeps; the sign of step is ignored
    and the direction is inferred from start vs stop.
    """
    step = abs(step)
    n    = round(abs(stop - start) / step)
    sign = 1 if stop >= start else -1
    return [round(start + i * sign * step, 10) for i in range(n + 1)]


# ─── Instrument helpers ───────────────────────────────────────────────────────

def _exec(inst, steps: list) -> float | None:
    """Execute a list of (action, scpi) tuples from instruments.py.

    Writes are sent with inst.write(); queries are sent with inst.query()
    and the returned string is cast to float. The last query value is returned
    (or None if there were no queries in the step list).
    """
    result = None
    for action, scpi in steps:
        if action == 'write':
            inst.write(scpi)
        elif action == 'query':
            result = float(inst.query(scpi))
        # 'note' and 'raw_query' are not used for PSU operations
    return result


def psu_reset(inst, family: dict):
    """Reset the PSU and enter remote control mode.

    The Keithley 2231A requires SYSTem:REMote before any other command.
    This is baked into the 'reset' entry in instruments.json for this family.
    """
    _exec(inst, get_command(family, 'reset'))


def psu_set_voltage(inst, family: dict, ch: int, voltage: float):
    _exec(inst, get_command(family, 'set_voltage', ch=ch, value=voltage))


def psu_set_limit(inst, family: dict, ch: int, current: float):
    _exec(inst, get_command(family, 'set_current_limit', ch=ch, value=current))


def psu_output(inst, family: dict, ch: int, on: bool):
    op = 'output_on' if on else 'output_off'
    _exec(inst, get_command(family, op, ch=ch))


def psu_measure_v(inst, family: dict, ch: int) -> float:
    return _exec(inst, get_command(family, 'measure_voltage', ch=ch))


def psu_measure_i(inst, family: dict, ch: int) -> float:
    return _exec(inst, get_command(family, 'measure_current', ch=ch))


# ─── Argument parsing ─────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog='IVCurveAnalysis.py',
        description='Sweep PSU channel(s) and log I-V measurements.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  1-D sweep — CH1 from 0 to 10 V in 0.1 V steps:
    python IVCurveAnalysis.py --ch1-sweep 0 10 0.1

  1-D sweep — CH2 from 0 to 5 V in 1 V steps:
    python IVCurveAnalysis.py --ch2-sweep 0 5 1

  2-D sweep — for each CH2 bias point, sweep the full CH1 range:
    python IVCurveAnalysis.py --ch1-sweep 0 10 0.1 --ch2-sweep 0 5 1

  Tighter current limit, longer settle, custom output file:
    python IVCurveAnalysis.py --ch1-sweep 0 5 0.05 --ch1-limit 0.1 --settle 0.5 --output diode.csv
""")

    p.add_argument('--ch1-sweep', nargs=3, type=float,
                   metavar=('START', 'STOP', 'STEP'),
                   help='Sweep CH1 from START to STOP V in STEP V increments')
    p.add_argument('--ch2-sweep', nargs=3, type=float,
                   metavar=('START', 'STOP', 'STEP'),
                   help='Sweep CH2 from START to STOP V in STEP V increments')
    p.add_argument('--ch1-limit', type=float, default=DEFAULT_CURRENT_LIMIT,
                   metavar='A',
                   help=f'CH1 current limit in A (default {DEFAULT_CURRENT_LIMIT})')
    p.add_argument('--ch2-limit', type=float, default=DEFAULT_CURRENT_LIMIT,
                   metavar='A',
                   help=f'CH2 current limit in A (default {DEFAULT_CURRENT_LIMIT})')
    p.add_argument('--settle', type=float, default=DEFAULT_SETTLE_TIME,
                   metavar='S',
                   help=f'Settle time after each voltage step in seconds (default {DEFAULT_SETTLE_TIME})')
    p.add_argument('--output', default=DEFAULT_OUTPUT_CSV,
                   metavar='FILE',
                   help=f'Output CSV filename (default: {DEFAULT_OUTPUT_CSV})')

    args = p.parse_args()
    if not args.ch1_sweep and not args.ch2_sweep:
        p.error('Specify at least one of --ch1-sweep or --ch2-sweep.')

    # Validate that all voltage arguments are non-negative
    for flag, vals in [('--ch1-sweep', args.ch1_sweep), ('--ch2-sweep', args.ch2_sweep)]:
        if vals:
            start, stop, step = vals
            if step == 0:
                p.error(f'{flag}: STEP must not be zero.')
            if start < 0 or stop < 0:
                p.error(f'{flag}: negative voltages are not supported on this PSU.')

    return args


# ─── CSV ──────────────────────────────────────────────────────────────────────

CSV_HEADER = [
    'timestamp',
    'ch1_set_V', 'ch1_meas_V', 'ch1_meas_A',
    'ch2_set_V', 'ch2_meas_V', 'ch2_meas_A',
]


def _fmt(v: float | None) -> str:
    return f'{v:.6f}' if v is not None else ''


def write_csv_row(writer, timestamp: str,
                  ch1_set, ch1_v, ch1_i,
                  ch2_set, ch2_v, ch2_i):
    writer.writerow([
        timestamp,
        _fmt(ch1_set), _fmt(ch1_v), _fmt(ch1_i),
        _fmt(ch2_set), _fmt(ch2_v), _fmt(ch2_i),
    ])


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    ch1_v_list = voltage_steps(*args.ch1_sweep) if args.ch1_sweep else None
    ch2_v_list = voltage_steps(*args.ch2_sweep) if args.ch2_sweep else None
    two_d      = ch1_v_list is not None and ch2_v_list is not None

    n1 = len(ch1_v_list) if ch1_v_list else 1
    n2 = len(ch2_v_list) if ch2_v_list else 1
    total_points = n1 * n2

    # ── Connect ────────────────────────────────────────────────────────────────
    rm  = pyvisa.ResourceManager()
    wb  = load_workbench()
    psu = open_by_role(rm, wb, 'psu')

    # Identify the PSU and resolve its command set from instruments.json
    idn    = psu.query('*IDN?').strip()
    family = classify(idn)
    if family is None:
        print(f'[error] Unrecognised PSU IDN: {idn!r}')
        print('[error] Check instruments.json has a matching pattern for this model.')
        psu.close(); rm.close(); sys.exit(1)

    # ── Summary ────────────────────────────────────────────────────────────────
    print('IV Curve Analysis')
    print('---')
    print(f'[info] PSU     : {idn}')
    print(f'[info] Family  : {family["id"]}')

    if ch1_v_list:
        print(f'[info] CH1     : {ch1_v_list[0]:.3f} → {ch1_v_list[-1]:.3f} V  '
              f'({len(ch1_v_list)} pts, limit {args.ch1_limit:.3f} A)')
    if ch2_v_list:
        print(f'[info] CH2     : {ch2_v_list[0]:.3f} → {ch2_v_list[-1]:.3f} V  '
              f'({len(ch2_v_list)} pts, limit {args.ch2_limit:.3f} A)')

    if two_d:
        print(f'[info] Mode    : 2-D — {n2} CH2 bias points × {n1} CH1 steps = {total_points} total')
    else:
        ch = 'CH1' if ch1_v_list else 'CH2'
        print(f'[info] Mode    : 1-D — {total_points} {ch} steps')

    print(f'[info] Settle  : {args.settle * 1000:.0f} ms/step')
    est = total_points * (args.settle + _OVERHEAD_PER_POINT)
    print(f'[info] Est.    : ~{est:.0f} s  ({est / 60:.1f} min)')
    print(f'[info] Output  : {os.path.abspath(args.output)}')
    print()

    # ── Initialise PSU ─────────────────────────────────────────────────────────
    # reset sends SYSTem:REMote first, then *RST/*CLS — required for 2231A remote control
    psu_reset(psu, family)
    time.sleep(1.0)  # *RST reinitialises internal state; wait before sending more commands

    if ch1_v_list:
        psu_set_limit(psu, family, ch=1, current=args.ch1_limit)
        psu_set_voltage(psu, family, ch=1, voltage=ch1_v_list[0])
        psu_output(psu, family, ch=1, on=True)

    if ch2_v_list:
        psu_set_limit(psu, family, ch=2, current=args.ch2_limit)
        psu_set_voltage(psu, family, ch=2, voltage=ch2_v_list[0])
        psu_output(psu, family, ch=2, on=True)

    time.sleep(0.5)  # let outputs reach their initial set points before the first measurement

    # ── Print table header ─────────────────────────────────────────────────────
    if two_d:
        print(f'  {"CH2 set":>8}  {"CH1 set":>8}  │  '
              f'{"CH2 meas":>10}  {"CH2 A":>9}  │  '
              f'{"CH1 meas":>10}  {"CH1 A":>9}')
        print(f'  {"(V)":>8}  {"(V)":>8}  │  '
              f'{"(V)":>10}  {"(A)":>9}  │  '
              f'{"(V)":>10}  {"(A)":>9}')
        sep = f'  {"─"*8}  {"─"*8}  ┼  {"─"*10}  {"─"*9}  ┼  {"─"*10}  {"─"*9}'
        print(sep)
    elif ch1_v_list:
        print(f'  {"CH1 set":>8}  │  {"CH1 meas":>10}  {"CH1 A":>9}')
        print(f'  {"(V)":>8}  │  {"(V)":>10}  {"(A)":>9}')
        sep = f'  {"─"*8}  ┼  {"─"*10}  {"─"*9}'
        print(sep)
    else:
        print(f'  {"CH2 set":>8}  │  {"CH2 meas":>10}  {"CH2 A":>9}')
        print(f'  {"(V)":>8}  │  {"(V)":>10}  {"(A)":>9}')
        sep = f'  {"─"*8}  ┼  {"─"*10}  {"─"*9}'
        print(sep)

    # ── Sweep ──────────────────────────────────────────────────────────────────
    write_header = not os.path.isfile(args.output)
    out_f  = open(args.output, 'a', newline='', encoding='utf-8')
    writer = csv.writer(out_f)
    if write_header:
        writer.writerow(CSV_HEADER)

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        if two_d:
            # Outer loop: CH2 (bias) — changes slowly
            # Inner loop: CH1 (swept) — changes quickly for each CH2 point
            # A blank line between CH2 groups makes the console output easy to scan.
            for v2 in ch2_v_list:
                psu_set_voltage(psu, family, ch=2, voltage=v2)
                time.sleep(args.settle)  # let CH2 settle before starting the CH1 sweep

                for v1 in ch1_v_list:
                    psu_set_voltage(psu, family, ch=1, voltage=v1)
                    time.sleep(args.settle)

                    ch2_v = psu_measure_v(psu, family, ch=2)
                    ch2_i = psu_measure_i(psu, family, ch=2)
                    ch1_v = psu_measure_v(psu, family, ch=1)
                    ch1_i = psu_measure_i(psu, family, ch=1)

                    print(f'  {v2:>8.4f}  {v1:>8.4f}  │  '
                          f'{ch2_v:>10.5f}  {ch2_i:>9.5f}  │  '
                          f'{ch1_v:>10.5f}  {ch1_i:>9.5f}')

                    write_csv_row(writer, timestamp,
                                  ch1_set=v1,  ch1_v=ch1_v,  ch1_i=ch1_i,
                                  ch2_set=v2,  ch2_v=ch2_v,  ch2_i=ch2_i)
                    out_f.flush()

                print()  # blank line between CH2 groups

        elif ch1_v_list:
            for v1 in ch1_v_list:
                psu_set_voltage(psu, family, ch=1, voltage=v1)
                time.sleep(args.settle)

                ch1_v = psu_measure_v(psu, family, ch=1)
                ch1_i = psu_measure_i(psu, family, ch=1)

                print(f'  {v1:>8.4f}  │  {ch1_v:>10.5f}  {ch1_i:>9.5f}')

                write_csv_row(writer, timestamp,
                              ch1_set=v1, ch1_v=ch1_v, ch1_i=ch1_i,
                              ch2_set=None, ch2_v=None, ch2_i=None)
                out_f.flush()

        else:  # CH2 only
            for v2 in ch2_v_list:
                psu_set_voltage(psu, family, ch=2, voltage=v2)
                time.sleep(args.settle)

                ch2_v = psu_measure_v(psu, family, ch=2)
                ch2_i = psu_measure_i(psu, family, ch=2)

                print(f'  {v2:>8.4f}  │  {ch2_v:>10.5f}  {ch2_i:>9.5f}')

                write_csv_row(writer, timestamp,
                              ch1_set=None, ch1_v=None, ch1_i=None,
                              ch2_set=v2, ch2_v=ch2_v, ch2_i=ch2_i)
                out_f.flush()

    except KeyboardInterrupt:
        print('\n[info] Sweep interrupted — returning outputs to 0 V.')

    finally:
        out_f.close()

        # Ramp swept channels back to 0 V before disabling output.
        # Abrupt output disable at high voltage can disturb sensitive DUTs.
        for ch, active in [(1, ch1_v_list), (2, ch2_v_list)]:
            if active:
                psu_set_voltage(psu, family, ch=ch, voltage=0.0)
                psu_output(psu, family, ch=ch, on=False)

        # Hand control back to the front panel
        psu.write('SYSTem:LOCal')

        psu.close()
        rm.close()

    print()
    print(f'[info] Results saved to {os.path.abspath(args.output)}')
    print('[info] Done.')


if __name__ == '__main__':
    main()
