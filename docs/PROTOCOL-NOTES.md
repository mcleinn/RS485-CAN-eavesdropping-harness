# Protocol notes — Pylontech RS485 & friends (LLM/diagnosis knowledge base)

Condensed from unofficial protocol PDFs and field-tested reference code
(see `docs/refs/`). This is the ground truth the harness implements.

## The scene

- **Inverter**: SAKO SUNON V 6.5 kVA 48 V, battery protocol set to "PYL".
  SAKO is a Voltronic/Axpert-clone family. Voltronic-family BMS options are
  PYL / SH / WEC / SOL / BAK / LIB / LIC; **PYL, LIB, LIC are RS485**;
  WEC/SOL are CAN. Caveat: Pylontech's own compatibility list says only
  Axpert **VM III and King** actually do RS485-PYL; SAKO support has been
  quoted that the battery must provide the **LIB** protocol.
- **Battery**: Seplos Mason 280L (3.0), Seplos Smart BMS 3.0 (16S LiFePO4,
  51.2 V, 314 Ah), BMS Soft V1.5, RTU Comm V0.3, set to "RS485-Pylontech".
  BMS 3.0's *native* serial protocol is **Modbus RTU @ 19200, slave id 0**
  (generation break vs BMS 2.0/V16 which was Pylontech-style ASCII @ 9600).
  Its Pylontech mode reportedly implements the 0x61/0x62/0x63 family.

## Pylontech RS485 ASCII frame (YD/T 1363.1 family)

```
SOI  VER ADR CID1 CID2 LENGTH INFO CHKSUM EOI
'~'  2ch 2ch 2ch  2ch  4ch    n ch 4ch    CR(0x0D)
```
Everything between SOI and EOI is uppercase ASCII hex; multi-byte values
big-endian. CID1 = 0x46 (LiFePO4 battery). Baud for the inverter link:
**9600 8N1** (115200 is the monitoring/PC rate; 1200 is only the RS232
console wake-up).

- LENGTH = LCHKSUM nibble + 12-bit LENID (LENID counts INFO **ASCII chars**).
  LCHKSUM = ((~(sum of LENID nibbles % 16)) & 0xF) + 1, truncated to 4 bits.
  Zero-length INFO → LENGTH "0000".
- CHKSUM = two's complement of the byte sum of all ASCII chars VER..INFO,
  mod 0x10000.
