#!/usr/bin/env python3
"""
open-EE-workbench GUI
─────────────────────────────────────────────────────────────────────
A graphical control surface for VISA-connected lab instruments.
Built on the open-EE-workbench framework (nachoVisa / workbench.py).

Usage
─────
    python gui.py                  # use active workbench
    python gui.py <name>           # use named workbench
    python gui.py --demo           # demo mode (no hardware required)

Requirements
────────────
    pyvisa + pyvisa-py  (already required by the project)
    tkinter             (Python stdlib)
"""

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# ── Path setup ──────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "core"))

WORKBENCH_DIR = ROOT / "workbenches"

try:
    from workbench import active_name, load_workbench, set_active
    from eewBackbone import classify, get_command, resolve_command, _resolve_family, _family_index
    HELPERS_OK = True
except ImportError as _e:
    HELPERS_OK = False
    print(f"[gui] workbench helpers unavailable: {_e}")
    # Provide safe no-op stubs so the GUI can open without the core/ helpers
    def active_name():        return None
    def load_workbench(n=None): raise RuntimeError("workbench helpers not available")
    def get_command(*a, **k):  raise KeyError("instruments helpers not available")
    def resolve_command(*a, **k): return []
    def _family_index():       return {}
    def _resolve_family(f):    return f

try:
    import pyvisa
    PYVISA_OK = True
except ImportError:
    PYVISA_OK = False
    print("[gui] pyvisa not available — demo mode only")

# ── Colour palette ──────────────────────────────────────────────────────────────
BG        = "#1a1b1e"
PANEL     = "#25262b"
PANEL2    = "#2c2e33"
PANEL3    = "#323540"
BORDER    = "#373a40"
FG        = "#c1c2c5"
FG_BRIGHT = "#f1f3f5"
FG_DIM    = "#5c5f66"
ACCENT    = "#339af0"
GREEN     = "#51cf66"
RED       = "#ff6b6b"
YELLOW    = "#fcc419"
PURPLE    = "#cc5de8"
ORANGE    = "#ff922b"

# ── Fonts ───────────────────────────────────────────────────────────────────────
def _fonts():
    """Return font specs, falling back gracefully."""
    import tkinter.font as tkfont
    families = set(tkfont.families())
    sans = next((f for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue",
                              "Ubuntu", "DejaVu Sans", "Arial") if f in families),
                "TkDefaultFont")
    mono = next((f for f in ("Cascadia Code", "JetBrains Mono", "Fira Code",
                              "Consolas", "Courier New", "DejaVu Sans Mono") if f in families),
                "TkFixedFont")
    return sans, mono

FSAN, FMONO = None, None  # filled after Tk root created

# ── Logo rendering ───────────────────────────────────────────────────────────────
GUI_ASSETS = ROOT / "gui_assets"

def _load_logo(size: int = 44, light: bool = True):
    """
    Return a tk.PhotoImage of the nacho chip logo, or None on failure.

    Rendering priority (most to least accurate):
      1. cairosvg + PIL  — perfect vector render (pip install cairosvg pillow)
      2. PIL polygon     — stdlib-quality approximation derived from SVG path
      3. None            — caller shows a text fallback
    """
    svg_file = GUI_ASSETS / ("nacho_white.svg" if light else "nacho_black.svg")

    # ── Attempt 1: cairosvg ──────────────────────────────────────────────
    try:
        import cairosvg, io
        from PIL import Image, ImageTk
        png_bytes = cairosvg.svg2png(
            url=str(svg_file),
            output_width=size * 2,
            output_height=size * 2,
        )
        img = Image.open(io.BytesIO(png_bytes)).resize(
            (size, size), Image.LANCZOS
        )
        return ImageTk.PhotoImage(img)
    except Exception:
        pass

    # ── Attempt 2: PIL polygon approximation ────────────────────────────
    # Coordinates derived by tracing the SVG path (viewBox 106×98, group
    # transform translate(-72.96, -110.47) applied to path absolute coords).
    #
    # Main outer shape key vertices (normalised to [0,1]):
    #   left tip (0.01, 0.87) → top spike (0.62, 0.01) → right shoulder
    #   (0.83, 0.49) → notch (0.76, 0.53)→(0.69, 0.53) → bump (0.88, 0.70)
    #   → bottom-right (0.99, 0.99) → (0.91, 1.00)
    #
    # Inner lower diamond (2nd subpath, m -17.73,-19.98 relative to start):
    #   centre ≈ (0.74, 0.82), half-width ≈ 0.10, half-height ≈ 0.09
    #
    # Inner upper notch triangle (3rd subpath, m -22.41,-13.41):
    #   centre ≈ (0.69, 0.86), base ≈ x 0.58–0.79, height ≈ 0.10
    try:
        from PIL import Image, ImageDraw, ImageTk

        S = size * 3          # render at 3× for smooth anti-aliasing
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        fill   = (255, 255, 255, 245) if light else (26, 27, 30, 245)
        erase  = (0, 0, 0, 0)        # transparent punch-through

        def sc(pts):
            return [(int(x * S), int(y * S)) for x, y in pts]

        # Outer chip
        outer = sc([
            (0.01, 0.87),
            (0.62, 0.01),
            (0.83, 0.49),
            (0.76, 0.53),
            (0.69, 0.53),
            (0.74, 0.67),
            (0.88, 0.70),
            (0.99, 0.99),
            (0.91, 1.00),
        ])
        draw.polygon(outer, fill=fill)

        # Inner lower diamond (cutout)
        inner1 = sc([
            (0.74, 0.73),   # top
            (0.84, 0.82),   # right
            (0.74, 0.91),   # bottom
            (0.64, 0.82),   # left
        ])
        draw.polygon(inner1, fill=erase)

        # Inner upper triangle/notch (cutout)
        inner2 = sc([
            (0.69, 0.76),   # top
            (0.79, 0.86),   # bottom-right
            (0.59, 0.86),   # bottom-left
        ])
        draw.polygon(inner2, fill=erase)

        # Downsample to target size (LANCZOS for smooth edges)
        img = img.resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        pass

    return None


# ── Helper: resolve family from workbench entry ─────────────────────────────────
def _family_for(entry: dict) -> dict | None:
    """Return resolved family dict for a workbench instrument entry, or None."""
    if not HELPERS_OK:
        return None
    fid = entry.get("family_id")
    if not fid:
        return None
    try:
        idx = _family_index()
        return _resolve_family(idx[fid]) if fid in idx else None
    except Exception:
        return None


def _run_cmd(resource, steps: list[tuple[str, str]]) -> list:
    """Execute resolved SCPI steps on a pyvisa resource. Returns list of query results."""
    results = []
    for action, scpi in steps:
        if action == "write":
            resource.write(scpi)
        elif action == "query":
            results.append(resource.query(scpi).strip())
        elif action == "raw_query":
            resource.write(scpi)
            results.append(resource.read_raw())
        # "note" items are silently skipped
    return results


# ── Widget helpers ───────────────────────────────────────────────────────────────
def _btn(parent, text, cmd=None, bg=PANEL2, fg=FG_BRIGHT,
         width=None, padx=10, pady=5, font=None, **kw):
    b = tk.Button(
        parent, text=text, command=cmd,
        bg=bg, fg=fg, activebackground=ACCENT,
        activeforeground=BG, relief="flat",
        padx=padx, pady=pady, cursor="hand2",
        font=font or (FSAN, 10),
        bd=0, highlightthickness=0,
        **({} if width is None else {"width": width}),
        **kw,
    )
    return b


def _label(parent, text="", font=None, fg=FG, bg=PANEL, **kw):
    return tk.Label(parent, text=text, font=font or (FSAN, 10),
                    fg=fg, bg=bg, **kw)


