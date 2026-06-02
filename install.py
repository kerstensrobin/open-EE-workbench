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
    "cairosvg":  "SVG logo rendering in Tkinter GUI",
    "pillow":    "image support in Tkinter GUI",
}

def install_deps():
    print("Installing required packages…")
    subprocess.check_call([PYTHON, "-m", "pip", "install", "--upgrade", *REQUIRED])

    print("\nOptional packages (skip with Ctrl-C to accept defaults):")
    for pkg, desc in OPTIONAL.items():
        ans = input(f"  Install {pkg} ({desc})? [y/N] ").strip().lower()
        if ans == "y":
            subprocess.check_call([PYTHON, "-m", "pip", "install", pkg])


# ── 2. Desktop launcher (Linux only) ─────────────────────────────────────────

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
Exec={PYTHON} {ROOT / "app.py"} %F
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

if __name__ == "__main__":
    print(f"open-EE-workbench installer\nProject root: {ROOT}\n")
    install_deps()
    make_desktop()
    print("\nDone.")
