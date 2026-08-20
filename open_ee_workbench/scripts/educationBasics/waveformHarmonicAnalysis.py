# This is an example script, setting up basic communication with an oscilloscope and performing an FFT analysis.
# robin.kerstens@uantwerpen.be
##

import pyvisa as visa
import time
import csv
import os
import sys
from datetime import datetime

# paths.py lives in core/ — see CLAUDE.md
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'core'))
from paths import today_output_dir

rm = visa.ResourceManager()
# the VISA adress for your scope can be found in using Keysight Connection Expert, or nachoVisa.py
scope = rm.open_resource('TCPIP::143.129.36.135::hislip0,4880::INSTR')
scope.timeout = 10000 #Always good to involve a time-out to avoid putting the scope into an endless waiting state.

measurements = {}  # This variable will be used to save your measurements.

# All output (screenshots + CSV) goes in today's dated results/ folder, regardless of cwd.
OUTPUT_DIR = today_output_dir()

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
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(data)
    time.sleep(0.1)
    print(f"Screenshot saved as {filepath}")

def save_measurements_to_csv(filename, measurements, header=False):
    filepath = os.path.join(OUTPUT_DIR, filename)
    file_exists = os.path.isfile(filepath)
    with open(filepath, "a", newline='') as csvfile:
        writer = csv.writer(csvfile)
        if header or not file_exists:
            writer.writerow(["Timestamp", "Measurement", "Value"])
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S:%f")
        writer.writerows([[timestamp, m, v] for m, v in measurements.items()])
    print(f"Measurements saved as {filepath}")


print('Scope based analysis of signal Harmonics')
print('---')
scope_idn = scope.query('*IDN?')
scope.write(':SYSTem:PRESet')
print('[info] scope found: ' + scope_idn)
print('[info] For the time being, using Autoscale to get a view of the waveform')
scope.write(':AUToscale %s' % ('CHANnel1'))

print('[info] Starting measurements')
print('[info] ---')
scope.write(':DISPlay:ANNotation:TEXT "Signal Harmonics Analysis"')
scope.write(':CHANnel1:LABel "Vout";:Display:LABel ON' )
# Enable the right measurements on screen, and store a value in the measurements object
scope.write(':MEASure:FREQuency %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:FREQuency?')
FREQuency = measurement_temp[0]
measurements['FREQuency(Hz)'] = FREQuency
scope.write(':MEASure:VAMPlitude %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:VAMPlitude?')
VAMPlitude = measurement_temp[0]
measurements['Vamplitude(V)'] = VAMPlitude
scope.write(':MEASure:PERiod %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:PERiod?')
PERiod = measurement_temp[0]
measurements['PERiod (s)'] = PERiod
# Save measurements to CSV
save_measurements_to_csv('signalHarmonicsAnalysis.csv', measurements)
time.sleep(2)
get_screenshot('signalHarmonicAnalysis.png')


print('[info] Setting up FFT Analysis ...')
print('[info] ---')
measurements = {}  # Clear the variable to avoid writing the same thing twice
timebase = PERiod*10
scope.write(":TIMebase:SCALe %s" % timebase) # Horizontal scaling to record enough signal --> more is better for frequency analysis
scope.write(":CHANnel1:SCALe %s" % VAMPlitude)  # Vertical scaling so that the signal does not take up to much screen.
offset = VAMPlitude*3
scope.write(":CHANnel1:OFFSet %s" % offset) # Horizontal displacement for a better view
scope.write(":VIEW FUNCtion")
scope.write(":FUNCtion:OPERation FFT")
scope.write(":FUNCtion:SCALe 20")
scope.write(":FUNCtion:OFFSet -50")
scope.write(":FUNCtion:FFT:CENTer %s" %FREQuency)
scope.write(':DISPlay:ANNotation:TEXT "FFT Centered around main Frequency %s Hz "' %FREQuency)
frequencySpan = FREQuency*10
frequencyPerDivision = frequencySpan/10
scope.write(":FUNCtion:FFT:SPAN %s" %frequencySpan)
time.sleep(2)
get_screenshot('signalHarmonicAnalysis_FFT_Centered.png')

print('[info] Analyzing the most significant harmonics ...')
print('[info] ---')
measurements = {}  # Clear the variable to avoid writing the same thing twice
scope.write(':MEASure:CLEar') #clean up the screen
scope.write(':DISPlay:ANNotation:TEXT "FFT Analysis"')
measurement_temp = scope.query_ascii_values(':MEASure:XMAX? FUNCtion')
peakPosition = measurement_temp[0]
measurements['baseSignal (Hz)'] = peakPosition
measurement_temp = scope.query_ascii_values(':MEASure:VMAX? FUNCtion') #Measure the intensity of the first harmonic
dBVharmonic = measurement_temp[0]
measurements['baseSignal (dBV)'] = dBVharmonic
print(f'[info] Base signal {dBVharmonic} dBV at {peakPosition} Hz.')
time.sleep(1)

# The following steps will analyze the first 5 harmonics found in the signal.
# There is no dedicated function for this built in to the scope, so we will have to do this manually
# Knowing the base frequency, calculated above, we will always look for the highest peak in the FFT on our screen.
# XMAX? will return the location of this peak on the horizontal axis (frequency)
# VMAN? will return the amplitude of this peak in dBv
# As these are our only tools available for amplitude analysis in FFT, we will always have to shift the signal so that the peaks
# that were already processed are positioned off-screen. So we will scroll through our frequency response, peak by peak
# and log each peak iteratively.

measurements = {}  # Clear the variable to avoid writing the same thing twice
numberOfHarmonics = 5
for curPeakIdx in range(numberOfHarmonics):
    scope.write(f':DISPlay:ANNotation:TEXT "Signal Harmonic Analysis: {curPeakIdx+1}"')
    centerFrequency = peakPosition+(5*frequencyPerDivision)+frequencyPerDivision/10 #shift the base signal peak to to the left by 5 divisions + a margin to avoid it still being present.
    scope.write(":FUNCtion:FFT:CENTer %s" %centerFrequency)
    scope.write(':MEASure:XMAX FUNCtion')
    scope.write(':MEASure:XMAX FUNCtion')
    scope.write(':MEASure:VMAX FUNCtion')
    measurement_temp = scope.query_ascii_values(':MEASure:XMAX? FUNCtion') #Measure the position of the first harmonic
    peakPosition = measurement_temp[0]
    measurements['Harmonic Frequency (Hz)'] = peakPosition
    measurement_temp = scope.query_ascii_values(':MEASure:VMAX? FUNCtion') #Measure the intensity of the first harmonic
    dBVharmonic = measurement_temp[0]
    measurements['Harmonic Energy (dBV)'] = dBVharmonic
    print(f'[info] Found harmonic with {dBVharmonic} dBV at {peakPosition} Hz.')
    save_measurements_to_csv('signalHarmonicsAnalysis.csv', measurements)
    time.sleep(1)

# Always clean up your mess when you're done.
print("[info] Analysis complete. Terminating.")
scope.write(':DISPlay:ANNotation OFF')
scope.write(':DISPlay:LABel OFF')
scope.write(':SYSTem:PRESet')
scope.write(':AUToscale %s' % ('CHANnel1'))
scope.close()
rm.close()

print("[info] Done.")



