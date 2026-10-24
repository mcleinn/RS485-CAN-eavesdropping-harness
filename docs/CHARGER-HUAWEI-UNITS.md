# Our Huawei-based chargers — technical notes on the units we have

Everything known about the two chargers bought for the generator-charging
project, as distinct from the general Huawei R48xx protocol (that lives in
the local `docs/refs/huawei/NOTES.md` and in `docs/CAN-PLAN.md` 3.6).
**Measured** = observed on our unit with the harness on 2026-10-24;
*stated* = seller's listing or the screen; *inferred* = conclusion from
measurements, marked as such. Bench procedure and the dated log of what was
done: `docs/PHASE-B-RUNBOOK.md`.

## 1. What was bought

*Stated by the seller* (Ukrainian reseller, prom.ua listing, 2026-10):

| | |
|---|---|
| Product | "Charger 42–58.8 V, 4000 W" for 48 V hybrid inverters (Deye, Solis, LuxPower named), generator charging |
| Inside | "pulse server rectifiers, telecom grade", Huawei industrial reliability |
| Output | 42–58.8 V adjustable, up to 70 A, up to 4000 W per block; blocks can be paralleled |
| Input | 85–300 V AC |
| Connector | XT90 on the output by default |
| Intended control | set voltage and current once; switch charging on/off on the AC side through a dry contact and a contactor driven by the inverter's generator port |
| Chemistries listed | LiFePO4 12S–16S, NMC 10S–14S, LTO 14S–20S, Na-ion 12S–16S |

Two units were bought. Only unit 1 has been on the bench so far.

## 2. The rectifier module inside

- *Inferred from the listing's 70 A / 4000 W:* an **R4875G1** (4 kW, 75 A)
  rather than the R4850G2 (3 kW, ~52 A) assumed in the Phase B documents.
  Same protocol family; the difference matters for the current-scale
  policies (`ratio1024` needs the nominal current) and for the caps.
  **To be settled by `rsh huawei info`** (model from the e-label, nominal
  current from info register 0x0001) as soon as the harness may transmit on
  this bus (see section 4).
- **Measured** on unit 1, 2026-10-24, listen-only at 125 kbit/s:
  - CAN address 1 (protocol 0x21 identifiers with address field 1),
    unsolicited `0x1001117E` every 0.377 s, `0x108111FE` every 0.377 s,
    `0x100011FE` every 3 s. No error frames at 125 k; stuff-error frames at
    500 k and 250 k (wrong rate, as expected).
  - Status registers while idle under the front-end: AC in 226 V, 50.1 Hz,
    0 A, 0 W; DC out **0 V, 0 A**, 0 W; limit register 0x76 = raw 70
    (= echo of the raw online-current write, see section 3); efficiency
    register 0; inlet 20.0 °C, output 21.0 °C (the screen showed 23/23 a
    little later); **32,085 operating hours**; status word 0x83 bytes 4–7
    `08 00 00 04`, bytes 2–5 `00 00 08 00`, bits not described by any
    source.
  - Reply timing over 24 requests: request → first frame 2.4 / 6.1 /
    8.1 ms (min / median / max); burst of 14 frames in 25.8 ms.
- The red indicator next to the screen was lit while the output was 0 V
  (fault or output-off indication of the module).
- Serial number, barcode, firmware versions, slot id: not yet read (needs
  an info request).

## 3. The add-on control board ("front-end")

### 3.1 What it is

