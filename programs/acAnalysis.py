# AC frequency sweep analysis
# Reads frequencies from acAnalysis.csv, sets the waveform generator to each frequency,
# and measures Vpp on CH1 and CH2 with the oscilloscope. Results are saved to acAnalysis_results.csv.
# robin.kerstens@uantwerpen.be

import csv
import os
import time
from datetime import datetime

# pyvisa lets Python talk to lab instruments over USB, GPIB, or LAN using the
# VISA standard (Virtual Instrument Software Architecture). It is the industry
# standard way to control bench equipment from a script.
import pyvisa as visa

# --- VISA resource strings ---
# Every instrument on the bus has a unique address. Run checkVisa.py to find yours.
# The format is: USB0::<vendor ID>::<product ID>::<serial number>::0::INSTR
SCOPE_RESOURCE     = 'USB0::10893::902::CN63126106::0::INSTR'    # DSOX1204A
GENERATOR_RESOURCE = 'USB0::10893::35841::CN65510072::0::INSTR'  # EDU33211A

# --- File paths ---
INPUT_CSV  = 'acAnalysis.csv'       # list of frequencies to sweep (one per row, column: frequency_hz)
OUTPUT_CSV = 'acAnalysis_results.csv'

# --- Generator settings ---
GEN_AMPLITUDE_VPP = 1.0  # Signal amplitude in Vpp
GEN_CHANNEL       = 1    # Generator output channel to use

# How long to wait after changing frequency before measuring.
# The circuit under test and the scope's trigger need a moment to settle.
SETTLE_TIME = 0.2  # seconds

# How many full waveform periods to show across the 10 horizontal divisions.
DISPLAY_PERIODS = 5

# --- Default scope state ---
# Both channels start at this scale before any autoscale or dynamic adjustment.
# A known starting point prevents the scope from carrying over odd settings
# from a previous run or a manual session.
DEFAULT_VOLTS_PER_DIV = 1  # V/div

# --- Trigger settings ---
# The trigger tells the scope when to start drawing each waveform frame.
# A stable trigger keeps the waveform from drifting or flickering on screen.
TRIGGER_SOURCE = 'CHANnel1'  # which channel to trigger on (CHANnel1 / CHANnel2)
TRIGGER_SLOPE  = 'POSitive'  # edge direction to trigger on (POSitive / NEGative / EITHer)
TRIGGER_LEVEL  = 0.0         # voltage threshold at which the trigger fires (V)

# --- Display settings ---
# These strings appear on the scope screen during the measurement.
SCOPE_TITLE = 'AC Frequency Sweep'
CH1_LABEL   = 'INPUT'   # CH1 monitors the generator output (what goes in)
CH2_LABEL   = 'OUTPUT'  # CH2 monitors the circuit output (what comes out)


# ─── Helper functions ────────────────────────────────────────────────────────
# All instrument commands use SCPI (Standard Commands for Programmable
# Instruments), a text-based language understood by most modern bench equipment.
# Commands that end with ? are queries — they return a value.

def configure_generator(gen, amplitude_vpp):
    # Set up a sine wave on the chosen channel. We leave frequency unset here;
    # set_frequency() handles that for each step of the sweep.
    gen.write(f'SOURce{GEN_CHANNEL}:FUNCtion SINusoid')
    gen.write(f'SOURce{GEN_CHANNEL}:VOLTage:AMPlitude {amplitude_vpp}')
    gen.write(f'OUTPut{GEN_CHANNEL} ON')


def set_frequency(gen, scope, freq_hz):
    gen.write(f'SOURce{GEN_CHANNEL}:FREQuency {freq_hz}')
    # Adjust the time axis so DISPLAY_PERIODS full periods fit across the 10 divisions.
    # scale = (periods × period) / 10 divisions
    period = 1.0 / freq_hz
    scope.write(f':TIMebase:SCALe {period * DISPLAY_PERIODS / 10:.6e}')


def configure_scope(scope):
    # ── Default channel state ─────────────────────────────────────────────────
    # Start both channels from a known scale so the scope is in a predictable
    # state regardless of what was set in a previous run or manual session.
    for ch in (1, 2):
        scope.write(f':CHANnel{ch}:SCALe {DEFAULT_VOLTS_PER_DIV}')
        scope.write(f':CHANnel{ch}:OFFSet 0')

    # ── Trigger ──────────────────────────────────────────────────────────────
    # Edge triggering fires when the signal crosses TRIGGER_LEVEL in the chosen
    # direction. This is the most common trigger mode for periodic signals.
    scope.write(':TRIGger:MODE EDGE')
    scope.write(f':TRIGger:EDGE:SOURce {TRIGGER_SOURCE}')
    scope.write(f':TRIGger:EDGE:SLOPe {TRIGGER_SLOPE}')
    scope.write(f':TRIGger:EDGE:LEVel {TRIGGER_LEVEL}')

    # ── Measurements ─────────────────────────────────────────────────────────
    # Enable all measurement badges once. Keeping setup separate from querying
    # avoids re-sending these enable commands every step, reducing USB traffic.
    scope.write(':MEASure:VPP CHANnel1')
    scope.write(':MEASure:VPP CHANnel2')
    # Frequency on CH1 — the generator input is the stable reference to lock on.
    scope.write(':MEASure:FREQuency CHANnel1')


def measure_vpp(scope, channel):
    # Only query — the measurement is already active from configure_scope_measurements().
    # Passing the channel explicitly avoids ambiguity when multiple sources are active.
    time.sleep(0.1)
    return scope.query_ascii_values(f':MEASure:VPP? CHANnel{channel}')[0]


