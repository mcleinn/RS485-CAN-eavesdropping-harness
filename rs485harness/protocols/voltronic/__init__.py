"""Voltronic 'Inverter and BMS 485' protocol (probably the 'LIB' battery
option on Axpert clones) - Modbus-style binary @ 9600 8N1, common node id
0x01, function 0x03 read / 0x10 write, CRC16-Modbus.

Planned module: battery-side register server (so the battery simulator can
answer a SAKO set to LIB) and inverter-side poller. Register map: docs/refs/
Voltronic-Inverter-BMS-485-protocol-20201202.txt (extracted from the docx):
0x0010 cell count, 0x0011+ cell voltages (0.1V), 0x0026+ temps (0.1K),
0x0030/31 charge/discharge current (0.1A), 0x0032 module voltage, 0x0033
SOC, 0x0070-0x0076 charge/discharge limits + status byte.

The identify scan already probes for LIB responders; CRC/frame detection
lives in analyzer/classify.py.
"""