def _entry(parent, textvariable=None, width=10, **kw):
    return tk.Entry(
        parent, textvariable=textvariable,
        bg=PANEL3, fg=FG_BRIGHT, insertbackground=ACCENT,
        relief="flat", font=(FMONO, 10), width=width,
        highlightthickness=1, highlightbackground=BORDER,
        highlightcolor=ACCENT, **kw,
    )


def _frame(parent, bg=PANEL, **kw):
    return tk.Frame(parent, bg=bg, **kw)


def _ledge(parent, color=FG_DIM, width=2, bg=PANEL):
    """A thin coloured ledge / accent line."""
    return tk.Frame(parent, bg=color, height=width, bd=0)


def _pill(parent, text, color=PANEL2, fg=FG, **kw):
    """Small non-interactive label pill."""
    return tk.Label(
        parent, text=text, bg=color, fg=fg,
        font=(FSAN, 8), padx=6, pady=2,
        relief="flat", **kw,
    )


def _status_dot(parent, on=False, bg=PANEL):
    """A ● indicator; toggle color with .on() / .off()."""
    lbl = tk.Label(parent, text="●", bg=bg,
                   fg=GREEN if on else FG_DIM,
                   font=(FSAN, 11))
    lbl.on  = lambda: lbl.config(fg=GREEN)
    lbl.off = lambda: lbl.config(fg=FG_DIM)
    lbl.warn = lambda: lbl.config(fg=YELLOW)
    return lbl


def _card_frame(parent, title: str, icon: str = "",
                subtitle: str = "", accent_color: str = ACCENT):
    """
    Create a card-style panel.
    Returns (outer_frame, body_frame, dot_widget).
    """
    outer = _frame(parent, bg=PANEL)
    outer.grid_propagate(True)

    # Top accent bar
    bar = tk.Frame(outer, bg=accent_color, height=3, bd=0)
    bar.pack(fill=tk.X)

    # Header row
    hdr = _frame(outer, bg=PANEL)
    hdr.pack(fill=tk.X, padx=14, pady=(10, 6))

    title_lbl = _label(hdr, text=f"{icon}  {title}" if icon else title,
                       font=(FSAN, 11, "bold"), fg=FG_BRIGHT, bg=PANEL)
    title_lbl.pack(side=tk.LEFT)

    dot = _status_dot(hdr, bg=PANEL)
    dot.pack(side=tk.RIGHT, padx=(4, 0))

    if subtitle:
        sub = _label(hdr, text=subtitle, font=(FSAN, 9),
                     fg=FG_DIM, bg=PANEL)
        sub.pack(side=tk.RIGHT, padx=(0, 8))

    # Separator
    sep = tk.Frame(outer, bg=BORDER, height=1)
    sep.pack(fill=tk.X, padx=14)

    # Body
    body = _frame(outer, bg=PANEL)
    body.pack(fill=tk.BOTH, expand=True, padx=14, pady=10)

    return outer, body, dot


# ════════════════════════════════════════════════════════════════════════════════
# Instrument card panels
# ════════════════════════════════════════════════════════════════════════════════

class ScopeCard:
    """Controls card for an oscilloscope."""

    def __init__(self, parent, entry: dict, app):
        self.entry = entry
        self.app = app
        self.family = _family_for(entry)
        self.resource = None  # set by app when connected

        subtitle = f"{entry['manufacturer']} {entry['model']} · {entry['connection']}"
        self.outer, body, self.dot = _card_frame(
            parent, "Oscilloscope", icon="📡",
            subtitle=subtitle, accent_color="#4dabf7",
        )
        self._build(body)

    def _build(self, body):
        # Transport buttons row
        btn_row = _frame(body)
        btn_row.pack(fill=tk.X, pady=(0, 10))

        self._btn_run    = _btn(btn_row, "▶  RUN",    cmd=self._run,      bg="#37b24d", pady=6)
        self._btn_stop   = _btn(btn_row, "■  STOP",   cmd=self._stop,     bg="#f03e3e", pady=6)
        self._btn_single = _btn(btn_row, "⊙  SINGLE", cmd=self._single,   bg=PANEL3,   pady=6)
        self._btn_auto   = _btn(btn_row, "⟳  AUTO",   cmd=self._autoscale,bg=PANEL3,   pady=6)
        self._btn_snap   = _btn(btn_row, "📷  Screenshot", cmd=self._screenshot,
                                bg=PANEL3, pady=6)

        for w in (self._btn_run, self._btn_stop, self._btn_single,
                  self._btn_auto, self._btn_snap):
            w.pack(side=tk.LEFT, padx=(0, 6))

        # Channel display (4 channels)
        ch_frame = _frame(body)
        ch_frame.pack(fill=tk.X)
        _label(ch_frame, text="Channels", font=(FSAN, 9, "bold"),
               fg=FG_DIM, bg=PANEL).pack(anchor=tk.W, pady=(0, 4))

        self._ch_dots  = []
        self._ch_labels = []
        grid = _frame(ch_frame)
        grid.pack(fill=tk.X)
        for i in range(4):
            col = _frame(grid)
            col.grid(row=0, column=i, padx=(0, 10), sticky=tk.W)
            dot = _status_dot(col, bg=PANEL)
            dot.pack(side=tk.LEFT, padx=(0, 4))
            lbl = _label(col, text=f"CH{i+1}", fg=FG_DIM, bg=PANEL,
                         font=(FSAN, 10))
            lbl.pack(side=tk.LEFT)
            self._ch_dots.append(dot)
            self._ch_labels.append(lbl)

    # ── SCPI helpers ────────────────────────────────────────────────────────
    def _op(self, op: str, **kw):
        """Run an operation via the family command table (background thread)."""
        if self.resource is None:
            return
        if self.family is None:
            self.app.log(f"[scope] No family — cannot execute {op!r}")
            return
        try:
            steps = get_command(self.family, op, **kw)
        except KeyError:
            self.app.log(f"[scope] {op!r} not supported on {self.entry['model']}")
            return
        self.app.visa_async(self.resource, steps)

    def _run(self):       self._op("run")
    def _stop(self):      self._op("stop")
    def _single(self):    self._op("single")
    def _autoscale(self): self._op("autoscale")

    def _screenshot(self):
        if self.resource is None:
            messagebox.showwarning("Not connected", "Connect to the workbench first.")
            return
        filename = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("BMP image", "*.bmp"), ("All files", "*.*")],
            initialfile=f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )
        if not filename:
            return

        family = self.family
        resource = self.resource
        app = self.app

        def do_screenshot():
            try:
                steps = get_command(family, "screenshot")
            except (KeyError, TypeError):
                return None, "Screenshot not supported for this scope family."

            # Split into pre-writes, raw_query, post-writes
            raw_idx = next((i for i, (a, _) in enumerate(steps) if a == "raw_query"), None)
            if raw_idx is None:
                return None, "No raw_query step in screenshot command."

            pre  = [(a, s) for a, s in steps[:raw_idx]      if a == "write"]
            cmd  = steps[raw_idx][1]
            post = [(a, s) for a, s in steps[raw_idx+1:]    if a == "write"]

            orig_timeout = resource.timeout
            resource.timeout = 12_000

            is_usb = resource.resource_name.upper().startswith("USB")
            if is_usb:
                resource.chunk_size = 4096

            for _, scpi in pre:
                resource.write(scpi)

            time.sleep(1.2)
            resource.write(cmd)

            if is_usb:
                chunks = []
                while True:
                    chunk = resource.read_raw()
                    chunks.append(chunk)
                    if len(chunk) < 4096:
                        break
                data = b"".join(chunks)
            else:
                data = resource.read_raw()

            for _, scpi in post:
                resource.write(scpi)

            resource.timeout = orig_timeout

            # Find image magic
            for magic, ext in [(b"\x89PNG", ".png"), (b"BM", ".bmp")]:
                idx = data.find(magic)
                if idx != -1:
                    data = data[idx:]
                    break

            base, fext = os.path.splitext(filename)
            out = filename if fext else filename

            with open(out, "wb") as f:
                f.write(data)
            return out, None

        def on_done(result, error):
            if error:
                app.log(f"[screenshot] Error: {error}")
                messagebox.showerror("Screenshot failed", str(error))
            else:
                out, err2 = result
                if err2:
                    app.log(f"[screenshot] {err2}")
                    messagebox.showerror("Screenshot failed", err2)
                else:
                    app.log(f"[screenshot] Saved → {out}")
                    messagebox.showinfo("Screenshot saved", f"Saved to:\n{out}")

        app._run_async(do_screenshot, on_done)

    def set_connected(self, connected: bool):
        if connected:
            self.dot.on()
        else:
            self.dot.off()


