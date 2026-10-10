# Charge-taper investigation — results (2026-10-06 / 2026-10-10)

SAKO SUNON V 6.5 kVA (program 05 = PYL) ↔ Seplos Mason 280L 3.0 (BMS 3.0,
firmware 1.5, RS485-Pylontech mode, 16S LiFePO₄, 314 Ah). Problem as
reported: charge current near 100 % SOC rebounds (e.g. 40 → 6 → 32 → 0 A,
OV warning; at 100 A an error state on the battery).

Plan and hypotheses: `CHARGE-TAPER-PLAN.md`. Tool usage: `USAGE.md`.
All captures: `captures/taper/` (git-ignored).

## 1. Summary

1. The rebound is a control-loop interaction between a BMS whose charge
   limit follows the fullest cell voltage instantly and an inverter that
   follows that limit late, in 10 A steps, and climbs back as soon as the
   limit rises. It is not caused by the cable, the protocol, or our tools.
2. The dangerous part is the end: the inverter keeps charging for 14–35 s
   after the BMS says "stop", and cannot go below ~10 A. At 60 A this drove
   the fullest cell to 3.625 V and raised a battery alert.
3. Root cause of the end-phase: the BMS requests a charge voltage of
   57.6 V (3.60 V/cell), so the inverter never reaches its own
   constant-voltage phase and everything hangs on the current-limit
   requests it follows badly.
4. **Fix, verified on 2026-10-10:** Seplos setting 102 "Charging request
   voltage" 57.6 → **55.2 V**. At 40 A the inverter then held the pack at
   55.1–55.2 V and tapered the current by itself (39 → 2 A, smoothly) while
   the BMS limit stayed at 86–94 A; fullest cell flat at 3.460 V, no alarm,
   no seesaw. Higher currents (60/80/100 A) not yet tested with the fix.

## 2. Measured behaviour (unmodified system)

### 2.1 Seplos side
- Polled by the SAKO with VER 20, ADR 0x12, commands 0x61 → 0x62 → 0x63,
  cycle 2.26 s; replies after 130 / 37 / 47 ms.
- **Charge current limit (CCL) is a memoryless function of the highest
  cell voltage** (identical when charging or resting, no hysteresis):

  | fullest cell | CCL |
  |---|---|
  | < 3.425 V | 180 A |
  | 3.45 V | ~100 A |
  | 3.50 V | ~40 A |
  | 3.545 V | ~2 A |
  | ≥ 3.548 V | 0 A, charge-enable off, CVL 57.6 → 55.9 V (latched) |

  This matches Seplos' own description of firmware 1.5 (taper from 3.42 V,
  0 A at 3.55 V; resume when min cell < 3.32 V, SOC < 96 %, or discharge
  > 3 A for 2 min). The 3.42/3.55 thresholds appear as fixed constants in
  the firmware image and are not settings.
- The alarm frame (0x62) stayed all-zero in every capture, including while
  the battery display showed WARN / OV-WARN. The inverter is only told
  "do not charge" (CCL 0, charge-enable bit), never the warning itself.
- Charge voltage sent (CVL): 57.6 V normally (= setting 102, which
  defaults to the pack over-voltage protection value); 55.9 V right after
  the forced stop; **56.3 V in the latched "charge finished" state** (this
  value does not follow setting 102). SAKO programs 26/27 only mirror the
  received value and are locked in PYL mode.
- Pack voltage vs fullest cell was the same in the 20/40/60 A runs:
  54.6 V → 3.43 V (taper starts), 55.2 V → 3.47 V (CCL 70–80 A),
  55.5 V → 3.50 V (40 A), 55.9 V → 3.55 V (stop).

### 2.2 SAKO side
- Charge current settles only at 8.3 / 18.5 / 28.7 / 38.8 / 58.3 A for
  settings 10 / 20 / 30 / 40 / 60 A: **10 A steps, 10 A minimum** (minus
  ~1.5 A own consumption).
- Follows a falling CCL with 3–21 s lag, stepping down to the 10 A step
  below the limit; ramps up at ~1 A/s as soon as the limit exceeds the
  current.
- Ignores CCL below its 10 A floor; kept 8.4 A for 31 s at CCL 1–5 A
  (20 A run) and 28.8 A for 35 s after charge-disable (60 A run).
- On mains-on it briefly charges at its minimum step even while the BMS
  says "charge disabled" (12 s), then stops.
