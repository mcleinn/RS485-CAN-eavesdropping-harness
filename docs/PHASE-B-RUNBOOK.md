# Phase B runbook — Huawei R4850G2 on the bench with `rsh`

One command line per Phase B test ID (`PhaseB_Windows_CAN_Test_Specification.md`,
2026-10-22). Nothing here has been run on hardware yet; every command was
exercised against `rsh huawei sim` on the virtual bus. Replace `<serial>` by
the 12-hex serial `info` prints and `<cap>` by the per-unit current cap.

Conventions used throughout:

- Channel 0 = controller (`can:gs:0` = `can:gs:0.0`), channel 1 = listen-only
  tap (`can:gs:0.1`) on the same bus. Termination jumpers on the board:
  "CAN1 120 Ohm" closed (channel 0 is one end of the rectifier bus, the
  rectifier the other), "CAN2 120 Ohm" **open** (the tap must not add a
  third terminator). Keep a tap running through every test:
  `rsh sniff --ch1 can:gs:0.1 --baud 125000 --quiet --label <ID>-<serial>`
  in a second terminal; it also reports `ADDR-NEG`/`ADDR-CHANGE`.
- `--label <ID>-<serial>` on every command: the capture is then
  `captures/<ts>-huawei-<sub>-<ID>-<serial>.jsonl` and its last record is the
  `summary`.
- Nothing is sent without `--apply`; refusals are never clamps; `--persist`
  is always a separate, deliberate flag.
- Instrument readings go into the running capture as markers (type them into
  the sniff terminal: `32` = 32 A on the clamp meter, free text otherwise).

## Step 0 of every session

```
rsh ports                                                   # expect 1D50:606F, two channels
rsh huawei caps --voltage-max 52.8 --current-max <cap>      # before any set
rsh huawei info --port can:gs:0 --label B0-<serial>         # binds serial <-> addr 1
rsh huawei set --port can:gs:0 --current 0 --persist --apply --label B3.0-<serial>
rsh huawei set --port can:gs:0 --voltage 52.4 --persist --apply --label B3.0-<serial>
```

`set --current 0 --persist` needs the unit's scale (B2.2). Before B2.2 has
run, the current write is refused: do B2.2 first, then B3.0, then everything
else. A `0x21` ack on the zero-current write is the B3.0 **FAIL** (exit 1,
`result: error`, `status_byte 33`).

## B0 — adapter and bus

| ID | Command | Pass |
| --- | --- | --- |
| B0.1 | `rsh ports` | both channels listed under `1D50:606F`; `0483:DF11` = stuck in DFU, stop |
| B0.2 | `rsh selftest can:gs:0.0 can:gs:0.1` (channel 0 to channel 1 straight: H-H, L-L; both "CANx 120 Ohm" jumpers closed) | `passed: true`, "listen-only peer does not ACK" ok |
| B0.3 | `rsh identify --port can:gs:0 --passive-only --bitrates 125000 --label B0.3-<serial>` | `HUAWEI R48xx PSU @ 125k`, 0x1001117E about every 0.377 s, no error-frame verdicts |

## B1 — identity and telemetry, single unit

| ID | Command | Record |
| --- | --- | --- |
| B1.1 | `rsh huawei info --port can:gs:0 --label B1.1-<serial>` | serial, versions, slot id, nominal current, e-label; `unknown` lines verbatim |
| B1.2 | `for /l %i in (1,1,10) do rsh huawei status --port can:gs:0 --label B1.2-<serial>` | `complete` ten times; `unknown registers:` lines verbatim |
| B1.3 | `rsh huawei status --port can:gs:0 --label B1.3-<serial>` at no load and at ~5 A, meter reading as marker in the tap | 0x75 within 1 %, 0x81/0x82 within 0.5 A; note which one tracks the meter |
| B1.4 | `rsh huawei hold --port can:gs:0 --poll-only --duration 100 --label B1.4-<serial>` then `rsh timeline <capture>` | request-to-first-frame and burst duration from the capture (100 requests at 1 s) |

## B2 — live control, single unit

