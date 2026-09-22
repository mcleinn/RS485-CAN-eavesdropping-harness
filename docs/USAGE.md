# Usage & diagnosis strategies

All tools are subcommands of `rsh` (installed via `pip install -e .`; or use
`python -m rs485harness.cli ...`). Every run writes a JSONL session to
`captures/` — timestamped, hex + decoded + errors per record — designed to
be read by an LLM or `rsh diagnose`. Add `--json` to any command for
machine-readable output. Humans can drive everything from `rsh menu`.

## The tools

| Command | What it does |
|---|---|
| `rsh ports` | List COM ports |
| `rsh selftest COMa COMb` | Verify the 2-ch adapter, ch1+ch2 wired A-A/B-B/GND: byte-exact transfer at 3 bauds, TX-echo detection, latency (flags FTDI 16 ms latency timer), golden-frame pass |
| `rsh identify --port COMx` | **"Who is there?"** — run this first on any channel. Passive listen per baud (who talks, what protocol, what cadence — Modbus polls get their register map named, e.g. "Voltronic LIB"), then active probing: Pylontech ADR 0/1/2 × VER 20/25 × {0x61, 0x42} + Modbus probes (Voltronic-LIB slaves 1/0x0F incl. cell-count read, Seplos-native slave 0 @ PIA). Ends with plain-language verdicts incl. which setting to switch. `--passive-only` to stay silent. |
| `rsh discover --port COMx` | **"What do they offer?"** — full inventory of a responder, after identify told you baud/protocol. Pylontech: sweeps ALL commands (both families), classifies each ok/unsupported(RTN=04)/error/silent, decodes every payload into a state snapshot (voltage, SOC, limits, alarms, cells, identity, serial), flags dialect quirks (response-ADR policy, CID2 echo, VER change). Modbus/LIB: reads the documented register blocks and decodes them. Report saved as `<session>.discovery.json` — diff it against the inverter's demands, the ideal sim, or the same battery after a settings/firmware change. |
| `rsh battery-sim --port COMx` | Ideal Pylontech battery (slave). `--preset ideal\|seplos-280l`, `--profile` picks dialect quirks, `--set soc=20 --set current_a=-15 --set voltage_v=51.2` overrides any state field, `--drift` animates values |
| `rsh inverter-sim --port COMx` | Inverter master. `--profile voltronic-6x` (0x61/62/63 @ ADR 1, like SAKO "PYL" should) or `monitoring-4x` (0x42-family walk); `--adr`, `--ver` overrides |
| `rsh sniff --ch1 COMa [--ch2 COMb]` | Passive capture, live decode, garbage classification (Modbus? wrong baud?) |
| `rsh bridge --ch1 COMa --ch2 COMb` | Man-in-the-middle forwarding + logging. Translation: `--fix-cid2-echo`, `--force-ver b2a=20`, `--force-adr b2a=02` |
| `rsh diagnose [session.jsonl]` | Hypothesis checks over a capture (default: newest). Findings tagged INFO/WARN/FAIL |
| `rsh decode "~2001...FD35"` | Decode one pasted frame (hex or ASCII), incl. payload fields |
| `rsh demo [--identify] [--set soc=20]` | No-hardware loopback: battery-sim vs inverter-sim (or identify) in-process |
| `rsh menu` | Interactive runner: pick WHAT (op) / WHICH (profile) / WHERE (channel); ops run concurrently as jobs (e.g. battery-sim on ch2 while identify runs on ch1); `jobs` lists/stops; `w` toggles live frame output |

## Strategy for the SAKO ↔ Seplos problem

Wiring context (from research; details in `PROTOCOL-NOTES.md`):
Seplos inverter port = RJ45 labelled **CAN** (A=4, B=5); SAKO BMS port:
B=3, A=5; needs a crossover cable. Battery DIP1 ON (pack 1), power-cycled.

### Day 0 (no adapter yet) — all simulated
```
python -m pytest                 # full suite incl. virtual 2-ch analyzer
rsh demo                         # sim inverter vs sim battery
rsh demo --identify              # identify scan vs sim battery
rsh diagnose                     # findings over the demo capture
```

### Day 1 with the Waveshare adapter
1. **Prove the adapter**: join ch1+ch2 (A-A, B-B, GND) → `rsh selftest COMa COMb`.
   Fix FTDI latency timer to 1 ms if the test warns.