class PSUChannelStrip:
    """One channel strip inside a PSU card."""

    def __init__(self, parent, ch: int, family: dict | None, app):
        self.ch = ch
        self.family = family
        self.app = app
        self.resource = None
        self._output_on = False

        f = _frame(parent, bg=PANEL2)
        f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True,
               padx=(0, 8 if ch < 3 else 0))
        self.frame = f

        # Channel title
        hdr = _frame(f, bg=PANEL2)
        hdr.pack(fill=tk.X, pady=(0, 4))
        _label(hdr, text=f"CH {ch}", font=(FSAN, 10, "bold"),
               fg=ACCENT, bg=PANEL2).pack(side=tk.LEFT)
        self._out_dot = _status_dot(hdr, bg=PANEL2)
        self._out_dot.pack(side=tk.RIGHT)

        # Setpoint inputs
        sp_frame = _frame(f, bg=PANEL2)
        sp_frame.pack(fill=tk.X, pady=(0, 4))

        self._v_var = tk.StringVar(value="0.000")
        self._i_var = tk.StringVar(value="0.500")

        for label, var, unit in [("V", self._v_var, "V"), ("I", self._i_var, "A")]:
            row = _frame(sp_frame, bg=PANEL2)
            row.pack(fill=tk.X, pady=1)
            _label(row, text=label, fg=FG_DIM, bg=PANEL2,
                   font=(FSAN, 9), width=2).pack(side=tk.LEFT)
            ent = _entry(row, textvariable=var, width=8)
            ent.pack(side=tk.LEFT, padx=2)
            _label(row, text=unit, fg=FG_DIM, bg=PANEL2,
                   font=(FSAN, 9)).pack(side=tk.LEFT)
            if label == "V":
                ent.bind("<Return>", lambda e: self._apply_v())
            else:
                ent.bind("<Return>", lambda e: self._apply_i())

        # Apply button
        _btn(f, "Apply", cmd=self._apply_all, bg=PANEL3, pady=3)\
            .pack(fill=tk.X, pady=(0, 6))

        # Readback display
        reads = _frame(f, bg=BG)
        reads.pack(fill=tk.X, pady=(0, 6))

        self._v_read = _label(reads, text="—.—— V", fg=GREEN,
                              bg=BG, font=(FMONO, 11, "bold"))
        self._i_read = _label(reads, text="—.—— A", fg=YELLOW,
                              bg=BG, font=(FMONO, 10))
        self._p_read = _label(reads, text="—.—— W", fg=FG_DIM,
                              bg=BG, font=(FMONO, 9))
        for w in (self._v_read, self._i_read, self._p_read):
            w.pack(anchor=tk.W, padx=4, pady=0)

        # Output toggle
        self._out_btn = _btn(f, "OUTPUT  OFF", cmd=self._toggle_output,
                             bg=RED, fg=FG_BRIGHT, pady=5)
        self._out_btn.pack(fill=tk.X)

    # ── command helpers ───────────────────────────────────────────────────
    def _op(self, op: str, **kw) -> bool:
        if self.resource is None or self.family is None:
            return False
        try:
            steps = get_command(self.family, op, ch=self.ch, **kw)
        except KeyError:
            self.app.log(f"[psu ch{self.ch}] {op!r} not supported")
            return False
        self.app.visa_async(self.resource, steps)
        return True

    def _apply_v(self):
        try:
            v = float(self._v_var.get())
        except ValueError:
            self.app.log(f"[psu ch{self.ch}] invalid voltage value")
            return
        self.app.log(f"[psu ch{self.ch}] set voltage → {v:.3f} V")
        self._op("set_voltage", value=f"{v:.4f}")

    def _apply_i(self):
        try:
            i = float(self._i_var.get())
        except ValueError:
            self.app.log(f"[psu ch{self.ch}] invalid current value")
            return
        self.app.log(f"[psu ch{self.ch}] set current → {i:.3f} A")
        self._op("set_current_limit", value=f"{i:.4f}")

    def _apply_all(self):
        self._apply_v()
        self._apply_i()

    def _toggle_output(self):
        if self._output_on:
            self._op("output_off")
            self._set_output_state(False)
            self.app.log(f"[psu ch{self.ch}] output OFF")
        else:
            self._op("output_on")
            self._set_output_state(True)
            self.app.log(f"[psu ch{self.ch}] output ON")

    def _set_output_state(self, on: bool):
        self._output_on = on
        if on:
            self._out_btn.config(text="OUTPUT  ON ", bg=GREEN, fg=BG)
            self._out_dot.on()
        else:
            self._out_btn.config(text="OUTPUT  OFF", bg=RED, fg=FG_BRIGHT)
            self._out_dot.off()

    def update_readings(self, v=None, i=None, p=None):
        if v is not None:
            try:
                self._v_read.config(text=f"{float(v):7.4f} V")
            except (ValueError, TypeError):
                pass
        if i is not None:
            try:
                self._i_read.config(text=f"{float(i):7.4f} A")
            except (ValueError, TypeError):
                pass
        if p is not None:
            try:
                self._p_read.config(text=f"{float(p):7.4f} W")
            except (ValueError, TypeError):
                pass

    def set_connected(self, connected: bool):
        if not connected:
            self._v_read.config(text="—.—— V")
            self._i_read.config(text="—.—— A")
            self._p_read.config(text="—.—— W")
            self._set_output_state(False)


