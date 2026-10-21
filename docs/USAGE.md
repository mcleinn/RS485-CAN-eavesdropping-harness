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
| `rsh discover --port COMx --baud 19200` | **Seplos BMS 3.0 service socket** (the pack's RS485-1/-2 sockets, not the inverter socket). Discover recognises it automatically (it also retries at the other baud if nothing answers at the given one) and prints pack state, all cell voltages, the cell spread and all 106 settings by number and name; the key charge settings (102 Charging request voltage, 103 Charging request current, cell/pack high-voltage alarm and protection, float and equalization values) are listed first. Read-only: only Modbus function 0x04 is sent. On the inverter socket discover says so ("no settings are served here"). |
| `rsh seplos-set --port COMx --setting 102 --value 55.2` | **EXPERIMENTAL guarded write** on the Seplos service socket, for settings 102 (Charging request voltage, 48.0-57.6 V) and 103 (Charging request current, 10-200 A) only. Without `--apply` it is a dry run: it reads the settings, shows the present value and the frame it would send. It refuses values outside the limits, refuses to raise a value unless `--allow-increase` (needed to restore), refuses when the settings read-out looks implausible, and decides success by reading the register back. Prints the command that restores the old value. Not yet proven on a real pack - use BMS Studio if in doubt. |
| `rsh ports` (CAN section) | gs_usb adapters (USBCANFD DUAL = candleLight/Candlelight 2.5, CANnectivity) with their channels as `can:gs:<index>.<channel>`, Slcan COM ports, SocketCAN interfaces, and a warning if a board sits in the STM32 DFU bootloader. Needs `pip install libusb1`. |
| `rsh identify --port can:gs:0 [--passive-only] [--bitrates 500000,250000,125000]` | CAN variant of identify: listen-only per bitrate, stops at the first rate with valid frames; verdicts name Huawei R48xx PSUs (address, heartbeat period, whether another controller is commanding them), Pylontech-CAN batteries (ids, brand string, inverter keep-alive present or not), error-frames-only = wrong bitrate. Active phase only for a Huawei PSU with no other controller on the bus: one status request + info. |
| `rsh huawei caps --voltage-max V --current-max A [--current-scale ratio1024\|x20\|x30] [--nominal-a A]` | Hard caps and the per-unit current scale in `.rsh-config.json` (defaults 55.2 V / 0 A, no scale): a setpoint above a cap is refused, never clamped; a current setpoint without an established scale is refused. No bus access. |
| `rsh huawei status --port can:gs:0 [--addr 1]` | One 0x40 request at 125 kbit/s, the decoded burst: AC in V/Hz/A/W, DC out V/A/W, efficiency, temperatures, current-limit ratio (0x76), status flags. Warns when frames from another controller are seen. Read-only. |
| `rsh huawei info --port can:gs:0` | 0x50 + e-label: nominal current (stored as `huawei.nominal_a`), serial, barcode, versions, slot id, board type. Read-only. |
| `rsh huawei set --port can:gs:0 --voltage V [--current A] [--persist] [--apply]` | Dry run by default (prints the exact frames, e.g. `0x108180FE 01 00 00 00 00 00 D2 00` = 52.5 V). `--apply` sends to the on-line registers 0x00/0x03 and checks the ack (byte 0 = 0x21 is a range error, reported verbatim), then verifies by measurement (0x75 within 0.3 V after 2 s) because set values cannot be read back. `--persist` writes the non-volatile defaults 0x01/0x04 instead (do this first with safe values: they are what the PSU falls back to ~60 s after the last frame). |
| `rsh huawei calibrate-current --port can:gs:0 [--test-a 5]` | Needs a load drawing more than `--test-a`. Sends the on-line current limit that means `test-a` under each scale policy in turn (never a raw value that any policy would read as more than the cap), reads 0x76 and the measured current, and stores the policy the unit obeys. Reports the equivalence class: on a 52 A unit `ratio1024` and `x20` are the same within 1.5 %. |
| `rsh huawei hold --port can:gs:0 --voltage V [--current A] [--duration s]` | Keep-alive loop: status every 1 s, setpoints re-sent every 5 s, `CHANGE` lines on output V/I/limit/flags, `STATE` heartbeat; Ctrl+C or any error stops sending and the PSU reverts to its stored defaults by itself. Same caps/scale refusals as `set`. |
| `rsh huawei sim --port can:virtual:lab [--nominal-a 52] [--scale ratio1024] [--load-ohm 2]` | Simulated R48xx PSU (status/info/e-label/acks with 0x21 range errors, heartbeat, 60 s revert, configurable current scale and load) - the fixture behind `tests/test_huawei.py` and a bench substitute on a second channel. |
| `rsh battery-sim --port can:gs:0 --can-profile pylontech-lv\|victron\|dyness-l\|dyness-l-ext\|luxpower\|seplos-can [--string n]` | CAN battery emitter from the same `BatteryState` as the RS485 simulator: profile decides frame set, DLCs, brand string, whether 0x35A/0x370-0x379 need the 0x305+0x307 unlock, Dyness native frames, 0x379 width; `--string` adds the LV-HUB id offset. Bench substitute for the LuxPower side. |
| `rsh inverter-sim --port can:gs:0 --can-profile pylon-inverter\|victron-gx\|pylon-unlock\|silent-inverter` | CAN inverter/GX side: 0x305 (+0x307 zero or 'VIC') every second, logs and decodes the battery, Victron rule (charge stops when 0x351 is missing 3 s). |
| `rsh identify --port can:gs:0` (battery bus) | Listen-only sweep; names the battery dialect from what it hears (ids, brand, DLCs, 29-bit quirks, Dyness native frames, LV-HUB strings) and whether an inverter keep-alive is present. Only when no inverter is on the bus it sends 0x305+0x307 for 3 s and reports which extension frames appear. |
| `rsh discover --port can:gs:0 [--listen 6] [--passive-only]` | CAN inventory: every id with count/period/DLC/last bytes and decoded name, dialect ranking with reasons, quirks (DLC deviations, padding, 29-bit ids, error frames), extension unlock when allowed, merged state snapshot. Report JSON next to the session. |
| `rsh dump --port can:gs:0 [--listen 4] [--passive-only]` | Battery snapshot from the broadcasts (limits, SOC/SOH, V/I/T, flags, module count, cell extremes, Dyness cells) to JSON. |
| `rsh soc --port can:gs:0` | SOC/SOH/V/I/T table from 0x355/0x356, one row per LV-HUB string. |
| `rsh sniff --ch1 can:gs:0 [--ch2 can:gs:0.1] --baud 500000 [--quiet]` | Listen-only CAN tap(s): every frame decoded and logged as `kind="can"`, the same `CHANGE`/`STATE` lines (CCL/CVL/enable/OV-warn/current) as on RS485, markers, `--service` unchanged. `rsh timeline` and `rsh diagnose` read these captures unchanged (CAN records carry `id`, `ext`, `dlc`, `can_ch`, `flow`). |
| `rsh bridge --ch1 can:gs:0 --ch2 can:gs:0.1 --baud 500000 [--policy cap:ccl_max=40,cvl_max=55.2] [--loss-timeout 10]` | CAN gateway between two terminated segments: store-and-forward relay of every frame both ways (error frames logged, never forwarded); with a policy only 0x351 in the BMS->INV direction is modified, CVL/CCL downwards only, DCL/DVL and all other ids locked, reset on 0x351 loss, two-record audit, far-side no-ACK counted and reported. Both channels must be CAN and in normal (ACKing) mode. |
| `rsh selftest can:gs:0.0 can:gs:0.1` | Both channels on ONE terminated bus: frames cross each way byte-exact and get ACKed at 500/250/125 k (11-bit, 29-bit, DLC 2), and a listen-only receiver really does not ACK. |
| `rsh seplos-monitor --port COMx` | **Continuous read-only monitor on the Seplos service socket** (RS485-1/-2, 19200). Every 2 s (`--interval`) it logs all 16 cell voltages, top/low cell and spread, the battery's own mode, alarm and protection flags, per-cell high-voltage alarms, charge/discharge switch state, balancing per cell and the BMS-internal allowed charge current; every 60 s it re-reads the settings and reports any change. Prints `SVC CHANGE ...` lines and a 30 s `SVC STATE` line. Refuses to start if the port is not a service socket. |
| `rsh sniff --ch1 COMa --service COMb --quiet` | **Tap on the inverter link + service monitor in one session, one clock** - the recommended way to record a charge. `COMa` listens on the inverter link (never transmits), `COMb` polls the service socket (read-only). Either adapter works for `COMb`: the second Waveshare channel or the Seplos USB-RS485 converter. Then `rsh timeline <session>` shows both sides: which cell ran ahead, which alarm or protection the battery raised, whether the battery opened its charge switch and at what current, next to what the inverter was told and did. A service session recorded separately can be merged with `rsh timeline tap.jsonl --service svc.jsonl`. |
| `rsh soc --port COMx` | **Quick per-pack SOC/V/I table** for a Pylontech stack — one `0x42 FF` poll returns every pack; prints SOC / voltage / current / capacity / cycles per pack + stack total. Falls back to per-pack `0x42` at ADR 02.. when the firmware lacks `FF` (US5000 fw 2.3 does). Defaults to 115200 baud (monitoring). |
| `rsh dump --port COMx` | **Complete per-unit battery state to JSON** for later analysis — scans pack addresses and polls every command at each: protocol version, identity (device name / manufacturer / **firmware**), serial, fixed limits & temp thresholds, full analog (**every cell voltage**, temps, current, voltage, capacity, cycles, SOC), and charge/discharge management. Saved as `<session>.dump.json`. |
| `rsh battery-sim --port COMx` | Ideal Pylontech battery (slave). `--preset ideal\|seplos-280l`, `--profile` picks dialect quirks, `--set soc=20 --set current_a=-15 --set voltage_v=51.2` overrides any state field, `--drift` animates values |
| `rsh inverter-sim --port COMx` | Inverter master. `--profile voltronic-6x` (0x61/62/63 @ ADR 1, like SAKO "PYL" should) or `monitoring-4x` (0x42-family walk); `--adr`, `--ver` overrides |
| `rsh sniff --ch1 COMa [--ch2 COMb]` | Passive capture, live decode, garbage classification (Modbus? wrong baud?) |
| `rsh bridge --ch1 COMa --ch2 COMb` | Man-in-the-middle. **Transparent** by default (byte-level pass-through, no store-and-forward). **Modify** mode when any rule is given: header rules `--fix-cid2-echo`, `--force-ver b2a=20`, `--force-adr b2a=02`, and/or a payload `--policy NAME[:k=v]` on BMS→INV responses (roles via `--role-ch1/--role-ch2`, default ch1=inverter, ch2=battery). Mode is in the start note and on every record; every modified frame is logged twice (ORIGINAL rx + MODIFIED tx with audit) |
| `rsh timeline [session]` | Charge-control analysis of a tap/bridge capture: request↔response pairing, decoded series, event log, timing stats, findings, `*.timeline.csv/json` (+PNG if matplotlib). Scaling overrides `--ccl-factor/--current-factor` for H5 |
| `rsh mark "maxcell=3.550"` | Append a timestamped, numbered MARK to a session (`--session f`, `--at epoch/ISO`). While `sniff`/`bridge` run in a terminal, markers are **one keypress**: bare Enter = numbered marker; `r`/`o`/`l`/`z`/`x` = rise/OV-warn/red LED/zero/odd; a number = inverter current; any text verbatim. Use `--quiet` so only decoded CHANGE lines, a 30 s STATE heartbeat and your markers appear |
| `rsh diagnose [session.jsonl]` | Hypothesis checks over a capture (default: newest). Findings tagged INFO/WARN/FAIL |
| `rsh send --port COMx` | Send **one** frame and decode the reply. Fields (`--ver 20 --adr 02 --cid2 61 --info ...`) or raw `--hex 7E..0D` (verbatim, can be malformed); `--expect-cid2 61` decodes the reply payload. The quickest way to hand-talk to a device |
| `rsh script --port COMx scenario.json` | Run a **scenario**: either a proactive **master** send/branch sequence, or a reactive **responder** ("if it writes X, answer Y"). See below |
| `rsh decode "~2001...FD35"` | Decode one pasted frame (hex or ASCII), incl. payload fields |
| `rsh demo [--identify] [--set soc=20]` | No-hardware loopback: battery-sim vs inverter-sim (or identify) in-process |
| `rsh menu` | Interactive runner: pick WHAT (op) / WHICH (profile) / WHERE (channel); ops run concurrently as jobs (e.g. battery-sim on ch2 while identify runs on ch1); `jobs` lists/stops; `w` toggles live frame output |

## Strategy for the SAKO ↔ Seplos problem

> For the terse at-the-bench version (wiring table, exact commands, tick
> boxes), use **`BENCH-CHECKLIST.md`**. This section explains the reasoning
> behind those steps.

Wiring context (per the Mason 280L 3.0 spec sheet; details in
`PROTOCOL-NOTES.md`): on this pack the CAN socket is CAN-only (pins 4/5 =
CAN-H/CAN-L), and RS485 is on the **RS485 sockets: A=2/7, B=1/8, GND=3/6**.
SAKO BMS port: B=3, A=5, GND=7/8. Crossover: Seplos 2/7→SAKO 5 (A),
Seplos 1/8→SAKO 3 (B), GND→8. But which Seplos socket actually serves the
PYLON-to-inverter link isn't settled by the spec — run `rsh identify` on
each socket to find out. Battery DIP1 ON (pack 1), power-cycled.

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

## Scripting your own exchanges (`send` / `script`)

For probes the built-in tools don't cover, drive the wire directly.

**One frame:** `rsh send --port COM7 --cid2 61 --adr 02 --expect-cid2 61`
(or `--hex 7E32...0D` for a verbatim/malformed frame). Prints the decoded
reply + payload; `--json` for automation.

**Scenarios** (`rsh script --port COM7 file.json`) come in two modes; mode
is inferred (`steps` → master, `rules` → responder) or set explicitly:

- **responder** — reactive rules, the "if it writes X, answer Y" case. Each
  incoming frame is matched against `rules` in order; the first match's
  action fires: `respond` a frame, `respond: null` (silence), `stop`, with
  optional `delay_ms`/`note`. Unmatched → `unmatched: "silence"|"rtn04"`.
  This is a fully user-defined battery/BMS emulator. Example:
  `docs/examples/responder-echo-adr.json`.
- **master** — proactive `steps`: each sends a frame, waits `wait_ms`, then
  branches via `expect` cases (`goto: next|stop|<label>`) matched against
  the reply; `default` handles no-match/timeout. Good for custom scans,
  conditional probes, replay-and-branch. Example:
  `docs/examples/master-address-scan.json`.

**Match conditions** (both modes): `cid2` (hex or list), `adr`, `ver`,
`cid1`, `family` (`4x`/`6x`), `info_prefix`, `info_regex`, `hex_regex`,
`is_request`, `is_response`, `has_errors`, `no_reply`. **Frame specs**:
fields `ver/adr/cid1/cid2/info` (values may echo the trigger via
`"$req.adr"`), `rtn` shortcut, or `hex`/`raw_hex` verbatim. 0x64 shutdown
is refused unless the scenario sets `allow_shutdown: true`. Everything is
logged to the session JSONL like every other tool.

Because scenarios are plain JSON, an LLM can generate and run them on the
fly — this is the "hand the model the wire" surface.

## Full-screen UI (`rsh tui`)

`pip install -e ".[tui]"` (adds Textual), then `rsh tui [--port COM5]`.
Left: the actions (Enter runs the highlighted one; touch works in Termux).
Top: PORT, PORT2, BAUD. Right: live log fed by the same `on_event`
callbacks the CLI prints. Keys: `s` stop the running sniff/monitor,
`c` clear log, `t` light/dark Claude theme, `q` quit. Every action writes
the usual session files under `captures/`; "Battery check" writes a
dated folder under `captures/battery-checks/`.

On the phones: `rsh-tui` (= `tools/termux/rsh-usb tui`, so the adapter
descriptor is in place and `usb:A`/`usb:B` work).

## Pre-purchase battery check (Pylontech-protocol packs, e.g. Dyness DL5.0C Pro)

What one `tools/termux/battery-readout.sh` run (or the same three commands
by hand) gives you, read-only, in under two minutes:

| from | fields |
|---|---|
| `identify` | protocol, baud, address, which command families answer |
| `discover` | every command supported/silent/error, dialect quirks, decoded state |
| `dump` | per unit: all cell voltages (spread = balance health), temperatures, pack V/I, SOC, remaining/total Ah, **cycle count**, charge/discharge limits, alarms/protections, **serial number**, firmware version, manufacturer |

SOH is only present if the pack puts it into the 0x42 user-defined block
(Pylontech US-series do; a Dyness B3 did not, per the field report linked in
`docs/refs/MANUAL-DOWNLOADS.md`). Cycle count and cell spread under load are
the stronger evidence anyway.

Dyness DL5.0C Pro specifics (manual in `docs/refs/dyness`): RS485 on the
"485" RJ45, **pin 1 = 485B, pin 2 = 485A**; 9600 8N1 with DIP #4 off (115200
if on); protocol family Pylontech 0x42 (monitoring) on addresses 00-02, the
0x6x inverter family may stay silent - `discover` shows this as "silent",
not as a fault. Battery power switch ON, nothing else connected. If the
pack answers at several addresses, trust the one whose voltage/current look
real (field report: a B3 answered at 00/01/02 with sane data only at 02);
`dump` prints all of them.