| ID | Command | Pass |
| --- | --- | --- |
| B2.1 | `rsh huawei set --port can:gs:0 --voltage 52.4 --apply --label B2.1-<serial>` | `verify: ... OK` (0x75 within 0.3 V after 2 s), ack ok |
| B2.2 | `rsh huawei calibrate-current --port can:gs:0 --test-a 5 --label B2.2-<serial>` (load > 5 A) twice | same `result` both times; stored under `huawei.units.<serial>.current_scale`; note the `equivalent` list |
| B2.3 | `rsh huawei set --port can:gs:0 --current 2 --apply --label B2.3-<serial>` then 5, 10, 20 (each within `<cap>`) | measured current follows within 0.5 A / 3 %; smallest distinct limit recorded |
| B2.4 | `rsh huawei set --port can:gs:0 --voltage 40.0 --apply` / `--voltage 59.0 --apply` / `--current <nominal+5> --apply` | the harness refuses out-of-range and above-cap values itself (exit 1, nothing sent); to see the unit's own 0x21 raise the cap deliberately for one test and restore it |
| B2.5 | standby is not a `set` option (no `--standby`); use the simulator-verified frame via the tap record: not automated in this phase | record from the unsolicited byte 3 and the meter |
| B2.6 | `rsh huawei hold --port can:gs:0 --voltage 52.4 --current <cap> --duration 1800 --label B2.6-<serial>` | `CHANGE out_temp_c` lines, flags, fan noted as markers |

## B3 — mandatory zero-current offline default

| ID | Command | Pass / Fail |
| --- | --- | --- |
| B3.0 | step 0 above (`--current 0 --persist --apply`, `--voltage 52.4 --persist --apply`) | both `applied`; `0x21` on the current write = **FAIL** |
| B3.1 | `rsh huawei hold --port can:gs:0 --voltage 52.4 --current 5 --silence-after 30 --observe 120 --tap can:gs:0.1 --label B3.1-<serial>` | summary: `t_zero_current`, `t_first_output_disabled`, `residual_current_a` (must be 0.0; any sustained current = **FAIL**), `unsolicited_period_s`; meter reading as marker |
| B3.2 | AC off 60 s, on; no CAN traffic for 5 min; then `rsh huawei status --port can:gs:0 --label B3.2-<serial>` once | tap shows unsolicited load 0 throughout; meter 0.0 A; `status` afterwards shows 52.4 V / 0 A |
| B3.3 | `rsh huawei hold --port can:gs:0 --poll-only --duration 120 --label B3.3-<serial>` then `rsh huawei set --port can:gs:0 --current 5 --apply --label B3.3-<serial>` | current stays 0.0 A under polling; appears only after the live write |
| B3.4 | `rsh huawei set --port can:gs:0 --current 5 --apply` then `rsh huawei hold --port can:gs:0 --poll-only --duration 180 --label B3.4-<serial>` | summary `first_offline_s`: `null` = polling keeps the unit online, a number = only set frames count; either is acceptable, record it |
| B3.5 | `rsh huawei set --port can:gs:0 --current 5 --apply`, remove AC, restore AC, CAN silent, tap running | tap: unsolicited load 0 after AC return and no current on the meter before a new live write; otherwise **FAIL** |

## B4 — failure behaviour, single unit

| ID | Command | Record |
| --- | --- | --- |
| B4.1 | `rsh huawei hold --port can:gs:0 --voltage 52.4 --current 5 --label B4.1-<serial>`, pull the controller cable mid-hold | hold stops with `no ACK` (exit 1); the tap capture gives the fallback time (`rsh timeline`), its error frames give the bus error picture |
| B4.2 | same hold, kill the process (Ctrl+Break) | same, from the tap capture only |
| B4.3 | `rsh huawei hold ... --label B4.3-<serial>`, drop AC with DC connected | `CHANGE status_flags` with `input-power-failure`, which registers reset, whether the heartbeat continues |
| B4.4 | restore AC during the same hold | `CHANGE out_voltage_v`/`out_current_a` timing; whether the live limit is still applied (expected: not) |
| B4.5 | `rsh huawei status --port can:gs:0 --label B4.5-<cond>` under each condition | `status_flags` table; unknown bits stay listed as raw `registers["0x83"]` |

## B5 — two units on one bus