- Pauses polling for 8–14 s and re-runs its battery address scan
  (0x4F at ADR 02,05,04,03,02,01, VER 00) at every mains switch-over;
  the user sees code 32 ("communication failure inverter ↔ display panel")
  at those moments. Observed with the direct cable, so unrelated to our
  equipment.

### 2.3 The three unmodified runs

| run | setting | result |
|---|---|---|
| 2026-10-06 19:57 (bridge inline) | 40 A | seesaw 38.8 → 28.7 → 18.5 → 28.7 → 8.3 → 27 A …; fullest cell 3.545 V; cut off by mains-off |
| 2026-10-06 20:11 (bridge inline) | 20 A | no seesaw (only one step below); 8.4 A held against CCL 1–5 A; BMS stop at 3.548 V, inverter stopped 14 s later |
| 2026-10-06 20:55 (passive tap) | 60 A | seesaw 58 → 49 → 29 → 39 → 29 → 19 → 29 → 8.5 → 29 A; BMS stop at 3.548 V ignored for 35 s; fullest cell 3.625 V, 160 mV spread, battery alert |

Why the seesaw: each current cut lets the fullest cell's measured voltage
sag 10–20 mV (less current-induced rise, plus slow relaxation — in the
60 A run it fell 3.522 → 3.500 V over 48 s while still charging at 8.5 A),
the CCL jumps back up by 10–15 A, the inverter ramps again.

## 3. The fix and its verification (2026-10-10)

- Setting 102 written through the Seplos service socket (RS485-1/-2,
  19200, device 0) with `rsh seplos-set --port COM8 --setting 102
  --value 55.2 --apply`; read back and re-read independently.
- Recording `T1-55V2-tap-plus-service.jsonl`: tap on the inverter link +
  service monitor in one session.

| time | pack V | current | BMS limit | fullest cell |
|---|---|---|---|---|
| 18:02:16 | 55.10 | 38.8 A | 106 A | 3.447 V |
| 18:03:02 | 55.13 | 25.5 A | 91 A | 3.457 V |
| 18:05:33 | 55.14 | 7.4 A | 89 A | 3.459 V |
| 18:08:03 | 55.16 | 3.9 A | 88 A | 3.460 V |
| 18:10:34 | 55.16 | 2.1 A | 88 A | 3.460 V |

No alarm or protection flag, battery mode "Charge" throughout, spread
19 mV, SOC 99.8 %. Ended by a mains outage at 18:11, not by either device.

Expected at higher currents (from the voltage/cell relation): up to ~60 A
the BMS never needs to intervene before 55.2 V is reached; at 100 A its
limit drops below 100 A at ~54.9 V, so some cut-back requests (and a mild,
bounded seesaw) are possible in the last 0.3 V. 54.8 V would avoid even
that at the price of a slightly lower end-of-charge.

## 4. Open points / next steps

1. Repeat with the fix at 60 A, then 80 A, then 100 A, same setup
   (`rsh sniff --ch1 <tap> --service <service port> --quiet`), checking the
   fullest cell stays below 3.50 V; lower setting 102 to 55.0 V if a cell
   creeps.
2. Watch how a charge ends by itself with 55.2 V (float / "full" handling
   by both devices) — the 40 A run was interrupted by the outage.
3. The cell that ran ahead on 2026-10-06 (160 mV spread at the top) did not
   show on 2026-10-10 (19 mV); keep an eye on the per-cell peaks in
   `.service.csv`.
4. Our inline bridge stopped the inverter from inverting on battery (user
   test 2026-10-06); cause unknown, logs show only a ≤10 ms reply delay and
   no data differences. Monitoring is done with a passive tap since; the
   bridge is needed only for Phase 2, which the voltage fix may make
   unnecessary.
5. Waveshare USB drop-outs under heavy inverter load (ClearCommError,
   auto-reopened; one 5-min gap). The FTDI Seplos adapter on the service
   side had none — consider swapping roles for long recordings.

## 5. Tooling built for this (all in `rsh`)

`timeline` (pairing, decoded series, events, findings, CSV/PNG),
one-keypress markers, `--quiet` bench mode, bridge liveness guard,
Seplos service socket: `discover` (auto-detects it, prints all 106
settings by name), `seplos-set` (guarded write of 102/103), `seplos-monitor`
and `sniff --service` (cells, flags, charge switch, balancing, settings
changes, merged into `timeline` with `.service.csv`). Verified against the
real pack on 2026-10-10.
