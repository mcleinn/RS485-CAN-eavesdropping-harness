# rs485harness — inverter ↔ battery RS485 test harness

Diagnoses why a battery BMS and an inverter that both claim to speak
"RS485 Pylontech" don't talk to each other — built for a **SAKO SUNON V
6.5 kVA** (protocol "PYL") vs **Seplos Mason 280L / BMS 3.0** (mode
"RS485-Pylontech") link, but protocol- and device-agnostic by design.

Windows-first, pure Python (pyserial + stdlib), runs unchanged on
Linux/Raspberry Pi.

## What's inside

- **Analyzer**: 2-channel sniffer, man-in-the-middle bridge with optional
  live frame **translation** (fix response address / version byte /
  CID2-echo), garbage classifier (Modbus RTU? wrong baud?), and a
  hypothesis-driven `diagnose` over captured sessions.
- **Identify** ("who is there?"): passive listen + active interrogation
  sweep (baud × address × protocol version × command family, plus
  Voltronic-LIB and Seplos-native Modbus probes) → plain-language verdicts
  including which setting to switch when protocols don't match.
- **Discover** ("what do they offer?"): full inventory of a responder —
  every command classified supported/unsupported/error/silent, all
  payloads decoded into a state snapshot, dialect quirks flagged; Modbus
  register blocks read and decoded for LIB-mode batteries. Reports persist
  as JSON for diffing across settings/firmware changes.
- **Scripting** (`send` / `script`): send arbitrary single frames, or run
  JSON scenarios — a proactive master send/branch sequence, or reactive
  "if it writes X, answer Y" responder rules. The LLM-drivable "hand me the
  wire" surface.
- **Simulators**: ideal Pylontech battery (both command families,
  configurable dialect quirks and state: `--set soc=20 ...`) and a
  Voltronic-style inverter poller — each bindable to any COM port.
- **Virtual 2-ch analyzer**: software model of the Waveshare USB TO 2CH
  RS485 (multi-drop buses, TX echo, baud-mismatch garbling, cross-wiring)
  so the entire suite runs and tests without hardware.
- **Adapter self-test** for the real Waveshare (ch1+ch2 cross-wired).
- **Sessions**: everything on the wire lands in timestamped JSONL under
  `captures/` — decoded, error-annotated, LLM-friendly.

## Install & first steps

```
pip install -e ".[dev]"
python -m pytest          # 33 tests, all hardware-free
rsh demo                  # sim inverter vs sim battery over loopback
rsh menu                  # interactive runner (concurrent jobs per channel)
rsh identify --port COM5  # first thing to run against a real device
```

## Documentation

- `docs/BENCH-CHECKLIST.md` — terse at-the-bench step card (wiring +
  exact commands + pass/fail gates)
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
