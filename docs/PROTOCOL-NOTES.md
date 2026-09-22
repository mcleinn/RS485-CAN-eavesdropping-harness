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

### V3.5 / "new protocol design" doc findings (docs/refs/pylontech-v35-design.txt — full 6x spec, verified 2026-09-22)

The Deye-variant design doc ("新增协议设计…9600", byte-identical to the
Akkudoktor mirror) is the authoritative 6x-family spec. Key facts, all
verified against our codec (frames pass checksum):

- **System commands 0x60–0x64 are only valid at master-group addresses
  0x12, 0x22, 0x32, 0x42, 0x52, 0x62, 0x72** (DIP-group dependent); the
  response **echoes** that address (`~2012 46 00 …`). Request golden
  vectors: `~201246600000FDAB\r`, `~201246610000FDAA\r`,
  `~201246620000FDA9\r`, `~201246630000FDA8\r`. identify/discover probe
  ADR 0x12; profiles `pylontech-v35` (battery) and `pylon-v35-adr12`
  (inverter) model this dialect.
- **0x60 = system basic info**: battery name (10B ASCII, e.g. "Force_L") +
  manufacturer (20B, "Pylon") + software version (2B) + battery count (1B)
  + 16B barcode per battery. This is the RS485 analogue of the CAN 0x35E
  "PYLON" name check — a prime suspect when an inverter probes and then
  rejects a battery.
- The 0x0101-style words in 0x61 are **module locators** (0x0304 = module
  4 in DIP-group-3), not constants; the duplicated fields are avg/max
  cycles and avg/min SOH. Temps = °C×10 + 2731 (matches the emulators).
- Scaling is dialect-dependent: doc's 0x61 example current 0x61A8 = 25.00 A
  (÷1000) vs Fahmula's centi-amps; doc's 0x63 currents ÷100 vs Fahmula's
  ÷10. When diagnosing content-level rejection, compare raw hex before
  trusting any single scaling.
- 0x64 = system shutdown — **never probe it against a real battery**
  (discover deliberately excludes it).
- Seplos's own compatibility list says its PYLON RS485 mode = this
  "RS485-protocol-pylon-low-voltage" **V3.5, auto-adaptation, 9600**.

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

## Field findings from forum threads (docs/refs/threads/, mined 2026-09-22)

Real captures and hard-won experience — several are decisive:

- **Real Voltronic↔Pylontech capture** (powerforum riogrande75, verified
  against our codec, now a regression test): MPI/Infinisolar polls at
  **ADR 02, VER 20**, sequence `4F`(protocol) → `61` → `63`, INFO carries
  the pack byte "02". Example replies decode cleanly:
  `~200246008062CFCDFF8E51...E897` = 53.197 V, −1.14 A, SoC 81%;
  `~20024600D012E100AF0003B603B6C0F9B5` = charge 57.6 V/95 A, discharge
  44.8 V/95 A, status C0 (charge+discharge enabled). JK inverter-BMS polls
  0x63 at **ADR 0x12** — confirming the V3.5 master-address rule in the
  wild.
- **The missing 0x61/0x63 payload spec** lives in the **Sunsynk/Deye
  "Battery Compatibility" document** (Sunsynk_BatteryCompatibility_v16) and
  the sunsky/deye comms protocol — that's where riogrande75 and others
  finally found it. (We already have the equivalent in the V3.5 design doc.)
