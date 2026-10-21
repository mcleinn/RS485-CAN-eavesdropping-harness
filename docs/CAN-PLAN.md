# CAN bus support — hardware recommendations and plan (2026-10-16)

Purpose: extend the harness to eavesdrop on, decode and (later) gateway
inverter ↔ battery CAN links of *other* systems. The SAKO ↔ Seplos RS485
case is solved and needs none of this.

## 1. Adapters

### For Windows / notebook (USB)

| adapter | notes | verdict |
|---|---|---|
| **candleLight / CANable (gs_usb)** | ~20 EUR, open firmware, python-can backend `gs_usb` out of the box, listen-only mode supported | **first choice on the PC** |
| SLCAN-type (serial CAN, e.g. CANable in slcan firmware, USBtin) | shows up as a COM port, python-can `slcan`; fits our existing serial code best | good alternative |
| Waveshare **USB-CAN-A** | ~15 EUR, CH340 serial with Waveshare's own frame protocol; has a hardware silent (listen-only) mode; no python-can backend — a ~100-line driver on our serial transport (a third-party pyserial class exists) | acceptable if staying with Waveshare |
| Waveshare **USB-CAN-B** | dual channel, isolated, proprietary protocol + Windows analyzer software; no python-can backend found | only if the Windows GUI is wanted |
| Waveshare **USB-CAN-FD** | industrial; python-can support advertised, users report no instructions | avoid for now |
| PCAN-USB | commercial, python-can `pcan` backend, Windows driver | fine but pricey |
| cheap "USB-CAN analyzer" boxes with proprietary Windows software only | no programmatic access | avoid |

### For the Raspberry Pi (HAT, SPI)

| HAT | controller | notes |
|---|---|---|
| Waveshare **RS485 CAN HAT** | MCP2515 + SN65HVD230 | socketcan, python-can native, listen-only is a kernel flag; also carries an RS485 port |
| Waveshare **2-CH CAN HAT** / **2-CH CAN FD HAT** | MCP2515 / MCP2518FD | two channels → the natural platform for a **CAN gateway** (bridge) |
| Isolated RS485/CAN HAT (B) | MCP2515 | same, isolated |

Pi + Waveshare HAT is the cleanest path: no custom driver, standard Linux
tools (`ip link`, `candump`), python-can `socketcan`.

### Phones (Termux)
No USB CAN adapter is usable without a kernel driver; gs_usb needs the
kernel module. Only SLCAN-type adapters (serial over USB, FTDI/CH340) could
work via our libusb path, and only FTDI chips are implemented. Treat CAN as
a PC/Pi feature.

## 2. Electrical / safety

- Tap in parallel anywhere on the bus, **no extra termination** (bus has
  120 Ω at both ends already). Two wires (CAN-H, CAN-L) plus ground.
- Use **listen-only** mode for the tap so the adapter never ACKs frames on
  someone else's bus.
- A **gateway** splits the bus into two segments: each needs its own
  termination; the gateway ACKs on both sides.
- Pinouts in our refs: Seplos BMS 3.0 CAN socket pins 4/5 = CAN-H/CAN-L;
  SAKO BMS RJ45 pins 6/7 = CAN; Dyness DL5.0C Pro CAN IN/OUT pins 5/6 =
  EXT CANH/CANL (docs/refs/dyness).

## 3. Software plan

1. **CAN link** on python-can with a virtual bus for tests (hardware-free
   like the rest of the suite). Backends then come for free: slcan, gs_usb,
   pcan, socketcan.
2. **Sniff / identify for CAN:** listen-only, auto-probe 500 then 250
   kbit/s (wrong rate = error frames), report IDs seen and their rates,
   guess the dialect from the ID set.
3. **Dialect decoders** (8-byte fixed layouts, one table each):
   Pylontech CAN (0x351 limits, 0x355 SOC/SOH, 0x356 V/I/T, 0x359 alarms,
   0x35C request flags, 0x35E manufacturer), Victron (same + 0x370–0x37x),
   SMA, Deye/Sunsynk variants.
4. **Timeline reuse:** 0x351/0x355/0x356/0x359/0x35C map onto CVL, CCL, DCL,
   SOC, V, I, alarms, charge-enable → event log, findings, CSV, charts
   unchanged. (Seplos service-side monitor stays RS485.)
5. **Simulator:** CAN battery sending the periodic frames (bench
   substitution tests).
6. **Gateway (bridge) — second step, not optional for other systems:**
   two CAN interfaces (2-CH HAT on the Pi, or two candleLight boards),
   relay every frame both ways with a per-dialect table of which IDs
   originate on which side, strict one-direction forwarding (no loops),
   bus-off/error handling per port, never forward what could not be parsed
   while claiming transparency. Policy engine and invariants carry over
   from the RS485 bridge (only the limits frame may be rewritten, only
   downwards, alarms/requests untouched, reset on silence, audit of every
   change). CAN has no reply timing to disturb (battery broadcasts every
   second), so the inverter sensitivity seen on RS485 has no equivalent.
   Open-source precedent: Battery-Emulator (ESP32, two CAN controllers).

Effort: tap + decoders + timeline + simulator ≈ 1–2 days; gateway ≈ +1 day.
The expensive parts are never the code: Windows driver setup for the
adapter, and getting a listen-only adapter.

## 4. Shopping list (minimal)

- PC: one candleLight-compatible USB-CAN board (listen-only capable).
- Pi: Waveshare 2-CH CAN HAT (gateway-ready) or RS485 CAN HAT (tap only).
- Two RJ45 breakouts or cut patch cables for CAN-H/CAN-L/GND.
