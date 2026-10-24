# CAN bus support — adapter, references, integration plan (rev 2, 2026-10-21)

Purpose: extend the harness from RS485 to CAN with the **same commands** — the
protocol stack is chosen by the kind of port that was opened, not by a
separate tool. First target: **control a Huawei R4850G2 rectifier**. Then the
battery dialects: Pylontech US5000 / US3000C, Victron CAN-bus BMS ("BMS-Can",
VE.Can), Dyness DL5.0F, LuxPower SNA PRO-EU 3-6.5K.

Rev 1 of this file (adapter shopping advice) is superseded by section 1; the
wrong claim that phones cannot use a USB CAN adapter without a kernel driver
is withdrawn (see 1.4).

All facts below come from the source-cited digests in `docs/refs/*/NOTES.md`
(git-ignored; rebuild with `docs/refs/MANUAL-DOWNLOADS.md`). Where sources
disagree, the plan says so and the code must not hard-code one reading.

## 1. The adapter we have: USBCANFD DUAL (can-module.com)

Bench findings on this board (enumeration, jumpers, the echo-is-not-an-ACK
behaviour of its firmware) are collected in `docs/ADAPTER-USBCANFD-DUAL.md`.

Identification (high confidence, `docs/refs/can-adapters/NOTES.md` §1): the
listing matches the **"USBCANFD DUAL"**, SKU CAN-G473-DUAL, by Oleksii
Mamontov, Ukraine: STM32G473, two FDCAN channels, isolated, upstream Zephyr
board `usbcanfd_dual`. The three firmwares in the listing are:

