"""Score the decisions already in a memory log as the trades they instructed.

Why this exists: settlement scores a RATING -- buy at the close, hold a fixed
window, compare to a benchmark. The system issues an entry, a stop and a size.
Those are different strategies, so the rating-based score does not describe what
the system would have done.

This costs nothing to run. It makes no model calls and reads decisions that
already exist, so it can be run on the live log right now.

Run:  source .venv/bin/activate && python score_decisions.py
      python score_decisions.py path/to/trading_memory.md
      python score_decisions.py --horizon 10 --fill-window 3

Reading it
  Expectancy is mean R per trade taken. One R is the distance from the
  decision's own entry to its own stop, so a stopped-out trade is exactly -1R
  and an expectancy of +0.30R means each trade made about a third of what it
  risked. Positive expectancy is the whole question.

  The three variants are the same decisions scored three ways, so their
  differences are attributable:

    as_specified   the limit entry and the stop, as the decision stated them
    market_entry   entered at the next open instead -- isolates entry timing
    rating_only    entered at the open with no stop  -- isolates the stop

  as_specified below rating_only means the entry and stop machinery is costing
  money, not making it.

  INCOMPLETE counts decisions that named a side but no usable entry and stop.
  Those are not bad trades, they are un-executable ones: there is nothing a
  broker could have been told to do. Decisions recorded before the Portfolio
  Manager began stating levels will all land here, and that is the expected
  result on an older log rather than a fault.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from tradingagents.backtest import score_log_plans  # noqa: E402
from tradingagents.dataflows.config import get_config  # noqa: E402
from tradingagents.trade_plan import (  # noqa: E402
    DEFAULT_FILL_WINDOW_DAYS,
    DEFAULT_HORIZON_DAYS,
)

BAR = "━"


def default_log_path() -> Path:
    config = get_config()
    configured = config.get("memory_log_path")
    if configured:
        return Path(configured)
    return Path(config["results_dir"]) / "trading_memory.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?", default=None,
                       help="memory log to score (default: the configured live log)")
    parser.add_argument("--horizon", type=int, default=DEFAULT_HORIZON_DAYS,
                       help="trading days a filled position is held before closing")
    parser.add_argument("--fill-window", type=int, default=DEFAULT_FILL_WINDOW_DAYS,
                       help="trading days a limit entry stays live")
    args = parser.parse_args()

    path = Path(args.log) if args.log else default_log_path()
    if not path.is_file():
        print(f"No memory log at {path}. Pass one as an argument.", file=sys.stderr)
        return 1

    print(f"{BAR * 78}")
    print("  DECISION SCORING — the instruction issued, not the rating")
    print(f"  log: {path}")
    print(f"  horizon {args.horizon} trading days · limit entry live for "
          f"{args.fill_window} days")
    print(f"{BAR * 78}\n")

    summary = score_log_plans(path, horizon=args.horizon, fill_window=args.fill_window)
    print(summary.render())

    print(f"\n{BAR * 78}")
    if summary.incomplete and not any(v.trades for v in summary.by_variant.values()):
        print("  Nothing could be scored as a trade. Every decision that named a side")
        print("  failed to state a usable entry and stop, so there was never an")
        print("  instruction to execute. That is the finding, not an error: it is")
        print("  measured now because the Portfolio Manager has only just started")
        print("  recording levels. Re-run this once some decisions carry them.")
    else:
        print("  Expectancy is per trade taken. A handful of trades cannot establish")
        print("  it either way — read the t column before believing the number.")
    print(f"{BAR * 78}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
