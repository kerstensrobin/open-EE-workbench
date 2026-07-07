"""
core/demo.py — DemoResource and demo BMP generator for offline testing.
"""
import math as _math
import random as _random
import re as _re
import struct as _struct
import threading
import time

from core.shared import _SCREENSHOT_CHUNK_SIZE


def _make_demo_bmp() -> bytes:
    """Minimal 64×64 grey BMP — returned by DemoResource.read_raw() (screenshots)."""
    w, h = 64, 64
    row_pad  = (4 - (w * 3) % 4) % 4
    row_size = w * 3 + row_pad
    pix_size = row_size * h
    bfh = _struct.pack("<2sIHHI", b"BM", 14 + 40 + pix_size, 0, 0, 14 + 40)
    bih = _struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, pix_size, 0, 0, 0, 0)
    row = b"\x30\x30\x38" * w + b"\x00" * row_pad   # dark-grey row
    return bfh + bih + row * h


_DEMO_BMP = _make_demo_bmp()


class DemoResource:
    """
    Drop-in fake VISA resource for offline demos and UI testing.
    `write()` tracks set-points; `query()` returns semi-realistic noisy values.
    Resource strings begin with DEMO:: to identify them.
    """

    def __init__(self, resource_name: str, idn: str, instr_type: str, slot: int = 0):
        self._resource_name  = resource_name
        self._idn            = idn
        self._type           = instr_type
        self._slot           = slot          # used to de-correlate multiple DMMs
        self._visa_lock      = threading.Lock()
        self._sp: dict       = {}            # set-points written via write()
        self.timeout         = 8000
        self.chunk_size      = _SCREENSHOT_CHUNK_SIZE
        self.read_termination  = "\n"
        self.write_termination = "\n"

    # pyvisa interface ---------------------------------------------------

    def write(self, scpi: str):
        s = scpi.upper()
        try:
            tok = scpi.split()
            val_str = tok[-1] if tok else ""
            val = float(val_str)
            if "VOLT" in s and "MEAS" not in s and "UPP" not in s and "LOW" not in s:
                self._sp["voltage"] = val
            elif "CURR" in s and "MEAS" not in s and "LIM" in s:
                self._sp["i_limit"] = val
            elif "CURR" in s and "MEAS" not in s and "UPP" not in s and "LOW" not in s:
                self._sp["load_current"] = val
            elif "CENT" in s:
                self._sp["sa_center"] = val
            elif "SPAN" in s:
                self._sp["sa_span"] = val
            elif "FREQ" in s:
                self._sp["freq"] = val
            elif "AMPL" in s or ("VOLT" in s and "AWG" in self._type.upper()):
                self._sp["amplitude"] = val
            elif "POW" in s:
                self._sp["rf_power"] = val
            elif "WA" in s and "?" not in s:
                self._sp["laser_wl"] = val
        except (ValueError, IndexError):
            pass
        # track non-numeric eload state
        if ":FUNC " in s:
            self._sp["func"] = scpi.split()[-1].upper()
        if ":INP " in s:
            self._sp["inp"] = scpi.split()[-1].strip()
        # thermotron setpoint: "SETP1,25.0"
        if "SETP" in s and "," in scpi:
            try:
                self._sp["therm_sp"] = float(scpi.split(",")[-1])
            except (ValueError, IndexError):
                pass
        # motion: move absolute / relative  "1PA25.000" / "1PR5.000"
        if self._type == "motion":
            m = _re.search(r"PA(-?[\d.]+)", s)
            if m:
                self._sp["mot_pos"] = float(m.group(1))
            m = _re.search(r"PR(-?[\d.]+)", s)
            if m:
                self._sp["mot_pos"] = self._sp.get("mot_pos", 0.0) + float(m.group(1))

    def query(self, scpi: str) -> str:
        if "*IDN?" in scpi.upper():
            return self._idn
        return self._fake_value(scpi) + "\n"

    def read_raw(self) -> bytes:
        return _DEMO_BMP

    def close(self):
        pass

    # internals ----------------------------------------------------------

    def _fake_value(self, scpi: str) -> str:
        t  = time.time()
        s  = scpi.upper()
        sl = self._slot
        tp = self._type   # "scope" | "psu" | "awg" | "dmm" | ...

        # slow drifts — each slot and instrument type gets a different phase
        def slow(amp, period, phase=0.0):
            return amp * _math.sin(t * 2 * _math.pi / period + phase + sl * 1.3)

        def noise(sigma):
            return _random.gauss(0, sigma)

        # ── PSU / AWG set-point readback (SOURce queries, no MEAS) ───────
        if "VOLT" in s and "?" in s and "MEAS" not in s:
            sp = self._sp.get("voltage", 5.0 + sl * 0.3)
            return f"{sp + slow(0.02, 30) + noise(0.001):.6f}"
        if "CURR" in s and "?" in s and "MEAS" not in s:
            sp = self._sp.get("i_limit", 0.5)
            return f"{sp:.6f}"

        # ── Electronic load measurements ─────────────────────────────────
        if tp == "load":
            load_i = self._sp.get("load_current", 1.0)
            # Simulate discharging battery — fast enough to complete in demo (12.6→3 V in ~96 s)
            batt_v = 12.6 - (t % 120) * 0.08 + slow(0.05, 20) + noise(0.005)
            batt_v = max(batt_v, 2.8)
            if "MEAS" in s and "VOLT" in s:
                return f"{batt_v:.4f}"
            if "MEAS" in s and "CURR" in s:
                return f"{load_i + noise(0.002):.4f}"
            if "MEAS" in s and "POW" in s:
                return f"{batt_v * load_i:.4f}"
            if "FUNC" in s and "?" in s:
                return self._sp.get("func", "CURR")
            if "INP" in s and "?" in s:
                return self._sp.get("inp", "0")
            if "STAT" in s and "?" in s:
                return "0"
            if "CURR" in s and "UPP" in s:
                return "30.0000"
            if "VOLT" in s and "UPP" in s:
                return "150.0000"
            if "CURR" in s and "?" in s:
                return f"{load_i:.4f}"
            if "VOLT" in s and "?" in s:
                return f"{self._sp.get('voltage', 12.0):.4f}"
            return "0.0000"

        # ── Gaussmeter ───────────────────────────────────────────────────────
        if tp == "gaussmeter":
            if "RDGFIELD" in s:
                return f"{1200.0 + slow(30, 8) + noise(0.5):.4f}"
            return "+0.0"

        # ── Vacuum gauge ─────────────────────────────────────────────────────
        if tp == "vacuum":
            if "PR" in s and "?" in s:
                base = 1.2e-6
                val = base * (1.0 + slow(0.08, 30) + noise(0.02))
                return f"{max(val, 1e-9):.3E}"
            return "0.0"

        # ── Power meter ──────────────────────────────────────────────────────
        if tp == "power_meter":
            if "POW" in s and "?" in s:
                return f"{1.05e-4 + slow(3e-6, 15) + noise(2e-7):.6E}"
            if "WAV" in s and "?" in s:
                return f"{self._sp.get('laser_wl', 1550.0):.3f}"
            return "0.0"

        # ── Frequency counter ─────────────────────────────────────────────────
        if tp == "freq_counter":
            if "FREQ" in s:
                return f"{10.0e6 + slow(50, 20) + noise(5):.6f}"
            if "PER" in s:
                return f"{1.0e-7 + noise(5e-13):.9E}"
            if "APER" in s:
                return "0.100"
            return "0.0"

        # ── Temperature controller ────────────────────────────────────────────
        if tp == "temp_ctrl":
            sp = self._sp.get("setpoint", 22.0)
            if "CRDG" in s:
                return f"{sp + slow(0.3, 60) + noise(0.02):.4f}"
            if "KRDG" in s:
                return f"{sp + 273.15 + slow(0.3, 60) + noise(0.02):.4f}"
            if "SETP" in s and "?" in s:
                return f"{sp:.4f}"
            return "0.0"

        # ── Thermostream ──────────────────────────────────────────────────────
        if tp == "thermal":
            target = self._sp.get("therm_sp", 25.0)
            cur = self._sp.get("therm_cur", 25.0)
            self._sp["therm_cur"] = cur + (target - cur) * 0.05 + noise(0.05)
            if "PVAR1" in s or "TEMP" in s:
                return f"{self._sp['therm_cur']:.2f}"
            if "SETP1" in s:
                return f"{target:.2f}"
            if "TMPA" in s:
                return f"{self._sp['therm_cur'] + noise(1.5):.2f}"
            if "EROR" in s or "TECR" in s:
                return "0"
            return "0.0"

        # ── Lock-in amplifier ─────────────────────────────────────────────────
        if tp == "lock_in":
            if "OUTP" in s:
                r   = 1.234e-6 + slow(0.08e-6, 12) + noise(3e-9)
                phi = 23.5 + slow(0.4, 25) + noise(0.05)
                x   = r * _math.cos(_math.radians(phi))
                y   = r * _math.sin(_math.radians(phi))
                if "?1" in s: return f"{x:.6E}"
                if "?2" in s: return f"{y:.6E}"
                if "?3" in s: return f"{r:.6E}"
                if "?4" in s: return f"{phi:.4f}"
            if "FREQ" in s and "?" in s:
                return "1000.0000"
            if "SLVL" in s:
                return "0.0010"
            return "0.0"

        # ── LCR meter ─────────────────────────────────────────────────────────
        if tp == "lcr":
            if "FETC" in s:
                z     = 9950 + slow(12, 20) + noise(1.5)
                theta = -15.3 + slow(0.2, 18) + noise(0.04)
                return f"{z:.5E},{theta:.5E}"
            if "FREQ" in s and "?" in s:
                return f"{self._sp.get('freq', 1000.0):.4f}"
            if "FUNC" in s and "?" in s:
                return "ZTD"
            return "0.0"

        # ── RF generator ──────────────────────────────────────────────────────
        if tp == "rf_gen":
            if "FREQ" in s and "?" in s:
                return f"{self._sp.get('freq', 1.0e9):.6f}"
            if "POW" in s and "?" in s:
                return f"{self._sp.get('rf_power', 0.0):.2f}"
            if "AMPR" in s:
                return f"{self._sp.get('rf_power', 0.0):.2f}"
            if "OUTP" in s and "?" in s:
                return self._sp.get("rf_out", "0")
            return "0.0"

        # ── Spectrum analyzer ─────────────────────────────────────────────────
        if tp == "spectrum":
            center = self._sp.get("sa_center", 1.0e9)
            span   = self._sp.get("sa_span",   1.0e6)
            if "PEAK" in s:
                return f"{-45.0 + slow(1.5, 10) + noise(0.3):.2f}"
            if "CENT" in s and "?" in s:
                return f"{center:.6E}"
            if "SPAN" in s and "?" in s:
                return f"{span:.6E}"
            return "0.0"

        # ── VNA ───────────────────────────────────────────────────────────────
        if tp == "vna":
            if "STAR" in s and "?" in s:
                return f"{self._sp.get('vna_start', 1.0e6):.6E}"
            if "STOP" in s and "?" in s:
                return f"{self._sp.get('vna_stop', 3.0e9):.6E}"
            if "OUTP" in s and "?" in s:
                return self._sp.get("vna_out", "0")
            return "0.0"

        # ── Laser ─────────────────────────────────────────────────────────────
        if tp == "laser":
            if "WA" in s and "?" in s:
                wl = self._sp.get("laser_wl", 1550.0)
                return f"{wl + noise(0.0005):.4f}"
            if "POWER" in s and "?" in s:
                return self._sp.get("laser_out", "0")
            return "0.0"

        # ── Motion controller ─────────────────────────────────────────────────
        if tp == "motion":
            if "TP" in s:
                pos = self._sp.get("mot_pos", 0.0)
                return f"{pos + noise(5e-5):.6f}"
            return "0.0"

        # ── PSU output measurements (:MEASure:VOLTage? / :MEASure:CURRent?) ─
        if tp == "psu" and "MEAS" in s and "VOLT" in s:
            sp = self._sp.get("voltage", 5.0 + sl * 0.3)
            return f"{sp + slow(0.015, 28) + noise(0.001):.6f}"
        if tp == "psu" and "MEAS" in s and "CURR" in s:
            return f"{0.150 + slow(0.03, 20, 1.1) + noise(0.0005):.6f}"

        # ── Scope measurements ───────────────────────────────────────────
        if "VPP" in s:
            amp = self._sp.get("amplitude", 1.0)
            return f"{amp * 2 + slow(0.04, 25) + noise(0.003):.6f}"
        if "VMAX" in s:
            return f"{1.02 + slow(0.02, 22) + noise(0.002):.6f}"
        if "VMIN" in s:
            return f"{-1.02 + slow(0.02, 22, 2.5) + noise(0.002):.6f}"
        if "VAVG" in s or "MEAN" in s:
            return f"{0.0 + slow(0.005, 40) + noise(0.001):.6f}"
        if "RMS" in s or "VRMS" in s:
            return f"{0.354 + slow(0.01, 18) + noise(0.001):.6f}"
        if "PERIOD" in s and "?" in s:
            freq = self._sp.get("freq", 1000.0)
            return f"{1.0 / freq:.8e}"
        if "FREQ" in s and "?" in s:
            freq = self._sp.get("freq", 1000.0)
            return f"{freq + slow(freq * 0.001, 15) + noise(freq * 0.0001):.4f}"
        if "DUTY" in s or "DCYC" in s:
            return f"{50.0 + slow(0.5, 35) + noise(0.05):.4f}"
        if "RISE" in s or "FALL" in s:
            return f"{1.2e-6 + noise(5e-8):.4e}"
        if "OVER" in s:
            return f"{2.5 + noise(0.2):.4f}"
        if "PRESHOOT" in s:
            return f"{1.1 + noise(0.15):.4f}"
        if "WIDT" in s or "NWID" in s or "PWID" in s:
            freq = self._sp.get("freq", 1000.0)
            return f"{0.5 / freq:.6e}"

        # ── DMM measurements (any remaining MEAS+VOLT or explicit DC/AC) ─
        if "MEAS" in s and "VOLT" in s:
            # Each slot gets a distinct baseline for interesting multi-DMM plots
            bases = [3.300, 5.000, 1.800, 12.000]
            base  = bases[sl % len(bases)]
            return f"{base + slow(base * 0.01, 60 + sl * 15) + noise(base * 0.0003):.7f}"
        if "MEAS" in s and "CURR" in s:
            return f"{0.0821 + slow(0.005, 45) + noise(0.0001):.7f}"
        if "RES" in s or "OHM" in s or ("MEAS" in s and "FRES" in s):
            return f"{9985.0 + slow(5, 40) + noise(0.3):.5f}"
        if "CAP" in s:
            return f"{100e-9 + slow(1e-9, 50) + noise(5e-12):.6e}"
        if "FREQ" in s:
            return f"{50.012 + slow(0.002, 30) + noise(0.001):.5f}"
        if "CONT" in s:
            return "0.000"
        if "DIOD" in s:
            return f"{0.650 + noise(0.002):.4f}"

        # ── Generic scope/instrument status ─────────────────────────────
        if "DISP" in s:
            return "1"
        if "TIM" in s and "?" in s:
            return "0.001"

        return "+0.0"
