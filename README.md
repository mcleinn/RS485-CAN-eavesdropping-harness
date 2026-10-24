# RS485 / CAN eavesdropping harness (`rsh`)

A protocol test harness for the serial buses between batteries, inverters
and chargers: **RS485** (Pylontech-style ASCII, Modbus RTU) and **CAN**
(the Pylontech/Victron LV-CAN family, Huawei R48xx rectifiers). It listens
without disturbing the bus, says who is talking and in which dialect,
inventories what a device offers, records everything on the wire in
machine-readable sessions, and — only when asked, within hard limits — sits
between two devices or commands a charger.

Brand-independent by design: protocol knowledge lives in data tables and
dialect profiles, every disputed reading of a field is carried as data
rather than decided, and every transport (serial, two-channel RS485
adapter, gs_usb CAN adapter, in-process virtual buses) is exercised by the
same commands. Pure Python; Windows-first, runs unchanged on Linux /
Raspberry Pi and on Android in Termux without root (adapters driven over
libusb from the descriptor `termux-usb` hands over).

## Tested with

- **Inverters**: SAKO SUNON V 6.5 kVA (RS485 "PYL" protocol) and the
  Voltronic-style LIB command family it descends from; LuxPower SNA PRO-EU
  (CAN, from documentation and simulation).
- **Batteries / BMS**: Seplos Mason 280L with BMS 3.0 (RS485-Pylontech
  mode and the native Modbus service socket), Pylontech US2000 / US3000C /
  US5000 (RS485 and CAN), Dyness DL5.0C Pro / DL5.0F (RS485 dialect and
  CAN "DYNESS-L" incl. native frames), Victron CAN-bus BMS profile, Seplos
  CAN.
- **Chargers**: Huawei R4850G2 / R48xx rectifier family (CAN, community
  protocol; bench qualification pending, see `docs/PHASE-B-RUNBOOK.md`).
- **Adapters**: Waveshare USB TO 2CH RS485 (FT2232H), plain USB-RS485
  dongles, USBCANFD DUAL (gs_usb / Candlelight firmware, both channels,
  listen-only taps); SocketCAN and slcan through the optional python-can
  backend.

Simulators exist for every device class above, so the whole suite runs and
is tested without hardware. The field investigation that drove the design
(a charge-current rebound between a BMS and an inverter, resolved by a
single BMS setting) is written up in `docs/CHARGE-TAPER-RESULTS.md`.

## What's inside

- **Identify** ("who is there?"): passive listen, then an active
  interrogation sweep (baud × address × protocol version × command family,
  plus Modbus probes) on RS485; a listen-only bitrate sweep on CAN. Plain
  language verdicts, including which setting to switch when two devices
  claim the same protocol but do not match.
- **Discover / dump / soc** ("what do they offer?"): full inventory of a
  responder — every command classified supported / unsupported / error /
  silent, every payload decoded into a state snapshot, dialect quirks
  flagged, Modbus register blocks decoded; per-pack SOC/V/I tables and
  complete per-unit dumps (cell voltages, temperatures, limits, firmware,
  serial) to JSON for diffing across settings or firmware changes. On CAN
  the same commands build an id inventory, rank the battery dialect with
  reasons, and request the extension frames only when no inverter is on
  the bus.
- **Sniff**: one- or two-channel tap (RS485 or CAN), every frame decoded
  and logged, `CHANGE` lines for the charge-control fields (CCL, CVL,
  enables, OV-warn, current), a periodic state line, typed markers aligned
  with the traffic, and a garbage classifier (Modbus RTU? wrong baud?). On
  a Huawei CAN bus it also reports address negotiation and address changes.
- **Timeline / diagnose**: pairs requests and responses from any capture,
  builds a chronological event log, timing statistics (poll period,
  latency, limit-update interval, limit-to-current delay, gaps), automatic
  findings (large limit increases, current above limit, oscillation near
  top of charge) and CSV/JSON/PNG; hypothesis-driven `diagnose` over
  captured sessions.
- **Bridge / gateway**: byte-exact store-and-forward between two channels
  by default, with optional live frame translation (address, version byte,
  echo fixes). With `--policy` a pluggable payload policy may rewrite the
  battery-to-inverter charge limits under enforced invariants: only CCL and
  CVL, only downwards, protection bits locked, malformed frames never
  rebuilt, reset on communication loss, two-record audit of every change.
  The same engine runs on CAN (frame 0x351) between two CAN channels.
- **Charger control** (`rsh huawei`): status, identity (serial bound to
  bus address), measured current-scale calibration stored per serial,
  setpoints that are dry runs unless `--apply`, hard caps that refuse
  rather than clamp, verification by measurement, keep-alive hold, and
  measurement modes for the charger's fallback behaviour (`--poll-only`,
  `--silence-after` with a listen-only tap). Broadcast to all units with
  every acknowledgement checked. `gateway` puts the harness between a
  charger's add-on control board and the module: the module hears only
  our setpoints, the board keeps its live display, its writes are acked
  and blocked.
- **BMS service socket** (Seplos BMS 3.0 native Modbus): read-only
  monitor of all cell voltages, balancing, FET state, alarms and settings
  in the same session as the inverter-link tap; a guarded, dry-run-first
  write for the two charge-request settings.