2. **Interrogate each device separately** (one channel at a time), each in
   two steps — `identify` (who/what protocol/which baud), then `discover`
   (full capability + state inventory):
   - `rsh identify --port COM_bat` on the battery (correct Seplos socket!).
     Expected good outcome: "RESPONDER: Pylontech-ASCII battery-like
     device", ADR/latency listed. If instead "Modbus RTU device (seplos-v3)"
     appears, the PYLON setting isn't active on that port. If SILENCE:
     wrong socket/pinout/DIP.
   - `rsh identify --port COM_inv --passive-only` on the inverter with PYL
     selected. Expected: "ACTIVE TALKER ... polling like an INVERTER"
     with its CID2 family and cadence. If the inverter polls Modbus
     instead → its "PYL" is actually the Voltronic-LIB register protocol.
   - After a successful identify: `rsh discover --port COM_bat` — archives
     the battery's complete command set, quirks and decoded state. Re-run
     it after every protocol-setting or firmware change on the BMS and diff
     the `.discovery.json` reports. The command-support table vs. the
     CID2s the real inverter polls (from sniff/battery-sim sessions) is
     the core compatibility comparison.
3. **Substitution tests** (the decisive experiments):
   - Battery-sim toward the real inverter: `rsh battery-sim --port COM_inv
     --preset seplos-280l`. If the inverter now shows a battery, inverter +
     cable are fine → the real battery's replies are the problem; the
     session shows exactly what the inverter asks.
   - Inverter-sim toward the real battery: `rsh inverter-sim --port
     COM_bat --profile voltronic-6x` (try `--adr 1`, `--adr 2`, `--ver 25`,
     and `--profile monitoring-4x`). Stats show which family/ADR/VER the
     battery serves.
4. **Full MITM**: inverter on ch1, battery on ch2 → `rsh bridge --ch1 ...
   --ch2 ...`, let it run through a recognition attempt, then
   `rsh diagnose`. Compare the real battery's 0x61/0x62/0x63 payloads with
   a `battery-sim` session (H10 finding prints the decoded fields).
5. **Translate if needed**: e.g. battery answers with wrong response ADR or
   echoes CID2 → `rsh bridge --force-adr b2a=02 --fix-cid2-echo` and see if
   the inverter accepts the link with rewriting on.

### What gets logged (completeness guarantees)
`captures/*.jsonl`: one JSON object per wire event with `t`, `chan`,
`dir` (rx/tx/fwd), `kind` (frame/raw/note), `hex`, `ascii`, `decoded`
(header fields + errors), optional `note` (latency, rewrites).

Everything needed for later analysis is in these files:
- **All wire bytes, both directions** — every Pylontech frame (decoded,
  with per-field error annotations) and every unframed raw burst
  (classified: Modbus? line noise?). Nothing on the bus is dropped.
- **Context notes**: tool start/stop with ports, baud and profile; every
  baud switch during an identify scan (so each record's baud is
  reconstructable); identify phase markers and probe traffic; selftest
  check results; simulator decisions *not* to answer (ignored ADR,
  silence-on-unknown); timeouts; bridge rewrites; final stats.
- **Result reports persist as JSON** next to the session:
  `<session>.identify.json`, `<session>.selftest.json`,
  `<session>.diagnosis.json`, `<session>.demo.json` — verdicts/findings
  survive without re-running anything.

`rsh diagnose --json` gives the structured findings (and re-classifies
raw records on the fly, so even minimal captures get full analysis); the
hypothesis matrix lives in `rs485harness/analyzer/diagnose.py`.

## Extending

- New battery dialect quirk: add a `DialectProfile` in
  `protocols/pylontech/dialects.py` (data-only) — simulators, translator
  and menu pick it up automatically.
- New protocol (Voltronic-LIB serving, Seplos-native polling): implement a
  module under `protocols/`, reuse `core.transport` + `core.session`;
  register response builders analogous to `simulators/battery_sim.py`.
  The register maps are already in `docs/refs/`.
- Linux/Raspberry Pi: everything is pyserial + stdlib; port names become
  `/dev/ttyUSB0`; virtual pairs via `socat -d -d pty,raw,echo=0
  pty,raw,echo=0` (Windows equivalent: HHD Free Virtual Serial Ports —
  com0com is unsigned/dead; avoid the third-party re-signed builds on
  com0com.com).
