#!/usr/bin/env python3
# nacho.works — live waveform analysis on the active workbench scope
#
# Three-phase analysis matching the original waveformAnalysis workflow,
# rewritten to use workbench.py + instruments.py so it works with any
# supported oscilloscope (Keysight, Rigol, Tektronix, …).
#
# Phase 1 — Overview
#   Autoscale, rescale V/div to Vpp/6, set trigger, show 3 cycles.
#   Measures freq, Vpp, risetime, duty cycle, overshoot.
#   Saves a screenshot and writes results to CSV.
#
# Phase 2 — Rising-edge zoom
#   Zooms the timebase to 1× risetime/div so the edge fills the screen.
#   Measures risetime and overshoot up close.
#   Saves a second screenshot and appends to CSV.
#
# Phase 3 — Frequency stability
#   Zooms back to show 2–3 cycles, then samples frequency N times.
#   Lets you watch the signal stabilise (or not) in real time.
#
# Usage
#   python waveformAnalysis.py                    # CH1, 20 stability samples
#   python waveformAnalysis.py --ch 2             # measure CH2
#   python waveformAnalysis.py --samples 50       # more stability samples
#   python waveformAnalysis.py --no-autoscale     # skip the initial autoscale
#   python waveformAnalysis.py --label "555 OUT"  # label shown on scope screen
#   python waveformAnalysis.py --out results.csv  # custom CSV name
#
# robin.kerstens@uantwerpen.be

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pyvisa

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "setup"))

from workbench import load_workbench, open_by_role
from instruments import classify, get_command


# ── Configuration ─────────────────────────────────────────────────────────────

AUTOSCALE_WAIT_S    = 4.0    # seconds to wait after :AUToscale
SETTLE_S            = 0.3    # brief pause after scope writes before measuring
EDGE_HOLD_S         = 2.5    # how long to stay in edge-zoom view before moving on
STABILITY_INTERVAL  = 0.5    # seconds between frequency samples in Phase 3


# ── SI / engineering unit formatter ──────────────────────────────────────────

def _eng(val, unit=""):
    """Format a float value with an appropriate SI prefix.

    Examples:  0.00123 V  →  '  1.230 mV'
               1050000 Hz →  '  1.050 MHz'
    """
    if val is None:
        return "    ——    "
    prefixes = [
        (1e12, "T"), (1e9, "G"), (1e6, "M"), (1e3, "k"),
        (1,    "" ), (1e-3,"m"), (1e-6,"µ"), (1e-9,"n"), (1e-12,"p"),
    ]
    for scale, pfx in prefixes:
        if abs(val) >= scale * 0.9995:
            return f"{val / scale:8.3f} {pfx}{unit}"
    return f"{val:.3e} {unit}"


# ── SCPI helpers ──────────────────────────────────────────────────────────────

def _safe(raw):
    """Return float, or None if the response is out-of-range / unreadable."""
    try:
        v = float(raw)
        return None if abs(v) > 1e30 else v
    except (TypeError, ValueError):
        return None


def _write(scope, fam, op, **kw):
    """Fire only the write step(s) for op.  Silent KeyError = unsupported."""
    try:
        for action, scpi in get_command(fam, op, **kw):
            if action == "write":
                scope.write(scpi)
    except KeyError:
        pass


def _rq(scope, fam, op, **kw):
    """Execute all steps for op (write-to-enable then query).
    Returns float value, or None on any error."""
    try:
        last = None
        for action, scpi in get_command(fam, op, **kw):
            if action == "write":
                scope.write(scpi)
            elif action == "query":
                last = scope.query(scpi).strip()
        return _safe(last)
    except Exception:
        return None


def _qonly(scope, fam, op, **kw):
    """Execute only query steps (items must already be enabled by _enable_items).
    Returns float value, or None on any error."""
    try:
        last = None
        for action, scpi in get_command(fam, op, **kw):
            if action == "query":
                last = scope.query(scpi).strip()
        return _safe(last)
    except Exception:
        return None


# ── Measurement-item management ───────────────────────────────────────────────

def _enable_items(scope, fam, ch, ops):
    """Clear existing display items then enable the listed ones.

    For Rigol the write step activates the badge; for Keysight the badge
    appears automatically via query — the write here is a no-op.
    Passing an empty ops list is a valid 'clear only' call.
    """
    _write(scope, fam, "measure_clear_all")   # KeyError silently skipped on Keysight
    time.sleep(0.1)
    for op in ops:
        _write(scope, fam, op, ch=ch)


# ── Display control ───────────────────────────────────────────────────────────

