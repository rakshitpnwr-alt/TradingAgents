"""Phase 0 validation run — crypto only, as-is, no code changes.

The question this answers is NOT "is it profitable". It is:
  does the bear researcher tell me something I hadn't already considered?

Run:  source .venv/bin/activate && python phase0_run.py
      python phase0_run.py ETH-USD          # single ticker
      python phase0_run.py BTC-USD ETH-USD  # several
"""
import sys, time, json
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

TICKERS = sys.argv[1:] or ["BTC-USD"]
TRADE_DATE = datetime.now().strftime("%Y-%m-%d")
OUT = Path("phase0_reports")
OUT.mkdir(exist_ok=True)

config = DEFAULT_CONFIG.copy()
# Fundamentals is auto-dropped for crypto by the CLI; we do it explicitly
# here because the programmatic path does not filter analysts for us.
SELECTED = ["market", "social", "news"]

print(f"provider   : {config['llm_provider']}")
print(f"quick/deep : {config['quick_think_llm']} / {config['deep_think_llm']}")
print(f"analysts   : {', '.join(SELECTED)}  (fundamentals dropped — crypto)")
print(f"date       : {TRADE_DATE}\n")

summary = []
for ticker in TICKERS:
    print(f"━━ {ticker} " + "━" * 40)
    t0 = time.time()
    ta = TradingAgentsGraph(selected_analysts=SELECTED, debug=True, config=config)
    try:
        final_state, decision = ta.propagate(ticker, TRADE_DATE, asset_type="crypto")
    except Exception as e:
        print(f"  ✗ {type(e).__name__}: {e}")
        summary.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
        continue
    elapsed = time.time() - t0
    path = ta.save_reports(final_state, ticker, save_path=OUT / ticker)
    print(f"  ✓ {decision}   ({elapsed:.0f}s)  →  {path}")
    summary.append({"ticker": ticker, "decision": str(decision),
                    "seconds": round(elapsed), "report": str(path)})

(OUT / "summary.json").write_text(json.dumps(summary, indent=2))
print(f"\nReports in ./{OUT}/ — read complete_report.md end to end.")
print("Then check spend at https://console.anthropic.com/settings/usage")
