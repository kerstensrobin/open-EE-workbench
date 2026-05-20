import time
import pyvisa

RESOURCE = "USB0::0x2A8D::0x8F01::CN62420033::0::INSTR"  # <-- your VISA address

rm = pyvisa.ResourceManager()
psu = rm.open_resource(RESOURCE)
psu.timeout = 5000  # ms

try:
    psu.write("*RST; *CLS")                           # clean start
    psu.write("OUTP:TRAC OFF")                        # ensure tracking disabled :contentReference[oaicite:0]{index=0}

    # Set initial ±10 V with 0.5 A limits on CH2 (P30V, +) and CH3 (N30V, -)
    psu.write('APPL P30V,10,0.5')                     # +10 V, 0.5 A on CH2 :contentReference[oaicite:1]{index=1}
    psu.write('APPL N30V,10,0.5')                     # -10 V (N30V uses positive magnitude), 0.5 A on CH3 :contentReference[oaicite:2]{index=2}
    psu.write('OUTP ON,(@2,3)')                       # turn on channels 2 & 3 :contentReference[oaicite:3]{index=3}

    # Step both channels up to ±15 V (1 V steps)
    for v in range(10, 16):                           # 10,11,12,13,14,15
        psu.write(f'VOLT {v}, (@2)')                  # set +v on CH2 :contentReference[oaicite:4]{index=4}
        psu.write(f'VOLT {v}, (@3)')                  # set -v on CH3 via N30V channel (magnitude) :contentReference[oaicite:5]{index=5}
        time.sleep(1)                               # small dwell between steps

    # Optional: leave outputs on, or turn off at the end:
    psu.write('OUTP OFF,(@2,3)')                    # disable outputs if desired :contentReference[oaicite:6]{index=6}

finally:
    psu.close()
    rm.close()
