"""Probe FRED for the policy / short-rate series behind each major currency.

Why this exists: the carry signal was left out of the signal layer because there
was no verified source for the non-US legs. Then, on the 2026-09-30 EURUSD run,
the news analyst passed ``ECBMRRFR`` as a raw series ID on its own initiative
and got the ECB Main Refinancing Rate back. So the data may well be there. This
finds out which IDs actually serve, rather than guessing and shipping a carry
signal built on a 404.

Run:  source .venv/bin/activate && python probe_fred.py

Reading it:
  OK        serves data, recent enough to price a current differential
  STALE     serves, but the last observation is old enough that a "current"
            rate differential built on it would be fiction
  NO DATA   the ID is wrong or discontinued, whatever the docs imply
"""
import re
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from tradingagents.dataflows.vendors.fred import get_macro_data  # noqa: E402

# Two families, deliberately. For a DIFFERENTIAL, a consistent cross-country
# measure matters more than each leg being the exact policy rate: a spread built
# from one country's policy rate and another's 3-month interbank rate is a
# spread between two different things. The OECD IR3TIB01 family is the same
# measure everywhere, so if it serves for every leg it is the better basis even
# where a true policy rate is also available.
CANDIDATES = {
    "USD": ["FEDFUNDS", "DFF", "SOFR", "IR3TIB01USM156N"],
    "EUR": ["ECBMRRFR", "ECBDFR", "IR3TIB01EZM156N"],
    "JPY": ["IRSTCI01JPM156N", "IR3TIB01JPM156N", "INTDSRJPM193N"],
    "GBP": ["IUDSOIA", "IR3TIB01GBM156N", "IRSTCI01GBM156N"],
    "CHF": ["IR3TIB01CHM156N", "IRSTCI01CHM156N"],
    "CAD": ["IR3TIB01CAM156N", "IRSTCI01CAM156N", "INTDSRCAM193N"],
    "AUD": ["IR3TIB01AUM156N", "IRSTCI01AUM156N"],
    "NZD": ["IR3TIB01NZM156N", "IRSTCI01NZM156N"],
}

STALE_AFTER_DAYS = 120   # a quarterly-lagged series cannot price today's carry

_TITLE = re.compile(r"^## FRED:\s*(.+?)\s*\(([A-Z0-9]+)\)\s*$", re.M)
_LATEST = re.compile(r"\*\*Latest:\*\*\s*(-?[\d.]+)\s*\((\d{4}-\d{2}-\d{2})\)")


def parse(report: str):
    """(title, value, observation_date) from a get_macro_data report, or None.

    get_macro_data returns prose on a bad series ID rather than raising, so an
    unparseable report means "no usable data" -- which is the answer we want,
    not an exception to handle.
    """
    title_match, latest_match = _TITLE.search(report or ""), _LATEST.search(report or "")
    if not latest_match:
        return None
    title = title_match.group(1) if title_match else ""
    return title, float(latest_match.group(1)), latest_match.group(2)


def main() -> None:
    today = date.today()
    as_of = today.strftime("%Y-%m-%d")

    print(f"FRED policy / short-rate probe   as of {as_of}\n")
    print(f"{'CCY':5s} {'SERIES':22s} {'STATUS':8s} {'LAST OBS':12s} {'VALUE':>8s}  TITLE")
    print("-" * 104)

    found: dict[str, tuple[str, float, str]] = {}
    for ccy, ids in CANDIDATES.items():
        for series_id in ids:
            try:
                report = get_macro_data(series_id, as_of, 720)
            except Exception as exc:
                print(f"{ccy:5s} {series_id:22s} {'ERROR':8s} {'':12s} {'':>8s}  "
                      f"{type(exc).__name__}: {str(exc)[:40]}")
                continue
            parsed = parse(report)
            if parsed is None:
                print(f"{ccy:5s} {series_id:22s} {'NO DATA':8s}")
                continue
            title, value, obs_date = parsed
            age = (today - date.fromisoformat(obs_date)).days
            status = "STALE" if age > STALE_AFTER_DAYS else "OK"
            print(f"{ccy:5s} {series_id:22s} {status:8s} {obs_date:12s} {value:8.3f}  {title[:44]}")
            if status == "OK" and ccy not in found:
                found[ccy] = (series_id, value, obs_date)

    print("\n--- usable, one per currency (first OK wins) ---")
    for ccy, (series_id, value, obs_date) in sorted(found.items()):
        print(f"  {ccy}: {series_id:22s} {value:7.3f}%   (as of {obs_date})")

    missing = sorted(set(CANDIDATES) - set(found))
    if missing:
        print(f"\n  NO USABLE SERIES: {', '.join(missing)}")
        print("  Carry cannot cover a pair whose leg is missing. Reporting those")
        print("  pairs unavailable beats substituting a proxy that quietly")
        print("  measures something else and still looks like the paper's result.")

    consistent = [c for c in CANDIDATES if c in found and found[c][0].startswith("IR3TIB01")]
    if len(consistent) == len(CANDIDATES):
        print("\n  The IR3TIB01 family serves every leg -- prefer it: one measure")
        print("  across all currencies makes the differentials comparable.")

    if "USD" in found and "EUR" in found:
        diff = found["EUR"][1] - found["USD"][1]
        side = "short EURUSD" if diff < 0 else "long EURUSD"
        print(f"\n  EURUSD short-rate differential (EUR - USD): {diff:+.3f}%")
        print(f"  Carry favours being {side} (before any spot move).")


if __name__ == "__main__":
    main()