- **Inverter firmware can be the fix** (general lesson, not a specific
  number): several Voltronic/Axpert owners only got the BMS recognized
  after a display-MCU firmware update ("didn't accept my commands until I
  updated - it was a firmware problem on the inverter"). ⚠️ Their "U2 =
  12.21" is a *different* Axpert-clone versioning scheme and does NOT map
  to this SAKO, which reports **U1 V85.00 / U2 V46.04**. Takeaway: if the
  frames look correct but the SAKO ignores them, ask SAKO whether a newer
  U1/U2 exists — don't chase the 12.21 number.
- **Seplos BMS 3.0 (V3) implements the Voltronic 0x61/0x62/0x63 queries by
  default**; setting the BMS RS485 protocol to "Pylontech" made an FSP/
  Voltronic 15 kW inverter communicate immediately, no BMS firmware update
  (older V10E did need special FW). So on our V3 pack, PYLON mode *should*
  be enough on the correct port — shifting suspicion to cabling/port/
  firmware, matching our plan.
- **LIB mode is flaky**: several report CAN+Pylon works but RS485 "LIB"
  loses the link / Error 61 after minutes (Voltronic Axpert Max + Seplos
  3.0 / Basen). Prefer PYLON-on-RS485 over LIB where possible.
- **Scaling gotchas from the wild**: one JK BMS reports current 5% low by
  design, and its values overflow/garble above ~68 A — sanity-check
  decoded currents against a clamp. Reinforces: compare raw hex before
  trusting a scaling.
- **US2000B DIP**: single battery needs **DIP1 ON for 9600 baud**
  (DIP1-4 = 1000); all-off = 115200. Matches our "DIP1 selects baud" note.
- **SAKO-specific** (DIY 93892): a SAKO Sunpolo 6 kW owner got a TDT 6022
  BMS working **over RS485 only, never CAN**; suggests JK inverter-BMS also
  works. Consistent with SAKO = Voltronic-family, RS485 path.

## Physical layer / cabling (top suspects for "battery not recognized")

**Authoritative for this pack = the Mason 280L 3.0 spec sheet.**
- **Official Mason 280L (3.0) pack specification** (docs/refs/
  mason280l-spec.txt): the CAN socket is CAN-only (**4 = CAN-H, 5 = CAN-L**,
  3/6 GND, 1/2/7/8 NC) and the **RS485 sockets carry A on pins 2/7, B on
  pins 1/8, GND 3/6, pins 4/5 dangling**, 19200 baud (native Modbus).
  On this pack pins 4/5 are CAN, never RS485 A/B.
- **For THIS pack (Mason 280L 3.0) the official spec is authoritative**:
  pins **4 = CAN-H, 5 = CAN-L** on the CAN socket (NOT RS485 A/B), and the
  RS485-to-inverter link is on the **RS485 sockets: 2/7 = A, 1/8 = B,
  3/6 = GND, 4/5 dangling**. So the inverter cable is **Seplos 2/7 → SAKO 5
  (A), Seplos 1/8 → SAKO 3 (B), GND 3/6 → SAKO 8**.
- ⚠️ The DIY Solar "BMS 4=A / 5=B" pinout (thread 47948, user sergea) is a
  **different/older Seplos RS485-board generation** — it does NOT apply to
  the Mason 280L 3.0. Don't use pins 4/5 for RS485 on this pack; on it 4/5
  are CAN. (Kept only as evidence that Seplos board pinouts vary by
  generation — always trust the specific pack's spec sheet.)
- That thread's other point still holds generally: on multi-socket Seplos
  boards the inverter port is one dedicated socket and its physical layer
  (CAN vs RS485) depends on the board variant; some RS485 packs have
  dedicated Voltronic firmware.
- Resolution: run `rsh identify` against **each socket** (it scans 9600 &
  19200 and both protocols) — the wire settles what the labels won't.
- **SAKO/Voltronic BMS RJ45**: RS485-B pin 3, RS485-A pin 5, GND 7/8.
- → A straight patch cable connects nothing useful. Crossover for the
  Mason 280L 3.0 RS485 socket → SAKO: **Seplos 2/7 (A) → SAKO 5 (A),
  Seplos 1/8 (B) → SAKO 3 (B), Seplos 3/6 (GND) → SAKO 7/8 (GND)**.
  (Note: A-to-A, B-to-B here because both sides name their own A/B — it is
  not a straight cable because the pin numbers differ, 2/7↔5 and 1/8↔3.)
- A/B polarity swap is harmless to try; 120 Ω termination only at the two
  physical bus ends; RS485 without bias floats when idle → garbage bytes.
- Inverter menu: try program 05 = `Li` → program 36 = `L01`, and `LIB`
  as alternative to `PYL`. Success indicator: battery icon/SOC follows BMS.

## SAKO ecosystem findings (from SAKO's own pairing video + Seplos compat list, 2026-09-22)

- SAKO's demo video (docs/refs/*.mp4) pairs the SUNON V with SAKO's
  **Li-Sun battery** by setting the *battery* (PACE-style LCD: PackSet →
  RS485 list) to **"RS485-SAKO"** — a house dialect. The battery firmware
  also offers RS485-PYLON / GROWATT / **Voltronic** / LXP / SVC /
  OLU VMII / Index12, so SUNON V's "PYL" plausibly maps to real
  Pylontech-ASCII, with SAKO/Voltronic as distinct alternatives. The
  inverter-side menu is never shown — pairing "just works" after the
  battery-side selection.
- Li-Sun battery ports (video): RJ45s labelled **BAT RS485B** (pack link),
  **RS232**, and **CAN / RS485A INVERTER** — the demo plugs the inverter
  cable into the INVERTER port; DIP block + status LEDs beside it.
- Seplos V16-era compatibility list (docs/refs/seplos-compat.txt): for
  genuine **Voltronic and MPP Solar** inverters, RS485 comm required
  **"Upgrade Voltronic Program V1.0"** — a dedicated BMS firmware, not
  auto-adaptation (only EA Sun's Voltronic-protocol inverters worked
  auto). **No SAKO row exists** — SAKO is unverified by Seplos.
- Seplos **BMS 3.0 firmware V1.5D [2025-11-18]** (docs/refs RAR):
  release note = "Added inverter communication protocols". Check whether
  the pack's BMS is hardware **V10 or V11** before flashing (wrong image
  doubles the displayed current).

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
