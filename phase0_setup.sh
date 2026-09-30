#!/usr/bin/env bash
# Phase 0 setup — run this once, in macOS Terminal, from this folder.
#   cd ~/my-agent/TradingAgents && bash phase0_setup.sh
set -euo pipefail

echo "▸ Python"
python3 --version

echo "▸ Virtualenv + install (uses uv if present, else pip)"
if command -v uv >/dev/null 2>&1; then
  uv venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  uv pip install -e ".[dev]"
else
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip -q
  pip install -e ".[dev]" -q
fi

echo "▸ Test suite (should be ~676 passed)"
python -m pytest tests/ -q 2>&1 | tail -3

echo
echo "▸ Preflight: can this machine reach what TradingAgents needs?"
python - <<'PY'
import os, urllib.request, ssl
from dotenv import load_dotenv
load_dotenv()

CHECKS = {
    "Yahoo Finance (prices — REQUIRED)": "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1d&range=5d",
    "StockTwits (crypto sentiment)":     "https://api.stocktwits.com/api/2/streams/symbol/BTC.X.json",
    "Reddit (social sentiment)":         "https://www.reddit.com/r/cryptocurrency/search.rss?q=BTC&restrict_sr=1",
    "Polymarket (prediction markets)":   "https://gamma-api.polymarket.com/markets?limit=1",
    "Anthropic API (the LLM)":           "https://api.anthropic.com/v1/models",
}
ctx = ssl.create_default_context()
for label, url in CHECKS.items():
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tradingagents-preflight/0.1"})
        code = urllib.request.urlopen(req, timeout=12, context=ctx).status
        print(f"  ✅ {label:38s} {code}")
    except urllib.error.HTTPError as e:
        ok = "✅" if e.code == 401 else "⚠️ "   # 401 on Anthropic = reachable, key not sent
        print(f"  {ok} {label:38s} {e.code}")
    except Exception as e:
        print(f"  ❌ {label:38s} {type(e).__name__}: {e}")

key = os.getenv("ANTHROPIC_API_KEY", "")
print()
print(f"  {'✅' if key.startswith('sk-ant-') else '❌'} ANTHROPIC_API_KEY "
      f"{'set (' + key[:11] + '…)' if key.startswith('sk-ant-') else 'MISSING — add it to .env'}")
PY

echo
echo "▸ Next:  source .venv/bin/activate && python phase0_run.py"