class PSUCard:
    """Controls card for a programmable power supply."""

    def __init__(self, parent, entry: dict, app, n_channels: int = 1):
        self.entry = entry
        self.app = app
        self.family = _family_for(entry)
        self.resource = None
        self.n_channels = n_channels

        subtitle = f"{entry['manufacturer']} {entry['model']} · {entry['connection']}"
        self.outer, body, self.dot = _card_frame(
            parent, "Power Supply", icon="⚡",
            subtitle=subtitle, accent_color=GREEN,
        )

        self._build_controls(body)
        self._build_channels(body)

    def _build_controls(self, body):
        ctrl = _frame(body)
        ctrl.pack(fill=tk.X, pady=(0, 10))

        # Channel count selector
        _label(ctrl, text="Channels:", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 9)).pack(side=tk.LEFT)

        self._n_var = tk.IntVar(value=self.n_channels)
        spin = tk.Spinbox(ctrl, from_=1, to=4, width=3,
                          textvariable=self._n_var,
                          command=self._rebuild_channels,
                          bg=PANEL3, fg=FG_BRIGHT,
                          buttonbackground=PANEL3,
                          relief="flat", font=(FMONO, 10))
        spin.pack(side=tk.LEFT, padx=(4, 12))

        _btn(ctrl, "Reset", cmd=self._reset, bg=PANEL3, pady=4)\
            .pack(side=tk.LEFT, padx=(0, 6))
        _btn(ctrl, "All ON",  cmd=self._all_on,  bg=GREEN, fg=BG, pady=4)\
            .pack(side=tk.LEFT, padx=(0, 4))
        _btn(ctrl, "All OFF", cmd=self._all_off, bg=RED,   pady=4)\
            .pack(side=tk.LEFT)

    def _build_channels(self, body):
        self._ch_container = _frame(body)
        self._ch_container.pack(fill=tk.X)
        self._channels: list[PSUChannelStrip] = []
        for i in range(1, self.n_channels + 1):
            strip = PSUChannelStrip(self._ch_container, i, self.family, self.app)
            strip.resource = self.resource
            self._channels.append(strip)

    def _rebuild_channels(self):
        n = self._n_var.get()
        for w in self._ch_container.winfo_children():
            w.destroy()
        self._channels = []
        for i in range(1, n + 1):
            strip = PSUChannelStrip(self._ch_container, i, self.family, self.app)
            strip.resource = self.resource
            self._channels.append(strip)

    def _reset(self):
        if self.resource is None or self.family is None:
            return
        try:
            steps = get_command(self.family, "reset")
            self.app.visa_async(self.resource, steps)
            self.app.log("[psu] reset")
        except KeyError:
            pass

    def _all_on(self):
        for ch in self._channels:
            ch._op("output_on")
            ch._set_output_state(True)

    def _all_off(self):
        for ch in self._channels:
            ch._op("output_off")
            ch._set_output_state(False)

    def set_connected(self, connected: bool):
        if connected:
            self.dot.on()
        else:
            self.dot.off()
        for ch in self._channels:
            ch.resource = self.resource if connected else None
            ch.set_connected(connected)

    def poll(self):
        """Called periodically; queries voltage/current/power for all channels."""
        if self.resource is None or self.family is None:
            return
        for ch in self._channels:
            self._query_ch(ch)

    def _query_ch(self, ch: PSUChannelStrip):
        resource = self.resource
        family = self.family
        idx = ch.ch
        app = self.app

        def do():
            results = {}
            for op, key in [("measure_voltage", "v"),
                            ("measure_current", "i"),
                            ("measure_power",   "p")]:
                try:
                    steps = get_command(family, op, ch=idx)
                    r = _run_cmd(resource, steps)
                    if r:
                        results[key] = r[-1]
                except (KeyError, Exception):
                    pass
            return results

        def on_done(result, error):
            if error or not result:
                return
            ch.update_readings(**result)

        app._run_async(do, on_done)


class AWGChannelPanel:
    """One channel panel inside an AWG card."""

    FUNCS = ["SIN", "SQU", "RAMP", "PULS", "NOIS", "DC"]
    FUNC_LABELS = {"SIN": "Sine", "SQU": "Square", "RAMP": "Ramp",
                   "PULS": "Pulse", "NOIS": "Noise", "DC": "DC"}

    def __init__(self, parent, ch: int, family: dict | None, app):
        self.ch = ch
        self.family = family
        self.app = app
        self.resource = None
        self._output_on = False

        f = _frame(parent, bg=PANEL2)
        f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8 if ch == 1 else 0))
        self.frame = f

        # Header
        hdr = _frame(f, bg=PANEL2)
        hdr.pack(fill=tk.X, pady=(0, 6))
        _label(hdr, text=f"Channel {ch}", font=(FSAN, 10, "bold"),
               fg=ACCENT, bg=PANEL2).pack(side=tk.LEFT)
        self._out_dot = _status_dot(hdr, bg=PANEL2)
        self._out_dot.pack(side=tk.RIGHT)

        # Waveform selector
        _label(f, text="Waveform", fg=FG_DIM, bg=PANEL2,
               font=(FSAN, 9)).pack(anchor=tk.W)

        self._func_var = tk.StringVar(value="SIN")
        wf_frame = _frame(f, bg=PANEL2)
        wf_frame.pack(fill=tk.X, pady=(2, 8))

        for row_funcs in [["SIN", "SQU", "RAMP"], ["PULS", "NOIS", "DC"]]:
            row = _frame(wf_frame, bg=PANEL2)
            row.pack(fill=tk.X, pady=1)
            for fn in row_funcs:
                rb = tk.Radiobutton(
                    row, text=fn, variable=self._func_var, value=fn,
                    bg=PANEL2, fg=FG, selectcolor=PANEL3,
                    activebackground=PANEL2, activeforeground=FG_BRIGHT,
                    font=(FMONO, 9), indicatoron=True,
                )
                rb.pack(side=tk.LEFT, padx=(0, 8))

        # Parameter inputs
        params = _frame(f, bg=PANEL2)
        params.pack(fill=tk.X, pady=(0, 6))

        self._freq_var  = tk.StringVar(value="1000")
        self._amp_var   = tk.StringVar(value="1.000")
        self._offs_var  = tk.StringVar(value="0.000")

        for label, var, unit, rtrn in [
            ("Freq",   self._freq_var,  "Hz",  self._apply_freq),
            ("Amp",    self._amp_var,   "Vpp", self._apply_amp),
            ("Offset", self._offs_var,  "V",   self._apply_offset),
        ]:
            row = _frame(params, bg=PANEL2)
            row.pack(fill=tk.X, pady=1)
            _label(row, text=f"{label}:", fg=FG_DIM, bg=PANEL2,
                   font=(FSAN, 9), width=7, anchor=tk.W).pack(side=tk.LEFT)
            ent = _entry(row, textvariable=var, width=9)
            ent.pack(side=tk.LEFT, padx=2)
            ent.bind("<Return>", lambda e, fn=rtrn: fn())
            _label(row, text=unit, fg=FG_DIM, bg=PANEL2,
                   font=(FSAN, 9)).pack(side=tk.LEFT, padx=2)

        # Apply + output
        ctrl = _frame(f, bg=PANEL2)
        ctrl.pack(fill=tk.X, pady=(2, 0))
        _btn(ctrl, "Apply", cmd=self._apply_all, bg=PANEL3, pady=3)\
            .pack(side=tk.LEFT, padx=(0, 6))
        self._out_btn = _btn(ctrl, "OUT OFF", cmd=self._toggle_output,
                             bg=RED, pady=3)
        self._out_btn.pack(side=tk.LEFT)

    # ── command helpers ───────────────────────────────────────────────────
    def _op(self, op: str, **kw) -> bool:
        if self.resource is None or self.family is None:
            return False
        try:
            steps = get_command(self.family, op, ch=self.ch, **kw)
        except KeyError:
            self.app.log(f"[awg ch{self.ch}] {op!r} not supported")
            return False
        self.app.visa_async(self.resource, steps)
        return True

    def _apply_freq(self):
        try:
            f = float(self._freq_var.get())
        except ValueError:
            return
        self.app.log(f"[awg ch{self.ch}] freq → {f} Hz")
        self._op("set_frequency", freq=f"{f:.6g}")

    def _apply_amp(self):
        try:
            a = float(self._amp_var.get())
        except ValueError:
            return
        self.app.log(f"[awg ch{self.ch}] amp → {a} Vpp")
        self._op("set_amplitude", amp=f"{a:.4f}")
        self._op("set_amplitude_unit", unit="VPP")

    def _apply_offset(self):
        try:
            o = float(self._offs_var.get())
        except ValueError:
            return
        self.app.log(f"[awg ch{self.ch}] offset → {o} V")
        self._op("set_offset", offset=f"{o:.4f}")

    def _apply_func(self):
        fn = self._func_var.get()
        self.app.log(f"[awg ch{self.ch}] func → {fn}")
        self._op("set_function", func=fn)

    def _apply_all(self):
        self._apply_func()
        self._apply_freq()
        self._apply_amp()
        self._apply_offset()

    def _toggle_output(self):
        if self._output_on:
            self._op("output_off")
            self._set_output_state(False)
            self.app.log(f"[awg ch{self.ch}] output OFF")
        else:
            self._op("output_on")
            self._set_output_state(True)
            self.app.log(f"[awg ch{self.ch}] output ON")

    def _set_output_state(self, on: bool):
        self._output_on = on
        if on:
            self._out_btn.config(text="OUT ON ", bg=GREEN, fg=BG)
            self._out_dot.on()
        else:
            self._out_btn.config(text="OUT OFF", bg=RED, fg=FG_BRIGHT)
            self._out_dot.off()

    def set_connected(self, connected: bool):
        if not connected:
            self._set_output_state(False)