def _set_vdiv(scope, fam, ch, vpp):
    """Set V/div = Vpp/6 and zero the offset — fits signal in ~6 of 8 visible divs."""
    if vpp and 0 < abs(vpp) < 1e30:
        vscale = abs(vpp) / 6.0
        _write(scope, fam, "channel_scale",  ch=ch, value=f"{vscale:.4e}")
        _write(scope, fam, "channel_offset", ch=ch, value="0")
        return vscale
    return None


def _set_timebase_cycles(scope, fam, freq, n_cycles=3):
    """Show n_cycles full periods across 10 divisions."""
    if freq and 0 < freq < 1e30:
        tscale = (1.0 / freq) * n_cycles / 10.0
        _write(scope, fam, "timebase_scale", value=f"{tscale:.6e}")
        return tscale
    return None


def _set_timebase_edge(scope, fam, rise_s):
    """Zoom to 1 risetime per division so the edge fills the screen.
    Optionally shift the time reference 2 rise-times forward so the
    edge lands near the centre rather than the very left."""
    if not (rise_s and 0 < rise_s < 1e30):
        return None
    tscale = max(rise_s, 1e-9)           # 1 risetime per division
    _write(scope, fam, "timebase_scale",    value=f"{tscale:.6e}")
    _write(scope, fam, "timebase_position", value=f"{rise_s * 2:.6e}")  # shift 2 rise-times
    return tscale


def _screenshot(scope, fam, filename):
    """Capture scope screen to file.  Returns True on success."""
    try:
        steps = get_command(fam, "screenshot")
    except KeyError:
        print(f"  [screenshot] not supported on this scope family — skipped")
        return False

    raw_idx = next((i for i, (a, _) in enumerate(steps) if a == "raw_query"), None)
    if raw_idx is None:
        print("  [screenshot] no raw_query step found — skipped")
        return False

    pre  = [(a, s) for a, s in steps[:raw_idx]     if a == "write"]
    cmd_ = steps[raw_idx][1]
    post = [(a, s) for a, s in steps[raw_idx + 1:] if a == "write"]

    orig_timeout = scope.timeout
    try:
        scope.timeout = 15_000
        for _, s in pre:
            scope.write(s)
        time.sleep(1.0)          # let scope render the image
        scope.write(cmd_)
        data = scope.read_raw()
        for _, s in post:
            try: scope.write(s)
            except Exception: pass
    except Exception as exc:
        print(f"  [screenshot] I/O error: {exc}")
        return False
    finally:
        try: scope.timeout = orig_timeout
        except Exception: pass

    if not data:
        print("  [screenshot] scope returned empty data")
        return False

    # Strip any leading SCPI header before the image magic bytes
    for magic, ext in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
        idx = data.find(magic)
        if idx != -1:
            data = data[idx:]
            if not filename.endswith(ext):
                base = filename.rsplit(".", 1)[0] if "." in filename else filename
                filename = base + ext
            break

    Path(filename).write_bytes(data)
    print(f"  Screenshot saved → {filename}")
    return True


# ── CSV helper ────────────────────────────────────────────────────────────────

