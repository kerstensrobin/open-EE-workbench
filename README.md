# Open-EE-workbench
A non proprietary, cross-compatible VISA toolset for automating the modern day electronic engineering workbench.
Open-EE-workbench strives to implement a workflow that analyses your bench, and provides a set of standard test that work out of the box, regardless of brand(s) of your TME.
Relying hard on python libraries such as pyVISA and pyVISA-py, as well as programming manuals that are provided by all different manufaturers such as Keysight, Tektronix, Rodde&Shwarz, Rigol, Siglent, and more.

Very much a work in progress.

## Scripts
### nachoVisa.py
nachoVisa scans you local network and USB devices to check for available devices. It includes error diagnostics and an automatic udev-fix for Arch and Debian based devices.

Made with love by [nacho.works](www.nacho.works)
