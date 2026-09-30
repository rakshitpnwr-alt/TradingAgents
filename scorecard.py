"""The running track record: every call the system made, and how each turned out.

The framework already logs each decision and settles it against realised prices
on a later run for the same ticker. This just reads that log and shows whether
the calls have been any good.

  python scorecard.py            # the record so far
  python scorecard.py --raw      # the log as written, unparsed
"""
import os, re, sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

log = Path(os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH")
           or Path.home()/".tradingagents/memory/trading_memory.md")
if not log.exists():
    sys.exit(f"No decision log at {log}\nRun an analysis first.")

text = log.read_text()
if "--raw" in sys.argv:
    print(text); sys.exit()

# [date | ticker | rating | raw% | alpha% | Nd | resolved:date]
TAG = re.compile(
    r"\[(\d{4}-\d{2}-\d{2})\s*\|\s*([^|\]]+?)\s*\|\s*([^|\]]+?)\s*"
    r"(?:\|\s*([^|\]]*?)\s*\|\s*([^|\]]*?)\s*\|\s*(\d+)d)?"
    r"(?:\s*\|\s*resolved:(\S+?))?\s*\]")

rows = [m.groups() for m in TAG.finditer(text)]
if not rows:
    print(f"{log}\n\nNo decision tags parsed. Showing the file instead:\n")
    print(text[:3000]); sys.exit()

settled = [r for r in rows if r[3]]
pending = [r for r in rows if not r[3]]

print(f"{log}\n")
print(f"{'DATE':<12} {'TICKER':<10} {'CALL':<10} {'RETURN':>9} {'vs BENCH':>9}  HELD")
print("-" * 62)
for d, tick, rating, raw, alpha, days, _res in rows:
    if raw:
        print(f"{d:<12} {tick:<10} {rating:<10} {raw:>9} {alpha:>9}  {days}d")
    else:
        print(f"{d:<12} {tick:<10} {rating:<10} {'pending':>9} {'—':>9}   —")

print(f"\n{len(rows)} call(s): {len(settled)} settled, {len(pending)} awaiting an outcome.")

def pct(s):
    try: return float(str(s).strip().rstrip('%'))
    except Exception: return None

vals = [pct(r[4]) for r in settled if pct(r[4]) is not None]
if vals:
    wins = sum(1 for v in vals if v > 0)
    print(f"Beat its benchmark {wins}/{len(vals)} times. "
          f"Mean alpha {sum(vals)/len(vals):+.2f}%.")
    if len(vals) < 20:
        print(f"\n{len(vals)} settled call(s) is far too few to mean anything.")
        print("Dozens, over months, before this number is worth reading.")
else:
    print("\nNothing settled yet. A call settles on a later run for the same")
    print("ticker, once its holding window has fully traded.")
