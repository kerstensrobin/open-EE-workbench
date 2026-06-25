#!/usr/bin/env python3
"""
install.py — set up open-EE-workbench on a new machine.

    python install.py

What it does:
  1. Installs required Python packages via pip
  2. Creates open-EE-workbench.desktop (Linux) so the app appears in
     your application menu and can be launched from the desktop
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT   = Path(__file__).parent.resolve()
PYTHON = sys.executable
VENV   = ROOT / ".venv"
VENV_PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


# ── 1. Dependencies ───────────────────────────────────────────────────────────

REQUIRED = [
    "pyvisa",
    "pyvisa-py",
    "pyusb",
    "pyserial",
    "flask",
    "flask-socketio",
    "pywebview",
]

OPTIONAL = {
    "zeroconf":  "mDNS / LAN instrument discovery",
}

def _venv_pkg():
    return f"python{sys.version_info.major}.{sys.version_info.minor}-venv"


def ensure_venv():
    if VENV_PYTHON.exists():
        print(f"Virtual environment already exists at {VENV}")
        return

    print("Creating virtual environment…")
    try:
        subprocess.check_call([PYTHON, "-m", "venv", str(VENV)])
    except subprocess.CalledProcessError:
        if sys.platform == "linux" and Path("/etc/debian_version").exists():
            print(
                f"\n[error] Could not create virtual environment.\n"
                f"        On Ubuntu/Debian, install the venv package first:\n\n"
                f"            sudo apt install {_venv_pkg()}\n\n"
                f"        Then re-run:  python3 install.py",
                file=sys.stderr,
            )
            sys.exit(1)
        raise


def install_deps():
    ensure_venv()

    print("Installing required packages…")
    subprocess.check_call([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", *REQUIRED])

    print("\nOptional packages (skip with Ctrl-C to accept defaults):")
    for pkg, desc in OPTIONAL.items():
        ans = input(f"  Install {pkg} ({desc})? [y/N] ").strip().lower()
        if ans == "y":
            subprocess.check_call([str(VENV_PYTHON), "-m", "pip", "install", pkg])


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


if __name__ == "__main__":
    print(f"open-EE-workbench installer\nProject root: {ROOT}\n")
    install_deps()
    patch_launchers()
    print_pywebview_apt_note()
    make_desktop()
    print("\nDone.")