class AWGCard:
    """Controls card for a function / arbitrary waveform generator."""

    def __init__(self, parent, entry: dict, app, n_channels: int = 1):
        self.entry = entry
        self.app = app
        self.family = _family_for(entry)
        self.resource = None
        self.n_channels = n_channels

        subtitle = f"{entry['manufacturer']} {entry['model']} · {entry['connection']}"
        self.outer, body, self.dot = _card_frame(
            parent, "Function Generator", icon="〰",
            subtitle=subtitle, accent_color=PURPLE,
        )
        self._build(body)

    def _build(self, body):
        ctrl = _frame(body)
        ctrl.pack(fill=tk.X, pady=(0, 8))

        _label(ctrl, text="Channels:", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 9)).pack(side=tk.LEFT)
        self._n_var = tk.IntVar(value=self.n_channels)
        spin = tk.Spinbox(ctrl, from_=1, to=4, width=3,
                          textvariable=self._n_var,
                          command=self._rebuild,
                          bg=PANEL3, fg=FG_BRIGHT,
                          buttonbackground=PANEL3,
                          relief="flat", font=(FMONO, 10))
        spin.pack(side=tk.LEFT, padx=(4, 12))

        _btn(ctrl, "Reset", cmd=self._reset, bg=PANEL3, pady=4)\
            .pack(side=tk.LEFT)

        self._ch_container = _frame(body)
        self._ch_container.pack(fill=tk.X)
        self._channels: list[AWGChannelPanel] = []
        self._make_channels()

    def _make_channels(self):
        for w in self._ch_container.winfo_children():
            w.destroy()
        self._channels = []
        n = self._n_var.get()
        for i in range(1, n + 1):
            ch = AWGChannelPanel(self._ch_container, i, self.family, self.app)
            ch.resource = self.resource
            self._channels.append(ch)

    def _rebuild(self):
        self._make_channels()

    def _reset(self):
        if self.resource is None or self.family is None:
            return
        try:
            steps = get_command(self.family, "reset")
            self.app.visa_async(self.resource, steps)
            self.app.log("[awg] reset")
        except KeyError:
            pass

    def set_connected(self, connected: bool):
        if connected:
            self.dot.on()
        else:
            self.dot.off()
        for ch in self._channels:
            ch.resource = self.resource if connected else None
            ch.set_connected(connected)


class DMMCard:
    """Controls card for a digital multimeter."""

    MODES = {
        "DC Voltage":    "measure_vdc",
        "AC Voltage":    "measure_vac",
        "DC Current":    "measure_idc",
        "AC Current":    "measure_iac",
        "Resistance":    "measure_resistance",
        "4W Resistance": "measure_fresistance",
        "Frequency":     "measure_frequency",
        "Continuity":    "measure_continuity",
        "Diode":         "measure_diode",
        "Capacitance":   "measure_capacitance",
    }

    MODE_UNITS = {
        "DC Voltage": "V", "AC Voltage": "V",
        "DC Current": "A", "AC Current": "A",
        "Resistance": "Ω", "4W Resistance": "Ω",
        "Frequency": "Hz", "Continuity": "Ω",
        "Diode": "V", "Capacitance": "F",
    }

    def __init__(self, parent, entry: dict, app):
        self.entry = entry
        self.app = app
        self.family = _family_for(entry)
        self.resource = None
        self._poll_active = False

        subtitle = f"{entry['manufacturer']} {entry['model']} · {entry['connection']}"
        self.outer, body, self.dot = _card_frame(
            parent, "Multimeter", icon="🔢",
            subtitle=subtitle, accent_color=ORANGE,
        )
        self._build(body)

    def _build(self, body):
        # Mode selector
        mode_row = _frame(body)
        mode_row.pack(fill=tk.X, pady=(0, 8))
        _label(mode_row, text="Mode:", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 9)).pack(side=tk.LEFT)

        self._mode_var = tk.StringVar(value="DC Voltage")
        mode_box = ttk.Combobox(
            mode_row, textvariable=self._mode_var,
            values=list(self.MODES.keys()),
            state="readonly", width=18,
        )
        mode_box.pack(side=tk.LEFT, padx=(6, 0))

        # Big reading display
        display = _frame(body, bg=BG)
        display.pack(fill=tk.X, pady=(0, 8))
        display.config(padx=12, pady=12)

        self._reading_var = tk.StringVar(value="———")
        self._reading_lbl = tk.Label(
            display, textvariable=self._reading_var,
            bg=BG, fg=GREEN, font=(FMONO, 28, "bold"),
        )
        self._reading_lbl.pack()

        self._unit_lbl = _label(display, text="V",
                                fg=FG_DIM, bg=BG, font=(FSAN, 12))
        self._unit_lbl.pack()

        # Controls
        ctrl = _frame(body)
        ctrl.pack(fill=tk.X)
        _btn(ctrl, "Measure", cmd=self._measure_once,
             bg=ACCENT, fg=BG, pady=5).pack(side=tk.LEFT, padx=(0, 8))
        self._poll_btn = _btn(ctrl, "Auto-poll: OFF",
                              cmd=self._toggle_poll, bg=PANEL3, pady=5)
        self._poll_btn.pack(side=tk.LEFT)

    def _measure_once(self):
        if self.resource is None:
            return
        mode = self._mode_var.get()
        op = self.MODES.get(mode)
        if op is None:
            return
        resource = self.resource
        family = self.family
        app = self.app

        def do():
            try:
                steps = get_command(family, op)
                results = _run_cmd(resource, steps)
                return results[-1] if results else None
            except (KeyError, Exception) as e:
                return f"ERR: {e}"

        def on_done(result, error):
            if error:
                self._reading_var.set("ERR")
                app.log(f"[dmm] {error}")
                return
            try:
                val = float(result)
                self._reading_var.set(f"{val:.6g}")
            except (ValueError, TypeError):
                self._reading_var.set(str(result)[:12])
            unit = self.MODE_UNITS.get(mode, "")
            self._unit_lbl.config(text=unit)

        app._run_async(do, on_done)

    def _toggle_poll(self):
        self._poll_active = not self._poll_active
        if self._poll_active:
            self._poll_btn.config(text="Auto-poll: ON ", bg=GREEN, fg=BG)
            self._schedule_poll()
        else:
            self._poll_btn.config(text="Auto-poll: OFF", bg=PANEL3, fg=FG_BRIGHT)

    def _schedule_poll(self):
        if self._poll_active and self.resource is not None:
            self._measure_once()
            self.outer.after(1200, self._schedule_poll)

    def set_connected(self, connected: bool):
        if connected:
            self.dot.on()
        else:
            self.dot.off()
            self._reading_var.set("———")
            self._poll_active = False
            self._poll_btn.config(text="Auto-poll: OFF", bg=PANEL3, fg=FG_BRIGHT)


# ════════════════════════════════════════════════════════════════════════════════
# Main application window
# ════════════════════════════════════════════════════════════════════════════════

# Default channel counts by family type (heuristic from model name)
_CH_DEFAULTS = {
    "psu": 1,
    "awg": 1,
    "scope": 4,
    "dmm": 1,
    "smu": 1,
    "load": 1,
}

