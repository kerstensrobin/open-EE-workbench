import pyvisa

RESOURCE = "USB0::0x2A8D::0x8E01::CN62270106::0::INSTR"  # <-- your VISA address

rm = pyvisa.ResourceManager()
dmm = rm.open_resource(RESOURCE)
dmm.timeout = 8000  # ms

try:
    dmm.write("*RST; *CLS")
    dmm.write("FORM:OUTP 1")      # numeric results only
    dmm.write("UNIT:TEMP C")      # temperature in °C

    print("=== Basic single-shot measurements ===")

    # Voltage
    print("DC Voltage:", dmm.query("MEAS:VOLT:DC?").strip())
    print("AC Voltage:", dmm.query("MEAS:VOLT:AC?").strip())

    # Current (check leads/fuse)
    print("DC Current:", dmm.query("MEAS:CURR:DC?").strip())
    print("AC Current:", dmm.query("MEAS:CURR:AC?").strip())

    # Resistance
    print("2-Wire Ohms:", dmm.query("MEAS:RES?").strip())
    print("4-Wire Ohms:", dmm.query("MEAS:FRES?").strip())

    # Capacitance / Frequency / Temperature
    print("Capacitance:", dmm.query("MEAS:CAP?").strip())
    print("Frequency:",  dmm.query("MEAS:FREQ?").strip())
    print("Temperature (°C):", dmm.query("MEAS:TEMP?").strip())

    # Diode / Continuity
    print("Diode Test (Vf):", dmm.query("MEAS:DIOD?").strip())
    print("Continuity (Ω):",  dmm.query("MEAS:CONT?").strip())

finally:
    dmm.close()
    rm.close()
