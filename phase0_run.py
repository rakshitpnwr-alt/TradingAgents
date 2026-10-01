"""Phase 0 validation run — no code changes, any asset class.

The question this answers is NOT "is it profitable". It is:
  does the bear researcher tell me something I hadn't already considered?

Run:  source .venv/bin/activate && python phase0_run.py
      python phase0_run.py ETH-USD          # single ticker
      python phase0_run.py BTC-USD ETH-USD  # several
      python phase0_run.py EURUSD XAUUSD    # forex and gold
"""
import os, sys, time, json
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG
from cli.prompts import detect_asset_type

TICKERS = sys.argv[1:] or ["BTC-USD"]
# Yesterday, not today. Today's bar is still forming: on the 2026-09-30 run its
# Open equalled its High and volume was ~38% below the prior day, yet every
# indicator was computed over it -- including the macdh = -48.51 crossover that
# the whole decision turned on. Analysing the last complete bar removes that.
# Override with PHASE0_DATE=YYYY-MM-DD.
TRADE_DATE = os.getenv("PHASE0_DATE") or (
    datetime.now() - timedelta(days=1)
).strftime("%Y-%m-%d")
OUT = Path("phase0_reports")
OUT.mkdir(exist_ok=True)

config = DEFAULT_CONFIG.copy()
# Fundamentals is excluded for every ticker here, not only the ones with no
# issuer: this script exists to compare runs at a fixed cost, and the three
# analysts below are the ones every asset class can actually serve.
SELECTED = ["market", "social", "news"]

print(f"provider   : {config['llm_provider']}")
print(f"quick/deep : {config['quick_think_llm']} / {config['deep_think_llm']}")
print(f"analysts   : {', '.join(SELECTED)}  (fundamentals excluded)")
print(f"date       : {TRADE_DATE}\n")

summary = []
for ticker in TICKERS:
    # Detected, not hardcoded. This was pinned to "crypto", so a forex or gold
    # ticker would have been analysed as a coin: the agents would have been told
    # to treat EURUSD as a crypto asset, searched crypto subreddits for it, and
    # none of the base/quote or rate-differential framing would have applied.
    asset_type = detect_asset_type(ticker).value
    print(f"━━ {ticker} ({asset_type}) " + "━" * 34)
    t0 = time.time()
    ta = TradingAgentsGraph(selected_analysts=SELECTED, debug=True, config=config)
    try:
        final_state, decision = ta.propagate(ticker, TRADE_DATE, asset_type=asset_type)
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}")
        summary.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
        continue
    elapsed = time.time() - t0
    # Dated directory per run: the first two runs both wrote to
    # phase0_reports/<ticker>/ and the second silently overwrote the first.
    # A decision log has to accumulate, not clobber.
    path = ta.save_reports(final_state, ticker, save_path=OUT / ticker / TRADE_DATE)
    print(f"  ✓ {decision}   ({elapsed:.0f}s)  →  {path}")
    summary.append({"ticker": ticker, "asset_type": asset_type,
                    "decision": str(decision),
                    "seconds": round(elapsed), "report": str(path)})

(OUT / "summary.json").write_text(json.dumps(summary, indent=2))
print(f"\nReports in ./{OUT}/ — read complete_report.md end to end.")
print("Then check spend at https://console.anthropic.com/settings/usage")