| listing name | what it is | USB VID:PID | host side |
|---|---|---|---|
| "USBCANFD ver 2.5" (default) | **ElmuSoft Candlelight 2.5** — gs_usb (candleLight) protocol, legacy-compatible, open source | 1D50:606F | Windows: WinUSB installs itself via MS OS descriptor (never Zadig); Linux: kernel `gs_usb` → can0/can1 |
| "COM (SLCAN 2.5)" | **ElmuSoft Slcan 2.5** — USB-CDC COM port, Lawicel/CANable ASCII superset | 16D0:117E | Windows: inbox usbser → COMx; Linux: ttyACMx |
| "CANnectivity" | Zephyr gs_usb firmware (vendor's own build) | 1209:CA01 | same as gs_usb above |

HUD ECU Hacker's "USBCANFD" protocol is therefore not proprietary: it is
gs_usb plus documented ElmuSoft extensions. Nothing blocks us.

### 1.1 Decision: stay on the default Candlelight 2.5 firmware, write our own small gs_usb client

Why not python-can's gs_usb backend (verified in python-can 4.6.1 / gs_usb 0.3):
it starts the device in normal mode (no listen-only parameter), hard-codes
channel 0, and is classic-CAN only. python-can's slcan backend has
`listen_only`, but it sends the Lawicel `L` command, which ElmuSoft's Slcan
2.5 does not define as listen-only (`M1` + `O` or `OS` is), and it reaches
channel 1 only.

Our own gs_usb client is ~250 lines (control requests HOST_FORMAT, BITTIMING,
MODE, DEVICE_CONFIG, BT_CONST; bulk IN 0x81 / OUT 0x02; 20/24-byte host
frames; reference headers in `docs/refs/can-adapters/`). It gives us:

- listen-only (mode flag bit 0) for every tap — the harness rule "never ACK
  on someone else's bus";
- both channels (wValue = channel, `channel` byte in the frame) → gateway on
  one adapter;
- the **same code on Windows, Linux and Android**: it sits on python-libusb1
  (`usb1`), which `core/ftdi_usb.py` already uses with the `termux-usb` file
  descriptor. gs_usb is a pure vendor-class USB protocol, so no kernel driver
  is needed anywhere.

python-can stays an **optional** backend (`PythonCanLink` wrapper) for
socketcan on the Pi, pcan/kvaser if ever needed, and its virtual bus.

### 1.2 Bench prerequisites (step 0, before any code is trusted)

1. Plug in, read the VID:PID: 1D50:606F = Candlelight 2.5 as expected;
   16D0:117E = Slcan; 1209:CA01 = CANnectivity; **0483:DF11 = stuck in the
   STM32 DFU bootloader** (this board's BOOT0 pin is CAN1 RX; the firmware
   must have cleared option byte nSWBOOT0 — see NOTES §2). Do not re-flash
   without the HUD ECU Hacker Firmware Programmer at hand.
2. Windows: WinUSB must appear by itself. `pip install libusb1` (python-libusb1
   wheels for Windows bundle libusb-1.0.dll — verify with
   `python -c "import usb1; usb1.USBContext()"`; if not, install the DLL per
   the libusb1 README).
3. On-board termination: **verified 2026-10-24** - two jumpers "CAN1 120 Ohm"
   and "CAN2 120 Ohm", one per channel, delivered closed (= terminated).
   Rule: closed only where the adapter channel is an END of the bus (the
   Huawei bench with the rectifier as the only other node; both channels of a
   gateway; both channels for the self-test). **Open** on a channel that taps
   an existing, already terminated bus (battery-inverter link, rectifier bus
   with the controller on channel 0), otherwise the bus sees a third 120 Ω
   and drops to 40 Ω.
4. Transceiver limit: ElmuSoft lists TJA1044 (5 Mbit/s); irrelevant for us
   (125–500 kbit/s classic CAN).

### 1.3 Raspberry Pi

Candlelight 2.5 → kernel `gs_usb` → `can0`, `can1`; configure with
`ip link set can0 up type can bitrate 500000 listen-only on`; our
`PythonCanLink(socketcan)` wrapper or the own gs_usb client (after
`rmmod gs_usb`/udev unbind) both work. No HAT needed any more.

### 1.4 Phones (Termux, no root)

Feasible with the gs_usb client over the `termux-usb` descriptor exactly like
the FTDI path (`USBContext(with_device_discovery=False).wrapSysDevice(fd)`).
Android grants the USB permission per session. The Pixel does OTG, the
Motorola did not enter host mode (earlier finding). Slcan over CDC would need
a user-space CDC-ACM driver — pointless, gs_usb is the Android path.

## 2. What the references establish (bus parameters the code will carry as data)

| device / dialect | bitrate | IDs | cadence / keep-alive | notes (source digest) |
|---|---|---|---|---|
| **Huawei R4850G2 / R48xx** | 125 kbit/s | 29-bit, fields proto(6) addr(7) cmd(8) dir(1) group(5) sw(1) cont(1) | PSU reverts to stored defaults ~60 s after the last valid frame (one source says ~30 s); implementations poll every 1–5 s | `refs/huawei/NOTES.md` |
| **Pylontech LV CAN** (US2000/3000C/5000) | 500 kbit/s | 11-bit 0x351 0x355 0x356 0x359 0x35C 0x35E (+0x35A, 0x370–0x379 extensions only after 0x305+0x307) | battery every 1–2 s; inverter 0x305 (all-zero) every 1 s optional for packs ≥ V2.4 | spec V2.0 2021 in `refs/pylontech/can/`; LV-HUB: ID + 0x1000·address |
| **Victron CAN-bus BMS** | 500 kbit/s ("BMS-Can"), 250 kbit/s profile selectable | 11-bit; GX requires 0x351 0x355 0x356 0x35A (+0x35E/0x35F); GX sends 0x305 and 0x307 (`12 34 56 78 'V' 'I' 'C' 00`) every 1 s | stops charge/discharge if 0x351 is missing for 3 s | VE.Can proper = 29-bit 250 kbit/s, must not share a bus with BMS-Can; GX RJ45 pins 7/8 |
| **Dyness DL5.0F** | 500 kbit/s (DIP #4 OFF; ON = 250 k) | Pylontech set, 0x35E = `DYNESS-L`; plus native extended 0x18BB000x / 0x18AA01xx (cells, temps, limits) seen on a Junior Box | as Pylontech | CAN IN pins 4/5 (EXT CAN), 7/8 battery cascade, 1/2 RS485; whether its 0x35x frames are really 29-bit is **unverified** |
| **LuxPower SNA PRO-EU 3-6.5K** | not stated; 500 kbit/s working assumption | battery codes 0 Standard / 2 Pylon / 6 "Luxpower protocol" / 8 Dyness; "Luxpower protocol" = Pylontech set + 0x379 capacity frame (bytes 0-1 Ah LE) per open-source converters | — | no official LUX CAN spec exists publicly; battery RJ45 pins 4/5 CAN, 1/2 RS485 = pin-compatible with the DL5.0F CAN IN (straight cable) |

Pinouts for the kit: Pylontech battery CAN port pins 4/5 (+6 GND on US5000);
Victron GX pins 7/8; Huawei edge connector: official tables differ by manual
revision, physically the CAN pair is top/white = CAN-L, bottom/black = CAN-H,
and both slot-detect pins must go to OUT- to enable the output.

## 3. Integration design — same commands, protocol by port kind

### 3.1 Port specs and recognition (`rsh ports`, TUI port list, `auto`)

Today: `COM7`, `/dev/ttyUSB0`, `usb:A` (FTDI over libusb). New:

| spec | opens |
|---|---|
| `can:gs:0` / `can:gs:0.1` | our gs_usb client, device index 0, channel 0 / 1 (Android: `can:gs:<fd>.1` via `RSH_USB_FD` like `usb:`) |
| `can:socketcan:can0` | python-can socketcan (Pi) |
| `can:slcan:COM9` | python-can slcan with an `M1` pre-command (fallback if the Slcan firmware is ever flashed) |
| `can:virtual:<name>` | in-process virtual CAN bus (tests, `demo`) |

`rsh ports` adds a CAN section: gs_usb devices by VID:PID (1D50:606F,
1209:CA01) with their channel count from DEVICE_CONFIG (`icount`), SLCAN COM
ports by 16D0:117E, socketcan interfaces on Linux (`/sys/class/net/*/type ==
280`). The TUI's adapter ranking appends CAN channels, so an empty PORT
("auto") tries RS485 channels and CAN channels alike.

**Dispatch rule:** `open_link()` returns a `CanLink` for `can:` specs. Every
command checks `isinstance(link, CanLink)` once and runs its CAN variant.
Users keep typing `rsh identify --port <x>`; what differs is the port.

### 3.2 Core (`rs485harness/core/canbus.py`, `core/gs_usb.py`)

- `CanMsg(ts, can_id, ext, rtr, err, data, channel)`; `CanLink` with
  `recv(timeout)`, `send(msg)`, `set_bitrate()`, `listen_only` flag, `name`,
  `bitrate`, `close()`. Bitrate is *not* a baud: identify sweeps 500k → 250k →
  125k, reopening the link in listen-only; a wrong rate shows as error
  frames / zero frames, never as garbled bytes (there is no `raw` kind on CAN).
- `GsUsbLink` (own client on `usb1`): BT_CONST → compute bit timing for the
  requested rate from the device's clock (no hard-coded 48/160 MHz), MODE
  start with LISTEN_ONLY when asked, HW timestamps if offered, reader thread
  with the same reopen/retry pattern as `ftdi_usb.py`.
- `PythonCanLink` wraps any `can.Bus`.
- `VirtualCanBus`: N endpoints, broadcast delivery, listen-only endpoints
  count as non-ACKing; a frame with no ACKing peer is reported back to the
  sender as "no ACK" so "nobody there" tests exist (mirrors the loopback/
  virtual-analyzer idea of the RS485 side).
- **Session records**: `kind="can"`, `hex` = data bytes, plus `id`
  (`"0x351"` / `"0x108040FE"`), `ext`, `dlc`, `chan`, `dir` rx/tx, `flow`
  (`BMS->INV`, `INV->BMS`, `PSU->CTL`, `CTL->PSU`), `decoded` from the dialect
  decoder. `read_session`, `mark`, `diagnose`, `timeline` consume them
  unchanged; `rsh sniff --service` still records the Seplos RS485 service
  socket into the same file on the same clock — a serial link and a CAN link
  in one process is the normal case, not a special one.

### 3.3 Protocol modules

- `protocols/pylontech_can/frames.py`: ID table and struct layouts for 0x351
  (CVL u16 0.1 V, CCL s16 0.1 A, DCL s16 0.1 A, DVL), 0x355 (SOC, SOH, optional
  hi-res SOC), 0x356 (V 0.01 V, I 0.1 A signed, T 0.1 °C), 0x359 (protection/
  alarm bytes, module count, 'P','N'), 0x35A (Victron 2-bit alarm pairs), 0x35C
  (request flags byte 0), 0x35E (8-char brand), 0x35F, 0x370–0x379 (names,
  modules, min/max cell mV and Kelvin temps, ids, energy, installed Ah), 0x305,
  0x307, Dyness native 0x18BB/0x18AA frames. `decode(id, data) -> dict` with
  units; `encode(state) -> frames` for the simulator. Known wire quirks are
  data, not special cases: DLC 2 on 0x35C from real packs, `"PYLON   "`
  padding, signedness disagreements for 0x356 voltage and 0x351 DVL recorded
  as decoder notes.
- `protocols/pylontech_can/dialects.py` (same idea as the RS485
  `DialectProfile`): `pylontech-lv`, `victron`, `dyness-l`, `luxpower`,
  `seplos-can` — bitrate, mandatory IDs, brand string, whether 0x305/0x307
  unlock extensions, keep-alive expectation, extended-ID quirks, 0x379.
  `guess_dialect(ids_seen, brand_string)` feeds identify's verdicts.
- `protocols/huawei_r48xx/`: `ids.py` (29-bit field pack/unpack as in
  NOTES §3), `regs.py` (status registers 0x70–0x83 with /1024 scales, set
  registers 0x00–0x04/0x32/0x34, info 0x50, e-label 0xD2, unsolicited 0x11),
  `decode_burst()`, `encode_set(reg, value)`. **Current scale is a named
  policy, not a constant**: `ratio1024` (A / nominal · 1024, nominal from
  info reg 0x001 — verified on three models by one source), `x20`, `x30`
  (other sources). The tool records which one the unit obeys (3.6) and
  refuses to send a current setpoint until that has been established.

### 3.4 Timeline / analysis reuse

Add `_extract_can()` beside the RS485 extractor: 0x351 → cvl/ccl/dcl; 0x355 →
soc/soh; 0x356 → pack_v/pack_i/temp; 0x359 / 0x35A → alarms, protections,
ov_warn; 0x35C → charge_enable, discharge_enable, charge_immediately,
full_charge_request; 0x373 → max_cell/min_cell. Everything downstream
(event log, timing stats, findings such as "current exceeds CCL" or
"oscillation near top of charge", CSV, PNG, markers) runs unchanged. The
Huawei side maps 0x75/0x81/0x76 → psu_v/psu_i/psu_limit so a charge driven
by the rectifier plots on the same axes as one driven by the inverter.

### 3.5 Command-by-command mapping

| command | RS485 behaviour today | CAN behaviour |
|---|---|---|
| `ports` | COM/usb: list | + gs_usb devices and channels, slcan COM, socketcan |
| `identify` | listen per baud, then probe VER×ADR×CID2 | listen-only per bitrate (500/250/125 k); report IDs, DLCs, periods, brand string, 29-bit vs 11-bit; verdicts: "Pylontech-CAN battery broadcasting, no inverter keep-alive seen", "Victron GX present (0x305+0x307)", "Huawei R48xx PSU addr n (0x1001117E every ~377 ms)", "wrong bitrate (error frames)". Active phase only when nothing else is talking: battery → send 0x305 (+0x307) to unlock extension frames; Huawei → one 0x40 status request. Same rule as on RS485: never become a second master |
| `discover` | every command classified, state snapshot, quirks | inventory of every ID with decoded content and period; which optional IDs are present; quirks (DLC 2, padding, extended IDs); Huawei: status burst + info (nominal current, serial, versions, slot id) + e-label |
| `dump` | per-unit cells/temps/limits/serial | battery snapshot from broadcasts (+ Victron extension frames if unlocked; Dyness native frames for cells) → JSON; Huawei: status + info |
| `soc` | one all-packs poll → table | table from 0x355/0x356 per LV-HUB string offset |
| `sniff` | tap 1–2 channels, `--quiet` CHANGE lines, `--service` | identical, listen-only; LiveState keyed on the extracted fields; `--service` unchanged |
| `send` / `script` | one frame / JSON scenario | `--id 0x108180FE --ext --data "01 00 00 00 00 00 D2 00"` with a reply window; scenarios gain `can` steps |
| `battery-sim` | ASCII responder | periodic CAN emitter from `BatteryState` + dialect profile (bench substitute for the LuxPower side) |
| `inverter-sim` | poller | sends 0x305/0x307 keep-alive, logs the battery |
| `bridge` | byte pass-through / frame translate / policy | two CAN channels, relay by per-dialect direction table, policy engine on 0x351 only (CCL/CVL downwards only, 0x359/0x35A/0x35C untouched, reset on silence, two-record audit); store-and-forward adds ~1 ms, acceptable at 1 s broadcast cadence |
| `selftest` | ch1↔ch2 cross-wired | channel 0 ↔ channel 1 on one terminated bus; checks ACK and listen-only really does not ACK |
| `timeline`, `diagnose`, `mark`, `tui` | — | unchanged apart from the extractor and the port list |
| **new `rsh huawei`** | — | charger control, see 3.6 |

### 3.6 First test: Huawei R4850G2 control (`rsh huawei`)

Wiring: adapter channel 0 ↔ rectifier CAN pair (CAN-L top/white, CAN-H
bottom/black), 120 Ω across the pair at the adapter, both slot-detect pins to
OUT-, AC on, a load or the battery on the output. Proof of wiring before
anything is sent: the PSU's unsolicited `0x1001117E` every ~377 ms must show
up in `rsh identify --port can:gs:0 --passive-only`.

Sub-commands, all read-only unless stated:

1. `status` — one 0x40 request, decoded burst (input V/f/I/W, output V/I/W,
   efficiency, temperatures, 0x76 limit point, 0x83 status bits).
2. `info` — 0x50 + 0xD2: nominal current, serial, firmware, slot id, e-label.
3. `calibrate-current` (needs a load) — set a small on-line current limit
   under each scale policy in turn, read 0x76 and the measured 0x81 current,
   write the scale the unit actually obeys into `.rsh-config.json`. Until this
   has been run, `set --current` refuses.
4. `set --voltage V [--current A] [--persist] [--apply]` — dry run by default
   (prints the frames); `--apply` sends to on-line registers 0x00/0x03;
   `--persist` is a separate explicit flag for 0x01/0x04 (non-volatile);
   checks the ack (byte 0 = 0x21 → error, reported verbatim); then verifies
   by **measurement** (0x75 output voltage within tolerance after settling)
   because set values cannot be read back.
5. `hold --voltage V --current A --duration S` — the keep-alive loop:
   status poll every 1 s, setpoint re-sent every 5 s, CHANGE lines + session
   log like `sniff --quiet`. Ctrl+C or any error stops sending; the PSU then
   reverts to its stored defaults within ~60 s by itself.

Safety invariants, carried over from the RS485 bridge:

- Hard caps in `.rsh-config.json` (voltage, current, per battery chemistry;
  default 55.2 V / 0 A until set). A setpoint above a cap is refused, never
  clamped silently.
- Step 0 of every bench session: program **safe off-line defaults** once
  (`set --persist`) so that a crash, a cable fault or a stopped script leaves
  the PSU at a harmless voltage and current limit.
- Every transmitted frame is logged with its ack and the audit (`policy`,
  `scale`, `cap`), the same two-record pattern as the bridge.
- No setpoint is ever derived from stale data: a later "charge by BMS limits"
  mode (Seplos service socket CCL/CVL → rectifier setpoints) stops re-sending
  when the BMS data is older than its loss timeout; the PSU's own fallback
  then takes over. That mode is the RS485 policy engine with a different
  output; it is not part of the first test.

Expected first-test outcome: identify → "Huawei R48xx addr 1 at 125 k";
status/info decode without "unknown register" lines; a dry-run set prints
exactly the frames the sources show (`0x108180FE 01 00 00 00 00 00 D2 00`
= 52.5 V); `--apply` of a voltage inside the caps changes 0x75 as measured;
calibrate-current settles the scale question for this unit.

### 3.7 Hardware-free tests (same discipline as the rest of the suite)

- gs_usb frame pack/unpack, bit-timing from BT_CONST, mode flags.
- Virtual CAN bus: listen-only endpoints do not ACK; lone sender gets "no ACK".
- CAN battery simulator ↔ identify/discover/dump/sniff/timeline round-trips
  for each dialect profile; DLC-2 and padding quirks; LV-HUB offsets.
- Huawei PSU simulator: answers 0x40/0x50/0xD2, acks sets with range checks
  (error 0x21 outside 41.0–58.6 V / 48.0–58.4 V default), applies the
  configured scale policy, reverts to defaults after a simulated 60 s silence.
  Tests for every refusal path of `rsh huawei` (cap, uncalibrated current,
  stale data) and for the keep-alive cadence.
- Bridge/policy on 0x351: downwards-only, locked IDs, reset on silence.

## 4. Order of work and effort

| step | content | effort | needs |
|---|---|---|---|
| 0 | bench prerequisites 1.2 (firmware id, WinUSB, libusb1 on Windows) | 1 h | adapter |
| 1 | `core/canbus.py`, `core/gs_usb.py`, virtual bus, session records, `ports`, `open_link` dispatch | 1 day | — |
| 2 | Huawei protocol + PSU simulator + `rsh huawei` + identify/discover for the PSU → **first bench test** | 1 day | R4850G2, 120 Ω, load |
| 3 | Pylontech/Victron/Dyness/LuxPower decoders, dialect profiles, CAN battery sim, identify/discover/dump/sniff/timeline | 1 day | — (bench: Pylontech rack, Dyness) |
| 4 | gateway (`bridge`) on both channels + policy on 0x351 | 1 day | two terminated segments |
| 5 | Android: gs_usb over the termux-usb descriptor, TUI port list | ½ day | Pixel |

Dependencies: `libusb1` becomes a core dependency (it already serves the
Android FTDI path); `python-can` is an optional extra (`[can-extra]`) for
socketcan/slcan/virtual. The references are complete; nothing in steps 1–3
waits on documentation.

## 5. Electrical / safety (unchanged from rev 1)

- Tap in parallel anywhere on the bus, no extra termination (the bus already
  has 120 Ω at both ends), two wires plus ground, **listen-only**.
- A gateway splits the bus into two terminated segments and ACKs on both.
- The Huawei bench is the exception: the rectifier is the only other node,
  so the adapter end needs the 120 Ω.
- Keep VE.Can (29-bit, 250 k) and BMS-Can (11-bit, 500 k) on separate buses;
  on a Cerbo GX the port profile decides which one a socket is.

## 6. Status

| step | state | where |
|---|---|---|
| 1 core | **done 2026-10-21** (`132a3e1`): `core/canbus.py`, `core/gs_usb.py`, virtual bus with ACK model, `can:` specs in `open_link`, `rsh ports` CAN section, session records `kind="can"`, 28 tests | `tests/test_canbus.py` |
| 2 Huawei | **done 2026-10-21** (code; bench test pending): `protocols/huawei_r48xx/{ids,regs}.py`, `simulators/huawei_psu_sim.py`, `analyzer/huawei.py`, `analyzer/identify_can.py` (identify's CAN branch), `rsh huawei status/info/calibrate-current/set/hold/caps/sim`, 20 tests | `tests/test_huawei.py` |
| 3 battery dialects | **done 2026-10-22** (code; bench pending): `protocols/pylontech_can/{frames,dialects}.py` (Pylontech LV / Victron / Dyness-L incl. native 0x18BB/0x18AA / LuxPower 0x379 / Seplos bitmap, LV-HUB offsets, both readings of every disputed field), `simulators/can_battery_sim.py` (battery + inverter/GX), `analyzer/can_battery.py` (inventory, dialect ranking, unlock rule, snapshot), identify/discover/dump/soc/sniff/timeline on CAN ports, 20 tests | `tests/test_pylontech_can.py` |
| 4 gateway | **done 2026-10-22** (code; bench pending): `analyzer/can_bridge.py` (`CanBridge` relay both ways, `CanPolicyEngine` on 0x351 with the RS485 invariants, `run_can_selftest`), `rsh bridge`/`rsh selftest` on CAN ports, 7 tests | `tests/test_can_bridge.py` |
| 5 Android | code path exists (gs_usb client over the termux-usb fd: `can:gs:.0` / `can:gs:.1`, `rsh ports` lists the channels); untested on the Pixel | |
| Huawei gateway | **done 2026-10-25** (code; bench pending): `analyzer/huawei_gateway.py` (`HuaweiGateway`: our setpoints to the module, the control board's requests forwarded, its writes blocked + acked, audit records), `simulators/huawei_frontend_sim.py` (the board as measured), `rsh huawei gateway` / `frontend-sim`, 4 tests | `tests/test_huawei_gateway.py` |
| adapter first contact | **done 2026-10-24** on the notebook: `rsh ports` lists both channels (1D50:606F, fw 2491140, 160 MHz, features listen-only/loopback/one-shot/hw-timestamp/identify/fd/elmuesoft-2.5; no berr-reporting or get-state flag, GET_STATE -> pipe error), `rsh identify --passive-only` gives SILENCE with nothing wired, **`rsh selftest can:gs:0.0 can:gs:0.1` PASSES** (ch0-ch1 straight, both 120 Ohm jumpers closed): byte-exact both ways at 500/250/125 k, 11/29-bit, DLC 2, listen-only side sees the frame and does not ACK; HW timestamps on every received frame; battery-sim <-> inverter-sim over the real wire (96/96 frames, unlock, CCL change). Firmware finding: the tx echo is NOT an ACK (comes ~0.3 ms after the write with no bus); the verdict is built from the transmit error counter in the error frames (`TxFailureTracker` in core/gs_usb.py, see its docstring). A transmitter with only a listen-only tap is not a valid bus: nobody ACKs and the controller retransmits back-to-back for 500 ms (the tap sees thousands of copies) - use `inverter-sim` on the other channel instead. | `tests/test_canbus.py` (tracker), captures/20261024-*-selftest |
| Phase B additions | **done 2026-10-22** (code; bench pending) - brief `Harness_Additions_rsh_huawei.md`: A5 simulator switches (`polls_refresh_timeout`, `reject_zero_default_current`, `on_ac_loss`, `power_cycle()`, `serial_hex`, negotiation, `set_addr()`), A1 `hold --poll-only`, A2 `hold --silence-after --tap`, A3 per-serial config (`huawei.units`/`addr_map`), identity check before current setpoints, `--broadcast --expect-acks`, `target` audit field, A4 address watch in `sniff` (`analyzer/huawei_addr.py`, `kind="addr_event"`), A6 `--label` + `summary` records, `docs/PHASE-B-RUNBOOK.md`; 16 new tests; commits `c13b4d9` (A1/A2/A3/A5/A6), `4fc6263` (A4), docs in the following commit | `tests/test_huawei.py`, `tests/test_huawei_addr.py` |

Finding from the simulator work (3.6 calibrate-current): on a 50-52 A unit
the `ratio1024` scale is A x 1024/52 = A x 19.7, i.e. the same as `x20`
within 1.5 % - that is why the sources disagree without anyone being
wrong. `calibrate-current` therefore reports the *equivalence class* of
scales the unit cannot tell apart; only `x30` (1.5x off on such a unit)
is really distinguishable. On a 35 A unit the roles swap (ratio1024 =
x29.3 = `x30`).

Bench run order for step 2 (unchanged from 3.6): `rsh ports` ->
`rsh identify --port can:gs:0 --passive-only` (expect the 0x1001117E
heartbeat) -> `rsh huawei caps ...` -> `rsh huawei status` -> `rsh huawei
info` -> `rsh huawei set --voltage V --persist --apply` (safe off-line
defaults first) -> `calibrate-current` with a load -> `set`/`hold`.
