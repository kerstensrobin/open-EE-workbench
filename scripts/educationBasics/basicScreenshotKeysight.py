# This is an example script, setting up basic communication with an oscilloscope and taking a screenshot.
# robin.kerstens@uantwerpen.be
##

import pyvisa as visa
import time

rm = visa.ResourceManager()
# the VISA adress for your scope can be found in using Keysight Connection Expert or nachoVisa.py
scope = rm.open_resource('USB0::0x2A8D::0x038B::CN63370620::0::INSTR')
scope.timeout = 10000 #Always good to involve a time-out to avoid putting the scope into an endless waiting state.

def get_screenshot(filename):
    time.sleep(0.1) # Interacting with real equipment takes time. If some commands are not going through, consider adding a small pause to make sure your equipment has finished the previous task.
    # Send command to take a printscreen
    scope.write(":DISP:DATA? PNG")
    # Read the binary data
    data = scope.read_raw()
    # Find the start of the PNG file
    start = data.find(b'\x89PNG')
    if start != -1:
        data = data[start:]
    # Save the binary data to a file with the specified filename
    with open(filename, "wb") as f:
        f.write(data)
    time.sleep(0.1)
    print(f"Screenshot saved as {filename}")

print('Taking Screenshot')
print('---')
scope_idn = scope.query('*IDN?')
print('[info] scope found: ' + scope_idn)

scope.write(':DISPlay:ANNotation:TEXT "SCREEN TITLE"')
scope.write(':CHANnel1:LABel "CHANNEL 1 SIGNAL";:Display:LABel ON' )
scope.write(':CHANnel2:LABel "CHANNEL 2 SIGNAL";:Display:LABel ON' )

get_screenshot('CircuitNameThatMakesSense.png')

# Always clean up your mess when you're done.
print("[info] Took screenshot")
scope.close()
rm.close()

print("[info] Done.")



