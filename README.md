# rs485harness — inverter ↔ battery RS485 test harness

Diagnoses why a battery BMS and an inverter that both claim to speak
"RS485 Pylontech" don't talk to each other — built for a **SAKO SUNON V
6.5 kVA** (protocol "PYL") vs **Seplos Mason 280L / BMS 3.0** (mode
"RS485-Pylontech") link, but protocol- and device-agnostic by design.

Windows-first, pure Python (pyserial + stdlib), runs unchanged on
Linux/Raspberry Pi and on Android in Termux without root (FTDI adapters
driven over libusb from the descriptor `termux-usb` hands over; see
`docs/USAGE.md`, "Android / Termux").

## What's inside

- **Analyzer**: 2-channel sniffer, man-in-the-middle bridge with optional
  live frame **translation** (fix response address / version byte /
  CID2-echo), garbage classifier (Modbus RTU? wrong baud?), and a
  hypothesis-driven `diagnose` over captured sessions.
- **Identify** ("who is there?"): passive listen + active interrogation
  sweep (baud × address × protocol version × command family, plus
  Voltronic-LIB and Seplos-native Modbus probes) → plain-language verdicts
  including which setting to switch when protocols don't match.
- **Read a Pylontech stack**: `soc` (quick per-pack SOC/V/I table via one
  all-packs poll) and `dump` (complete per-unit state — every cell voltage,
  temps, limits, firmware, serial — to JSON for later analysis).
- **Discover** ("what do they offer?"): full inventory of a responder —
  every command classified supported/unsupported/error/silent, all
  payloads decoded into a state snapshot, dialect quirks flagged; Modbus
  register blocks read and decoded for LIB-mode batteries. Reports persist
  as JSON for diffing across settings/firmware changes.
- **Seplos BMS 3.0 service socket** (RS485-1/-2, native Modbus @ 19200):
  `identify`/`discover` recognise it by themselves (discover also tries the
  other baud when nothing answers) and say which kind of socket the
  adapter is on. `discover` then prints pack state, all 16 cell voltages
  and all 106 settings by number and name (e.g. 102 Charging request
  voltage). Read-only. `seplos-set` is an EXPERIMENTAL guarded write for
  the two charge-request settings only (102 voltage, 103 current): dry run
  unless `--apply`, hard limits, lower-only unless `--allow-increase`,
  refuses on an implausible read-out, verifies by read-back. Tested only
  against the built-in simulated service socket so far.
- **Seplos service-side monitor** (`seplos-monitor`, or `sniff --service
  PORT` to record it together with the inverter-link tap in ONE session on
  one clock): polls the service socket every 2 s, read-only, and logs all
  16 cell voltages, which cell is on top and the spread, the battery's own
  mode (charge / float / full / standby), every alarm and protection flag,
  per-cell high-voltage alarms, the charge/discharge switch (FET) state,
  balancing per cell, the BMS-internal allowed charge current, and any
  change of a setting. Prints only changes plus a 30 s state line.
  `timeline` merges it: service events in the event log, findings such as
  "the battery switched its charge path OFF (current just before: X A)",
  per-cell peaks, and a `<session>.service.csv` with one column per cell.
- **Scripting** (`send` / `script`): send arbitrary single frames, or run
  JSON scenarios — a proactive master send/branch sequence, or reactive
  "if it writes X, answer Y" responder rules. The LLM-drivable "hand me the
  wire" surface.
- **Charge-control analysis** (`timeline`): pairs requests/responses from a
  tap or bridge capture, decodes every charge-related field (SOC, pack V/I,
  max/min cell, CVL, CCL, DCL, charge-enable, full-charge-request, OV-warn,
  protections), builds a chronological event log (every CCL/CVL/enable/
  alarm change), timing stats (poll period, latency, CCL update interval,
  CCL→current delay, gaps), automatic findings (large CCL increases, current
  exceeding CCL, oscillation near top of charge, gaps before a rise) and
  CSV/JSON (+PNG with matplotlib). Live `MARK ...` markers while capturing
  (`sniff`/`bridge` read stdin) or `rsh mark` after the fact.
