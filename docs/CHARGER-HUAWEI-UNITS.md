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

*Inferred:* request/answer protocol of the builder's own app; "123456" is
the login sent inside it. **No writes have been attempted**: an unknown
command could change setpoints or the run state of a 4 kW charger. The way
to the commands is the app (name or APK from the seller), then either a
look at the APK or an Android Bluetooth HCI snoop log of one session.

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
- Bluetooth probe output: reproduced in section 3.4 (script in the session
  scratchpad, read-only).
