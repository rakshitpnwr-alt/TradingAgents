"""What Yahoo actually serves for FX and metals. Data only -- no LLM, no cost.

Run before building anything on these instruments. Every design choice below
depends on facts this prints: whether a pair has history at all, whether the
last bar is complete, and which of the several gold symbols is usable.

  python probe_symbols.py
"""
from datetime import date, timedelta

from tradingagents.dataflows.symbols import normalize_symbol

CANDIDATES = {
    "FX majors":  ["EURUSD", "GBPUSD", "USDJPY", "USDCHF",
                   "AUDUSD", "NZDUSD", "USDCAD"],
    "FX crosses": ["EURGBP", "EURJPY", "GBPJPY", "AUDNZD", "USDINR"],
    "Gold":       ["XAUUSD", "GC=F", "XAUUSD=X", "GLD", "IAU"],
    "Silver":     ["XAGUSD", "SI=F", "SLV"],
    "Dollar idx": ["DX-Y.NYB", "UUP"],
}

end = date.today()
start = end - timedelta(days=60)
print(f"window {start} .. {end}\n")
print(f"{'INPUT':<11} {'-> YAHOO':<12} {'ROWS':>5} {'LAST BAR':>11} "
      f"{'CLOSE':>11}  NOTE")
print("-" * 78)

import yfinance as yf

for group, syms in CANDIDATES.items():
    print(f"\n{group}")
    for raw in syms:
        try:
            canon = normalize_symbol(raw)
        except Exception as e:
            print(f"  {raw:<11} normalize failed: {e}"); continue
        try:
            h = yf.Ticker(canon).history(start=str(start), end=str(end))
        except Exception as e:
            print(f"  {raw:<11} {canon:<12} fetch failed: {type(e).__name__}")
            continue
        if h is None or h.empty:
            print(f"  {raw:<11} {canon:<12} {'0':>5}  NO DATA")
            continue
        last = h.iloc[-1]
        d = h.index[-1].date()
        # Open == High to the cent is the signature of a bar still forming.
        partial = "partial?" if float(last["Open"]) == float(last["High"]) else ""
        vol = float(last.get("Volume", 0) or 0)
        novol = "no volume" if vol == 0 else ""
        gaps = ""
        if len(h) > 5:
            span = (h.index[-1].date() - h.index[0].date()).days
            gaps = f"{len(h)}/{span}d"
        note = " ".join(x for x in (partial, novol, gaps) if x)
        print(f"  {raw:<11} {canon:<12} {len(h):>5} {str(d):>11} "
              f"{float(last['Close']):>11,.4f}  {note}")

print("\nReading it:")
print("  NO DATA      -> unusable, whatever the docs imply")
print("  partial?     -> today's bar is still forming (Open == High exactly)")
print("  no volume    -> spot FX has none; fine, but VWMA becomes meaningless")
print("  rows/days    -> far below ~5/7 means weekend gaps or thin coverage")