def _infer_channels(entry: dict) -> int:
    """Guess channel count from model name / family.

    Explicit model-pattern table takes priority so we never misread a trailing
    digit (e.g. EDU36311A ends in '1' but has 3 outputs).
    """
    model = entry.get("model", "")
    fid   = entry.get("family_id", "")
    itype = entry.get("type", "")

    if itype == "scope":
        return 4   # show 4 channel indicators for all scopes

    # Explicit overrides: checked before any regex ─────────────────────────
    # Keysight / EDU PSUs
    if any(p in model for p in ("36311A", "36312A", "36313A",
                                 "E36311A", "E36312A", "E36313A")):
        return 3
    if any(p in model for p in ("E36231A", "E36232A", "E36233A", "E36234A")):
        return 2

    # Rigol DP800 series
    if re.search(r"DP8[23]1", model):   return 1  # single-output
    if re.search(r"DP8[23]2", model):   return 2
    if re.search(r"DP8[23][13]", model):return 3

    # R&S HMC804x
    if "HMC8041" in model: return 1
    if "HMC8042" in model: return 2
    if "HMC8043" in model: return 3

    # Keithley 223x/2231A
    if re.search(r"2231A", model):  return 3
    if re.search(r"2230", model):   return 2

    # AWGs: look for channel count digit in the *significant* part of the name
    # e.g. EDU33211A → 1, EDU33212A → 2, AFG3022 → 2, DG832 → 2
    if itype == "awg":
        # Siglent SDG
        m = re.search(r"SDG\d*0([124])\d*X?$", model)
        if m:
            return int(m.group(1))
        # General pattern: digit followed by digit(s) then optional letter at end
        # We want the second-to-last numeric group
        nums = re.findall(r'\d+', model)
        if nums:
            last = nums[-1]
            if len(last) >= 1 and 1 <= int(last[-1]) <= 4:
                return int(last[-1])
        return 1

    return _CH_DEFAULTS.get(itype, 1)


