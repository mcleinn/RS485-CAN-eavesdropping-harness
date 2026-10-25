# USBCANFD DUAL — notes on the adapter we have

What this specific board is, how it presented itself on 2026-10-24, and what
its firmware does that the harness had to learn the hard way. **Measured**
means observed on this board with the harness; *documented* means taken
from the firmware author's or vendor's material (link list in
`docs/refs/MANUAL-DOWNLOADS.md`).

## 1. The board

- **USBCANFD DUAL**, SKU CAN-G473-DUAL, can-module.com (Oleksii Mamontov,
  Ukraine). Naked board, two isolated CAN FD channels, USB.
- STM32G473, 8 MHz crystal, **160 MHz CAN clock (measured** in BT_CONST**)**.
  Upstream Zephyr board name `usbcanfd_dual`.
- *Documented:* BOOT0 is shared with the CAN1 RX pin. The stock firmware
  disables the BOOT0 pin through option byte nSWBOOT0. If the board ever
  enumerates as **0483:DF11 "STM32 BOOTLOADER"** it is stuck in ROM DFU and
  needs that option byte cleared again (HUD ECU Hacker Firmware Programmer,
  or ST-LINK + CubeProgrammer). Do not flash the CANnectivity variant
  without one of those at hand.
- **Termination (measured):** two jumpers, labelled **"CAN1 120 Ohm"** and
  **"CAN2 120 Ohm"**, one per channel, **delivered closed**. Not switchable
  from software (no termination feature advertised, the termination
  requests are not implemented).
- LEDs: blink on plug-in, then steady blue = firmware up, channels idle.

## 2. Firmware on our board

Delivered with **"USBCANFD ver 2.5" = ElmuSoft Candlelight 2.5**, enumerating
as **VID:PID 1D50:606F**. Windows bound WinUSB by itself; no Zadig was used
and none must ever be (it would replace the binding with a wrong GUID).
The two alternatives from the listing, Slcan 2.5 (16D0:117E, COM port;
listen-only is `M1`+`O`, not Lawicel `L`) and CANnectivity (1209:CA01), are
not flashed and were not tried.

## 3. How it enumerates (measured, `rsh ports`)

```
can:gs:0.0  USB VID:PID=1D50:606F  gs_usb 606f channel 0 (Candlelight 2.5, fw 2491140, 160 MHz CAN clock)
can:gs:0.1  USB VID:PID=1D50:606F  gs_usb 606f channel 1
features: listen-only, loopback, one-shot, hw-timestamp, identify, fd, elmuesoft-2.5
```

- USB product string is literally `606f`; firmware word 2491140.
- Two channels on **one** vendor interface (legacy gs_usb mode): control
  requests take the channel in `wValue`, frames carry it in the channel
  byte. One process owns the whole device, so both channels must be opened
  from the same process (self-test, gateway, `hold --tap` do that).
- Feature word: listen-only, loopback, one-shot, hardware timestamps,
  identify, FD, and bit 25 (the ElmuSoft 2.5 marker). **Not advertised:**
  BERR reporting, GET_STATE, termination. `GET_STATE` (bRequest 14) stalls
  the pipe. Error frames are delivered regardless (section 5).
- BT_CONST per channel: tseg1 1–256, tseg2 1–128, sjw 1–128, brp 1–512.
  Resulting timings at 87.5 % sample point: 500 kbit/s = prescaler 2 and
  160 time quanta, 250 k = prescaler 4, 125 k = prescaler 8.
- Host frames are 24 bytes: the 4-byte hardware timestamp (µs, 32-bit,
  free-running) is present on every received frame and lands in the
  session as `ts_hw_us`.

## 4. What was verified on 2026-10-24

| Step | Result |
|---|---|
| open / close either channel, normal and listen-only | ok, about 0.1 s |
| bitrate change on an open channel (500 → 250 → 125 k, listen-only kept) | ok |
| `rsh identify --port can:gs:0 --passive-only`, nothing wired | SILENCE verdict, no error frames at any rate |
| lone transmit, nothing wired | **no ACK** reported within 1 ms (after the fix in section 5; before it, "sent") |
| **`rsh selftest can:gs:0.0 can:gs:0.1`**, channel 0 wired straight to channel 1 (H–H, L–L), both jumpers closed | **16/16 pass**: 11-bit, 29-bit and DLC-2 frames byte-exact both ways at 500/250/125 k, ~15 ms per check, listen-only side sees the frame and does not ACK |
| Pylontech-CAN battery simulator on channel 0, inverter simulator on channel 1, over the wire | 96/96 frames acknowledged and received, hardware timestamps on all, 0x305/0x307 keep-alive crosses back, extension frames and a CCL change decoded, zero error frames |
| no-ACK storm first, then the valid bus | storm 24/24 refused; recovered bus 96/96 acknowledged |

Sessions: `captures/20261024-170309-selftest.jsonl`,
`captures/20261024-171120-selftest.jsonl`.

## 5. The transmit echo is not an ACK on this firmware (measured)

The gs_usb convention is that a transmitted frame is echoed to the host
once the bus has acknowledged it. **Candlelight 2.5 on this board does not
do that:**

1. A frame written to the bulk OUT endpoint is **echoed back ~0.3 ms later
   even with no bus attached**. The echo only means "accepted by the CAN
   controller".
