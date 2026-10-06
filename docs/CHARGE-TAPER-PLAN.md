# Charge-taper investigation: Seplos BMS 3.0Y ↔ SAKO SUNON V (RS485 Pylon_485 / PHY)

Status 2026-10-05. Comms work (cable pinout was the original fault). New
problem: **current rebound near full charge** (e.g. 40 A → 6 A → 32 A → 0 A,
OV-Warn, occasional red state at 100 A). Goal: find *which device* causes
the rebound, then — only afterwards — test a conservative CCL filter.

## 1. Hypotheses (from the task) and what would prove each

| # | Hypothesis | Evidence that confirms it |
|---|---|---|
| H1 | Seplos raises its advertised CCL again (BMS-driven rebound) | 0x63/0x92 CCL field goes e.g. 6 → 40 A *before* current rises |
| H2 | SAKO raises current while BMS still advertises low CCL | CCL stays low in every frame, yet current rises; inverter ignores limit |
| H3 | Timeout / missing / stale frame → SAKO reverts to its own max | a comm gap (no poll, no response, RTN≠00 or bad checksum) immediately precedes the rise |
| H4 | Another protocol state flips (charge-enable, full-charge-request, CVL) | 0x63 status bits / CVL change at the transition while CCL doesn't |
| H5 | Scaling / sign / unit / address / checksum mismatch between Seplos Pylon_485 and SAKO PHY | decoded values implausible (e.g. CCL = 400 A), sign flips, VER/ADR oddities, checksum errors |

The decisive artefact is a **timeline** that puts CCL, CVL, charge-enable,
OV-warn, MaxCell, pack current and comm gaps on one millisecond axis.

## 2. What the harness already does (mapped to the task)

| Task requirement | Harness today | Status |
|---|---|---|
| Transparent capture, both directions, raw hex preserved even if unparsed | `rsh sniff` (passive tap) / `rsh bridge` (inline, no translator) log every frame + raw burst to JSONL | ✅ |
| Timestamps, ms resolution | JSONL `t` = wall-clock at log time, 0.1 ms resolution | ⚠️ log-time not arrival-time; no monotonic field |
| Direction | by channel; on a single-wire tap, request vs response is inferred (request = known CID2 + short INFO) | ✅ (explicit BMS→INV / INV→BMS label: small gap) |
| Decoded header, checksum validity | `ParsedFrame`: VER/ADR/CID1/CID2(RTN)/LENID/INFO + `errors` (chksum-mismatch got/expected, lchksum, lenid) | ✅ (store checksum value even when valid: trivial) |
| Request/response pairing | `diagnose` pairs within one channel (1.5 s window) | ⚠️ not across the two bridge channels |
| Decode charge fields: SOC, SOH, pack V/I, max/min cell V, CVL, CCL, DCL, charge/discharge enable, charge-immediately, full-charge-request, alarm/protection bits (cell-voltage-high = OV-warn), temp-based limits (0x47), identity | `payloads.decode_61/62/63/92/42/47/51` — validated byte-exact against real Pylontech US5000 | ✅ decoders exist; ⚠️ **scaling must be verified for Seplos** (§6) |
| Per-frame decoded payload in the capture | only the header is decoded at capture time; payload decode needs the request CID2 → needs pairing | ❌ gap |
| High-level chronological event log ("CCL changed 20 → 10") | — | ❌ gap |
| Timing analysis (poll interval, latency, CCL-update interval, gaps, CCL→current delay, stale duration) | `diagnose` has request→response latency only | ❌ gap |
| User markers (`MARK maxcell=3.550`) aligned with traffic | — | ❌ gap |
| Automatic findings (CCL/CVL/enable/alarm transitions, gaps, large CCL increases, current > CCL with delay tolerance, oscillation) | — | ❌ gap |
| CSV/JSON for graphing; graph | — (no matplotlib installed) | ❌ gap |
| Phase 2: modify BMS→INV frames with recalculated checksum | `Translator` rewrites VER/ADR/CID2-echo and re-checksums | ⚠️ header-only; no CCL shaping |
| Audit log original + modified frame | logs the original with a "rewrote:" note; modified bytes **not** logged | ❌ gap |
| Fail-safe definitions; mode clearly visible | transparent vs translate noted at start only | ⚠️ gap |
| Live transparent forwarding latency | bridge forwards **per frame** (store-and-forward): ~100–150 ms added per frame at 9600 even with no translator | ⚠️ matters for H3 — see setup choice |

