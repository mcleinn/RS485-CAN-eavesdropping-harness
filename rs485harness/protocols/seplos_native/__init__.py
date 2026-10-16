"""Seplos BMS 3.0 native protocol - Modbus RTU @ 19200 8N1, slave id 0.

Planned V2 module: read ground truth directly from the battery's RS485-1
port independent of its Pylontech mode. Register blocks: PIA 0x1000 (pack
status), PIB 0x1100 (cell voltages/temps), PIC 0x1200 (alarm coils), SPA
0x1300 (settings). Full map: docs/refs/seplos/Seplos-BMS3.0-Modbus-RTU-Protocol.pdf
and docs/refs/seplos/Seplos-BMSv3-modbus-registers.xlsx.

The identify scan (analyzer/identify.py) already probes for this device;
CRC + frame scanning lives in analyzer/classify.py.
"""