def measure_frequency(scope, channel):
    # Query the frequency measurement enabled during setup. Using CH1 as the
    # reference because its amplitude is fixed — easier for the scope to lock on.
    return scope.query_ascii_values(f':MEASure:FREQuency? CHANnel{channel}')[0]


def adjust_channel_scale(scope, channel, vpp):
    # Keysight scopes return 9.99e37 when a measurement cannot be made
    # (e.g. no signal, clipping). Skip the rescale in that case.
    if vpp <= 0 or vpp > 1e30:
        return
    # Spread the waveform across ~6 of the 8 visible divisions so it is easy
    # to read without touching the top or bottom of the screen.
    scale = vpp / 6.0
    scope.write(f':CHANnel{channel}:SCALe {scale:.4e}')
    scope.write(f':CHANnel{channel}:OFFSet 0')


def load_frequencies(filename):
    # Read the sweep table. The CSV must have a column named 'frequency_hz'.
    with open(filename, newline='') as f:
        reader = csv.DictReader(f)
        return [float(row['frequency_hz']) for row in reader]


def save_results(filename, rows):
    # Append results to the output file. If the file doesn't exist yet,
    # write the header row first so the CSV is self-describing.
    write_header = not os.path.isfile(filename)
    with open(filename, 'a', newline='') as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(['timestamp', 'set_frequency_hz', 'measured_frequency_hz', 'vpp_ch1_V', 'vpp_ch2_V'])
        writer.writerows(rows)


# ─── Connect to instruments ───────────────────────────────────────────────────
# ResourceManager scans for all VISA instruments connected to this PC.
rm = visa.ResourceManager()
scope = rm.open_resource(SCOPE_RESOURCE)
gen   = rm.open_resource(GENERATOR_RESOURCE)

# Timeout (ms): how long pyvisa waits for a response before raising an error.
# Without this, a missed command could hang the script forever.
scope.timeout = 10000
gen.timeout   = 5000

print('AC Frequency Sweep Analysis')
print('---')
# *IDN? is a universal SCPI command — every instrument must support it.
# It returns: manufacturer, model, serial number, firmware version.
print(f'[info] Scope     : {scope.query("*IDN?").strip()}')
print(f'[info] Generator : {gen.query("*IDN?").strip()}')
print()

# ─── Load sweep frequencies ───────────────────────────────────────────────────
frequencies = load_frequencies(INPUT_CSV)
print(f'[info] Loaded {len(frequencies)} frequencies from {INPUT_CSV}')
print(f'[info] Generator output: {GEN_AMPLITUDE_VPP} Vpp sine, channel {GEN_CHANNEL}')
print()

# ─── Initial setup ────────────────────────────────────────────────────────────
configure_generator(gen, GEN_AMPLITUDE_VPP)
time.sleep(0.5)  # give the generator output time to stabilise before we do anything else

scope.write(':MEASure:CLEar')  # remove any leftover measurement badges from a previous run

# Show a title and channel labels on the scope screen so it is clear what is
# being measured when the scope is observed during the sweep.
scope.write(f':DISPlay:ANNotation:TEXT "{SCOPE_TITLE}"')
scope.write(':DISPlay:ANNotation ON')
scope.write(f':CHANnel1:LABel "{CH1_LABEL}"')
scope.write(f':CHANnel2:LABel "{CH2_LABEL}"')
scope.write(':DISPlay:LABel ON')

# Set CH1 scale from the known generator amplitude — the input does not change.
adjust_channel_scale(scope, 1, GEN_AMPLITUDE_VPP)
# CH2 amplitude is unknown (depends on the circuit), so let the scope figure it out.
scope.write(':AUToscale CHANnel2')
time.sleep(2)  # autoscale takes a moment to complete
configure_scope(scope)  # set trigger and enable measurement badges — queried each step, not re-enabled

# ─── Frequency sweep ──────────────────────────────────────────────────────────
print(f'  {"Set Freq":>12}  |  {"Meas Freq":>12}  |  {"CH1 Vpp":>10}  |  {"CH2 Vpp":>10}')
print(f'  {"-"*12}  |  {"-"*12}  |  {"-"*10}  |  {"-"*10}')

results = []
timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

for freq in frequencies:
    set_frequency(gen, scope, freq)
    time.sleep(SETTLE_TIME)

    # Two-pass measurement on CH2: first get a rough Vpp to rescale the channel,
    # then measure again once the display has settled for an accurate reading.
    # This is needed because the circuit's output amplitude changes with frequency.
    rough_vpp2 = measure_vpp(scope, 2)
    adjust_channel_scale(scope, 2, rough_vpp2)
    time.sleep(0.2)

    vpp1      = measure_vpp(scope, 1)
    vpp2      = measure_vpp(scope, 2)
    freq_meas = measure_frequency(scope, 1)

    print(f'  {freq:>10.1f} Hz  |  {freq_meas:>10.1f} Hz  |  {vpp1:>8.4f} V  |  {vpp2:>8.4f} V')
    results.append([timestamp, freq, freq_meas, vpp1, vpp2])

# ─── Save & clean up ──────────────────────────────────────────────────────────
save_results(OUTPUT_CSV, results)
print()
print(f'[info] Results saved to {os.path.abspath(OUTPUT_CSV)}')

# Turn off the generator output and restore the scope to a clean state.
# Always release instrument resources at the end of your script.
gen.write(f'OUTPut{GEN_CHANNEL} OFF')
scope.write(':MEASure:CLEar')
scope.write(':DISPlay:ANNotation OFF')
scope.write(':DISPlay:LABel OFF')
scope.close()
gen.close()
rm.close()
print('[info] Done.')
