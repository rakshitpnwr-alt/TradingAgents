"""Browse the point-in-time archive: what the agents actually saw, and when.

  python archive_peek.py                 # summary of every archived day
  python archive_peek.py 2026-09-30      # that day's fetches, one line each
  python archive_peek.py 2026-09-30 3    # full payload of record #3
"""
import json, os, sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

root = Path(os.getenv("TRADINGAGENTS_ARCHIVE_DIR")
            or Path.home() / ".tradingagents" / "archive")
if not root.exists():
    sys.exit(f"No archive at {root} — run an analysis first.")

days = sorted(d for d in root.iterdir() if d.is_dir())

if len(sys.argv) == 1:
    print(f"{root}\n")
    for d in days:
        files = sorted(d.glob("*.jsonl"))
        n = sum(len(f.read_text().strip().splitlines()) for f in files if f.stat().st_size)
        kb = sum(f.stat().st_size for f in files) / 1024
        print(f"  {d.name}  {n:4d} fetches  {kb:8.1f} KB  "
              f"[{', '.join(f.stem for f in files)}]")
    print(f"\n{len(days)} day(s). Detail: python archive_peek.py <YYYY-MM-DD>")
    sys.exit()

day = root / sys.argv[1]
if not day.exists():
    sys.exit(f"No records for {sys.argv[1]}. Have: {', '.join(d.name for d in days)}")

records = []
for f in sorted(day.glob("*.jsonl")):
    for line in f.read_text().strip().splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
records.sort(key=lambda r: r.get("fetched_at", ""))

if len(sys.argv) >= 3:
    try:
        i = int(sys.argv[2])
    except ValueError:
        sys.exit(f"Record number must be an integer, got {sys.argv[2]!r}")
    if not -len(records) <= i < len(records):
        sys.exit(f"No record {i}: {day.name} has {len(records)} "
                 f"(0-{len(records) - 1}).")
    r = records[i]
    print(f"#{i}  {r['source']} / {r['call']}  at {r['fetched_at']}")
    print(f"args={r['args']}  kwargs={r['kwargs']}\n{'-' * 70}")
    p = r["payload"]
    print(p if isinstance(p, str) else json.dumps(p, indent=2)[:20000])
    sys.exit()

print(f"{day.name} — {len(records)} fetches\n")
for i, r in enumerate(records):
    chars = r.get("payload_chars")
    size = f"{chars:>7,}c" if chars else "  (obj) "
    print(f"  {i:3d}  {r['fetched_at'][11:19]}  {r['source']:<11} "
          f"{str(r['call'])[:34]:<34} {size}")
print(f"\nFull payload: python archive_peek.py {day.name} <n>")