def _csv_append(filename, rows):
    """Append measurement rows to a CSV.  Writes header on first call."""
    write_header = not os.path.isfile(filename)
    with open(filename, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(["timestamp", "phase", "measurement", "value", "unit"])
        writer.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Three-phase waveform analysis using the active workbench scope.")
    parser.add_argument("--ch",           type=int, default=1,
                        help="Scope channel to analyse (default: 1)")
    parser.add_argument("--samples",      type=int, default=20,
                        help="Frequency stability samples in Phase 3 (default: 20)")
    parser.add_argument("--no-autoscale", action="store_true",
                        help="Skip the initial autoscale")
    parser.add_argument("--label",        default="",
                        help="Signal label shown on scope screen (if supported)")
    parser.add_argument("--out",          default="waveformAnalysis.csv",
                        help="CSV output file (default: waveformAnalysis.csv)")
    parser.add_argument("--workbench",    default=None,
                        help="Workbench name (default: active workbench)")
    args = parser.parse_args()

    ch      = args.ch
    csv_out = args.out

    # ── Connect ───────────────────────────────────────────────────────────────
    try:
        wb = load_workbench(args.workbench)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    rm = pyvisa.ResourceManager("@py")
    try:
        scope = open_by_role(rm, wb, "scope")
    except RuntimeError as exc:
        print(f"Error: {exc}")
        rm.close()
        sys.exit(1)

    scope.timeout = 10_000
    idn = scope.query("*IDN?").strip()
    fam = classify(idn)

    print(f"Waveform Analysis")
    print(f"─────────────────────────────────────────────────")
    print(f"Scope   : {idn}")
    print(f"Family  : {fam['vendor'] if fam else '(unrecognised)'} {fam['series'] if fam else ''}")
    print(f"Channel : CH{ch}")
    print(f"Output  : {csv_out}")
    print()

    if fam is None:
        print("Warning: scope family not recognised in instruments.json.")
        print("         Falling back to Keysight-style SCPI.")
        from instruments import _resolve_family, _family_index
        fam = _resolve_family(_family_index()["keysight_infiniivision"])

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Initial scope state ───────────────────────────────────────────────────
    _write(scope, fam, "run")
    time.sleep(SETTLE_S)

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 1 — OVERVIEW
    # ══════════════════════════════════════════════════════════════════════════
    print("Phase 1 — Overview")
    print("──────────────────")

    if not args.no_autoscale:
        print("  :AUToscale ", end="", flush=True)
        _write(scope, fam, "autoscale")
        for _ in range(int(AUTOSCALE_WAIT_S / 0.1)):
            time.sleep(0.1)
            print(".", end="", flush=True)
        print(" done")
    else:
        print("  (autoscale skipped)")

    # Enable overview measurements once (write-to-enable on Rigol, no-op on Keysight)
    overview_ops = [
        "measure_vpp", "measure_vrms", "measure_freq",
        "measure_dutycycle", "measure_risetime", "measure_overshoot",
    ]
    _enable_items(scope, fam, ch, overview_ops)
    time.sleep(SETTLE_S)

    # Read initial values using full write+query (items just cleared → must re-enable)
    freq     = _rq(scope, fam, "measure_freq",      ch=ch)
    vpp      = _rq(scope, fam, "measure_vpp",       ch=ch)
    vrms     = _rq(scope, fam, "measure_vrms",      ch=ch)
    duty     = _rq(scope, fam, "measure_dutycycle", ch=ch)
    rise     = _rq(scope, fam, "measure_risetime",  ch=ch)
    overshoot = _rq(scope, fam, "measure_overshoot", ch=ch)

    # Rescale to fill the screen
    vscale = _set_vdiv(scope, fam, ch, vpp)

    # Trigger: edge, rising, this channel, 0 V
    _write(scope, fam, "trigger_mode")            # :TRIGger:MODE EDGE (Keysight only; Rigol: no-op)
    _write(scope, fam, "trigger_source", ch=ch)
    _write(scope, fam, "trigger_slope",  slope="POSitive")
    _write(scope, fam, "trigger_level",  value="0")

    # Timebase: 3 cycles across 10 divisions
    tscale = _set_timebase_cycles(scope, fam, freq, n_cycles=3)

    # Screen annotation and channel label (Keysight/supported scopes only)
    label = args.label or f"CH{ch} analysis"
    _write(scope, fam, "annotation_text", text=label)
    _write(scope, fam, "annotation_on")
    _write(scope, fam, "channel_label",   ch=ch, label=f"CH{ch}")
    _write(scope, fam, "labels_on")

    time.sleep(SETTLE_S)

    print()
    print(f"  {'Freq':<14}  {_eng(freq,  'Hz').strip()}")
    print(f"  {'Vpp':<14}  {_eng(vpp,   'V').strip()}")
    print(f"  {'Vrms':<14}  {_eng(vrms,  'V').strip()}")
    print(f"  {'Duty cycle':<14}  {duty:.2f} %" if duty else f"  {'Duty cycle':<14}  ——")
    print(f"  {'Rise time':<14}  {_eng(rise, 's').strip()}")
    print(f"  {'Overshoot':<14}  {overshoot:.2f} %" if overshoot else f"  {'Overshoot':<14}  ——")
    if vscale:
        print(f"  {'V/div':<14}  {_eng(vscale, 'V/div').strip()}")
    if tscale:
        print(f"  {'Timebase':<14}  {_eng(tscale, 's/div').strip()}")
    print()

    # Screenshot
    _screenshot(scope, fam, f"waveformAnalysis_overview_{ts}.png")

    # CSV
    csv_rows = [
        [ts, "overview", "freq_Hz",       freq,      "Hz"],
        [ts, "overview", "vpp_V",         vpp,       "V"],
        [ts, "overview", "vrms_V",        vrms,      "V"],
        [ts, "overview", "duty_pct",      duty,      "%"],
        [ts, "overview", "rise_s",        rise,      "s"],
        [ts, "overview", "overshoot_pct", overshoot, "%"],
    ]
    _csv_append(csv_out, csv_rows)

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 2 — RISING-EDGE ZOOM
    # ══════════════════════════════════════════════════════════════════════════
    print("Phase 2 — Rising-edge zoom")
    print("──────────────────────────")

    if rise and 0 < rise < 1e30:
        # Clear, re-enable only edge-relevant measurements
        edge_ops = ["measure_risetime", "measure_falltime", "measure_overshoot", "measure_preshoot"]
        _enable_items(scope, fam, ch, edge_ops)
        time.sleep(SETTLE_S)

        # Zoom: 1 risetime per division, offset 2 rise-times so edge is centred
        tscale_edge = _set_timebase_edge(scope, fam, rise)
        _write(scope, fam, "annotation_text", text=f"CH{ch} Rising Edge")
        time.sleep(SETTLE_S)

        # Query at this zoom level
        rise2      = _qonly(scope, fam, "measure_risetime",  ch=ch)
        fall2      = _qonly(scope, fam, "measure_falltime",  ch=ch)
        overshoot2 = _qonly(scope, fam, "measure_overshoot", ch=ch)
        preshoot2  = _qonly(scope, fam, "measure_preshoot",  ch=ch)

        print(f"  Timebase    → {_eng(tscale_edge, 's/div').strip()}  (1 rise-time / div)")
        print(f"  Rise time   :  {_eng(rise2,      's').strip()}")
        print(f"  Fall time   :  {_eng(fall2,      's').strip()}")
        print(f"  Overshoot   :  {overshoot2:.2f} %" if overshoot2 else "  Overshoot   :  ——")
        print(f"  Preshoot    :  {preshoot2:.2f} %"  if preshoot2  else "  Preshoot    :  ——")
        print()

        time.sleep(EDGE_HOLD_S)   # let the user observe the edge on the scope screen
        _screenshot(scope, fam, f"waveformAnalysis_risingEdge_{ts}.png")

        csv_rows = [
            [ts, "edge_zoom", "rise_s",        rise2,      "s"],
            [ts, "edge_zoom", "fall_s",         fall2,      "s"],
            [ts, "edge_zoom", "overshoot_pct",  overshoot2, "%"],
            [ts, "edge_zoom", "preshoot_pct",   preshoot2,  "%"],
        ]
        _csv_append(csv_out, csv_rows)
    else:
        print(f"  Rise time not available ({rise!r}) — skipping edge zoom")
    print()

    # ══════════════════════════════════════════════════════════════════════════
    # PHASE 3 — FREQUENCY STABILITY
    # ══════════════════════════════════════════════════════════════════════════
    print("Phase 3 — Frequency stability")
    print("─────────────────────────────")
    print(f"  Sampling frequency {args.samples}× at {STABILITY_INTERVAL}s interval")
    print()

    # Restore overview timebase (~2.5 cycles: scale = period / 4 divs)
    tscale_stab = _set_timebase_cycles(scope, fam, freq, n_cycles=3)
    _write(scope, fam, "timebase_position", value="0")   # reset any edge offset

    _enable_items(scope, fam, ch, ["measure_freq"])
    time.sleep(SETTLE_S)

    _write(scope, fam, "annotation_text", text=f"CH{ch} Freq Stability")

    print(f"  {'#':>4}  {'Frequency':>16}  {'delta vs #1':>14}")
    print(f"  {'─'*4}  {'─'*16}  {'─'*14}")

    freq_ref = None
    csv_rows = []
    for i in range(1, args.samples + 1):
        f = _qonly(scope, fam, "measure_freq", ch=ch)
        if freq_ref is None and f:
            freq_ref = f
        delta = (f - freq_ref) if (f and freq_ref) else None
        delta_str = f"{_eng(delta, 'Hz').strip():>14}" if delta is not None else f"{'——':>14}"
        print(f"  {i:>4}  {_eng(f, 'Hz'):>16}  {delta_str}")
        csv_rows.append([ts, "freq_stability", f"sample_{i:03d}_freq_Hz", f, "Hz"])
        time.sleep(STABILITY_INTERVAL)

    _csv_append(csv_out, csv_rows)
    print()

    # ── Cleanup ───────────────────────────────────────────────────────────────
    _write(scope, fam, "annotation_off")
    _write(scope, fam, "labels_off")
    _write(scope, fam, "measure_clear_all")
    _write(scope, fam, "timebase_position", value="0")
    _write(scope, fam, "autoscale")          # restore a clean default view
    print(f"Results saved → {os.path.abspath(csv_out)}")
    print("Done.")

    scope.close()
    rm.close()


if __name__ == "__main__":
    main()
