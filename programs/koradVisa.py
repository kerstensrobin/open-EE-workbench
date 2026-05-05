#!/usr/bin/env python3

import argparse
import time

import pyvisa


DEFAULT_RESOURCE = "ASRL/dev/ttyACM0::INSTR"


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Example control script for a Korad KA-series supply over VISA serial."
    )
    parser.add_argument(
        "resource",
        nargs="?",
        default=DEFAULT_RESOURCE,
        help=f"VISA serial resource. Defaults to {DEFAULT_RESOURCE!r}.",
    )
    parser.add_argument(
        "--voltage",
        type=float,
        default=5.0,
        help="Voltage setpoint in volts. Defaults to 5.0.",
    )
    parser.add_argument(
        "--current",
        type=float,
        default=0.2,
        help="Current limit in amps. Defaults to 0.2.",
    )
    parser.add_argument(
        "--no-output",
        action="store_true",
        help="Set voltage/current but leave the supply output off.",
    )
    parser.add_argument(
        "--idn",
        action="store_true",
        help="Query *IDN? using Korad serial framing before other commands.",
    )
    parser.add_argument(
        "--idn-diagnostics",
        action="store_true",
        help="Try *IDN? with no terminator, CR, LF, and CRLF, then exit.",
    )
    return parser


def configure_serial(inst):
    inst.timeout = 1000
    inst.baud_rate = 9600
    inst.data_bits = 8
    inst.parity = pyvisa.constants.Parity.none
    inst.stop_bits = pyvisa.constants.StopBits.one
    inst.read_termination = None
    inst.write_termination = None


def discard_read_buffer(inst):
    inst.flush(pyvisa.constants.BufferOperation.discard_read_buffer)


def write_command(inst, command: str):
    discard_read_buffer(inst)
    inst.write_raw(command.encode("ascii"))
    time.sleep(0.2)


def query_bytes_until_quiet(inst, command: str, suffix: bytes = b"", quiet_timeout_ms: int = 200) -> bytes:
    discard_read_buffer(inst)
    original_timeout = inst.timeout
    try:
        inst.write_raw(command.encode("ascii") + suffix)
        time.sleep(0.05)
        inst.timeout = quiet_timeout_ms

        data = bytearray()
        while True:
            try:
                chunk = inst.read_bytes(1, break_on_termchar=False)
                if not chunk:
                    break
                data.extend(chunk)
            except pyvisa.errors.VisaIOError:
                break
        return bytes(data)
    finally:
        inst.timeout = original_timeout


def query_text_until_quiet(inst, command: str, suffix: bytes = b"") -> str:
    data = query_bytes_until_quiet(inst, command, suffix=suffix)
    return data.replace(b"\x00", b"").decode("ascii", errors="replace").strip()


def query_fixed(inst, command: str, byte_count: int) -> str:
    discard_read_buffer(inst)
    write_command(inst, command)
    data = inst.read_bytes(byte_count, break_on_termchar=False)
    return data.replace(b"\x00", b"").decode("ascii", errors="replace").strip()


def diagnose_idn(inst):
    variants = (
        ("no terminator", b""),
        ("CR", b"\r"),
        ("LF", b"\n"),
        ("CRLF", b"\r\n"),
    )

    print("Korad *IDN? diagnostics")
    for label, suffix in variants:
        data = query_bytes_until_quiet(inst, "*IDN?", suffix=suffix)
        if data:
            text = data.replace(b"\x00", b"").decode("ascii", errors="replace").strip()
            print(f"{label:13}: {data!r}  {text}")
        else:
            print(f"{label:13}: no response")


def main():
    args = build_arg_parser().parse_args()

    rm = pyvisa.ResourceManager("@py")
    psu = rm.open_resource(args.resource)

    try:
        configure_serial(psu)

        if args.idn_diagnostics:
            diagnose_idn(psu)
            return

        if args.idn:
            identity = query_text_until_quiet(psu, "*IDN?")
            print(f"Identity        : {identity or 'no response'}")

        write_command(psu, f"VSET1:{args.voltage:05.2f}")
        write_command(psu, f"ISET1:{args.current:05.3f}")

        if not args.no_output:
            write_command(psu, "OUT1")
        else:
            write_command(psu, "OUT0")

        time.sleep(0.25)

        measured_voltage = query_fixed(psu, "VOUT1?", 4)
        measured_current = query_fixed(psu, "IOUT1?", 5)
        voltage_setpoint = query_fixed(psu, "VSET1?", 4)
        current_limit = query_fixed(psu, "ISET1?", 6)

        print(f"Resource        : {args.resource}")
        print(f"Voltage setting : {voltage_setpoint} V")
        print(f"Current limit   : {current_limit} A")
        print(f"Output voltage  : {measured_voltage} V")
        print(f"Output current  : {measured_current} A")

    finally:
        psu.close()
        rm.close()


if __name__ == "__main__":
    main()