- **Measured:** an **ESP32-class controller** (Bluetooth address
  80:B5:4E:E6:DD:FA is in Espressif's block) with a colour TFT in the
  black front panel, Bluetooth LE and WiFi (icons on screen). The screen
  did not react to touch in any of the user's attempts: treat it as
  display-only. A personal photo is shown at power-up: the builder's own
  firmware image.
- *Inferred:* a hand-built conversion of the kind sold by individual
  builders on Xianyu ("Huawei big cannon adjustable power supply": rewound
  or re-fed module in a machined or original housing, touch/BT board,
  full-charge shut-off). Ours is the 48 V-class variant (listing: 42–58.8 V),
  not the 71–101 V e-scooter variant. A sticker on the case reads
  **"password 123456"**.
- No maker, model, documentation, app name or firmware source is known.
  Web searches for the screen's UI wording, the Bluetooth name and the
  frame header found nothing.

### 3.2 Screen (photo 2026-10-24 18:35)

| Field (Chinese) | Meaning | Value seen |
|---|---|---|
| 待机 | standby (mode pill, orange) | standby |
| 我是最帅 | device nickname ("I am the most handsome", user-settable) | — |
| WiFi / Bluetooth icons, 00:00 | link status, charge timer | — |
| 设置电压 | set voltage | 48.0 V |
| 设置电流 | set current | 15.0 A |
| 输出电压 / 输出电流 | output voltage / current | 00.0 V / 00.0 A |
| 功率 / 容量 | power / delivered capacity | 0 W / 0.00 Ah |
| 进风温度 / 输出温度 | inlet air / output temperature | 23.0 / 23.0 |

The temperatures are the module's registers 0x80 and 0x7F, so the screen
is fed from the status bursts the board requests over CAN.

### 3.3 What it does on the CAN bus (measured, 15 s capture)

The board is a **full CAN master**. Every **0.62 s**:

1. `0x108140FE` status request to address 1 (answered with the 14-frame
   burst);
2. then within 10 ms, five writes to `0x108180FE`, all acked ok:
   - `01 01 … C7 ED` default voltage, raw 51181 = **49.98 V** (board setting
     50.0 V; it encodes V × 1023.6);
   - `01 04 … 00 D2` default current, **raw 210**;
   - `01 00 … BF EE` online voltage, raw 49134 = **47.98 V** (setting
     48.0 V), sent twice per cycle;
   - `01 32 00 00 …` standby = 0 (**on**);
   - `01 03 … 00 46` online current, **raw 70**.
3. Every **3 s** a broadcast `0x108081FE 01 13 00 01 00 00 00 00` (command
   0x81, register 0x13). The only other known sighting is a power-up
   candump in one community repository; purpose undocumented.

Consequences:

- The screen shows 15.0 A set, the bus carries raw 70. **In standby the
  board writes a reduced current** (3.5 A under the ×20 reading, 3.55 A
  under ratio1024 on a 52 A unit, 5.1 A on a 75 A unit, 2.33 A under ×30).
  Capturing one cycle in run mode gives the pair (screen value, raw value)
  that fixes the board's current encoding.
- The module's **persistent defaults are rewritten every 0.6 s** to 50.0 V
  and raw 210 (10.5 A under ×20). B3.0's zero-current default cannot hold
  while the board is attached.
- Output 0 V with online 48 V requested and standby = 0 on the bus: the
  board gates the output by other means (output relay or the slot-detect
  pins), not through the CAN standby register. The seller's "full-charge
  shut-off" works the same way.
- With the board transmitting, **no second master may command the module**
  (the harness refused to go active, by rule). The Phase B active tests and
  the Cerbo GX driver need the board silent.

### 3.4 Bluetooth LE interface (read-only probe, 2026-10-24)

| | |
|---|---|
| Name | `HW充电器` ("HW charger") |
| Address | 80:B5:4E:E6:DD:FA (Espressif) |
| Advertising | service UUID FFE0; manufacturer data id 0x5748 ("HW") with payload "CHARGE" |
| Services | Generic Access (device name, appearance 0), Generic Attribute, **FFE0 vendor**: FFE1 notify + read + write, FFE2 write |
| Pairing | none, no PIN; MTU 512. System "pair" dialogs on Android and Windows therefore do nothing; an app connects and talks over FFE1 |
| Traffic | nothing unsolicited in 20 s of listening; FFE1 reads back a constant frame |
| Frame | `B5 5B 07 00 F1 01 F2` = header `B5 5B`, 16-bit little-endian total length 7, command 0xF1, data 0x01, 8-bit sum checksum (F1 + 01 = F2) |

How to connect: `pip install bleak`, then
`python -X utf8 tools/ble_inspect.py --name HW` (or `--address
80:B5:4E:E6:DD:FA`). That tool is **read-only**: it scans, connects without
pairing, lists services and characteristics, reads what is readable and
prints every notification for `--listen` seconds. To *talk* to the board
(command the FFE2 / reply-FFE1 path in section 3.4.1) use the probe tools
`tools/ble_probe.py` … `tools/ble_probe6.py` — `ble_probe6.py` is the most
recent (reads the 0xF9 block before/after each candidate command and reports
which slot changed). On Android, "nRF Connect" shows the same (connect,
expand FFE0, enable notifications on FFE1, write to FFE2).

Note the board advertises by *name* `HW充电器` in the charger state and
`123456:` in the "Connect to WIFI" state (section 3.4.2); scan by the
constant MAC `80:B5:4E:E6:DD:FA` so it works in either state.

*Inferred:* request/answer protocol of the builder's own app; "123456" is
the login sent inside it. **No writes have been attempted**: an unknown
command could change setpoints or the run state of a 4 kW charger. The way
to the commands is the app (name or APK from the seller), then either a
look at the APK or an Android Bluetooth HCI snoop log of one session.

#### 3.4.1 Command protocol (reverse-engineered over BLE, 2026-10-24)

**Measured** from live writes to FFE2 / replies on FFE1 (bench, unloaded):

- **Frame format — confirmed.** All traffic both ways is
  `B5 5B | LEN(16-bit LE, total frame bytes) | CMD(1) | DATA(n) | SUM(1)`
  with `SUM = (CMD + DATA[0..n-1]) mod 256`. Verified against the board's own
  frames (`F1 01 F2`, `F1 00 F1`, `F1 02 F3`, `F1 FF F0`) and the default
  reply `CC 00 00 00 00 CC`.
- **FFE1 is read/write/notify two-way; FFE2 (write-only) is the command path.**
  Writes to FFE1 produce no board response (only the default frame); the first
  meaningful replies appeared once commands were sent to FFE2. FFE1 reads
  return whatever the *app* last read (a stale mailbox, e.g. our own frames) —
  ignore FFE1 reads as data.
- **Board replies arrive as FFE1 notifications**, possibly split into MTU
  chunks; reassemble on `B5 5B` + LEN.
- **Command map so far** (reply = anything other than the default `CC` no-op):

  | Cmd | Reply (hex, data part) | Reading |
  |---|---|---|
  | 0x01 | `0xF9` frame, 64 B = **16 LE float32** | config/status block |
  | 0x05 | `0x05 01` | 1-byte ack OK |
  | 0x11 | `0x00`-frame, 52 B (float32 pair + limits) **then** 0xF9 | status page A |
  | 0x12 | same shape, two counters differ **then** 0xF9 | status page B |
  | 0x15 | `0x15 01` | 1-byte ack OK |
  | all others tested, 0x02–0x04, 0x06–0x10, 0x13, 0x14, 0x16–0xFF | default `CC` frame or none | unknown / needs other payload or auth |

- **0xF9 config block decoded** (LE float32 slots 0–15):
  slot 0 = **59.0** max output V | slot 1 = **42.0** min output V |
  slot 2 = **75.0** max A (=> points to R4875G1, 75 A) | slot 4 = 1 (state bit?) |
  slot 5 = **50.0** default V (matches board's CAN default-voltage writes) |
  slot 6 = **15.0** default A (matches 设置电流 15.0 A on the screen) |
  slot 7 = 35.0, slot 8 = 55.0 (limits, meaning unconfirmed) |
  slots 9–12 = 0 | slot 13 = **48.0** online V (matches CAN online-voltage
  writes of 47.98 V) | slot 14 = **15.0** online A | slot 15 = 0.
- **0x11/0x12 status pages**: two slots are float32 counters that increase by
  ~0.1 between reads seconds apart (uptime-style), plus a fixed 25.0 float
  (limit). Exact field meanings unconfirmed.
- **Setpoints could not be moved from BLE**: writing the expected values
  (48.0 f, 15.0 f) over every tested command left the 0xF9 block unchanged.
  The setters most likely need the login first.
- **Password / login**: not yet located in the command map. Sticker says
  `123456`. *Inferred:* the login is a frame whose data is the password,
  and it gates the setpoint/write commands; the 0xF9/0x11/0x12 status reads
  work unauthenticated.
- **Device name tracks stored config + state.** It advertised `HW充电器`
  in the charger state, then `123456:` once the "Connect to WIFI" loop
  began. The switch appears to be a **stored name/label we set during
  session 1** (F9 slot 4 went from `01 00 00 00` to ASCII `31 32 33 34`
  while the board was still named `HW充电器`, right after `F1 …"12345"` /
  `B8 …"123456"` writes) — *inferred*, not yet isolated to a single
  command. After the final reboot it advertised the factory name `ESP32`
  (stored name/SSID may have been reset). Useful side-channel: the name
  tells you the board's state without the screen, but remember it can
  reflect a value *we* wrote, not only a firmware state.

#### 3.4.2 The "Connect to WIFI" state, and how to satisfy it

**Measured** (2026-10-24, with help from the AP host):

- The board's **WiFi MAC is `80:B5:4E:E6:DD:F8`** — exactly BLE MAC `…FA`
  minus 2, standard for ESP32; it is the same physical radio, no second chip.
- While it sits on the "Connect to WIFI" screen it **probes for the SSID
  `123456J`** (captured as 802.11 Probe Requests from `…F8` on 2.4 GHz).
  That SSID is from the board's previous configuration.
- It will join **any** 2.4 GHz AP named `123456J` in Open mode; it does not
  require a password and does not bring up an AP of its own. On join it
  grabs the DHCP lease (`192.168.251.15` on our AP, hostname `espressif`).
- After joining it did **not** open any TCP/UDP service we could see, and
  with a proper gateway+DNS supplied by the AP it still only produced
  DHCP/ARP on our capture before it next dropped and re-entered the
  "Connect to WIFI" state on its own (~3 min after join). It has no
  visible HTTP/MQTT/TCP server, no visible outbound API on our network.
- **The "Connect to WIFI" prompt and its periodic re-entry into it are
  independent of our BLE session.** It happens on its own schedule (every
  few minutes when not connected to `123456J`, or a few minutes after a
  brief join). It is the reason the board's BLE session intermittently
  goes silent or drops: the firmware's WiFi task takes priority.
- **Practical way to keep the board reachable over BLE for a long
  session:** stand up a 2.4 GHz AP named `123456J` in Open mode (or
  WPA2 with no key) on whatever host is convenient. The board will join
  it on demand, and as long as that AP remains visible the board is
  unlikely to be stuck in the "Connect to WIFI" state. When it *is* stuck,
  the on-screen EXIT button is a no-op for our purposes; waiting until it
  times out (or power-cycling and letting it re-probe) both work.

**Inferred** (from `80:B5:4E:E6:DD:F8` probing for `123456J`, and the
absence of any application traffic on our side): the builder's app
probably speaks over BLE (which we are doing); the WiFi link is used by
the board for something we did not trigger in the window we watched —
either an outbound call to the previous owner's backend (which requires our
AP to have a working gateway; we supplied one and it still produced only
DHCP/ARP before dropping) or a soft-AP for an OTA/pairing flow we haven't
reached. It is **not** an HTTP configuration portal on any port we probed
(80, 81, 1234, 1883, 3000, 4180, 8000, 8060, 8080, 8081, 9600, 9999 — all
closed on 192.168.251.15).

### 3.5 WiFi

Icon on screen, but no access point visible on the phone or the notebook:
the ESP32 is in station mode, configured through the app. Nothing further
known.

## 4. Rules for working with these units

1. **Listen-only until the board is silent.** `rsh identify` and `rsh sniff`
   are safe at any time; `rsh huawei status/info/set/hold` only after the
   board no longer transmits (disconnected, powered down, or set to a
   display-only mode if the builder has one).
2. **Terminal voltage by meter before any battery.** The listing says
   42–58.8 V, so the 1.7× e-scooter rebuild is unlikely, but one meter
   reading beside register 0x75 the first time the output is on is
   required before the harness's verify-by-measurement is trusted.
3. **Model and nominal current via `info` before any current setpoint**; the
   per-serial scale binding of the harness then applies to this unit's
   serial.
4. **B3.0 again after the board is silenced**: its last writes (50.0 V,
   raw 210) are the module's defaults until then.
5. Termination: the adapter channel wired to the module keeps its 120 Ω
   jumper closed (module and adapter are the two ends); a tap channel on
   the same pair has it open (`docs/ADAPTER-USBCANFD-DUAL.md`).

### 4.1 The gateway: our master, the board's display kept (decided 2026-10-25)

Instead of silencing the board or reverse-engineering its Bluetooth
protocol, its CAN pair is cut from the module and wired out; the harness sits
in between on the two adapter channels:

```
module  <-- CAN -->  adapter ch0 (can:gs:0.0, jumper CLOSED)
                     [ rsh huawei gateway ]
board   <-- CAN -->  adapter ch1 (can:gs:0.1, jumper CLOSED)
```

`rsh huawei gateway --port can:gs:0.0 --front-end can:gs:0.1 --voltage V
--current A` is then the only master the module hears (caps, per-serial
scale and identity checks as for `set`, re-sent every 5 s). The board's
status / info / e-label requests are forwarded and the module's replies and
heartbeats go back, so the screen shows the module's real output voltage,
current, power and temperatures at the board's own 0.62 s refresh; the two
"set" fields on the screen remain the board's own wishes. The board's writes
are answered with a synthesized ack and never forwarded; what it wanted is
logged (`blocked=True`) and summarised (`fe_setpoints`); its 0x81 config
broadcast is dropped without ack, as the module itself was never seen to
ack it. Verified against the front-end simulator
(`rsh huawei frontend-sim`, modelled on the measured board) in
`tests/test_huawei_gateway.py`; not yet run against the real board.

For the target system (Cerbo GX as master; the battery has its own VE.BMS
bus and plays no part here), the four CAN pairs end up as:

```
module 1 pair -+
               +-- ONE module bus -- GX VE.Can port A   (modules = addresses 1 and 2)
module 2 pair -+

board 1 pair -+
              +-- board side: GX port B (our driver answering the boards), either
board 2 pair -+   one channel per board, or one joined board bus (see below)
```

CAN is multi-drop: the two module pairs are joined (as in a Huawei rack;
the seller's "can be paralleled"), the modules negotiate addresses 1 and 2,
the GX driver addresses each. The boards must never be on that bus (each is
a master). What answers the boards can be the GX itself: there is no master
or slave at the CAN level, port B is a SocketCAN interface, and the driver
is our code. Measured so far: board 1 addresses its module as 1. Board 2
not yet measured; whether a board can be given another address, or learns
it from the bus, is unknown (tests: unit 2 passive capture; a board on its
own channel against `rsh huawei sim --addr 2`).

- **One channel per board** (adapter on the GX, or a bench adapter):
  per-unit screens; where a board addresses "1" but its module is 2, the
  gateway rewrites the address field in flight (`--front-end-addr 1
  --addr 2`, implemented and tested).
- **One joined board bus on port B**: works if both boards carry identical
  settings (then their identical frames never collide on arbitration) and
  the driver answers every request with a *system view* (voltage averaged,
  current and power summed, temperatures max) as address 1 - both screens
  show the charger group, per-unit values stay on the GX display / VRM. A
  per-board attribution by polling phase is possible but fragile.
- Open firmware on the front panel (a replacement, listener-only) would
  remove the board side entirely; a later decision.
- A USBCANFD DUAL plugged into the Cerbo (if Venus OS carries the `gs_usb`
  kernel driver - check `modinfo gs_usb` over SSH) adds two SocketCAN
  channels: one per board, per-unit screens, no joined-bus trick, module
  bus still on the built-in VE.Can port. One adapter suffices for two boards.

### 4.2 Alternatives to the gateway (noted 2026-10-25, not implemented)

- **Interleaving our frames between the board's cycles.** The board's cycle
  is regular (0.62 s, writes within 10 ms after its status request), so our
  frames could be timed into the gap without collisions. It does not give
  control: the module applies every write at once and keeps the last one,
  so a setpoint of ours survives for under a second until the board's next
  cycle overwrites it, and the board rewrites the persistent defaults every
  cycle as well. Interleaving only holds when our values equal the
  board's. It is useful for **reading**: status and info requests change
  nothing, and an interleaved `info` would give serial, model and nominal
  current without cutting a wire. Not implemented: the harness still
  refuses any transmission while another controller is present; a
  read-only exception timed into the gap is a possible later option.
- **Board receive-only.** The board stays on the module bus with its CAN
  transmit line disconnected. It can neither write nor acknowledge, so it
  is no longer a master; our master (harness, later the GX driver) polls
  the module, and the board sees every reply and heartbeat on the bus. Its
  own requests go nowhere. Whether its screen keeps updating depends on
  the firmware: it works if the board renders any status burst for "its"
  address it sees, it fails if the board only accepts replies to its own
  requests or shows a communication error after missing acks (its CAN
  controller will go error-passive internally, invisible on the bus). This
  needs the housing opened and one test with the harness as master; it
  removes the board side of the topology entirely (one GX port, no
  gateway, no second channel). Cheapest end state if the firmware
  cooperates.

  *Development option - testing failure mode 1 without cutting anything:*
  inject one synthetic status burst for address 1 (PSU->controller frames
  only, which the module ignores by their direction bit, so nothing is
  commanded) with marker values such as 12.3 V / 4.5 A, timed a few
  hundred ms after the module's real burst, and watch the screen. Marker
  values flick up = the board renders any burst for its address; never
  shown = it only accepts replies to its own requests, and the
  receive-only route is dead before any wire is cut. Needs a small
  `rsh huawei` sub-command (listen for the board's cycle, delay, send one
  logged burst). Failure mode 2 cannot be tested on the live bus: the
  module acknowledges every board frame as long as it is connected.

  *Likelihood (judgement, 2026-10-25):* mode 1 is probably fine - the
  open-source ESP32 controllers for these modules all decode any
  `0x1081407F` frame they receive without matching it to a request, and
  builder firmware tends to follow the same pattern (perhaps 70 %). Mode 2
  is the bigger risk: with its transmit line open the ESP32's CAN
  controller reads recessive where it sent dominant, counts a bit error
  (+8) per attempt, reaches error-passive after ~16 and **bus-off after
  ~32 attempts, i.e. within a few seconds at the board's rate of ~7 frames
  per cycle** - and a controller in bus-off receives nothing until the
  firmware explicitly recovers it. Unless the builder's firmware handles
  bus-off recovery (and does not display an error on it), the screen goes
  dead regardless of mode 1. Overall the receive-only route is more likely
  to fail than to work; the injection test is still worth its hour because
  a negative result on mode 1 saves opening the housing at all.

- **Receive-only with series resistors.** A few kOhm in each line between
  the board's transceiver and the bus: the board still reads back its own
  dominant bits (no bit error), the bus sees only tens of mV (recessive),
  reception through the resistors still works. The failure becomes a
  missing ACK instead of a bit error, and an error-passive node does not
  count ACK errors any further - no bus-off (this is what our adapter did
  with nothing connected: counter pinned at 128). What it does not fix:
  the unacknowledged frame is retransmitted back to back on the board's
  private side, and every real frame must arrive in a gap between those
  retries to be received - the board would miss most of the traffic unless
  its firmware sends single-shot frames or clears its queue on failure
  (unknown). Turns a certain failure into a probable one; not a plan.
- **Dumb repeater (the smallest thing that can ACK).** An acknowledgement
  is a dominant bit placed in the ACK slot by a receiving CAN controller;
  no passive part can produce it, but any CAN controller in normal mode on
  the board's private pair does so for every valid frame without
  transmitting anything of its own. Add forwarding of every module-bus
  frame onto that private pair and the board is fully content: it sees the
  master's requests, the module's replies and heartbeats, and its own
  frames are absorbed and acknowledged. No protocol knowledge, no master
  role, no caps, nothing time-critical - the gateway reduced to ~50 lines.
  Runs on a 5-euro ESP32 with two transceivers or on the DUAL adapter at
  the Cerbo; with two board channels it can also rewrite the address per
  board. GX stays the sole master, screens stay live, no surgery on the
  boards. The strongest candidate for the target system after the plain
  gateway.

## 5. Open questions (asked of the seller 2026-10-24)

- How does the unit leave standby; is the screen touch or display only?
- What is the app, and what does 123456 unlock (BLE login, WiFi, app)?
- Which module exactly (R4850G2 or R4875G1)?
- Can the board be set to display-only / stop sending CAN commands?
- Is there any documentation or wiring diagram of the board?

## 6. Captures

- `captures/20261024-181026-identify.jsonl` — passive identify, three
  bitrates, Huawei verdict with "another controller present".
- `captures/20261024-181052-sniff-charger-with-controller.jsonl` — 15 s,
  762 frames, the board's command cycle, the module's idle status.
- Raw BLE write/reply logs from the command reverse-engineering (section
  3.4.1/3.4.2), in order of the session:
  - `captures/ble-reverse-01.txt` / `-02.txt` — first FFE1 writes; frame
    grammar + default `CC` reply + FFE1-is-a-mailbox finding.
  - `captures/ble-reverse-03-ffe2.txt` — full 0x00–0xC5 command sweep over
    FFE2; 0x01/0x05/0x11/0x12/0x15 respond, session drops at 0xC6.
  - `captures/ble-reverse-04.txt` / `-05-state.txt` — re-confirm 0x01/0x05/0x11/0x12/0x15
    and the 0xF9 config block in the "123456:" state.
  - `captures/ble-reverse-06-sweep-provision.txt` — 0x00–0xFB sweep with
    password payload in the provisioning state.
  - `captures/ble-reverse-07-set-probe.txt` — setter diff-probe: no 0xF9
    slot changes from any tested command with a float payload (needs login).
- `captures/ble-reverse-progress.md` — the session log of findings as they
  were made.
- Probe tools: `tools/ble_inspect.py` (read-only), `tools/ble_probe*.py`
  (write-capable, one per experiment stage).

## 7. Sources on this kind of modification

Found while identifying the board (2026-10-24). None describes our exact
unit or its Bluetooth protocol; they describe the class of conversion.

- Seller's listing of our chargers (Ukrainian reseller):
  https://0088.prom.ua/ua/p3025187059-zaryadnoe-ustrojstvo-universalnoe.html
- smzdm article on the Xianyu hand-built "Huawei big cannon" conversions
  (touch + Bluetooth board, machined housing, 71–101 V e-scooter variant):
  https://post.smzdm.com/p/ako7rp58/
- yleee forum: changing the module's default output via a 4-wire
  communication-control board (41–58.5 V, 0–60 A, defaults persist):
  https://www.yleee.com.cn/thread-62600-1-1.html
- crystalradio forum: "communication-control conversion to an adjustable
  charger, simple wiring, default output can be changed" (registration
  required): http://www.crystalradio.cn/thread-1620930-1-1.html
- mydigit forum: R4850G2 CAN protocol collection and adjustable-conversion
  tutorial (registration required, blocks automated fetching):
  https://www.mydigit.cn/thread-285596-1-1.html ,
  https://www.mydigit.cn/thread-134438-1-1.html
- AliExpress "modified adjustable charger" listings of the same class (knobs,
  no touch): https://www.aliexpress.com/i/1005004827908520.html
- OpenDTU-OnBattery discussion of output switching via the slot-detect
  relay ("data set error during relay shutdown"):
  https://github.com/hoylabs/OpenDTU-OnBattery/discussions/1268
- Reference CAN controllers (what a well-behaved external master does, and
  the register tables our decoder follows):
  https://github.com/patagonaa/huawei-r48xx ,
  https://github.com/craigpeacock/Huawei_R4850G2_CAN ,
  https://github.com/mjpalmowski/CAN-BUS-control-R4875G1-with-ESPHome-and-MQTT ,
  https://github.com/thornung/esphome-huawei-r48xx ,
  https://github.com/iPuzzle/huawei-r4850g2-web ,
  https://github.com/BotoX/huawei-r48xx-esp32 ,
  https://github.com/anwa/esphome-r4875g1-3phase-charger
- Bluetooth address block lookup (80:B5:4E = Espressif):
  https://api.macvendors.com/80:B5:4E
