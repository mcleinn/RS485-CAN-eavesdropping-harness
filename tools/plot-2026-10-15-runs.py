"""Plot the four charge runs of 2026-10-15 (60 / 80 / 100->84 / 100->90 A settings,
charge request 55.2 V) from the service-side samples (2 s) in the tap+service
capture. Run from the repo root: python tools/plot-2026-10-15-runs.py
"""
import json, datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CAPTURE = "captures/taper/T2-runs-snapshot-2031.jsonl"
S = []
for line in open(CAPTURE, encoding="utf-8"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("kind") == "service":
        d = r["decoded"]
        S.append((r["t"], d["pack_v"], d["pack_i"], d["max_cell"], d["bms_charge_a"]))

runs, cur = [], None
for s in S:
    if s[2] > 3:
        cur = (cur or []) + [s]
    else:
        if cur and len(cur) > 20:
            runs.append(cur)
        cur = None
if cur and len(cur) > 20:
    runs.append(cur)

labels = ["setting 60 A (from 99 % SOC)", "setting 80 A (from 99 %)",
          "setting 100 A (from 99 %): inverter hit the cap at 84 A",
          "setting 100 A (from 94 %): inverter ceiling 89.6 A"]
colors = ["tab:blue", "tab:green", "tab:orange", "tab:red"]
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8.5), sharex=True)
for run, label, color in zip(runs, labels, colors):
    t0 = run[0][0]
    tr = [(s[0] - t0) / 60 for s in run]
    ax1.plot(tr, [s[2] for s in run], color=color, lw=1.3, label=label)
    ax1.plot(tr, [min(s[4], 130) for s in run], color=color, ls=":", lw=1)
    ax2.plot(tr, [s[3] for s in run], color=color, lw=1.3, label=label)
    start = datetime.datetime.fromtimestamp(t0).strftime("%H:%M:%S")
    print(label, "start", start, "peak %.1f A" % max(s[2] for s in run),
          "min limit %d A" % min(s[4] for s in run),
          "top cell peak %.3f V" % max(s[3] for s in run))
ax1.set_ylabel("battery current [A]  (solid; dotted = BMS limit, clipped 130 A)")
ax1.set_ylim(0, 135); ax1.grid(alpha=.3); ax1.legend(fontsize=8)
ax1.set_title("2026-10-15: four charge runs with charge request 55.2 V (service-side samples every 2 s)")
ax2.set_ylabel("fullest cell [V]"); ax2.set_xlabel("minutes since charging started")
for y, txt in ((3.50, "3.50 V: BMS limit ~40 A"), (3.55, "3.55 V: BMS charge stop")):
    ax2.axhline(y, color="gray", ls="--", lw=.8); ax2.text(0.1, y + .001, txt, fontsize=8, color="gray")
ax2.grid(alpha=.3)
plt.tight_layout()
out = "docs/img/2026-10-15-runs-60-80-100A.png"
plt.savefig(out, dpi=130); print("wrote", out)