2. The missing acknowledgement shows up as **error frames**, and every one
   of them carries the **transmit error counter (TEC) in data[6]** and the
   receive counter in data[7]. The firmware reports on error-state changes
   and then about every 100 ms while a counter is non-zero. Observed
   sequence after a lone transmit:
   - ~0.15 ms after the echo: an ACK-class frame (id 0x20) or a
     **class-less frame (id 0x00)**, TEC = 8;
   - +100 ms: `0x24` (CRTL | ACK, data[1] = 0x20 tx-passive, TEC 128);
   - +500 ms: `0x25` (adds TX_TIMEOUT): the frame is **aborted**;
   - +600 ms: `0x04` (CRTL only).
   During those 500 ms the controller retransmits back-to-back: a
   listen-only peer on the same pair saw **15,557 copies of 30 frames in
   4 s**.
3. Recovery once a bus with an acknowledging node is present: `0x04` frames
   with data[1] = 0x08 (tx-warning) and the **TEC falling by 1 per
   successful frame**, data[1] = 0x40 when it drops below 96 (error-active
   again), then **class-less counter reports** every ~100 ms until the TEC
   reaches 0. About 32 good frames are needed after a storm.
4. Frames queued behind a failing frame produce **no error frame of their
   own**. Two interim rules failed on exactly this: "error frame within
   10 ms of the echo" reported 16 of 30 storm frames as acknowledged, and
   "any error frame keeps the channel failing" refused 58 of 96 good frames
   after a storm because it took the recovery reports as failures.

What the harness does now (`core/gs_usb.py`, `TxFailureTracker`):

- after the echo it waits 10 ms for a failure report;
- a failure report is a **rising TEC**, or an ACK/bus-error/protocol-class
  frame with a non-falling TEC; it puts the channel into a **failing** state
  in which every transmit raises "no ACK" immediately;
- a **falling TEC** clears the failing state at once (frames go through
  again); 0.6 s without a failure report clears it as a fallback;
- CRTL-only state frames and class-less reports with a falling TEC are not
  failures; a channel restart resets the counter baseline.

A replay of the measured sequence is `tests/test_canbus.py::test_tx_failure_tracker_follows_the_error_counter`.

Practical consequence: **a transmitter with only a listen-only tap on this
adapter is not a bus.** Nobody acknowledges, the transmitter storms, the tap
drowns in copies. To exercise channel 0 against channel 1, run something
that acknowledges on channel 1 (`rsh inverter-sim --can-profile ...`,
`rsh huawei sim`, or the self-test), not `rsh sniff`.

### 5.1 Two more measured facts (2026-10-25, real bus)

- **The transmit error counter survives a channel restart**, and the
  firmware's first counter report may arrive only after the first
  transmission of the new session. A tracker that assumes a zero baseline
  then reads the leftover value as a rise and declares the first frame
  unacknowledged while the peer is answering. The harness now learns the
  baseline from the first report (and allows 150 ms instead of 10 ms for a
  dead bus to show its ACK-class frame on that first frame).
- **Unacknowledged frames of a lone transmitter are reported with the
  identifier cut to 11 bits** (the low 11 bits, EFF flag missing): a
  control board alone on a channel with the adapter listen-only showed
  `0x0FE 01 01 .. C7 ED` 2791 times in 3 s instead of `0x108180FE`. With
  the channel in normal mode (acknowledging) the same frames arrive as
  proper 29-bit frames. Treat 11-bit ids on a bus that should carry 29-bit
  traffic as the signature of "nobody acknowledges", not as data.

## 6. Jumper setting per job

| Job | CAN1 jumper (channel 0) | CAN2 jumper (channel 1) |
|---|---|---|
| self-test, channel 0 wired straight to channel 1 | closed | closed |
| Huawei bench: channel 0 is the controller, the rectifier the only other node | closed | — |
| same bench with channel 1 as listen-only tap on the same pair | closed | **open** |
| tap on an already terminated bus (battery ↔ inverter, GX ↔ rectifiers) | **open** | **open** |
| gateway between two segments (adapter is an end of both) | closed | closed |

Ohmmeter across H and L, all nodes unpowered: ~60 Ω two terminators, ~120 Ω
one, ~40 Ω means a tap channel still has its jumper closed.

## 7. Symptoms seen or to expect on this board

| Symptom | Meaning |
|---|---|
| Device Manager shows "STM32 BOOTLOADER" 0483:DF11, `rsh ports` has no CAN section | stuck in DFU (section 1) |
| transmit returns "sent" instantly although nothing is connected | the echo-is-not-an-ACK behaviour; fixed in the harness (section 5), would reappear with a client that trusts the echo (python-can gs_usb does) |
| "no ACK … nobody on the bus / wrong bitrate / termination / only listen-only peers?" | one of those four; with one real node plus the adapter, the adapter channel's jumper must be closed |
| thousands of identical frames on a tap | transmitter without an acknowledging node (section 5) |
| `USBErrorPipe` on a control request | that request is not implemented by this firmware (GET_STATE, termination); the harness does not use them |
| libusb open errors on Windows | a Zadig binding; repair with the firmware author's programmer tool |

## 8. Not yet tried on this board

- Android (Pixel) via the `termux-usb` descriptor (`can:gs:.0`).
- Any real device: the Huawei R4850G2 is next (`docs/PHASE-B-RUNBOOK.md`).
- The Slcan 2.5 and CANnectivity firmwares.
- The ElmuSoft protocol extension of Candlelight 2.5 (one interface per
  channel, 2-byte frame headers, error-state request). It may provide a true
  transmit-complete report; worth a look only if the counter-based verdict
  ever proves insufficient.
