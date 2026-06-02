import time
import pyvisa

RESOURCE = "USB0::0x2A8D::0x8D01::CN63430108::0::INSTR"  # <-- put your VISA address

rm = pyvisa.ResourceManager()
awg = rm.open_resource(RESOURCE)
awg.timeout = 5000  # ms

try:
    awg.write("*RST;*CLS")

    # --- Channel 1: 10 kHz sine, 2 Vpp, 0 V offset ---
    awg.write("SOUR1:VOLT:UNIT VPP")          # make amplitude units Vpp  (applies to VOLT/APPL amplitude)
    awg.write("SOUR1:APPL:SIN 10E3,2,0")      # frequency, amplitude (Vpp), offset
    awg.write("OUTP1 ON")                     # explicitly enable output

    time.sleep(2.0)

    # --- Channel 2: 10 kHz square, 2 Vpp, 75% duty ---
    awg.write("SOUR2:VOLT:UNIT VPP")
    awg.write("SOUR2:FUNC SQU")               # select square without resetting duty via APPL
    awg.write("SOUR2:FREQ 10E3")
    awg.write("SOUR2:VOLT 2")
    awg.write("SOUR2:VOLT:OFFS 0")
    awg.write("SOUR2:FUNC:SQU:DCYC 75")       # 75% duty cycle
    awg.write("OUTP2 ON")

    # ... do whatever you need while outputs are on ...
    time.sleep(1.0)

finally:
    # --- Finish: turn both channels off ---
    awg.write("OUTP1 OFF; OUTP2 OFF")
    awg.close()
    rm.close()
