"""
core/shared.py — global mutable state, threading primitives, and executor.

All modules import this module to access shared state.  `sio` starts as None
and is injected by app.py after SocketIO is created.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ── PyVISA ────────────────────────────────────────────────────────────────────
try:
    import pyvisa
    PYVISA_OK = True
except ImportError:
    PYVISA_OK = False
    pyvisa = None

# ── Paths ─────────────────────────────────────────────────────────────────────
# core/ lives one level below the project root
WORKBENCH_DIR = Path(__file__).parent.parent / "workbenches"

# ── Screenshot chunk size ─────────────────────────────────────────────────────
# Must exceed the largest screenshot so USBTMC.read() accumulates the full
# binary payload (e.g. 1.15 MB BMP) before returning.
_SCREENSHOT_CHUNK_SIZE = 2 * 1024 * 1024   # 2 MB

# ── App state ─────────────────────────────────────────────────────────────────
_state: dict = {
    "workbench":    None,   # loaded workbench dict (with _unique list)
    "wb_name":      None,
    "rm":           None,   # pyvisa ResourceManager
    "resources":    {},     # resource_str -> pyvisa resource
    "families":     {},     # resource_str -> resolved family dict
    "psu_channels": {},     # resource_str -> int (channel count probed at connect)
    "connected":    False,
}
_lock        = threading.Lock()
_executor    = ThreadPoolExecutor(max_workers=8)
_poll_stop   = threading.Event()
_poller_idle = threading.Event()
_poller_idle.set()   # no poller running initially
_psu_ch_cache: dict = {}  # resource → confirmed live channel count
_polling_enabled = False  # True only when user enables it via Resume polling button

# ── SocketIO handle (injected by app.py) ─────────────────────────────────────
sio = None
