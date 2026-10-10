"""Plot the two recorded 40 A charge runs (57.6 V default vs 55.2 V) from their
timeline CSVs. Run from the repo root: python tools/plot-40A-runs.py
"""
import csv, datetime, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

RUNS = [
    ("captures/taper/T0-bridge-eavesdrop-live6.timeline.csv",
     "2026-10-06  40 A, charge request 57.6 V (default)", "tab:red"),
    ("captures/taper/T1-55V2-tap-plus-service.timeline.csv",
     "2026-10-10  40 A, charge request 55.2 V", "tab:green"),
]

def load(path):
    allrows = list(csv.DictReader(open(path)))
    rows = [r for r in allrows if r["cid2"] == "61" and r["pack_i"] and r["max_cell"]]
    t = [float(r["t"]) for r in rows]
    i = [float(r["pack_i"]) for r in rows]
    mv = [float(r["max_cell"]) for r in rows]
    pv = [float(r["pack_v"]) for r in rows]
    ccl_rows = [(float(r["t"]), float(r["ccl"])) for r in allrows if r["cid2"] == "63" and r["ccl"]]
    # BMS limit valid at each 0x61 sample = last 0x63 before it
    ccl = []; k = 0
    for x in t:
        while k + 1 < len(ccl_rows) and ccl_rows[k + 1][0] <= x:
            k += 1
        ccl.append(ccl_rows[k][1] if ccl_rows and ccl_rows[k][0] <= x else float("nan"))
    return t, i, mv, pv, ccl

def charge_window(t, i):
    """first sample with I > 5 A .. last sample before current drops below 0.5 A for good"""
    end = max(k for k, x in enumerate(i) if x > 0.5)
    start = end
    while start > 0 and i[start - 1] > 0:
        start -= 1
    return start, end + 1

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(11, 11))
for path, label, color in RUNS:
    t, i, mv, pv, ccl = load(path)
    s, e = charge_window(t, i)
    t0 = t[s]
    tr = [(x - t0) / 60 for x in t[s:e]]
    ax1.plot(tr, i[s:e], color=color, label=label, lw=1.2)
    ax1.plot(tr, [min(c, 60) for c in ccl[s:e]], color=color, ls=":", lw=1,
             label="   BMS charge limit (CCL from 0x63, clipped at 60 A)")
    ax2.plot(tr, mv[s:e], color=color, label=label, lw=1.2)
    ax3.plot(mv[s:e], i[s:e], color=color, label=label, lw=0.8, marker=".", ms=3)
    d0 = datetime.datetime.fromtimestamp(t[s]).strftime("%H:%M:%S")
    d1 = datetime.datetime.fromtimestamp(t[e - 1]).strftime("%H:%M:%S")
    print(f"{label}: {e - s} samples {d0}-{d1}, I max {max(i[s:e]):.1f} A, "
          f"max cell peak {max(mv[s:e]):.3f} V, pack V max {max(pv[s:e]):.2f}")

ax1.set_ylabel("charge current [A]  (pack_i from 0x61)"); ax1.set_ylim(0, 62)
ax1.set_xlabel("minutes since current > 5 A")
ax1.grid(alpha=.3); ax1.legend()
ax1.set_title("Two 40 A charge runs on the inverter link (frame 0x61, polled every 2.3 s)")

ax2.set_ylabel("fullest cell [V]  (max_cell from 0x61)")
ax2.set_xlabel("minutes since current > 5 A")
for y, txt in ((3.50, "3.50 V: BMS limit ~40 A"), (3.55, "3.55 V: BMS charge stop")):
    ax2.axhline(y, color="gray", ls="--", lw=.8); ax2.text(0.1, y + .001, txt, fontsize=8, color="gray")
ax2.grid(alpha=.3); ax2.legend()

ax3.set_xlabel("fullest cell [V]"); ax3.set_ylabel("charge current [A]")
ax3.set_title("charge current vs fullest cell (same samples, time runs along the trace)")
ax3.grid(alpha=.3); ax3.legend()
plt.tight_layout()
out = "docs/img/40A-runs-57V6-vs-55V2.png"
import os; os.makedirs("docs/img", exist_ok=True)
plt.savefig(out, dpi=130)
print("wrote", out)