| ID | Command | Pass |
| --- | --- | --- |
| B5.1 | tap running (`rsh sniff --ch1 can:gs:0.1 --baud 125000 --label B5.1`), power both; then `rsh identify --port can:gs:0 --passive-only --bitrates 125000` | `ADDR-NEG phase=3001/3002` lines with timestamps, `ADDR-LEARN` for both serials, `huawei_psu_addrs: [1, 2]` |
| B5.2 | `rsh huawei info --port can:gs:0 --addr 1 --label B5.2-<serial1>` and `--addr 2 --label B5.2-<serial2>`; `status --addr 1` / `--addr 2` | each answers only on its own address; `huawei.addr_map` in `.rsh-config.json` is the serial -> address record |
| B5.3 | `rsh huawei calibrate-current --port can:gs:0 --addr 2 --test-a 5` (once per serial), then `rsh huawei hold --port can:gs:0 --addr 1 --voltage 52.4 --current 5 --label B5.3-<serial1>` and in a second terminal `... --addr 2 --current 10 --label B5.3-<serial2>` | each unit holds its own limit (meters), sum matches; note any native sharing |
| B5.4 | hold on `--addr 2` running, restart unit 1 | tap: `ADDR-NEG`, `ADDR-CHANGE`/`ADDR-TAKEN` with timestamps, `ADDR-WINDOW` gap; whether the hold on addr 2 keeps its output; if the serial on addr 2 changed, the next current setpoint is refused until `info --addr 2` |
| B5.5 | hold running, add a unit to the bus | same as B5.4 |
| B5.6 | B3.0 to B3.3 with both units: `--addr 1` and `--addr 2` runs of each command | each falls back to zero individually; the tap shows module-to-module unsolicited traffic does not keep either online |
| B5.7 | `rsh huawei hold --port can:gs:0 --voltage 52.4 --current 5 --silence-after 30 --observe 120 --tap can:gs:0.1 --label B5.7`, both units linked | `per_addr` in the silence summary: both reach load 0; times recorded |
| B5.8 | `rsh huawei set --port can:gs:0 --broadcast --expect-acks 1,2 --voltage 52.4 --apply --label B5.8` then `--current 5` the same way | `acks` from 1 and 2, no `MISSING`; a broadcast current setpoint is refused unless both serials share the same (scale, nominal) |

## B6 — timing summary

`rsh timeline <capture> --json` on the B1.4, B3.1, B3.4, B4.1, B4.2, B5.7
captures; at least five `t_zero_current` values per unit and condition from
repeated B3.1 runs (`--label B3.1-<serial>-<n>`). Median, min, max from the
`summary` records:

```
python - <<'EOF'
import json, glob
for f in sorted(glob.glob("captures/*-huawei-hold-B3.1-*.jsonl")):
    for line in open(f, encoding="utf-8"):
        r = json.loads(line)
        if r.get("kind") == "summary" and r["summary"].get("silence"):
            s = r["summary"]["silence"]
            print(f, s["t_zero_current"], s["t_first_output_disabled"], s["residual_current_a"])
EOF
```

## B7 — generator-fed run

Only after B1–B6 pass on both units. Same commands as B2.3/B2.6 with
`--broadcast --expect-acks 1,2` for the group steps and the AC meter readings
typed as markers into the tap; B7.3 = `set --broadcast --expect-acks 1,2
--current 0 --apply`; B7.4 = B4.3/B3.5 with the generator stopped by hand.

## What the harness does not do in this phase

- No standby (`0x0132`) command on the CLI (B2.5): the protocol module and
  the simulator know the register; a `set --standby` sub-option is a later
  addition once the hibernate/wake behaviour is observed on the tap.
- `set --current` deliberately refuses above-cap and out-of-range values
  itself (B2.4), so the unit's own `0x21` for such values is only seen when
  the cap is raised on purpose for that one test.
- `t_first_output_disabled` is reported from the unsolicited byte 3 as the
  sources describe it (hibernate / not ready); whether a real unit sets it on
  the zero-current fallback is itself a Phase B finding, so the simulator does
  not pretend it does and the value may legitimately stay `null`.

## Bench log

### 2026-10-24 — B0.1, B0.2 passed; B0.3 with a surprise

- B0.1 `rsh ports`: both channels, 1D50:606F. B0.2 `rsh selftest`: 16/16
  (notes on the adapter: `docs/ADAPTER-USBCANFD-DUAL.md`).