## 3. Gaps to implement — prioritised

> **Status 2026-10-05:** P1 items 1–6 implemented (`rsh timeline`, `rsh
> mark` + live `MARK` input, arrival-time/flow/mode/len/chksum on every
> record, byte-level transparent bridge). P2 implemented as **generic
> infrastructure** (items 7–9: pluggable payload policy engine with
> enforced invariants, two-record audit, fail-safes, loss reset, CLI
> `--policy`); the experimental CCL *shaper policy itself* is deliberately
> not written yet — it plugs in as a `PayloadPolicy` subclass once the
> Phase-1 captures say what Seplos actually does. Item 10 remains open.

**P1 — before tomorrow's captures (Phase 1 analysis quality)**
1. **Capture fidelity**: arrival time from the serial chunk (`t_mono`
   perf_counter + wall `t`), explicit `direction` (BMS→INV / INV→BMS),
   `len`, `chksum` value, `mode` (tap / transparent / modify) on every record.
2. **Pairing + payload decode** (`rsh timeline`, post-processing): pair
   request→response on one tap channel *or* across the two bridge channels;
   decode every response payload with its request's CID2; emit a flat
   per-sample series: t, CID2, SOC, SOH, packV, packI, maxCell, minCell, CVL,
   CCL, DCL, charge_enable, discharge_enable, charge_immediately,
   full_charge_request, OV-warn (cell_voltage_high_alarm), protections, RTN,
   latency_ms, gap_since_prev_same_dir.
3. **Event log + findings** (same tool): changes in CCL/CVL/enable/alarm/SOC
   step; timing stats (poll period, response latency, CCL update interval,
   gaps > N × period); flags: large CCL *increase* (> X A or > Y %), current
   > advertised CCL beyond a configurable delay window, repeated up/down
   oscillation near top of charge, comm gap within ±T of any CCL rise.
4. **Markers**: typing `MARK maxcell=3.550` (any text) + Enter while
   `sniff`/`bridge` runs inserts a timestamped note; `rsh mark --session f
   "text"` for after-the-fact; markers appear in the timeline/CSV.
5. **Export**: `timeline.csv` (one row per sample, marker rows included) +
   `timeline.json`; optional PNG via matplotlib if installed, else the CSV
   graphs in Excel/LibreOffice in seconds.
6. **Byte-level transparent forwarding** in the bridge when no translator is
   active (forward each chunk immediately; frame-parse in parallel for the
   log) — removes store-and-forward latency so an inline bridge is a
   faithful observer.

**P2 — Phase 2 (only after captures answer the primary question)**
7. **CCL shaper** policy in the translator (BMS→INV 0x63 — and 0x92 if the
   SAKO polls it): config `max_ccl`, `up_slew_a_per_s`, `down_passthrough`,
   `min_hold_s_after_decrease`, `up_hysteresis_a`, `top_of_charge_mode`,
   `fallback_on_loss`. Invariants enforced in code, not by policy:
   `fwd_CCL ≤ BMS_CCL`, `fwd_CVL ≤ BMS_CVL`; never touch charge/discharge
   disable, protections, alarms, SOC, voltage, current.
8. **Audit log**: for every modified frame two records — original RX and
   modified TX (raw + decoded + policy reason) — never a silent change.
9. **Fail-safe table** implemented and logged (§7).
10. *(optional, needs a third RS485 port)* Seplos native Modbus side-channel
    (RS485-1/2 socket, 19200, slave 0, PIB block = all 16 cell voltages)
    logged into the same session — independent bus, cannot touch the
    inverter link.

