#!/usr/bin/env python3
"""Cycle a Korad KA-series power supply through a few voltages over USB/serial."""

import time
import pyvisa

RESOURCE = "ASRL/dev/ttyACM0::INSTR"
CURRENT_LIMIT = 0.2   # amps
VOLTAGES = [1.0, 3.3, 5.0, 12.0]  # volts to step through
DWELL = 2.0           # seconds to hold each voltage


def open_supply(resource):
    rm = pyvisa.ResourceManager("@py")
    psu = rm.open_resource(resource)
    psu.timeout = 1000
    psu.baud_rate = 9600
    psu.data_bits = 8
    psu.parity = pyvisa.constants.Parity.none
    psu.stop_bits = pyvisa.constants.StopBits.one
    psu.read_termination = None
    psu.write_termination = None
    return rm, psu


def send(psu, command):
    """Send a command string; Korad needs no terminator and a short gap."""
    psu.flush(pyvisa.constants.BufferOperation.discard_read_buffer)
    psu.write_raw(command.encode("ascii"))
    time.sleep(0.1)


def query(psu, command, num_bytes):
    """Send a query and read back a fixed number of bytes."""
    send(psu, command)
    data = psu.read_bytes(num_bytes, break_on_termchar=False)
    return data.replace(b"\x00", b"").decode("ascii").strip()


def main():
    print(f"Connecting to {RESOURCE} ...")
    rm, psu = open_supply(RESOURCE)

    try:
        # Set current limit once, leave it for the whole run
        send(psu, f"ISET1:{CURRENT_LIMIT:05.3f}")
        send(psu, "OUT1")  # turn output on

        for volts in VOLTAGES:
            send(psu, f"VSET1:{volts:05.2f}")
            time.sleep(DWELL)

            vout = query(psu, "VOUT1?", 4)
            iout = query(psu, "IOUT1?", 5)
            print(f"Set {volts:5.1f} V  →  measured {vout} V  {iout} A")

        # Done — turn output off and zero the voltage
        send(psu, "OUT0")
        send(psu, "VSET1:00.00")
        print("Output off.")

    finally:
        psu.close()
        rm.close()


if __name__ == "__main__":
    main()