Kit: phone2 + Waveshare on OTG (or the notebook), an RJ45 patch cable cut
open or an RJ45 breakout, two wires to the adapter's A/B. The simulated
Dyness (`rsh battery-sim --profile dyness-dl --preset dyness-dl5`) is what
the test suite checks the readout against.

## Android / Termux (no root)

CAN on the phone: the same `termux-usb -r -e` descriptor path serves the USBCANFD DUAL (gs_usb, no kernel driver needed). With the CAN adapter plugged in, `rsh-usb ports` lists `can:gs:.0` / `can:gs:.1`, and every command above takes those specs (`rsh-usb identify --port can:gs:.0 --passive-only`, `rsh-usb huawei status --port can:gs:.0`). Untested on the Pixel as of 2026-10-22.

The harness runs unchanged on a phone in Termux; only the serial transport
differs. Android never exposes `/dev/ttyUSB*` to an app; it hands over one
already-open USB file descriptor per device after the user taps "allow".
`rs485harness/core/ftdi_usb.py` therefore drives FTDI chips (FT232R/X,
FT232H, FT2232H = Waveshare 2CH, FT4232H) directly over libusb from that
descriptor. Non-FTDI adapters (CH340, CP210x) are not supported on Android.

Setup once (Termux from F-Droid/GitHub or the current Play build, plus the
separate **Termux:API** app):

