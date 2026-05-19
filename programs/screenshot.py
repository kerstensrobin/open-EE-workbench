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

SCREENSHOT_TIMEOUT_MS = 10_000
# The Rigol DS1000Z (and likely other USBTMC scopes) only respond to
# REQUEST_DEV_DEP_MSG_IN in small increments via pyvisa-py. Read in 4 KB
# chunks until we receive a short packet (end of data).
USBTMC_CHUNK_SIZE = 4096
RENDER_SLEEP = 1.0  # seconds for scope to finish rendering before first read


def _detect_format(data: bytes) -> tuple[int, str]:
    """Return (offset, extension) for the first recognised image magic in data."""
    for magic, ext in _IMAGE_FORMATS:
        idx = data.find(magic)
        if idx != -1:
            return idx, ext
    return 0, ''


def _screenshot_steps(idn: str) -> list[tuple[str, str]] | None:
    """Return the screenshot step list for this IDN, or None if unsupported.

    Falls back to a single :DISPlay:DATA? raw_query for unrecognised instruments.
    Returns None only when the instrument is known and screenshot is explicitly null.
    """
    if classify and get_command:
        family = classify(idn)
        if family:
            try:
                return get_command(family, 'screenshot')
            except KeyError:
                return None  # known scope, screenshot explicitly unsupported
    return [('raw_query', ':DISPlay:DATA?')]


def get_screenshot(scope, idn: str, filename: str):
    steps = _screenshot_steps(idn)
    if steps is None:
        raise RuntimeError(
            f"Screenshot not supported over VISA for this scope ({idn}).\n"
            f"  Use the front-panel Save button or :SAVe:IMAGe to save to USB."
        )

    for action, text in steps:
        if action == 'note':
            print(f"Note: {text}")

    # Split into: writes before the binary read, the raw_query read, writes after.
    raw_idx = next((i for i, (a, _) in enumerate(steps) if a == 'raw_query'), None)
    if raw_idx is None:
        raise RuntimeError(f"Screenshot command for {idn} has no data-read step.")

    pre_steps  = [(a, s) for a, s in steps[:raw_idx]      if a == 'write']
    read_cmd   = steps[raw_idx][1]
    post_steps = [(a, s) for a, s in steps[raw_idx + 1:]  if a == 'write']

    scope.timeout = SCREENSHOT_TIMEOUT_MS

    # USBTMC delivers data in small increments; loop until a short packet signals EOF.
    # VXI-11 (TCPIP inst0) and raw SOCKET connections return the full payload in one read.
    is_usbtmc = scope.resource_name.upper().startswith('USB')
    if is_usbtmc:
        scope.chunk_size = USBTMC_CHUNK_SIZE

    for _action, scpi in pre_steps:
        scope.write(scpi)

    time.sleep(RENDER_SLEEP)
    scope.write(read_cmd)

    if is_usbtmc:
        chunks = []
        while True:
            chunk = scope.read_raw()
            chunks.append(chunk)
            if len(chunk) < USBTMC_CHUNK_SIZE:
                break  # short packet signals end of transfer
        data = b''.join(chunks)
    else:
        data = scope.read_raw()

    for _action, scpi in post_steps:
        scope.write(scpi)

    offset, detected_ext = _detect_format(data)
    data = data[offset:]

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

    try:
        get_screenshot(scope, idn, args.filename)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        scope.close()
        rm.close()
        sys.exit(1)
    except pyvisa.errors.VisaIOError as exc:
        if 'timeout' in str(exc).lower() or 'VI_ERROR_TMO' in str(exc):
            print("Error: Scope did not respond in time. "
                  "Check that the scope is ready and (if applicable) a USB stick is inserted.")
        else:
            print(f"Error: VISA communication failed — {exc}")
        scope.close()
        rm.close()
        sys.exit(1)

    scope.close()
    rm.close()


if __name__ == "__main__":
    main()
