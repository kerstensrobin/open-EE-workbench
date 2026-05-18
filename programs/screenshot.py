#!/usr/bin/env python3
# nacho.works — capture a screenshot from the active workbench scope

import argparse
import os
import sys
import time

import pyvisa
from workbench import load_workbench, open_by_role

try:
    from instruments import classify, get_command
except ImportError:
    classify = None
    get_command = None

# (magic_bytes, extension)
_IMAGE_FORMATS = [
    (b'\x89PNG', '.png'),
    (b'BM',      '.bmp'),
]

SCREENSHOT_TIMEOUT_MS = 30_000


def _detect_format(data: bytes) -> tuple[int, str]:
    """Return (offset, extension) of the first recognised image magic in data."""
    for magic, ext in _IMAGE_FORMATS:
        idx = data.find(magic)
        if idx != -1:
            return idx, ext
    return 0, ''


def _screenshot_command(idn: str) -> str:
    """Return the SCPI write string to request a screenshot for this IDN."""
    if classify and get_command:
        family = classify(idn)
        if family:
            try:
                steps = get_command(family, 'screenshot')
                # raw_query step: the string is the command to write
                for _action, scpi in steps:
                    return scpi
            except KeyError:
                pass
    return ':DISPlay:DATA?'


def get_screenshot(scope, idn: str, filename: str):
    cmd = _screenshot_command(idn)
    time.sleep(0.1)

    scope.timeout = SCREENSHOT_TIMEOUT_MS
    scope.chunk_size = 1024 * 1024
    scope.write(cmd)
    data = scope.read_raw()

    offset, detected_ext = _detect_format(data)
    data = data[offset:]

    # If the caller gave a bare name with no extension, add the detected one
    base, ext = os.path.splitext(filename)
    if not ext and detected_ext:
        filename = base + detected_ext

    with open(filename, 'wb') as f:
        f.write(data)
    print(f"Screenshot saved: {os.path.abspath(filename)}")


def main():
    parser = argparse.ArgumentParser(description="Capture a screenshot from the workbench scope.")
    parser.add_argument("filename", nargs="?", default="screenshot",
                        help="Output filename (default: screenshot, extension auto-detected)")
    parser.add_argument("--workbench", metavar="NAME",
                        help="Workbench to use (default: active workbench)")
    parser.add_argument("--backend", default="@py", metavar="BACKEND",
                        help="PyVISA backend (default: @py)")
    args = parser.parse_args()

    try:
        wb = load_workbench(args.workbench)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    print(f"Workbench : {wb['name']}")

    rm = pyvisa.ResourceManager(args.backend)
    try:
        scope = open_by_role(rm, wb, "scope")
    except RuntimeError as exc:
        print(f"Error: {exc}")
        rm.close()
        sys.exit(1)

    idn = scope.query('*IDN?').strip()
    print(f"Scope     : {idn}")

    # Resolve the final filename (extension may be added after format detection)
    base, ext = os.path.splitext(args.filename)
    check_path = args.filename if ext else args.filename  # check before we know extension

    if os.path.exists(check_path):
        try:
            answer = input(f"'{check_path}' already exists. Overwrite? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        if answer != "y":
            print("Aborted.")
            scope.close()
            rm.close()
            sys.exit(0)

    # Optional: annotation and channel labels
    # scope.write(':DISPlay:ANNotation:TEXT "my note"')
    # scope.write(':DISPlay:ANNotation ON')
    # scope.write(':CHANnel1:LABel "CH1"')
    # scope.write(':DISPlay:LABel ON')

    get_screenshot(scope, idn, args.filename)

    scope.close()
    rm.close()


if __name__ == "__main__":
    main()