## 4. Tomorrow — bench plan (Phase 1, no manipulation)

### 4.1 Setup choice
**Option A (recommended for Phase 1): passive tap.** One Waveshare channel
wired *in parallel* onto the live inverter↔battery RS485 pair (A→A, B→B,
PE→GND). Nothing sits in the control path, zero added latency, nothing can
fail into the link. Both directions appear on one channel; request vs
response is inferred and paired by sequence. Command: `rsh sniff`.

**Option B: inline bridge, transparent.** Inverter on ch1, battery on ch2,
`rsh bridge` with no translator. Needed later for Phase 2; for Phase 1 it
adds per-frame latency (until P1-6 is done) and one more failure point.

Do **A first**. Then run **T3** to prove the bridge changes nothing before
it is ever trusted with Phase 2.

### 4.2 Wiring (tap)
- Seplos inverter RS485 socket: A = 2/7, B = 1/8, GND = 3/6 (Mason 280L 3.0
  spec; pins 4/5 are CAN). SAKO BMS RJ45: A = 5, B = 3, GND = 8.
- Tap: Waveshare A1 → bus A, B1 → bus B, PE → bus GND. No termination on the
  tap (the two devices are the bus ends). Note the COM number (`rsh ports`;
  Port 1 was COM6 on this adapter).
- Baud: the SAKO/PYL link is **9600 8N1** — confirm with
  `rsh identify --port COMx --passive-only --bauds 9600,115200`.

### 4.3 Test sequence
| Test | Inverter charge limit | Goal | Capture |
|---|---|---|---|
| T0 sanity (5 min) | current setting | tap sees both directions, checksums valid, decoded CCL/CVL/SOC plausible, poll period measured | `captures/taper/T0-*.jsonl` |
| T1 full charge cycle | **20 A** | baseline taper: CCL / current / OV-warn behaviour from ~90 % to termination | `T1-20A-*.jsonl` |
| T2 full charge cycle | **40 A** | reproduce the 6 → 32 A rebound with the wire visible | `T2-40A-*.jsonl` |
| T3 bridge equivalence | 20 A (short, ~30 min near top) | inline transparent bridge yields the same traffic/behaviour as the tap | `T3-bridge-*.jsonl` |

Do **not** start with 100 A.

Run (tap): `rsh sniff --ch1 COMx --baud 9600 --quiet --session captures/taper/T1-20A.jsonl`

`--quiet` keeps the screen readable: no per-frame lines, only a decoded
**CHANGE** line whenever CCL/CVL/charge-enable/OV-warn/current moves (plus
a STATE heartbeat every 30 s), e.g.
`14:02:11 CHANGE ccl 40.0->6.0  | I=5.8A CCL=6A CVL=56.80V MaxCell=3.552V SOC=99% chg=True full=False OVwarn=True`
— MaxCell, pack current, OV-warn and the limits are all read off the wire,
so you do **not** transcribe them.

Markers are **one keypress**, only for things the wire cannot see
(inverter LCD current, red LED, your own observations):
- **Enter** alone = numbered marker "now" (`MARK #7`), fill in the meaning
  later;
- **`r`**+Enter = rebound/current rise, **`o`** = OV-warn seen, **`l`** =
  red LED, **`z`** = current 0, **`x`** = something odd;
- a **number**+Enter (`32`) = inverter current reading;
- anything else is kept verbatim.
Markers are numbered and timestamped; afterwards `rsh mark --at <time>`
or the CSV lets you attach exact values to each number.

### 4.4 What to watch live
Decoded one-line-per-frame view: CCL / CVL / charge_enable / OV-warn /
MaxCell / packI. Any gap > 2 poll periods or any RTN≠00 / checksum error is
worth a marker.

