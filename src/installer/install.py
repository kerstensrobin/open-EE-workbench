#!/usr/bin/env python3
"""
install.py — set up open-EE-workbench on a new machine.

Students don't run this directly: they double-click install-windows.bat or
run install-linux-mac.sh in the project root, which find Python and call
this (on Windows via the install_gui.pyw window). Directly:

    python src/installer/install.py

What it does:
  1. Installs required Python packages via pip (plus, on Windows, libusb and
     the WinUSB driver for USB instruments)
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # src/
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

ROOT    = Path(__file__).resolve().parents[2]   # project root (this is src/installer/)
APP_DIR = ROOT / "src"
PYTHON  = sys.executable
VENV    = ROOT / ".venv"
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

    When stdout isn't a real terminal (piped to a GUI subprocess, redirected
    to a log file, …) the cursor-movement escapes that animate the box in
    place would just show up as literal garbage, so this falls back to
    plain "[install] <message>" lines instead.
    """

    def __init__(self, message: str = "Working"):
        self._message = message
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._tty = sys.stdout.isatty()
        self._thread = threading.Thread(target=self._spin, daemon=True) if self._tty else None

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
        if not self._tty:
            print(f"[install] {message}", flush=True)

    def __enter__(self):
        if self._tty:
            _print_logo(frame=_SPINNER_FRAMES[0], msg=self._message)
            self._thread.start()
        else:
            print(f"[install] {self._message}", flush=True)
        return self

    def __exit__(self, *args):
        if self._tty:
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
]

# Installed best-effort, after REQUIRED: a failure here must not abort the
# install, since the rest of the app runs fine without it (h5py export is
# used by one route that already degrades gracefully when it's missing).
# On Windows especially, h5py sometimes has no prebuilt wheel for a given
# Python version and falls back to building from source, which needs the
# HDF5 C library and fails outright — that used to take the whole install
# down with it, including flask, pyvisa, etc.
BEST_EFFORT = [
    "h5py",
    "psutil",   # lets pyvisa-py's VXI-11 discovery scan every network interface
]

OPTIONAL = {
    "zeroconf":  "mDNS / LAN instrument discovery",
}

def _venv_pkg():
    return f"python{sys.version_info.major}.{sys.version_info.minor}-venv"


def ensure_venv(spinner: Spinner):
    # Always rebuild from scratch rather than reusing what's there — a venv left
    # over from an interrupted or failed install (e.g. missing flask) looks the
    # same as a good one once VENV_PYTHON exists, so reuse could mask that.
    if VENV.exists():
        spinner.update("Removing existing virtual environment")
        shutil.rmtree(VENV)

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
                f"        Then re-run:  bash install-linux-mac.sh"
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