class WorkbenchGUI(tk.Tk):
    """Main application window."""

    def __init__(self, workbench_name: str | None = None, demo: bool = False):
        super().__init__()
        self.demo = demo
        self._initial_wb_name = workbench_name

        self.wb: dict | None = None
        self.rm = None
        self._resources: dict[str, object] = {}   # resource_string → pyvisa resource
        self._cards: list = []                     # all instrument card objects
        self._psu_cards: list[PSUCard] = []
        self._poll_interval_ms = 1500
        self._connected = False
        self._visa_lock = threading.Lock()

        global FSAN, FMONO
        FSAN, FMONO = _fonts()

        self._configure_window()
        self._apply_ttk_style()
        self._build_layout()
        self.after(200, self._initial_load)
        self.after(self._poll_interval_ms, self._poll_tick)

    def _configure_window(self):
        self.title("open-EE-workbench")
        self.geometry("1280x820")
        self.minsize(900, 600)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Try to set a window icon from a simple unicode title bar hack (no icon file needed)
        try:
            self.wm_iconname("open-EE-workbench")
        except Exception:
            pass

    def _apply_ttk_style(self):
        style = ttk.Style(self)
        # Use a base theme then override
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("TCombobox",
                        fieldbackground=PANEL3, background=PANEL3,
                        foreground=FG_BRIGHT, arrowcolor=FG,
                        selectbackground=ACCENT, selectforeground=BG)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL3)])
        style.configure("TSeparator", background=BORDER)

    # ── Layout ─────────────────────────────────────────────────────────────
    def _build_layout(self):
        self._build_header()

        body = _frame(self, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Sidebar
        sidebar = self._build_sidebar(body)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(12, 0), pady=(8, 8))

        sep = tk.Frame(body, bg=BORDER, width=1)
        sep.pack(side=tk.LEFT, fill=tk.Y, padx=8)

        # Main scrollable instrument area
        right = _frame(body, bg=BG)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=8)
        self._instrument_container = right
        self._scroll_canvas, self._scroll_frame = self._scrollable_frame(right)

        self._build_statusbar()

    def _build_header(self):
        hdr = _frame(self, bg=PANEL)
        hdr.pack(fill=tk.X)

        # Left: nacho logo + title
        left = _frame(hdr, bg=PANEL)
        left.pack(side=tk.LEFT, padx=16, pady=8)

        # Try to render the nacho logo; fall back to a Unicode glyph
        self._logo_img = _load_logo(size=40, light=True)
        if self._logo_img:
            logo_lbl = tk.Label(left, image=self._logo_img,
                                bg=PANEL, cursor="hand2")
            logo_lbl.pack(side=tk.LEFT, padx=(0, 10))
            logo_lbl.bind("<Button-1>", self._open_nacho_url)
        else:
            # Text fallback — a simple chip glyph in the accent colour
            tk.Label(left, text="◈", font=(FSAN, 20),
                     fg=ACCENT, bg=PANEL, cursor="hand2")\
              .pack(side=tk.LEFT, padx=(0, 8))

        title_col = _frame(left, bg=PANEL)
        title_col.pack(side=tk.LEFT)
        _label(title_col, text="open-EE-workbench",
               font=(FSAN, 13, "bold"), fg=FG_BRIGHT, bg=PANEL).pack(anchor=tk.W)
        nacho_lnk = tk.Label(title_col, text="nacho.works",
                             font=(FSAN, 8), fg=FG_DIM, bg=PANEL,
                             cursor="hand2")
        nacho_lnk.pack(anchor=tk.W)
        nacho_lnk.bind("<Button-1>", self._open_nacho_url)
        nacho_lnk.bind("<Enter>", lambda e: nacho_lnk.config(fg=ACCENT))
        nacho_lnk.bind("<Leave>", lambda e: nacho_lnk.config(fg=FG_DIM))

        # Right: workbench selector + connect button
        right = _frame(hdr, bg=PANEL)
        right.pack(side=tk.RIGHT, padx=16, pady=10)

        _label(right, text="Workbench:", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 9)).pack(side=tk.LEFT, padx=(0, 6))

        self._wb_var = tk.StringVar()
        self._wb_combo = ttk.Combobox(right, textvariable=self._wb_var,
                                      state="readonly", width=20)
        self._wb_combo.pack(side=tk.LEFT, padx=(0, 8))
        self._wb_combo.bind("<<ComboboxSelected>>", self._on_wb_selected)

        self._connect_btn = _btn(right, "Connect", cmd=self._toggle_connect,
                                 bg=ACCENT, fg=BG, pady=6, padx=16)
        self._connect_btn.pack(side=tk.LEFT)

        # Thin bottom border
        tk.Frame(hdr, bg=BORDER, height=1).pack(fill=tk.X, side=tk.BOTTOM)

    @staticmethod
    def _open_nacho_url(event=None):
        """Open nacho.works in the default browser."""
        webbrowser.open("https://nacho.works")

    def _build_sidebar(self, parent) -> tk.Frame:
        sb = _frame(parent, bg=PANEL)
        sb.config(width=210)
        sb.pack_propagate(False)

        # Workbench summary
        _label(sb, text="WORKBENCH", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 8, "bold")).pack(anchor=tk.W, padx=12, pady=(12, 4))

        self._wb_name_lbl = _label(sb, text="—",
                                   fg=FG_BRIGHT, bg=PANEL,
                                   font=(FSAN, 11, "bold"))
        self._wb_name_lbl.pack(anchor=tk.W, padx=12, pady=(0, 2))

        self._wb_host_lbl = _label(sb, text="",
                                   fg=FG_DIM, bg=PANEL,
                                   font=(FSAN, 8))
        self._wb_host_lbl.pack(anchor=tk.W, padx=12, pady=(0, 8))

        tk.Frame(sb, bg=BORDER, height=1).pack(fill=tk.X, padx=12)

        # Instruments list
        _label(sb, text="INSTRUMENTS", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 8, "bold")).pack(anchor=tk.W, padx=12, pady=(8, 4))

        self._instr_list_frame = _frame(sb, bg=PANEL)
        self._instr_list_frame.pack(fill=tk.X, padx=12)

        tk.Frame(sb, bg=BORDER, height=1).pack(fill=tk.X, padx=12, pady=8)

        # Quick scripts
        _label(sb, text="SCRIPTS", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 8, "bold")).pack(anchor=tk.W, padx=12, pady=(0, 4))

        scripts = [
            ("Screenshot",  self._run_screenshot),
            ("AC Sweep",    self._run_ac_sweep),
        ]
        for name, cmd in scripts:
            btn = _btn(sb, f"▶  {name}", cmd=cmd, bg=PANEL2,
                       width=20, anchor=tk.W, padx=10)
            btn.pack(fill=tk.X, padx=12, pady=2)

        tk.Frame(sb, bg=BORDER, height=1).pack(fill=tk.X, padx=12, pady=8)

        # Poll interval control
        _label(sb, text="POLL INTERVAL", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 8, "bold")).pack(anchor=tk.W, padx=12, pady=(0, 4))
        poll_row = _frame(sb, bg=PANEL)
        poll_row.pack(fill=tk.X, padx=12)
        self._poll_var = tk.IntVar(value=self._poll_interval_ms // 1000)
        tk.Spinbox(poll_row, from_=1, to=30, width=4,
                   textvariable=self._poll_var,
                   bg=PANEL3, fg=FG_BRIGHT,
                   buttonbackground=PANEL3,
                   relief="flat", font=(FMONO, 10))\
            .pack(side=tk.LEFT, padx=(0, 4))
        _label(poll_row, text="seconds", fg=FG_DIM, bg=PANEL,
               font=(FSAN, 9)).pack(side=tk.LEFT)

        # ── nacho.works branding footer ─────────────────────────────────
        # Push it to the bottom of the sidebar
        footer = _frame(sb, bg=PANEL)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=12, pady=(8, 10))

        tk.Frame(footer, bg=BORDER, height=1).pack(fill=tk.X, pady=(0, 8))

        logo_row = _frame(footer, bg=PANEL)
        logo_row.pack(anchor=tk.W)

        self._sidebar_logo_img = _load_logo(size=22, light=True)
        if self._sidebar_logo_img:
            lbl = tk.Label(logo_row, image=self._sidebar_logo_img,
                           bg=PANEL, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=(0, 6))
            lbl.bind("<Button-1>", self._open_nacho_url)

        link = tk.Label(logo_row, text="nacho.works",
                        font=(FSAN, 9), fg=FG_DIM, bg=PANEL,
                        cursor="hand2")
        link.pack(side=tk.LEFT)
        link.bind("<Button-1>", self._open_nacho_url)
        link.bind("<Enter>", lambda e: link.config(fg=ACCENT))
        link.bind("<Leave>", lambda e: link.config(fg=FG_DIM))

        return sb

    def _scrollable_frame(self, parent) -> tuple[tk.Canvas, tk.Frame]:
        """Create a canvas + scrollbar + inner frame for scrollable content."""
        canvas = tk.Canvas(parent, bg=BG, bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = _frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=inner, anchor=tk.NW)

        def _resize(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfig(window_id, width=event.width)
        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(window_id, width=e.width))

        # Mouse wheel
        def _wheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _wheel)
        canvas.bind_all("<Button-4>",
                        lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>",
                        lambda e: canvas.yview_scroll(1, "units"))

        return canvas, inner

    def _build_statusbar(self):
        bar = _frame(self, bg=PANEL2)
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(bar, bg=BORDER, height=1).pack(fill=tk.X)

        # Log area toggle
        log_toggle_row = _frame(bar, bg=PANEL2)
        log_toggle_row.pack(fill=tk.X, padx=8, pady=2)

        _btn(log_toggle_row, "▼ Log", cmd=self._toggle_log,
             bg=PANEL2, pady=2, padx=6).pack(side=tk.LEFT)

        self._status_lbl = _label(log_toggle_row, text="Ready",
                                  fg=FG_DIM, bg=PANEL2, font=(FSAN, 9))
        self._status_lbl.pack(side=tk.RIGHT, padx=8)

        # Log text (collapsed by default)
        self._log_frame = _frame(bar, bg=BG)
        self._log_text = tk.Text(
            self._log_frame, bg=BG, fg=FG, font=(FMONO, 9),
            height=6, state=tk.DISABLED, relief="flat",
            insertbackground=ACCENT,
        )
        log_sb = ttk.Scrollbar(self._log_frame, command=self._log_text.yview)
        self._log_text.config(yscrollcommand=log_sb.set)
        log_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(fill=tk.BOTH, expand=True)
        self._log_visible = False

    def _toggle_log(self):
        if self._log_visible:
            self._log_frame.pack_forget()
            self._log_visible = False
        else:
            self._log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
            self._log_visible = True

    # ── Workbench loading ──────────────────────────────────────────────────
    def _initial_load(self):
        wb_names = self._list_workbenches()
        self._wb_combo["values"] = wb_names
        name = self._initial_wb_name or active_name()
        if name and name in wb_names:
            self._wb_var.set(name)
            self._load_workbench(name)
        elif wb_names:
            self._wb_var.set(wb_names[0])
            self._load_workbench(wb_names[0])

    def _list_workbenches(self) -> list[str]:
        names = []
        for f in WORKBENCH_DIR.glob("*.json"):
            if f.name != "active.json":
                names.append(f.stem)
        return sorted(names)

    def _on_wb_selected(self, event=None):
        name = self._wb_var.get()
        if name:
            self._disconnect()
            self._load_workbench(name)

    def _load_workbench(self, name: str):
        try:
            self.wb = load_workbench(name)
        except Exception as e:
            self.log(f"[workbench] Failed to load {name!r}: {e}")
            return

        self._wb_name_lbl.config(text=self.wb.get("name", name))
        host = self.wb.get("host", "")
        created = self.wb.get("created_at", "")[:10]
        self._wb_host_lbl.config(text=f"{host}  {created}".strip())

        self._populate_instrument_list()
        self._build_instrument_cards()
        self.log(f"[workbench] Loaded {name!r}: "
                 f"{len(self.wb.get('instruments', []))} instrument(s)")

    def _populate_instrument_list(self):
        for w in self._instr_list_frame.winfo_children():
            w.destroy()

        if not self.wb:
            return

        for instr in self.wb.get("instruments", []):
            row = _frame(self._instr_list_frame, bg=PANEL)
            row.pack(fill=tk.X, pady=1)
            icon = {"scope": "📡", "psu": "⚡", "awg": "〰",
                    "dmm": "🔢", "smu": "🔬", "load": "⬇"
                    }.get(instr.get("type", ""), "●")
            _label(row, text=f"{icon} {instr.get('model', '?')}",
                   fg=FG, bg=PANEL, font=(FSAN, 9)).pack(side=tk.LEFT)
            _label(row, text=instr.get("connection", ""),
                   fg=FG_DIM, bg=PANEL, font=(FSAN, 8)).pack(side=tk.RIGHT)

    def _build_instrument_cards(self):
        """Rebuild the instrument cards area from the current workbench."""
        for w in self._scroll_frame.winfo_children():
            w.destroy()
        self._cards = []
        self._psu_cards = []

        if not self.wb:
            _label(self._scroll_frame,
                   text="No workbench loaded.\nRun  python core/nachoVisa.py  to scan your bench.",
                   fg=FG_DIM, bg=BG, font=(FSAN, 12),
                   justify=tk.CENTER).pack(expand=True, pady=60)
            return

        instruments = self.wb.get("instruments", [])
        if not instruments:
            _label(self._scroll_frame, text="No instruments in this workbench.",
                   fg=FG_DIM, bg=BG, font=(FSAN, 12)).pack(pady=60)
            return

        # De-duplicate by serial+type (workbench may list same instrument twice)
        seen = set()
        unique = []
        for instr in instruments:
            key = (instr.get("serial", ""), instr.get("type", ""))
            if key not in seen:
                seen.add(key)
                unique.append(instr)

        # Layout in a 2-column grid
        col = 0
        row = 0
        self._scroll_frame.columnconfigure(0, weight=1)
        self._scroll_frame.columnconfigure(1, weight=1)

        for instr in unique:
            itype = instr.get("type", "")
            n_ch = _infer_channels(instr)
            card = None

            if itype == "scope":
                sc = ScopeCard(self._scroll_frame, instr, self)
                sc.outer.grid(row=row, column=col, padx=8, pady=8,
                              sticky=tk.NSEW)
                card = sc

            elif itype == "psu":
                pc = PSUCard(self._scroll_frame, instr, self, n_channels=n_ch)
                pc.outer.grid(row=row, column=col, padx=8, pady=8,
                              sticky=tk.NSEW)
                self._psu_cards.append(pc)
                card = pc

            elif itype == "awg":
                ac = AWGCard(self._scroll_frame, instr, self, n_channels=n_ch)
                ac.outer.grid(row=row, column=col, padx=8, pady=8,
                              sticky=tk.NSEW)
                card = ac

            elif itype == "dmm":
                dc = DMMCard(self._scroll_frame, instr, self)
                dc.outer.grid(row=row, column=col, padx=8, pady=8,
                              sticky=tk.NSEW)
                card = dc

            if card is not None:
                card._wb_entry = instr  # back-ref for connect
                self._cards.append(card)
                col += 1
                if col >= 2:
                    col = 0
                    row += 1

    # ── VISA connection ────────────────────────────────────────────────────
    def _toggle_connect(self):
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        if not PYVISA_OK:
            messagebox.showinfo("Demo mode",
                                "pyvisa is not available.\n"
                                "The GUI runs in demo mode only.")
            return
        if not self.wb:
            messagebox.showwarning("No workbench", "Load a workbench first.")
            return

        self._connect_btn.config(text="Connecting…", state=tk.DISABLED)
        self.log("[connect] Opening pyvisa ResourceManager…")

        def do_connect():
            rm = pyvisa.ResourceManager("@py")
            results = {}
            for instr in self.wb.get("instruments", []):
                resource_str = instr.get("resource", "")
                if not resource_str or resource_str in results:
                    continue
                try:
                    res = rm.open_resource(resource_str)
                    res.timeout = 8000
                    if "SOCKET" in resource_str.upper():
                        res.read_termination  = "\n"
                        res.write_termination = "\n"
                    idn = res.query("*IDN?").strip()
                    results[resource_str] = (res, idn, None)
                except Exception as e:
                    results[resource_str] = (None, None, str(e))
            return rm, results

        def on_done(result, error):
            self._connect_btn.config(state=tk.NORMAL)
            if error:
                self.log(f"[connect] Failed: {error}")
                self._connect_btn.config(text="Connect", bg=ACCENT, fg=BG)
                messagebox.showerror("Connection error", str(error))
                return

            rm, results = result
            self.rm = rm
            self._resources = {}

            any_ok = False
            for resource_str, (res, idn, err) in results.items():
                if err:
                    self.log(f"[connect] ✗ {resource_str}: {err}")
                else:
                    self._resources[resource_str] = res
                    self.log(f"[connect] ✓ {resource_str}  →  {idn}")
                    any_ok = True

            # Wire resources to cards
            for card in self._cards:
                entry = getattr(card, "_wb_entry", None) or card.entry
                res = self._resources.get(entry.get("resource", ""))
                card.resource = res
                card.set_connected(res is not None)

            self._connected = any_ok
            if any_ok:
                self._connect_btn.config(text="Disconnect", bg=RED, fg=FG_BRIGHT)
                self._status("Connected — " +
                             f"{sum(1 for _, (r,_,_) in results.items() if r)} instrument(s)")
            else:
                self._status("No instruments connected")
                self._connect_btn.config(text="Connect", bg=ACCENT, fg=BG)

        self._run_async(do_connect, on_done)

    def _disconnect(self):
        for res in self._resources.values():
            try:
                res.close()
            except Exception:
                pass
        self._resources = {}
        if self.rm:
            try:
                self.rm.close()
            except Exception:
                pass
            self.rm = None

        self._connected = False
        for card in self._cards:
            card.resource = None
            card.set_connected(False)

        self._connect_btn.config(text="Connect", bg=ACCENT, fg=BG,
                                 state=tk.NORMAL)
        self._status("Disconnected")
        self.log("[connect] Disconnected from all instruments")

    # ── Polling ─────────────────────────────────────────────────────────────
    def _poll_tick(self):
        interval = max(1, self._poll_var.get()) * 1000
        if self._connected:
            for pc in self._psu_cards:
                pc.poll()
        self.after(interval, self._poll_tick)

    # ── Background VISA worker ───────────────────────────────────────────────
    def _run_async(self, fn, callback=None):
        """Run fn() in a background thread; call callback(result, error) on main thread."""
        def worker():
            try:
                result = fn()
                err = None
            except Exception as e:
                result = None
                err = e
            if callback:
                self.after(0, lambda: callback(result, err))
        t = threading.Thread(target=worker, daemon=True)
        t.start()

    def visa_async(self, resource, steps: list[tuple[str, str]]):
        """Fire-and-forget VISA write steps (no callback)."""
        if self.demo or resource is None:
            cmds = [s for a, s in steps if a in ("write", "query", "raw_query")]
            self.log("[demo] " + " ; ".join(cmds[:3]))
            return

        def worker():
            with self._visa_lock:
                for action, scpi in steps:
                    try:
                        if action == "write":
                            resource.write(scpi)
                            self.after(0, lambda s=scpi: self.log(f"→  {s}"))
                        elif action == "query":
                            r = resource.query(scpi).strip()
                            self.after(0, lambda s=scpi, rv=r: self.log(f"?  {s}  ←  {rv}"))
                    except Exception as e:
                        self.after(0, lambda err=e: self.log(f"[err] {err}"))

        threading.Thread(target=worker, daemon=True).start()

    # ── Script shortcuts ─────────────────────────────────────────────────────
    def _run_screenshot(self):
        # Find the first scope card and trigger its screenshot
        for card in self._cards:
            if isinstance(card, ScopeCard):
                card._screenshot()
                return
        messagebox.showinfo("No scope", "No oscilloscope found in the current workbench.")

    def _run_ac_sweep(self):
        script = ROOT / "scripts" / "acAnalysis.py"
        if not script.exists():
            messagebox.showwarning("Not found", f"Script not found:\n{script}")
            return
        subprocess.Popen([sys.executable, str(script)],
                         cwd=str(ROOT / "scripts"))
        self.log(f"[script] Launched {script.name}")

    # ── Logging / status ─────────────────────────────────────────────────────
    def log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {text}"
        self._log_text.config(state=tk.NORMAL)
        self._log_text.insert(tk.END, msg + "\n")
        self._log_text.see(tk.END)
        self._log_text.config(state=tk.DISABLED)
        print(msg)

    def _status(self, text: str):
        self._status_lbl.config(text=text)

    def _on_close(self):
        self._disconnect()
        self.destroy()


# ════════════════════════════════════════════════════════════════════════════════
# Entry point
# ════════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="open-EE-workbench GUI")
    parser.add_argument("workbench", nargs="?", default=None,
                        help="Workbench name to load on startup")
    parser.add_argument("--demo", action="store_true",
                        help="Demo mode — no hardware required")
    args = parser.parse_args()

    if args.demo and not PYVISA_OK:
        print("[gui] Running in demo mode (pyvisa not available)")

    app = WorkbenchGUI(workbench_name=args.workbench, demo=args.demo)
    app.mainloop()


if __name__ == "__main__":
    main()