- **Responses carry RTN in the CID2 position** (00 OK, 01 VER err, 02
  CHKSUM err, 03 LCHKSUM err, 04 CID2 unsupported, 05 format err, 06
  invalid data, 90 ADR err, 91 comm err). Echoing the command there
  ("4661" instead of "4600") is a known third-party bug (Battery-Emulator
  PR #3003) that strict inverters reject.
- Golden vector (hand-verified): `7E 32303031343634324530303230314644 3335 0D`
  = `~20014642E00201FD35\r` → VER 20 ADR 01 CID1 46 CID2 42 LEN E002 INFO "01"
  CHKSUM FD35.

### Two command families

| Family | CID2 | Used by |
|---|---|---|
| 4x (per-pack) | 42 analog, 44 alarms, 47 sys params, 4F protocol ver, 51 manufacturer, 92 charge/discharge mgmt, 93 serial, 94 set mgmt | monitoring software, PC tools |
| 6x (system) | 60 basic info, 61 analog, 62 alarms, 63 charge/discharge mgmt | **inverters**, esp. Voltronic clones |

An inverter that gets RTN=04 for its family may or may not fall back to the
other one. The harness battery-sim answers both; the diagnoser reports
which family the real inverter asks for.

### Key payload layouts (from field-tested emulators)

- **0x61 response** (49 bytes / 98 chars): voltage(mV,u16) current(cA,i16)
  SOC(u8) cycles(u16) cycles(u16) SOH(u8) SOH(u8) maxCellV(mV) 0x0101
  minCellV(mV) 0x0108 avgCellT maxCellT 0x0103 minCellT 0x0109 avgMosT
  maxMosT 0x0101 minMosT 0x0101 avgBmsT maxBmsT 0x0101 minBmsT 0x0101.
  **Temperatures in deci-Kelvin**: (°C + 273.15) × 10. The 0x01xx words are
  index tags real inverters expect.
- **0x62 response**: 4 bytes: alarm1, alarm2, protect1, protect2 (bit maps
  in `payloads.ALARM_BITS_62`).
- **0x63 response** (9 bytes): maxChargeV(mV) minDischargeV(mV)
  maxChargeI(dA) maxDischargeI(dA) status (b7 charge-enable, b6
  discharge-enable, b5 charge-immediately/force).
- **0x42 response**: InfoFlag + module payload: ncells, cell mV × n, ntemp,
  temps (K×10, decode (raw−2731)/10 °C), current (dA,i16), voltage (mV),
  remaining cap, user-defined count, total cap, cycles (24-bit capacity
  extension when user-defined > 2).

### Addressing (messy on purpose — never filter on ADR when sniffing)

- Spec says ADR 1..254. But: master pack commonly answers on **ADR 02**
  even when polled at another address (Fahmula hardcodes it); some
  firmwares only accept ADR 01; `pylon2mqtt` found a US2000B where only
  ADR 1 worked and 0/2 returned RTN=90.
- Voltronic BMS-485 doc: common node id **0x01**, every BMS must respond
  to it.
- Seplos DIP: bits 1–4 pack address; **pack 0 reportedly not addressable
  for inverter comms → set DIP1 ON (pack 1) and power-cycle**. DIP 5 is a
  CAN bit — keep OFF for RS485.
- VER byte: 0x20 common; 0x25 (PACE-standard) responses append ~8 extra
  bytes. Request VER selects response format on some firmwares.

## Voltronic "Inverter and BMS 485" protocol (probably = LIB)

**Not** Pylontech ASCII: Modbus-style binary, 9600 8N1, slave/common id
0x01, function 0x03 read / 0x10 write, CRC16-Modbus (low byte first).
Register map (see `docs/refs/Voltronic-Inverter-BMS-485-protocol-20201202.txt`):
0x0001 protocol type/version; 0x0010 cell count; 0x0011.. cell voltages
(0.1 V); 0x0026.. temperatures (0.1 K); 0x0030/31 charge/discharge current
(0.1 A); 0x0032 module voltage (0.1 V); 0x0033 SOC; 0x0037.. alarm/protect
bit words; 0x0070..0x0076 charge/discharge limits & status (b7 charge
enable, b6 discharge enable, b5/b4 charge-immediately, SOC thresholds
9%/14%). The harness identify scan probes this map.

## Seplos BMS 3.0 native Modbus (ground-truth side channel)

19200 8N1, slave id 0 (single pack). Blocks: PIA 0x1000 (pack status, 18
regs), PIB 0x1100 (cell voltages + temps, 26 regs), PIC 0x1200 (alarm
coils), SPA 0x1300 (settings), SFA 0x1400, SCA 0x150D. Full map:
`docs/refs/Seplos-BMS3.0-Modbus-RTU-Protocol.pdf` + registers XLSX.
Use the RS485-1/RS485-2 socket for this — those ports never talk to
inverters.

## Physical layer / cabling (top suspects for "battery not recognized")

- **Seplos inverter-comms socket is the RJ45 labelled "CAN"** — in RS485
  mode it carries RS485-A on pin 4, RS485-B on pin 5, GND 3/6. The
  RS485-1/RS485-2 sockets are pack-interlink/PC only.
- **SAKO/Voltronic BMS RJ45**: RS485-B pin 3, RS485-A pin 5, GND 7/8.
- → A straight patch cable connects nothing useful. Build crossover:
  Seplos 4 → SAKO 5 (A), Seplos 5 → SAKO 3 (B), Seplos 3/6 → SAKO 7/8 (GND).
- A/B polarity swap is harmless to try; 120 Ω termination only at the two
  physical bus ends; RS485 without bias floats when idle → garbage bytes.
- Inverter menu: try program 05 = `Li` → program 36 = `L01`, and `LIB`
  as alternative to `PYL`. Success indicator: battery icon/SOC follows BMS.

## USB adapter notes (Waveshare USB TO 2CH RS485)

- FTDI FT2232HL → two independent COM ports. **Set latency timer to 1 ms**
  on both (Device Manager → COM → Advanced), close/reopen the port.
- 2-wire adapters may echo their own TX (board-dependent) → the harness
  auto-detects (`selftest`, `--detect-echo`) and suppresses.
- Per-port 120 Ω termination jumpers: enable only at bus ends.

## Diagnosis flow with this harness

1. `rsh selftest COMa COMb` (ch1+ch2 wired A-A/B-B) — prove the adapter.
2. `rsh identify --port COMx` on each side separately — who talks, what
   protocol, which baud/address, how fast.
3. `rsh battery-sim --port COMx` toward the inverter — proves inverter +
   cable if the inverter shows the battery. Watch which CID2s it polls.
4. `rsh inverter-sim --port COMy` / probe toward the battery — proves the
   battery side; scan ADR/VER/family.
5. `rsh sniff --ch1 --ch2` or `rsh bridge` in the middle of the real link,
   then `rsh diagnose` on the capture.
6. If dialects differ: `rsh bridge --fix-cid2-echo --force-adr/--force-ver`
   as a live translator.
