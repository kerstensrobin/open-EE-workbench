# This is an example script, setting up basic communication with an oscilloscope and taking a screenshot.
# robin.kerstens@uantwerpen.be
##

import os
import pyvisa as visa
import sys
import time

rm = visa.ResourceManager()
# the VISA adress for your scope can be found in using Keysight Connection Expert
scope = rm.open_resource('USB0::10893::907::CN63430291::0::INSTR')
scope.timeout = 10000 #Always good to involve a time-out to avoid putting the scope into an endless waiting state.

def get_screenshot(filename):
    time.sleep(0.1) # Interacting with real equipment takes time. If some commands are not going through, consider adding a small pause to make sure your equipment has finished the previous task.
    # Send command to take a printscreen
    scope.write(":DISP:DATA? PNG")
    # Read the binary data — chunk_size must exceed the full image size
    scope.chunk_size = 1024 * 1024  # 1 MB
    data = scope.read_raw()
    # Find the start of the PNG file
    start = data.find(b'\x89PNG')
    if start != -1:
        data = data[start:]
    # Save the binary data to a file with the specified filename
    with open(filename, "wb") as f:
        f.write(data)
    time.sleep(0.1)
    print(f"Screenshot saved as {os.path.abspath(filename)}")

print('Taking Screenshot')
print('---')
scope_idn = scope.query('*IDN?')
print('[info] scope found: ' + scope_idn)

# Optional: add an annotation box to the screenshot (up to 254 chars)
# scope.write(':DISPlay:ANNotation:TEXT "my note"')  # set text
# scope.write(':DISPlay:ANNotation ON')              # show it; use OFF to hide
#
# Optional: label channels (max 10 characters each)
# scope.write(':CHANnel1:LABel "CH1 label"')
# scope.write(':CHANnel2:LABel "CH2 label"')
# scope.write(':DISPlay:LABel ON')                   # show labels; use OFF to hide

filename = sys.argv[1] if len(sys.argv) > 1 else 'screenshot.png'

if os.path.exists(filename):
    try:
        answer = input(f"'{filename}' already exists. Overwrite? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        answer = ""
    if answer != "y":
        print("Aborted.")
        scope.close()
        rm.close()
        sys.exit(0)

get_screenshot(filename)

# Always clean up your mess when you're done.
print("[info] Took screenshot")
scope.close()
rm.close()

print("[info] Done.")



