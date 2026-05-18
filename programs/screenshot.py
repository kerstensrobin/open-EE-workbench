#!/usr/bin/env python3
# nacho.works — capture a screenshot from the active workbench scope

import argparse
import os
import sys
import time

import pyvisa
from workbench import load_workbench, open_by_role


def get_screenshot(scope, filename: str):
    time.sleep(0.1)
    scope.write(":DISP:DATA? PNG")
    scope.chunk_size = 1024 * 1024
    data = scope.read_raw()
    start = data.find(b'\x89PNG')
    if start != -1:
        data = data[start:]
    with open(filename, "wb") as f:
        f.write(data)
    time.sleep(0.1)
    print(f"Screenshot saved: {os.path.abspath(filename)}")


def main():
    parser = argparse.ArgumentParser(description="Capture a screenshot from the workbench scope.")
    parser.add_argument("filename", nargs="?", default="screenshot.png",
                        help="Output filename (default: screenshot.png)")
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

    print(f"Scope     : {scope.query('*IDN?').strip()}")

    if os.path.exists(args.filename):
        try:
            answer = input(f"'{args.filename}' already exists. Overwrite? [y/N] ").strip().lower()
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

    get_screenshot(scope, args.filename)

    scope.close()
    rm.close()


if __name__ == "__main__":
    main()