def install_deps(optional_choices: dict | None = None, usb_driver: str | None = None):
    """optional_choices: {pkg_name: "y"/"n"} to pre-answer OPTIONAL prompts
    (e.g. from a GUI) instead of calling input(). usb_driver: "y"/"n" to
    pre-answer ensure_usb_driver_windows() the same way. Either left as None
    preserves the existing interactive prompt.
    """
    optional_choices = optional_choices or {}
    with Spinner("Preparing virtual environment") as spinner:
        ensure_venv(spinner)
        spinner.update("Installing required packages")
        returncode, output = _pip_install_with_progress(
            spinner, ["--upgrade", *REQUIRED], "Installing required packages"
        )
    if returncode != 0:
        sys.exit("Failed to install required packages:\n" + output)
    print(f"Virtual environment ready → {VENV}")

    spinner_msg = "Installing best-effort packages"
    failures = []
    with Spinner(spinner_msg) as spinner:
        for pkg in BEST_EFFORT:
            spinner.update(f"{spinner_msg}: {pkg}")
            returncode, output = _pip_install_with_progress(
                spinner, ["--upgrade", pkg], spinner_msg
            )
            if returncode != 0:
                failures.append((pkg, output))
    for pkg, output in failures:
        print(
            f"\n[install] Could not install {pkg} — some features that depend on it "
            f"will be unavailable, but the app will still run.\n{output}",
            file=sys.stderr,
        )

    print("\nOptional packages (skip with Ctrl-C to accept defaults):")
    for pkg, desc in OPTIONAL.items():
        if pkg in optional_choices:
            ans = optional_choices[pkg]
        else:
            try:
                ans = input(f"  Install {pkg} ({desc})? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                ans = "n"
        if ans == "y":
            subprocess.check_call([str(VENV_PYTHON), "-m", "pip", "install", pkg])
            if pkg == "zeroconf":
                ensure_zeroconf_importable()

    ensure_libusb_windows()
    ensure_usb_driver_windows(usb_driver)


def ensure_zeroconf_importable():
    """Fall back to zeroconf's pure-Python modules if its compiled ones won't load.

    zeroconf's wheels ship Cython-compiled .pyd files next to the .py sources
    they were built from. Windows 11 Smart App Control blocks unsigned DLLs
    without download reputation ("DLL load failed … An Application Control
    policy has blocked this file"), so `import zeroconf` fails even though pip
    installed it, and pyvisa-py reports it as missing. Removing the compiled
    modules makes Python import the .py sources instead, which zeroconf
    supports as a first-class mode (just slower, irrelevant for discovery).
    """
    def _imports() -> bool:
        return subprocess.run(
            [str(VENV_PYTHON), "-c", "import zeroconf"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0

    if _imports():
        return
    try:
        # find_spec locates the package without executing its __init__.
        pkg_dir = Path(subprocess.check_output(
            [str(VENV_PYTHON), "-c",
             "import importlib.util; print(importlib.util.find_spec('zeroconf').origin)"],
            text=True,
        ).strip()).parent
        for ext in ("*.pyd", "*.so"):
            for f in pkg_dir.rglob(ext):
                f.unlink()
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"[install] Could not repair zeroconf ({exc}).", file=sys.stderr)
        return
    if _imports():
        print("[install] zeroconf's compiled modules were blocked (Windows Smart App "
              "Control?) — using its pure-Python version instead.")
    else:
        print("[install] zeroconf is installed but cannot be imported — HiSLIP "
              "instrument discovery will be unavailable.", file=sys.stderr)


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
# libusb alone is not enough: Windows has no USBTMC driver, so instruments are
# driverless until ensure_usb_driver_windows() (below) binds WinUSB to them.
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


# ── 1c. USB instrument driver (Windows only) ──────────────────────────────────
#
# Binds Windows' inbox WinUSB driver to every USBTMC instrument, present or
# plugged in later (see core/usbtmc_winusb.ps1). Skipped when a vendor VISA is
# installed — it brings its own driver and PyVISA prefers it.
def ensure_usb_driver_windows(choice: str | None = None):
    """choice: pre-answer ("y"/"n") to skip the input() prompt, e.g. from a GUI."""
    if sys.platform != "win32":
        return
    from core import winusb

    if winusb.vendor_visa_installed():
        print("\n[install] A vendor VISA is installed — it provides the USB instrument driver.")
        return

    print(
        "\nWindows has no built-in driver for USB lab instruments. The installer can add\n"
        "one (Windows' own WinUSB driver), which needs a single admin prompt."
    )
    if choice is not None:
        ans = choice
    else:
        try:
            ans = input("Install the USB instrument driver? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "n"
    if ans not in ("", "y"):
        print("[install] Skipped. Run it later with:\n"
              "          python src/core/nachoVisa.py --install-usb-driver")
        return
    winusb.install_driver()


# ── 2. Launcher (open-eew.bat on Windows, open-eew elsewhere) ────────────────

def write_launcher():
    """Write the start file for this platform into the project root.

    The launchers aren't in the repo: before installing, the root holds only
    the install-* files, and afterwards exactly one way to start the app.
    """
    if sys.platform != "win32":
        launcher = ROOT / "open-eew"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            'cd "$(dirname "$0")"\n'
            'if [ -f .venv/bin/python ]; then\n'
            "    exec .venv/bin/python src/app.py \"$@\"\n"
            "else\n"
            '    exec python3 src/app.py "$@"\n'
            "fi\n"
        )
        launcher.chmod(launcher.stat().st_mode | 0o755)
        print(f"\n[install] Start the app with: {launcher}")
        return

    # pythonw.exe via `start`: no console window lingers that a user could
    # close (killing the backend while the app window stayed open). The app
    # logs to results\open-eew.log instead.
    bat = ROOT / "open-eew.bat"
    bat.write_text(
        "@echo off\n"
        'cd /d "%~dp0"\n'
        "rem No console window: the app runs under pythonw.exe and logs to\n"
        "rem results\\open-eew.log. Run .venv\\Scripts\\python.exe src\\app.py\n"
        "rem directly to see its output in this window instead.\n"
        'if exist .venv\\Scripts\\pythonw.exe (\n'
        '    set "PATH=%~dp0.venv\\Scripts;%PATH%"\n'
        '    start "" .venv\\Scripts\\pythonw.exe src\\app.py %*\n'
        ") else (\n"
        "    python src\\app.py %*\n"
        ")\n"
    )
    print(f"\n[install] Start the app by double-clicking {bat.name} in {ROOT}")


# ── 3. Desktop entry (Linux only) ────────────────────────────────────────────

def make_desktop(choice: str | None = None):
    """choice: pre-answer ("y"/"n") to skip the input() prompt, e.g. from a GUI."""
    if sys.platform != "linux":
        print("\n[install] Desktop launcher is Linux-only — skipping.")
        return

    icon_path = APP_DIR / "ui" / "gui_assets" / "icon.svg"
    if not icon_path.exists():
        icon_path = APP_DIR / "ui" / "gui_assets" / "icon.png"

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

    if choice is not None:
        ans = choice
    else:
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


def _yn(s):
    if s not in ("y", "n"):
        raise argparse.ArgumentTypeError("expected 'y' or 'n'")
    return s


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zeroconf", type=_yn, default=None,
                         help="Pre-answer the zeroconf optional-package prompt (y/n) instead of asking")
    parser.add_argument("--usb-driver", type=_yn, default=None,
                         help="Pre-answer the Windows USB driver prompt (y/n) instead of asking")
    parser.add_argument("--desktop-entry", type=_yn, default=None,
                         help="Pre-answer the Linux app-menu entry prompt (y/n) instead of asking")
    args = parser.parse_args()

    print(f"open-EE-workbench installer\nProject root: {ROOT}\n")
    install_deps(optional_choices={"zeroconf": args.zeroconf} if args.zeroconf else None,
                 usb_driver=args.usb_driver)
    write_launcher()
    print_pywebview_apt_note()
    print_chrome_note()
    make_desktop(choice=args.desktop_entry)
    print("\nDone.")
