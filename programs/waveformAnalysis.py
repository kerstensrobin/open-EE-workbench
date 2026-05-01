# This is an example script, setting up basic communication with an oscilloscope and performing some measurements
# robin.kerstens@uantwerpen.be
##

import pyvisa as visa
import time
import csv
import os
import numpy as np
from datetime import datetime

rm = visa.ResourceManager()
# the VISA adress for your scope can be found in using Keysight Connection Expert
#scope = rm.open_resource('USB0::0x2A8D::0x038B::CN63370620::0::INSTR')
scope = rm.open_resource('USB0::0x1AB1::0x044D::DHO8A254403952::0::INSTR')
scope.timeout = 10000 #Always good to involve a time-out to avoid putting the scope into an endless waiting state.

measurements = {}  # This variable will be used to save your measurements.


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

def save_measurements_to_csv(filename, measurements, header=False):
    file_exists = os.path.isfile(filename)
    with open(filename, "a", newline='') as csvfile:
        writer = csv.writer(csvfile)
        if header or not file_exists:
            writer.writerow(["Timestamp", "Measurement", "Value"])
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S:%f")
        writer.writerows([[timestamp, m, v] for m, v in measurements.items()])
    print(f"Measurements saved as {filename}")


print('Scope based square wave analysis')
print('---')
scope_idn = scope.query('*IDN?')
print('[info] scope found: ' + scope_idn)
print('[info] For the time being, using Autoscale to get a view of the waveform')
scope.write(':AUToscale %s' % ('CHANnel1'))
#print('Going back to system preset 1')
#scope.write(':SYSTem:PRESet')

print('[info] Starting measurements')
print('[info] ---')
scope.write(':DISPlay:ANNotation:TEXT "555 Timer Output"')
scope.write(':CHANnel1:LABel "OUTPUT";:Display:LABel ON' )
# Enable the right measurements on screen, and store a value in the measurements object
scope.write(':MEASure:FREQuency %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:FREQuency?')
FREQuency = measurement_temp[0]
measurements['FREQuency(Hz)'] = FREQuency
scope.write(':MEASure:VAMPlitude %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:VAMPlitude?')
VAMPlitude = measurement_temp[0]
measurements['Vamplitude(V)'] = VAMPlitude
scope.write(':MEASure:RISEtime %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:RISEtime?')
RISEtime = measurement_temp[0]
measurements['Rise Time(ns)'] = RISEtime
scope.write(':MEASure:OVERshoot %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:OVERshoot?')
OVERshoot = measurement_temp[0]
measurements['Overshoot(%)'] = OVERshoot
# Save measurements to CSV
save_measurements_to_csv('555WaveformAnalysis.csv', measurements)
get_screenshot('555WaveformAnalysis_overview.png')
time.sleep(1)
scope.write(":TRIGGER:EDGE:SLOPE POSITIVE")

print('[info] Analyzing waveform in detail ...')
print('[info] ---')
scope.write(':MEASure:CLEar') #clean up the screen
scope.write(':MEASure:OVERshoot %s' % ('CHANnel1'))
scope.write(':MEASure:RISEtime %s' % ('CHANnel1'))
measurement_temp = scope.query_ascii_values(':MEASure:RISEtime?')
risetime = measurement_temp[0]
measurements['Rise Time'] = risetime
scope.write(":TIMebase:SCALe %s" % risetime) # zoom in based on risetime value
scope.write(":TIMebase:POSition %s" % (risetime*2)) # Horizontal displacement for a better view
scope.write(':MEASure:VPP')
measurement_temp = scope.query_ascii_values(':MEASure:VPP?') #Get decent vertical settings based on measured values
VAMPlitude = measurement_temp[0]
voltagePerDivision = VAMPlitude/6
scope.write(":CHANnel1:SCALe %s" % voltagePerDivision)
scope.write("CHANnel1:OFFSet %s" % (voltagePerDivision*3))
scope.write(':DISPlay:ANNotation:TEXT "555 Rising edge"')
get_screenshot('555WaveformAnalysis_RisingEdge.png')
# Save measurements to CSV
save_measurements_to_csv('555WaveformAnalysis.csv', measurements)

scope.write(':DISPlay:ANNotation:TEXT "555 Frequency Stability Check"')
scope.write(':MEASure:CLEar') #clean up the screen
scope.write(':MEASure:FREQuency %s' % ('CHANnel1'))
scaleValue = (1/FREQuency)*0.25 #period = 1/frequency, and I want one period to take up 4 divisions, so *0.25
scope.write(":TIMebase:SCALe %s" % scaleValue) #period = 1/frequency
print("[info] Checking frequency stability ...")
numberOfMeasurements = 20
measurements.clear() # reset this table
for currentMeasurement in range(numberOfMeasurements):
    measurement_temp = scope.query_ascii_values(':MEASure:FREQuency?')
    measurements['Frequency checking (Hz)'] = measurement_temp[0]
    save_measurements_to_csv('555WaveformAnalysis.csv', measurements)
    print('[info] Current frequency: ' + str(measurement_temp[0]) + 'Hz')
    time.sleep(0.5)


# Always clean up your mess when you're done.
print("[info] Analysis complete. Terminating.")
scope.write(':DISPlay:ANNotation OFF')
scope.write(':DISPlay:LABel OFF')
scope.write(':SYSTem:PRESet')
scope.write(':AUToscale %s' % ('CHANnel1'))
scope.close()
rm.close()

print("[info] Done.")

