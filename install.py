#!/usr/bin/env python3
"""
install.py — set up open-EE-workbench on a new machine.

    python install.py

What it does:
  1. Installs required Python packages via pip
  2. Creates open-EE-workbench.desktop (Linux) so the app appears in
     your application menu and can be launched from the desktop
"""

import itertools
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from core.browser import find_chrome

# cmd.exe defaults to a legacy codepage (e.g. cp1252) that can't encode "→"/"…" —
# reconfigure so a print() with those characters can't crash the installer.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    # Legacy conhost.exe (a plain cmd.exe window, not Windows Terminal) does not
    # interpret ANSI/VT cursor-movement escapes by default — the spinner below
    # would print them as literal garbage instead of animating in place.
    try:
        import ctypes
        _kernel32 = ctypes.windll.kernel32
        _handle = _kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        _mode = ctypes.c_uint32()
        if _kernel32.GetConsoleMode(_handle, ctypes.byref(_mode)):
            _kernel32.SetConsoleMode(_handle, _mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

ROOT   = Path(__file__).parent.resolve()
PYTHON = sys.executable
VENV   = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


# ── Boxed logo + spinner (same visual as core/nachoVisa.py) ──────────────────

_SPINNER_FRAMES = ["|", "/", "-", "\\"]

# Logo lines — padded to _LOGO_PAD chars so right-side text lines up cleanly.
_LOGO_LINES = [
    "                ####",
    "              #######",
    "             #########",
    "           ############",
    "          ##############",
    "         ################",
    "       ###################",
    "      #######     ####",
    "    ##########   ######  ##",
    "   ###############   #######",
    "  ###############     #######",
    "##############################",
    "    ##########################",
    "                  #############",
]
_LOGO_PAD    = 36   # width each logo line is padded to before right-side text
_BOX_INNER   = _LOGO_PAD + 42  # inner width of the surrounding box (logo + text + padding)
_TITLE_ROW   = 4    # "nacho.works" goes here
_SUB_ROW     = 5    # subtitle goes here
_SPINNER_ROW = 7    # rotating arrow + status goes here
# Lines to move up from after the full box to reach the spinner row.
# +2 accounts for the top and bottom border lines.
_ROWS_TO_SPINNER = len(_LOGO_LINES) - _SPINNER_ROW + 1  # +1 for bottom border


def _logo_line(idx: int, frame: str = " ", msg: str = "") -> str:
    logo = _LOGO_LINES[idx].ljust(_LOGO_PAD)
    if idx == _TITLE_ROW:
        right = "nacho.works"
    elif idx == _SUB_ROW:
        right = "open-EE-workbench installer"
    elif idx == _SPINNER_ROW:
        right = f"{frame}  {msg}" if msg else ""
    else:
        right = ""
    inner = (logo + right).ljust(_BOX_INNER)
    return f"│{inner}│"


def _print_logo(frame: str = " ", msg: str = ""):
    print("┌" + "─" * _BOX_INNER + "┐")
    for i in range(len(_LOGO_LINES)):
        print(_logo_line(i, frame, msg))
    print("└" + "─" * _BOX_INNER + "┘")


class Spinner:
    """Animated status box shown while non-interactive setup steps run.

    Interactive steps (input() prompts) must happen outside this context
    manager — concurrent spinner redraws and prompt input would garble
    the terminal.
    """

    def __init__(self, message: str = "Working"):
        self._message = message
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _write_spinner_row(self, frame: str, msg: str):
        line = _logo_line(_SPINNER_ROW, frame, msg)
        n = _ROWS_TO_SPINNER
        sys.stdout.write(f"\033[{n}A\r{line}\033[{n}B\r")
        sys.stdout.flush()

    def _spin(self):
        for frame in itertools.cycle(_SPINNER_FRAMES):
            if self._stop.is_set():
                break
            with self._lock:
                msg = self._message
            self._write_spinner_row(frame, msg)
            time.sleep(0.15)
        self._write_spinner_row(" ", "")

    def update(self, message: str):
        with self._lock:
            self._message = message

    def __enter__(self):
        _print_logo(frame=_SPINNER_FRAMES[0], msg=self._message)
        self._thread.start()
        return self

    def __exit__(self, *args):
        self._stop.set()
        self._thread.join()
        print()


# ── 1. Dependencies ───────────────────────────────────────────────────────────

REQUIRED = [
    "pyvisa",
    "pyvisa-py",
    "pyusb",
    "pyserial",
    "flask",
    "flask-socketio",
    "pywebview",
    "pymeasure",
    "numpy",
    "h5py",
]

OPTIONAL = {
    "zeroconf":  "mDNS / LAN instrument discovery",
}

def _venv_pkg():
    return f"python{sys.version_info.major}.{sys.version_info.minor}-venv"


def ensure_venv(spinner: Spinner):
    if VENV_PYTHON.exists():
        return

    spinner.update("Creating virtual environment")
    try:
        subprocess.run(
            [PYTHON, "-m", "venv", str(VENV)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        if sys.platform == "linux" and Path("/etc/debian_version").exists():
            sys.exit(
                f"\n[error] Could not create virtual environment.\n"
                f"        On Ubuntu/Debian, install the venv package first:\n\n"
                f"            sudo apt install {_venv_pkg()}\n\n"
                f"        Then re-run:  python3 install.py"
            )
        raise


def _pip_install_with_progress(spinner: Spinner, args: list, label: str = "Installing"):
    """Run `pip install <args>`, updating the spinner with each package pip reports.

    pip's own progress bars aren't let through directly — they'd interleave with
    the spinner's cursor-movement escapes and garble the terminal — but "Collecting
    X" lines are parsed out so the spinner still shows what's currently happening
    instead of sitting on one static message for the whole (often slow) install.
    """
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "pip", "install", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    output = []
    for line in proc.stdout:
        output.append(line)
        m = re.match(r"Collecting (\S+)", line)
        if m:
            spinner.update(f"{label}: {m.group(1)}")
    proc.wait()
    return proc.returncode, "".join(output)


def install_deps():
    with Spinner("Preparing virtual environment") as spinner:
        ensure_venv(spinner)
        spinner.update("Installing required packages")
        returncode, output = _pip_install_with_progress(
            spinner, ["--upgrade", *REQUIRED], "Installing required packages"
        )
    if returncode != 0:
        sys.exit("Failed to install required packages:\n" + output)
    print(f"Virtual environment ready → {VENV}")

    print("\nOptional packages (skip with Ctrl-C to accept defaults):")
    for pkg, desc in OPTIONAL.items():
        ans = input(f"  Install {pkg} ({desc})? [y/N] ").strip().lower()
        if ans == "y":
            subprocess.check_call([str(VENV_PYTHON), "-m", "pip", "install", pkg])

    ensure_libusb_windows()


# ── 1b. libusb DLL (Windows only) ─────────────────────────────────────────────
#
# pyvisa-py's USB transport calls pyusb's `usb.core.find()` with no backend
# argument, so it falls back to `ctypes.util.find_library()`. On Windows that
# function only checks directories literally listed in the PATH env var — it
# does not consult the running executable's own directory. Without libusb-1.0
# somewhere on PATH, PyVISA-py silently finds zero USB instruments.
#
# The `libusb` PyPI package ships a prebuilt libusb-1.0.dll for every Windows
# arch, so we can stage it next to python.exe and have the launcher put that
# directory on PATH — no manual DLL hunting, no admin rights needed.
#
# NOTE: this does not replace Zadig. If an instrument's USB interface is
# already claimed by another driver (Windows' in-box USBTMC class driver, or
# a vendor IO Suite), libusb still cannot open it — that rebind is a manual,
# per-device, admin-elevated step (see README) that can't be done safely by
# a generic installer.
def ensure_libusb_windows():
    if sys.platform != "win32":
        return

    print("\nInstalling libusb (Windows USB backend for pyvisa-py)…")
    try:
        subprocess.check_call([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "libusb"])
        dll_path = subprocess.check_output(
            [str(VENV_PYTHON), "-c", "from libusb._platform import DLL_PATH; print(DLL_PATH)"],
            text=True,
        ).strip()
        dest = VENV_PYTHON.parent / "libusb-1.0.dll"
        shutil.copyfile(dll_path, dest)
        print(f"[install] libusb DLL staged → {dest}")
    except (subprocess.CalledProcessError, OSError) as exc:
        print(
            f"[install] Could not stage libusb-1.0.dll ({exc}).\n"
            "          USB instruments may not be found unless a vendor VISA "
            "implementation (NI-VISA, Keysight IO Libraries, etc.) is installed.",
            file=sys.stderr,
        )


# ── 2. Desktop launcher (Linux only) ─────────────────────────────────────────

def patch_launchers():
    launcher = ROOT / "open-eew"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        'cd "$(dirname "$0")"\n'
        'if [ -f .venv/bin/python ]; then\n'
        "    exec .venv/bin/python app.py \"$@\"\n"
        "else\n"
        '    exec python3 app.py "$@"\n'
        "fi\n"
    )
    launcher.chmod(launcher.stat().st_mode | 0o755)

    bat = ROOT / "open-eew.bat"
    bat.write_text(
        "@echo off\n"
        'cd /d "%~dp0"\n'
        'if exist .venv\\Scripts\\python.exe (\n'
        '    set "PATH=%~dp0.venv\\Scripts;%PATH%"\n'
        '    .venv\\Scripts\\python.exe app.py %*\n'
        ") else (\n"
        "    python app.py %*\n"
        ")\n"
    )


def make_desktop():
    if sys.platform != "linux":
        print("\n[install] Desktop launcher is Linux-only — skipping.")
        return

    icon_path = ROOT / "ui" / "gui_assets" / "icon.svg"
    if not icon_path.exists():
        icon_path = ROOT / "ui" / "gui_assets" / "icon.png"

    desktop_file = ROOT / "open-EE-workbench.desktop"
    content = f"""\
[Desktop Entry]
Version=1.1
Type=Application
Name=open-EE-workbench
GenericName=Lab Instrument Controller
Comment=Graphical control surface for VISA-connected lab instruments
Exec={ROOT / "open-eew"} %F
Path={ROOT}
Icon={icon_path}
Terminal=false
StartupNotify=true
StartupWMClass=open-EE-workbench
Categories=Science;Engineering;Electronics;Education;
Keywords=VISA;SCPI;oscilloscope;power supply;DMM;AWG;lab;instrument;
"""
    desktop_file.write_text(content, encoding="utf-8")
    desktop_file.chmod(desktop_file.stat().st_mode | 0o755)
    print(f"\n[install] Launcher written → {desktop_file}")

    ans = input(
        "Install into ~/.local/share/applications so it appears in your app menu? [y/N] "
    ).strip().lower()
    if ans == "y":
        apps_dir = Path.home() / ".local" / "share" / "applications"
        apps_dir.mkdir(parents=True, exist_ok=True)
        dest = apps_dir / desktop_file.name
        dest.write_text(desktop_file.read_text(), encoding="utf-8")
        dest.chmod(dest.stat().st_mode | 0o755)
        os.system("update-desktop-database ~/.local/share/applications 2>/dev/null")
        print(f"[install] Installed → {dest}")
    else:
        print("[install] Skipped. Double-click open-EE-workbench.desktop to launch.")


# ── Main ──────────────────────────────────────────────────────────────────────

def print_pywebview_apt_note():
    """pywebview needs GTK/WebKit apt packages that pip cannot provide."""
    if sys.platform != "linux" or not Path("/etc/debian_version").exists():
        return
    # Detect Ubuntu release to pick the right webkit package
    webkit_pkg = "gir1.2-webkit2-4.0"
    try:
        out = subprocess.check_output(["lsb_release", "-rs"], stderr=subprocess.DEVNULL).decode().strip()
        if float(out) >= 24.0:
            webkit_pkg = "gir1.2-webkit2-4.1"
    except Exception:
        pass
    print(
        "\n[install] pywebview requires system GTK/WebKit packages on Ubuntu/Debian.\n"
        "          If the app fails to open a window, run:\n\n"
        f"              sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 {webkit_pkg}\n"
    )


def print_chrome_note():
    """The app opens as a standalone window via a Chromium browser's --app mode.
    Without one, it falls back to a regular browser tab instead of its own window."""
    if find_chrome():
        return
    print(
        "\n[install] No Chrome, Edge, Brave, or Chromium install found.\n"
        "          The app will still work, but will open in a regular browser tab\n"
        "          instead of its own standalone window. Install one of those\n"
        "          browsers for the full app-window experience.\n"
    )


if __name__ == "__main__":
    print(f"open-EE-workbench installer\nProject root: {ROOT}\n")
    install_deps()
    patch_launchers()
    print_pywebview_apt_note()
    print_chrome_note()
    make_desktop()
    print("\nDone.")