- **Scripting** (`send` / `script`): arbitrary single frames, JSON
  scenarios as a proactive master or a reactive responder — the surface an
  LLM or a test script drives.
- **Simulators**: battery (RS485 and CAN, dialect profiles, settable
  state), inverter / GX poller, BMS service socket, Huawei PSU with
  selectable readings for every behaviour the sources disagree on.
- **Virtual buses**: a software model of a two-channel RS485 analyzer
  (multi-drop, TX echo, baud-mismatch garbling, cross-wiring) and a virtual
  CAN bus with an ACK model (listen-only ends do not ACK, lone senders see
  no ACK, wrong-rate ends see error frames).
- **Self-test** for the real adapters (channels cross-wired on one bus).
- **Sessions**: everything on the wire lands in timestamped JSONL under
  `captures/` — decoded, error-annotated, with audit fields and a
  `summary` record per run.
- **TUI** (`rsh tui`, Textual): full-screen front end for ports, identify,
  discover, dump, battery check, sniff, monitor, timeline and self-test,
  with a live log and job control; also on Android.
- **Battery pre-purchase check**: one command (or the TUI button) runs
  identify + discover + dump into a folder: cells, temperatures, cycles,
  serial, firmware, limits, alarms.

## Install & first steps

```
pip install -e ".[dev]"
python -m pytest          # 192 tests, all hardware-free
rsh demo                  # simulated inverter vs simulated battery over loopback
rsh ports                 # serial ports and CAN adapters
rsh identify --port COM5  # first thing to run against a real RS485 device
rsh identify --port can:gs:0 --passive-only   # CAN: who talks, at which bitrate
rsh sniff --ch1 COM6 --ch2 COM5 --quiet       # two-channel tap, changes only
rsh discover --port COM5                      # inventory of a responder
rsh timeline captures/<session>.jsonl         # events, timing, findings
rsh menu                  # interactive runner (concurrent jobs per channel)
rsh tui                   # full-screen UI (pip install -e ".[tui]")
```

Port specs: `COM5` / `/dev/ttyUSB0` (serial), `usb:0` / `usb:1` (FTDI
channel on Android), `can:gs:0` / `can:gs:0.1` (gs_usb adapter, channel),
`can:socketcan:can0`, `can:slcan:COM9`, and `can:virtual:<name>` (an
in-process CAN bus; `rsh demo` does the same for RS485). The protocol follows from the port kind; the commands
do not change.

## Documentation

- `docs/USAGE.md` — tool reference, every sub-command and flag, diagnosis
  playbook, Android / Termux setup
- `docs/PROTOCOL-NOTES.md` — condensed RS485 protocol ground truth (frame
  format, checksums, command families, payload layouts, addressing and
  dialect quirks, cabling / pinouts)
- `docs/CAN-PLAN.md` — CAN side: adapter and firmwares, bus parameters of
  the supported protocols, the integration design, status
- `docs/CHARGER-HUAWEI-UNITS.md` — the Huawei-based chargers on our bench: what
  was bought, the module as measured, the add-on ESP32 control board (its CAN
  command cycle, screen, Bluetooth LE interface), rules and open questions
- `docs/ADAPTER-USBCANFD-DUAL.md` — the USBCANFD DUAL board we use: firmware,
  enumeration, termination jumpers, measured transmit/ACK behaviour of its
  firmware and what the harness does about it
- `docs/PHASE-B-RUNBOOK.md` — bench qualification of a CAN charger, one
  command per test ID
- `docs/BENCH-CHECKLIST.md` — at-the-bench step card (wiring, exact
  commands, pass/fail gates)
- `docs/CHARGE-TAPER-PLAN.md` / `docs/CHARGE-TAPER-RESULTS.md` — a
  complete investigation with this tool: hypotheses, capture plan, what was
  measured, the fix and its verification (charts in `docs/img/`)
- `docs/refs/MANUAL-DOWNLOADS.md` — link list of the vendor documents and
  upstream projects the protocol tables were built from (the documents
  themselves are not part of the repository)

## Layout

```
rs485harness/
  core/        serial / FTDI-over-libusb / gs_usb transports, virtual RS485
               analyzer, virtual CAN bus, JSONL sessions
  protocols/   pylontech (RS485 frames, payloads, dialect profiles),
               pylontech_can (LV-CAN family: Pylontech, Victron, Dyness,
               LuxPower, Seplos), huawei_r48xx, seplos_native (Modbus),
               voltronic (LIB)
  simulators/  battery state and presets, RS485 and CAN battery, inverter /
               GX, BMS service socket, Huawei PSU
  analyzer/    identify, discover, dump, sniff, timeline, diagnose, bridge
               and CAN gateway with policy engine, translate, selftest,
               charger control, address watch
  cli.py       `rsh` sub-commands (all support --json)
  runner.py    `rsh menu` interactive job runner
  tui.py       `rsh tui` Textual front end
tools/         Termux helpers, plotting scripts
tests/         golden vectors, payload round-trips, virtual-bus integration,
               fault injection -> diagnose, simulator <-> tool round-trips
```
