#!/usr/bin/env python3
"""
setup_launcher.py — generate the .desktop launcher for open-EE-workbench.

Run this once after cloning (or after moving the project folder):
    python setup_launcher.py

It creates:
  • gui_assets/icon.png          — app icon (PNG so all file managers can read it)
  • open-EE-workbench.desktop    — double-clickable launcher in this folder

On GNOME / Nautilus you may need to right-click the .desktop file once and
choose "Allow Launching" before double-clicking works.
On KDE / Dolphin it works immediately.
"""

import os
import sys
from pathlib import Path

ROOT    = Path(__file__).parent.resolve()
ASSETS  = ROOT / "gui_assets"
ICON    = ASSETS / "icon.png"
DESKTOP = ROOT / "open-EE-workbench.desktop"
PYTHON  = sys.executable   # exact interpreter that ran this script


# ── 1. Generate the icon ──────────────────────────────────────────────────────
def make_icon(size: int = 256) -> bool:
    """Render the nacho chip onto a dark rounded background → gui_assets/icon.png."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("[setup] PIL not found — skipping icon generation")
        return False

    # Background: rounded dark panel
    bg_color = (37, 38, 43, 255)   # matches PANEL colour in gui.py
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded rectangle background
    r = size // 8
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=bg_color)

    # Nacho chip (white) centred with a little padding
    pad = size * 0.08
    S = size - int(2 * pad)
    chip = Image.new("RGBA", (S * 3, S * 3), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(chip)

    def sc(pts):
        return [(int(x * S * 3), int(y * S * 3)) for x, y in pts]

    white = (255, 255, 255, 245)
    erase = (0, 0, 0, 0)

    outer = sc([
        (0.01, 0.87), (0.62, 0.01), (0.83, 0.49),
        (0.76, 0.53), (0.69, 0.53), (0.74, 0.67),
        (0.88, 0.70), (0.99, 0.99), (0.91, 1.00),
    ])
    cdraw.polygon(outer, fill=white)

    inner1 = sc([(0.74, 0.73), (0.84, 0.82), (0.74, 0.91), (0.64, 0.82)])
    cdraw.polygon(inner1, fill=erase)

    inner2 = sc([(0.69, 0.76), (0.79, 0.86), (0.59, 0.86)])
    cdraw.polygon(inner2, fill=erase)

    chip = chip.resize((S, S), Image.LANCZOS)

    # Paste chip onto background
    img.paste(chip, (int(pad), int(pad)), chip)

    # Flatten to RGB PNG (file managers don't always handle RGBA well)
    final = Image.new("RGB", (size, size), (37, 38, 43))
    final.paste(img, mask=img.split()[3])

    ASSETS.mkdir(exist_ok=True)
    final.save(str(ICON), "PNG")
    print(f"[setup] Icon written → {ICON}")
    return True


# ── 2. Write the .desktop file ────────────────────────────────────────────────
def make_desktop():
    # Prefer the composed icon SVG (logo on dark-purple tile);
    # fall back to plain logo SVG, then the generated PNG.
    for candidate in [ASSETS / "icon.svg", ASSETS / "nacho_white.svg", ICON]:
        if candidate.exists():
            icon_path = str(candidate)
            break
    else:
        icon_path = ""

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
Categories=Science;Engineering;Electronics;Education;
Keywords=VISA;SCPI;oscilloscope;power supply;DMM;AWG;lab;instrument;
"""

    DESKTOP.write_text(content, encoding="utf-8")
    DESKTOP.chmod(DESKTOP.stat().st_mode | 0o755)   # make executable
    print(f"[setup] Launcher written → {DESKTOP}")


# ── 3. Optionally install into ~/.local/share/applications ───────────────────
def maybe_install_system():
    apps_dir = Path.home() / ".local" / "share" / "applications"
    answer = input(
        "\nAlso install into ~/.local/share/applications so it appears\n"
        "in your application menu / search? [y/N] "
    ).strip().lower()
    if answer == "y":
        apps_dir.mkdir(parents=True, exist_ok=True)
        dest = apps_dir / DESKTOP.name
        dest.write_text(DESKTOP.read_text(), encoding="utf-8")
        dest.chmod(dest.stat().st_mode | 0o755)
        # Refresh the desktop database
        os.system("update-desktop-database ~/.local/share/applications 2>/dev/null")
        print(f"[setup] Installed → {dest}")
        print("        You can now search for 'open-EE-workbench' in your app launcher.")
    else:
        print("[setup] Skipped system install.")


if __name__ == "__main__":
    print(f"Setting up launcher for: {ROOT}\n")
    made_icon = make_icon()
    if not made_icon:
        print("[setup] Proceeding without icon.")
    make_desktop()
    print()
    maybe_install_system()
    print("\nDone.  Double-click  open-EE-workbench.desktop  to launch the GUI.")
