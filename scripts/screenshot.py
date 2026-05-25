#!/usr/bin/env python3
# nacho.works — capture a screenshot from the active workbench scope

import argparse
import math
import os
import sys
import time

import pyvisa

# Allow running from any directory: add setup/ (where workbench.py / instruments.py live)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'setup'))

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

SCREENSHOT_TIMEOUT_MS = 30_000   # 30 s — BMP over USBTMC takes ~16 s

# BMP24 screenshot is ~1.15 MB; set chunk_size larger than the image so
# USBTMC.read() accumulates all chunks internally and returns the full image
# from a single read_raw() call.
SCREENSHOT_CHUNK_SIZE = 2 * 1024 * 1024   # 2 MB


# ─── PyVISA-Py USBTMC bug fix ─────────────────────────────────────────────────
#
# pyvisa-py ≤ 0.8.1 has a bug in USBTMC.read(): the inner while loop uses
#
#   (len(resp) % wMaxPacketSize == 0) OR (received < transfer_size)
#
# The OR causes it to call raw_read() again without sending a new
# REQUEST_DEV_DEP_MSG_IN when the scope's USBTMC chunk is exactly
# wMaxPacketSize bytes.  Rigol DS1000Z sends each :DISPlay:DATA? chunk as
# a 64-byte USB packet (12 header + 52 data = wMaxPacketSize), which
# triggers the bug on every chunk → the read hangs waiting for data that
# never arrives → VI_ERROR_TMO.
#
# Fix: change OR → AND so the inner loop only continues when BOTH:
#   • the last USB packet was full-sized (possible more data in this transfer)
#   • we have not yet received all transfer_size bytes for this chunk
#
# ─────────────────────────────────────────────────────────────────────────────

def _apply_usbtmc_patch():
    """Monkey-patch PyVISA-Py USBTMC.read() to fix the 'or → and' inner loop."""
    try:
        from pyvisa_py.protocols import usbtmc as _usbtmc_mod
        from pyvisa_py.protocols.usbtmc import BulkInMessage, USBRaw, USBTMC
    except ImportError:
        return   # not using pyvisa-py — nothing to patch

    try:
        import usb.core as _usb_core
    except ImportError:
        return

    def _patched_read(self, size):
        usbtmc_header_size = 12
        eom = False
        raw_read  = USBRaw.read.__get__(self, USBTMC)
        raw_write = USBRaw.write.__get__(self, USBTMC)
        received_message = bytearray()

        while not eom:
            received_transfer = bytearray()
            self._btag = (self._btag % 255) + 1
            req = BulkInMessage.build_array(self._btag, size, None)
            raw_write(req)
            try:
                chunk_size = (
                    math.floor(
                        (size + usbtmc_header_size) / self.usb_recv_ep.wMaxPacketSize
                    ) + 1
                ) * self.usb_recv_ep.wMaxPacketSize
                resp = raw_read(chunk_size)
                if len(resp) < 12:
                    # Zero-length packet / short response — scope not ready yet.
                    # Outer loop will increment btag and issue a fresh REQUEST.
                    continue

                response = BulkInMessage.from_bytes(resp)
                received_transfer.extend(response.data)

                if len(received_transfer) >= response.transfer_size:
                    eom = response.transfer_attributes & 1
                if not eom and len(received_transfer) >= size:
                    eom = True
                else:
                    # FIX: AND instead of OR — stop when all transfer_size bytes
                    # received, even if last USB packet was exactly wMaxPacketSize.
                    while (
                        (len(resp) % self.usb_recv_ep.wMaxPacketSize) == 0
                        and len(received_transfer) < response.transfer_size
                    ) and not eom:
                        chunk_size = (
                            math.floor(
                                (size - len(received_transfer))
                                / self.usb_recv_ep.wMaxPacketSize
                            ) + 1
                        ) * self.usb_recv_ep.wMaxPacketSize
                        resp = raw_read(chunk_size)
                        received_transfer.extend(resp)
                    if len(received_transfer) >= response.transfer_size:
                        eom = response.transfer_attributes & 1
                    if not eom and len(received_transfer) >= size:
                        eom = True

                received_message.extend(received_transfer[: response.transfer_size])
            except (_usb_core.USBError, ValueError):
                self._abort_bulk_in(self._btag)
                raise

        return bytes(received_message)

    _usbtmc_mod.USBTMC.read = _patched_read


# Apply the patch once at import time.
_apply_usbtmc_patch()


# ─────────────────────────────────────────────────────────────────────────────

def _detect_format(data: bytes) -> tuple[int, str]:
    """Return (offset, extension) for the first recognised image magic in data."""
    for magic, ext in _IMAGE_FORMATS:
        idx = data.find(magic)
        if idx != -1:
            return idx, ext
    return 0, ''


def _run_scpi_writes(scope, family, op: str) -> bool:
    """Send all write-type steps for an operation.  Returns True on success."""
    if not (classify and get_command and family):
        return False
    try:
        for action, scpi in get_command(family, op):
            if action == 'write':
                scope.write(scpi)
        return True
    except (KeyError, Exception):
        return False


def _screenshot_steps(family) -> list[tuple[str, str]] | None:
    """Return the screenshot step list for this family, or None if unsupported.

    Falls back to a single :DISPlay:DATA? raw_query for unrecognised instruments.
    Returns None only when the instrument is known and screenshot is explicitly null.
    """
    if get_command and family:
        try:
            return get_command(family, 'screenshot')
        except KeyError:
            return None   # known scope, screenshot explicitly unsupported
    return [('raw_query', ':DISPlay:DATA?')]


def get_screenshot(scope, idn: str, filename: str):
    family = classify(idn) if classify else None

    steps = _screenshot_steps(family)
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

    pre_steps  = [(a, s) for a, s in steps[:raw_idx]     if a == 'write']
    read_cmd   = steps[raw_idx][1]
    post_steps = [(a, s) for a, s in steps[raw_idx + 1:] if a == 'write']

    # Stop the scope before capturing.
    # In run/waiting-for-trigger state the scope holds :DISPlay:DATA? until the
    # next complete acquisition — which may never arrive (e.g. DUT is off at 0 V).
    # :STOP freezes the display and guarantees an immediate response.
    scope_stopped = _run_scpi_writes(scope, family, 'stop')
    if scope_stopped:
        time.sleep(0.2)    # let display latch the frozen frame

    orig_timeout    = scope.timeout
    orig_chunk_size = scope.chunk_size
    try:
        scope.timeout    = SCREENSHOT_TIMEOUT_MS
        # Must be > image size so USBTMC.read() accumulates until EOM instead
        # of returning on the first chunk_size boundary.
        scope.chunk_size = SCREENSHOT_CHUNK_SIZE

        for _action, scpi in pre_steps:
            scope.write(scpi)

        scope.write(read_cmd)
        time.sleep(0.5)    # let scope compose the image before we REQUEST data
        data = scope.read_raw()

        for _action, scpi in post_steps:
            scope.write(scpi)

    finally:
        try:
            scope.timeout    = orig_timeout
            scope.chunk_size = orig_chunk_size
        except Exception:
            pass
        # Always resume acquisition after the capture attempt
        if scope_stopped:
            _run_scpi_writes(scope, family, 'run')

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
    check_path = args.filename if ext else args.filename

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
                  "Check that the scope is ready and try again.")
        else:
            print(f"Error: VISA communication failed — {exc}")
        scope.close()
        rm.close()
        sys.exit(1)

    scope.close()
    rm.close()


if __name__ == "__main__":
    main()
