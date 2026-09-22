# Bench checklist — SAKO SUNON V ↔ Seplos Mason 280L

Terse field card. Rationale and tool details are in `USAGE.md`; protocol
facts and pinouts in `PROTOCOL-NOTES.md`. Every command writes a JSONL
capture + a saved report under `captures/`.

## Wiring reference

Waveshare USB TO 2CH RS485: each channel has **A, B, PE** where **PE =
signal ground** (Waveshare's own label; not chassis earth). Channels are
isolated.

| Signal | Waveshare | Seplos Mason 280L (RS485 socket) | SAKO SUNON V (BMS RJ45) |
|--------|-----------|----------------------------------|--------------------------|
| A      | A1 / A2   | pin 2 or 7                       | pin 5                    |
| B      | B1 / B2   | pin 1 or 8                       | pin 3                    |
| GND    | PE        | pin 3 or 6                       | pin 8                    |

⚠️ On the Mason 280L, the **CAN socket pins 4/5 are CAN-H/CAN-L, not RS485** —
use the RS485 socket. If unsure which Seplos socket serves the inverter
link, run `identify` on each.

## Step 1 — Drivers & ports
- [ ] Plug in adapter; install FTDI VCP driver if needed.
- [ ] `rsh ports` shows two COM ports for the one device. Note them as
      COM_A, COM_B.
- [ ] Device Manager → each COM → Advanced → **Latency Timer = 1 ms** →
      unplug/replug.

## Step 2 — Adapter self-test (gate: must PASS before trusting results)
- [ ] Cross-wire ch1↔ch2 on one bus: **A1↔A2, B1↔B2, PE1↔PE2**.
- [ ] `rsh selftest COM_A COM_B`
- [ ] All checks PASS (byte transfer at 9600/19200/115200, golden frame,
      latency not flagged). If latency flagged → redo Step 1 timer.

## Step 3 — Battery alone (try each Seplos socket)
- [ ] Wire one channel to the Seplos: A→A(2/7), B→B(1/8), PE→GND(3/6).
- [ ] `rsh identify --port COM_bat`
- [ ] `rsh discover --port COM_bat`
- Expected good: "RESPONDER: Pylontech-ASCII battery-like device" +
      decoded state. Modbus verdict → PYLON not active / wrong socket.
      Silence → wrong socket/pinout/DIP; try the other socket, swap A/B.
- [ ] Battery DIP: pack 1 (DIP1 ON per Pylontech convention) + power-cycle.

## Step 4 — Inverter alone
- [ ] Set inverter **program 05 = PYL**.
- [ ] Wire one channel to SAKO BMS port: A→5, B→3, PE→8.
- [ ] `rsh identify --port COM_inv --passive-only`
- Expected: "ACTIVE TALKER … polling like an INVERTER" with CID2 family +
      cadence. Modbus verdict → SAKO's PYL is really LIB; try program 05 =
      LIB or LIC.

## Step 5 — Substitution tests (decisive)
- [ ] `rsh battery-sim --port COM_inv --preset seplos-280l`
      → SAKO shows a battery? Then inverter + cable are OK; the real
      battery's replies are the issue. Note which CID2s it polls.
- [ ] `rsh inverter-sim --port COM_bat --profile voltronic-6x`
      (also try `--adr 1`, `--adr 2`, `--profile pylon-v35-adr12`,
      `--profile monitoring-4x`) → shows what the battery actually serves.

## Step 6 — Full MITM + diagnose
- [ ] Inverter → ch1, battery → ch2 (each: A→A, B→B, PE→GND).
- [ ] `rsh bridge --ch1 COM_inv --ch2 COM_bat`
- [ ] Let a recognition attempt run, Ctrl+C, then `rsh diagnose`.
- [ ] If mismatch reported: retry with translation, e.g.
      `rsh bridge --ch1 COM_inv --ch2 COM_bat --fix-cid2-echo --force-adr b2a=02`

## After the bench
- [ ] Keep the `captures/*.jsonl` + `*.report.json` — paste them for
      analysis. `rsh diagnose <file>` re-runs the hypothesis matrix anytime.