- B0.3 on charger 1, channel 0, listen-only: **Huawei PSU at 125 kbit/s,
  address 1**, unsolicited `0x1001117E` every 0.377 s, `0x108111FE` every
  0.377 s, `0x100011FE` every 3 s, no error frames at 125 k. **But the bus
  is not quiet: the charger carries an add-on control board (display
  front-end, exact type unconfirmed; it did not react to presses) that is a
  CAN controller.** Every 0.62 s it sends a status request and then, within
  10 ms, five writes: default voltage 49.98 V (raw 0xC7ED), default current
  raw 210, online voltage 47.98 V (raw 0xBFEE, sent twice), standby = 0
  (on), online current raw 70; every 3 s a broadcast `0x108081FE 01 13 00
  01 00 00 00 00` (register 0x13, undocumented). All writes acked ok.
- Unit state under that controller: AC in 226 V / 50.1 Hz, **output 0 V,
  0 A**, limit ratio 0x76 = 70/1024 (echo of the raw online-current write),
  temperatures 20–21 °C, 32085 operating hours, status word bytes 4-7
  `08 00 00 04`, bytes 2-5 `00 00 08 00` (bits not in any source).
- Free B1.4 data from 24 requests: request → first frame 2.4 / 6.1 / 8.1 ms
  (min / median / max), burst 14 frames in 25.8 ms.
- Consequence: `identify` reported "another controller is talking, do NOT
  send" and the harness stayed passive. **Every active Phase B test needs
  the front-end's CAN disconnected** (or the front-end powered off); two
  masters re-sending different setpoints every 0.6 s would make every
  timeout, fallback and setpoint measurement meaningless. Tap-only tests
  (B5.1 negotiation, B1.4 timing) can run with it attached.
- What the board's values say about it: voltages are written as V x 1023.6
  (0xBFEE = 48.00 V, 0xC7ED = 50.00 V setting), i.e. it holds 48.0 V online
  and 50.0 V default; currents raw 70 and 210 are 3.5 A and 10.5 A under
  `x20` (both on a 0.5 A grid), 3.55 / 10.66 A under `ratio1024`, 2.33 /
  7.0 A under `x30` - the board most likely encodes A x 20. The unit's
  persistent defaults are therefore 50.0 V / raw 210 right now, rewritten
  every 0.6 s; B3.0 cannot hold while the board is attached.
- Board class (from the signature and from Chinese forum threads on
  "communication-control boards" for the R4850G2/G6, which are blocked for
  automated fetching: mydigit.cn threads 134438 and 285596, crystalradio.cn
  thread 1620930): a small MCU + CAN-transceiver PCB wired to CAN-H/CAN-L
  and the rectifier's aux power, with a display and knob/buttons, that
  continuously re-sends its setpoints and also writes them as the unit's
  defaults; variants with touch screen and Bluetooth exist (Xianyu "big
  cannon" CNC builds). The register 0x13 broadcast every 3 s is this
  board's; the only other sighting is a power-up candump (CP). Output 0 V
  with online 48 V requested and standby = 0 suggests the board also gates
  the output (relay or slot-detect), as several of these boards do.
- Captures: `captures/20261024-181026-identify.jsonl`,
  `captures/20261024-181052-sniff-charger-with-controller.jsonl`.

### 2026-10-24, later — the front-end identified by class; voltage-scale warning

- The charger matches the hand-built "Huawei big cannon adjustable power
  supply" conversions sold by individual builders on Xianyu (smzdm article
  2026-01-10, https://post.smzdm.com/p/ako7rp58/): rewound transformer, new
  MOSFETs/capacitors, touchscreen + Bluetooth app, full-charge auto
  shut-off, machined or original housing. The user's unit: black case,
  slightly different screen, a personal photo at start-up (builder's own
  firmware image). No maker, no model, no documentation; the board is a
  one-off CAN master and cannot be silenced or reprogrammed over CAN. The
  hardware route (lift the transceiver TXD pin so the board becomes
  receive-only) is the only way to keep the screen with the GX as master.
- **Warning:** the article's build outputs 71-101 V from a 48 V module (the
  feedback is moved so the module regulates to a 48 V-class internal reading
  while the terminals carry ~1.7x). Our unit's CAN values are 48 V-class,
  which proves nothing. **Before any battery is connected: enable the
  output, measure the terminals with a meter and compare with 0x75.** Until
  that is done once, 0x75 is not trusted evidence of the terminal voltage
  and the verify-by-measurement step needs the meter beside it.
- The 0 V output seen so far fits these builds: output relay open until a
  battery is sensed or the app enables it; the touchscreen may be inactive
  while the output is off, or the controls live in the phone app (a
  Bluetooth scan with the charger powered would show what it advertises).