### 4.5 Analysis (right after each capture)
1. `rsh timeline captures/taper/T2-40A.jsonl` → event log + findings +
   CSV/JSON (+ PNG if matplotlib present).
2. Locate the 6 → 32 A rise in the pack-current series; read, at that
   timestamp ± 5 s: CCL, CVL, charge_enable, full_charge_request, OV-warn,
   any gap/RTN error, and the markers.
3. Decision table:
   - CCL rose first → **H1** (Seplos) → a Phase 2 CCL filter is the right lever.
   - CCL stayed low, current rose → **H2** (SAKO ignores CCL) → filtering CCL
     cannot help; examine CVL / charge-enable semantics or inverter settings.
   - Gap / error immediately before → **H3** (timeout) → fix link
     reliability/timing first (bridge latency, cable, termination).
   - charge_enable / full_charge / CVL flipped → **H4** → policy must act on
     that field, not CCL.
   - implausible values / sign / VER-ADR oddities → **H5** → protocol fix.
4. Compare T1 vs T2 timelines (same tool, two files): does the rebound scale
   with the limit, or appear only above a CCL threshold?
5. Keep everything: `captures/taper/` + `*.timeline.csv/json`.

## 5. Phase 2 — only after §4.5 answers the primary question

Architectural principle (binding): **the bridge is not a charge
controller.** Seplos stays authoritative for the maximum permissible charge
current. The bridge may only make that permission *more conservative*:
filter, delay, or rate-limit **upward** CCL changes.
`forwarded_CCL ≤ BMS_CCL` at every instant; decreases propagate
immediately; no CCL is ever computed from SOC, pack V or cell V. A
voltage/SOC supervisory algorithm is out of scope until captures show that
simple CCL filtering is insufficient.

First experiment (if H1): `rsh bridge --ch1 COM_inv --ch2 COM_bat --ccl-shape
up_slew=2 hold=30 hyst=5 max=40` at 40 A, looking for 40 → 6 → (6…10…) → 0
instead of 40 → 6 → 32 → 0. Every modified frame is logged twice (RX
original / TX modified, with the policy reason).

## 6. Scaling / unit verification (H5) — do this in T0
Do not trust the decoder's units for Seplos until cross-checked:
- 0x63 CCL/DCL are decoded as **deci-amps (÷10)**, validated on Pylontech
  (stack 400 A, pack 100 A). Some documents use ÷100. Cross-check against
  the Seplos app's configured charge-current limit and the inverter's
  displayed limit.
- 0x61 current is decoded as **centi-amps (÷100)**; cross-check sign
  (charge +) and magnitude against the inverter/app current.
- CVL (÷1000 V) should be ≈ the Seplos charge-voltage limit (e.g. 56.8 V);
  OV-warn = 0x62 byte 1 bit 5 `cell_voltage_high_alarm`.
If any disagree, the timeline tool takes a `--scale` override and the
discrepancy is itself an H5 result.

## 7. Fail-safe definitions (inline bridge; every event logged)
| Condition | Transparent mode | Manipulation mode |
|---|---|---|
| One RS485 side disconnects | forward nothing; log the gap; keep running | same; **no synthesized frames** |
| Software crash | no frames pass → inverter sees comm loss → its own safe state | same (the bridge never holds a permissive state the devices didn't send) |
| Malformed frame / CRC fail | forwarded **unchanged** (byte-transparent) | forwarded unchanged — never "repaired" into a permissive frame |
| BMS stops responding | nothing to forward | nothing to forward; shaper state reset; the next real frame is filtered fresh |
| Inverter stops polling | log the gap | log the gap; shaper state expires |
| Unknown frame | forwarded unchanged, logged raw | forwarded unchanged |
| Modification logic exception | n/a | forward the **original BMS frame** (BMS-advertised = never more permissive than the BMS) and log the exception |

Rule: the bridge never creates a charging permission from stale or
synthetic data; losing trustworthy BMS information yields the BMS's own
last *actual* frame or nothing — never the shaper's memory of a high value.