- **Modify-mode bridge infrastructure**: byte-level transparent forwarding
  by default; with `--policy` a pluggable payload policy may rewrite
  BMS→INV charge limits under enforced invariants (only CCL/CVL, only
  downwards, status/protection bits locked, bad frames never rebuilt, policy
  reset on comm loss, two-record audit of every change, exception →
  original forwarded). Ships with a trivial `cap` policy as the pipeline
  fixture; experimental shaping policies plug in as classes.
- **Simulators**: ideal Pylontech battery (both command families,
  configurable dialect quirks and state: `--set soc=20 ...`) and a
  Voltronic-style inverter poller — each bindable to any COM port.
- **Virtual 2-ch analyzer**: software model of the Waveshare USB TO 2CH
  RS485 (multi-drop buses, TX echo, baud-mismatch garbling, cross-wiring)
  so the entire suite runs and tests without hardware.
- **Adapter self-test** for the real Waveshare (ch1+ch2 cross-wired).
- **Sessions**: everything on the wire lands in timestamped JSONL under
  `captures/` — decoded, error-annotated, LLM-friendly.
- **TUI** (`rsh tui`, Textual, Claude colour scheme): full-screen interactive
  front end for the core functions — ports, identify, discover, dump,
  one-shot battery check, sniff, Seplos monitor, timeline, self-test — with
  a live log and job control; runs on Windows, Linux and in Termux
  (`rsh-tui` there, adapter via `termux-usb`).
- **Battery pre-purchase check**: `tools/termux/battery-readout.sh` (or the
  TUI's "Battery check") runs identify + discover + dump into one folder:
  cells, temperatures, cycles, serial, firmware, limits, alarms. Dyness
  DL-series dialect simulated and tested (`--profile dyness-dl`).

## Install & first steps

```
pip install -e ".[dev]"
python -m pytest          # 93 tests, all hardware-free
rsh demo                  # sim inverter vs sim battery over loopback
rsh menu                  # interactive runner (concurrent jobs per channel)
rsh tui                   # full-screen UI (pip install -e ".[tui]")
rsh identify --port COM5  # first thing to run against a real device
```

## Documentation

- `docs/CHARGE-TAPER-RESULTS.md` — what was measured, why it rebounds, the 55.2 V fix and its verification at 40/60/80/90 A (charts in `docs/img/`), open points
- `docs/BENCH-CHECKLIST.md` — terse at-the-bench step card (wiring +
  exact commands + pass/fail gates)
- `docs/CHARGE-TAPER-PLAN.md` — the Seplos↔SAKO charge-rebound
  investigation: hypotheses, capability map, Phase-1 capture plan,
  Phase-2 policy principle and fail-safes
- `docs/CAN-PLAN.md` — CAN bus: the USBCANFD DUAL adapter and its firmwares, bus parameters of Huawei R48xx / Pylontech / Victron / Dyness / LuxPower, the integration plan (same commands, protocol by port kind), first test = Huawei R4850G2 control
- `docs/USAGE.md` — tool reference + diagnosis strategy/playbook
- `docs/PROTOCOL-NOTES.md` — condensed protocol ground truth (frame format,
  checksums, both command families, payload layouts, addressing quirks,
  cabling/pinouts, likely root causes for this setup)
- `docs/refs/` — downloaded protocol PDFs and reference source files;
  `docs/refs/MANUAL-DOWNLOADS.md` lists what must be fetched manually
  (captcha/registration) and all upstream repos

## Layout

```
rs485harness/
  core/        transport (serial/loopback), virtual 2ch analyzer, sessions
  protocols/   pylontech (frames, payloads, dialect profiles), stubs for
               seplos_native (Modbus V3) and voltronic (LIB)
  simulators/  battery state/presets, battery-sim (slave), inverter-sim (master)
  analyzer/    identify, sniff, bridge, translate, selftest, diagnose, classify
  cli.py       `rsh` subcommands (all support --json)
  runner.py    `rsh menu` interactive job runner
tests/         golden vectors, payload round-trips, loopback + virtual-analyzer
               integration, fault-injection -> diagnose
```