```
pkg install python libusb termux-api git openssh
pip install libusb1 pyserial
git clone <repo> ~/dev/inverter && cd ~/dev/inverter && pip install -e ".[dev]"
python -m pytest -q            # 90 tests, hardware-free (~90 s on a phone)
```

Plug the adapter in through a real OTG adapter or USB-C hub (the phone must
switch to host role; a charge/data cable without the CC resistor will not do
it - the adapter's LED stays off). Then run any `rsh` command through the
launcher, which asks Android for the device and passes the descriptor:

```
tools/termux/rsh-usb ports                       # usb:A, usb:B for a Waveshare 2CH
tools/termux/rsh-usb identify --port usb:A
tools/termux/rsh-usb sniff --ch1 usb:A --ch2 usb:B --quiet
tools/termux/rsh-usb selftest usb:A usb:B        # A-A / B-B cross-wired
RSH_USB_DEV=/dev/bus/usb/001/021 tools/termux/rsh-usb ports   # if several devices
```

Port specs: `usb:A` / `usb:B` (device from the launcher), or `usb:<fd>:A`
when you obtained the descriptor yourself (`termux-usb -r -e`). What is
different from a COM port: no automatic reopen after a USB re-enumeration
(Android hands the descriptor over once; replug and restart), and the
FTDI latency timer is set to 1 ms by the harness itself. Everything else
(sessions, `timeline`, markers, simulators, bridge) is identical.

Verified 2026-10-16 on a Pixel 7 Pro (Android 16): FT2232H opened via
`termux-usb`, both channels claimed, configured and read error-free
(2000 bulk reads, 0 errors) - after replacing the Waveshare's USB cable;
the original cable caused re-enumeration every few seconds and bursts of
USB I/O errors (the same cable caused the drop-outs on the notebook). A
Motorola Edge 60 Pro did not enter USB host mode with the adapters tried.
The transport rides through transient USB errors and uses a 4 ms latency
timer; one `USBErrorIO` burst no longer ends a session.

What is installed on the phones (Termux, `~/bin` on PATH):
`rsh-usb <cmd>` and `rsh-tui` (harness with the adapter), `battery-readout.sh`
(home directory), `claude` and `opencode` (both run inside a proot Alpine
container because neither ships an Android build; Termux home is visible
there as `/root/termux-home`; first run asks for login).


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
