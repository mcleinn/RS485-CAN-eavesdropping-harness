#!/data/data/com.termux/files/usr/bin/bash
# One-command battery health readout (Pylontech-protocol packs: Pylontech,
# Dyness, Seplos in Pylontech mode, ...), for Android/Termux with an FTDI
# RS485 adapter via OTG. Also runs on Linux/PC with a COM port argument.
#
#   battery-readout.sh                # Termux: adapter via termux-usb, channel A
#   battery-readout.sh usb:B          # Termux: Waveshare channel B
#   battery-readout.sh COM5           # PC
#   BAUD=115200 battery-readout.sh    # Dyness DIP #4 on, or Pylontech console-ish rates
#
# Wiring (RJ45 on the battery's RS485 port, see docs/refs/<brand>):
#   Dyness DL5.0C Pro : pin 1 = 485B, pin 2 = 485A            (9600 8N1 default)
#   Pylontech US-series: use the master's B/RS485 socket; pins per docs/PROTOCOL-NOTES.md
#   Seplos BMS 3.0     : A = pins 2/7, B = pins 1/8, GND 3/6   (inverter socket)
# Connect A<->A, B<->B (and GND if available). Battery switched ON.
#
# Output: a folder ~/battery-checks/<date-time>/ with the discovery report,
# the per-unit dump (every cell, temps, limits, cycles, serial, firmware),
# the raw session log, and summary.txt. Everything read-only.
set -u
PORT="${1:-usb:A}"
BAUD="${BAUD:-}"            # empty = detect (Pylontech 115200, Dyness/Seplos 9600, ...)
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="${RSH_REPO:-$HOME/dev/inverter}"
[ -d "$REPO/rs485harness" ] || REPO="$HERE/../.."
OUT="$HOME/battery-checks/$(date +%Y-%m-%d_%H-%M-%S)"
mkdir -p "$OUT"
cd "$REPO" || { echo "harness not found (set RSH_REPO)"; exit 1; }

case "$PORT" in
  usb:*) RSH="$REPO/tools/termux/rsh-usb" ;;
  *)     RSH="python -m rs485harness.cli" ;;
esac

log() { echo "$*" | tee -a "$OUT/summary.txt"; }
log "== battery readout $(date)  port=$PORT baud=${BAUD:-auto}"
log "== 1/3 who is there (passive listen, then active probes at every baud)"
if [ -n "$BAUD" ]; then BAUDARG="--bauds $BAUD"; else BAUDARG=""; fi
$RSH identify --port "$PORT" $BAUDARG --listen 3 --session "$OUT/identify.jsonl" 2>&1 | tee -a "$OUT/summary.txt" | tail -6
if [ -z "$BAUD" ]; then
  BAUD=$(python - "$OUT/identify.identify.json" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1], encoding="utf-8"))
    hits = [a for a in r.get("active", []) if a.get("answered")]
    print(hits[0]["baud"] if hits else 9600)
except Exception:
    print(9600)
PY
)
  log "detected baud: $BAUD"
fi
log "== 2/3 discovery: every command, decoded state, quirks"
$RSH discover --port "$PORT" --baud "$BAUD" --session "$OUT/discover.jsonl" 2>&1 | tee -a "$OUT/summary.txt" | tail -25
log "== 3/3 dump: all units, every cell/temperature/limit, cycles, serial, firmware"
$RSH dump --port "$PORT" --baud "$BAUD" --session "$OUT/dump.jsonl" 2>&1 | tee -a "$OUT/summary.txt" | tail -40
log ""
log "files: $OUT"
ls -1 "$OUT" | sed 's/^/   /' | tee -a "$OUT/summary.txt"
log "JSON for later analysis: $OUT/discover.discovery.json, $OUT/dump.dump.json"
