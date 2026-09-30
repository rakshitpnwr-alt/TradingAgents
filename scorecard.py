"""The running track record: every call the system made, and how each turned out.

The framework logs each decision and settles it on a later run for the same
ticker, against what prices actually did. This reads that log back.

  python scorecard.py          # the record
  python scorecard.py --raw    # the log file as written
"""
import os, sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

log = Path(os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH")
           or Path.home() / ".tradingagents/memory/trading_memory.md")
if not log.exists():
    sys.exit(f"No decision log at {log}\nRun an analysis first.")

text = log.read_text()
if "--raw" in sys.argv:
    print(text); sys.exit()


def parse(line: str):
    """A tag is [date | ticker | rating | ...], the tail varying by state:
      pending   -> [d | t | r | pending]
      settled   -> [d | t | r | +1.2% | -0.3% | 5d]  (+ optional | resolved:date)
    Splitting beats one regex: the tail has changed shape across versions and a
    rigid pattern silently drops entries rather than failing loudly."""
    line = line.strip()
    if not (line.startswith("[") and line.endswith("]")):
        return None
    f = [p.strip() for p in line[1:-1].split("|")]
    if len(f) < 4 or not f[0][:4].isdigit():
        return None
    rec = {"date": f[0], "ticker": f[1], "rating": f[2],
           "raw": None, "alpha": None, "days": None}
    if f[3].lower() != "pending" and len(f) >= 6:
        rec.update(raw=f[3], alpha=f[4], days=f[5].rstrip("d"))
    return rec


rows = [r for r in (parse(l) for l in text.splitlines()) if r]
if not rows:
    sys.exit(f"{log}\n\nNo decision tags found in {len(text.splitlines())} lines.")

settled = [r for r in rows if r["raw"]]

print(f"{log}\n")
print(f"{'DATE':<12} {'TICKER':<10} {'CALL':<10} {'RETURN':>9} {'vs BENCH':>9}  HELD")
print("-" * 62)
for r in rows:
    if r["raw"]:
        print(f"{r['date']:<12} {r['ticker']:<10} {r['rating']:<10} "
              f"{r['raw']:>9} {r['alpha']:>9}  {r['days']}d")
    else:
        print(f"{r['date']:<12} {r['ticker']:<10} {r['rating']:<10} "
              f"{'pending':>9} {'—':>9}   —")

calls = {}
for r in rows:
    calls[r["rating"]] = calls.get(r["rating"], 0) + 1
print(f"\n{len(rows)} call(s): " + ", ".join(f"{v} {k}" for k, v in sorted(calls.items())))
print(f"{len(settled)} settled, {len(rows) - len(settled)} awaiting an outcome.")

def pct(s):
    try: return float(str(s).strip().rstrip("%"))
    except Exception: return None

vals = [pct(r["alpha"]) for r in settled if pct(r["alpha"]) is not None]
if vals:
    wins = sum(1 for v in vals if v > 0)
    print(f"\nBeat its benchmark {wins}/{len(vals)}. Mean alpha {sum(vals)/len(vals):+.2f}%.")
    if len(vals) < 20:
        print(f"{len(vals)} settled call(s) is far too few to mean anything —")
        print("dozens, over months, before that number is worth reading.")
else:
    print("\nNothing settled yet. A call settles on a later run for the same")
    print("ticker, once its holding window has fully traded.")

if len(set(r["rating"] for r in rows)) == 1 and len(rows) >= 3:
    print(f"\nEvery call so far is the same verdict ({rows[0]['rating']}). Worth")
    print("watching: a system that never commits can never be scored.")
