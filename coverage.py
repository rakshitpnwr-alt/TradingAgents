"""What each analyst actually had to work with, per call.

A run whose news feed came back empty reads exactly like one that had
sixteen headlines: same length, same confident prose, same rating. On
2026-09-29 that difference moved BTC-USD from Hold to Overweight, 25
minutes apart, on identical inputs.

So classify every archived payload and record the result beside the
decision. Nothing here blocks a run or changes a rating -- it only makes
the difference visible, so a track record can separate calls made on full
data from calls made on gaps.

Three states, because two of them are not the same thing:

  ok           real content
  empty        the source was reached and genuinely held nothing.
               Informative: no chatter is a fact about the market.
  unavailable  the source could not be reached, or withheld its answer.
               Not information, just absence of it -- and the case the
               agents are most likely to read as "no news is neutral".
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Reached, and genuinely nothing there. Checked first: several of these also
# contain the word "unavailable" in their explanatory tail.
_EMPTY = (
    "no reddit posts found",
    "no stocktwits messages found",
    "no open prediction markets matched",
    "no news found",
)

# Could not be reached, or deliberately withheld.
_UNAVAILABLE = (
    "data_unavailable",
    "no_data_available",
    "unavailable:",
    "unavailable for",
    "news unavailable",
    "are withheld for",
    "no data for this date",
    "<unavailable>",
    "<raised ",
)


def classify(payload) -> str:
    """One of 'ok', 'empty', 'unavailable'."""
    if payload is None:
        return "unavailable"
    if not isinstance(payload, str):
        return "ok"
    text = payload.strip()
    if not text:
        return "unavailable"
    low = text.lower()
    for marker in _EMPTY:
        if marker in low:
            return "empty"
    for marker in _UNAVAILABLE:
        if marker in low:
            return "unavailable"
    return "ok"


def _archive_root() -> Path:
    return Path(os.getenv("TRADINGAGENTS_ARCHIVE_DIR")
                or Path.home() / ".tradingagents" / "archive")


def summarize(ticker: str, since_iso: str) -> dict[str, dict[str, int]]:
    """Tally payload states per tool, for one ticker, since a timestamp."""
    tally: dict[str, dict[str, int]] = {}
    root = _archive_root()
    if not root.exists():
        return tally
    for path in root.rglob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("fetched_at", "") < since_iso:
                continue
            if ticker.upper() not in str(rec.get("args", "")).upper():
                continue
            # Records written before the decorator was fixed name the call
            # after the ticker; fall back to the source file so an old archive
            # still groups sensibly instead of inventing per-ticker "tools".
            call = str(rec.get("call") or "?")
            if call.upper() == ticker.upper():
                call = str(rec.get("source") or call)
            state = classify(rec.get("payload"))
            tally.setdefault(call, {"ok": 0, "empty": 0, "unavailable": 0})[state] += 1
    return tally


def record(ticker: str, trade_date: str, since_iso: str, log_path: str | None = None) -> dict:
    """Append a coverage line beside the decision log. Never raises."""
    tally = summarize(ticker, since_iso)
    try:
        base = Path(log_path or os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH")
                    or Path.home() / ".tradingagents/memory/trading_memory.md")
        base.parent.mkdir(parents=True, exist_ok=True)
        gaps = sorted(c for c, s in tally.items() if s["unavailable"] and not s["ok"])
        thin = sorted(c for c, s in tally.items() if s["empty"] and not s["ok"])
        with open(base.with_suffix(".coverage.md"), "a", encoding="utf-8") as f:
            f.write(
                f"[{trade_date} | {ticker} | "
                f"gaps:{','.join(gaps) or 'none'} | "
                f"empty:{','.join(thin) or 'none'}]\n"
            )
    except Exception:
        pass
    return tally
