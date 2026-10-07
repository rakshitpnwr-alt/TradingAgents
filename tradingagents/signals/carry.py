"""Currency carry: the short-rate differential, from Lustig, Roussanov and
Verdelhan (2011) and the forward-premium literature it sits in.

The claim is uncomfortable and well documented: uncovered interest parity says
a higher-yielding currency should depreciate by exactly its rate advantage, and
empirically it does not. Being long the higher-yielding currency has earned a
premium. LRV's explanation is that this is compensation for a specific risk --
high-rate currencies depreciate sharply when global risk rises, so the carry
trade is short a disaster. The premium is the price of writing that insurance,
which is why carry does not look like free money and should not be sized as if
it were.

Why this no longer sets direction
---------------------------------
It was built as a directional signal and has been demoted to a diagnostic,
because it was finally tested. Twenty-five years of daily closes on the seven
dollar majors, rebalanced monthly with costs charged, gave a pooled Sharpe of
-0.21 at t=-1.04, losing to simply holding the basket. t=-1.04 cannot establish
that the rule loses money -- that would be overstating in the other direction --
but it certainly fails the fourth bar in ``base.py``: it has not survived on the
instruments we trade.

The failure was predictable from the deviations already recorded below, which is
the best thing that can be said about it. The paper's premium comes from sorting
37 currencies on forward discounts and trading the spread between the extremes;
a single-pair differential built from policy rates keeps neither the
cross-sectional diversification nor the tradable financing cost that produced
it. The deviations field existed to flag exactly this risk, and the backtest
collected on it.

What survives is the differential itself, which is a real fact about what
holding a position costs to finance, and which the trader has used well -- it
read the 60-day-stale JPY leg correctly and sized down for it. So the number is
still reported, with ``rule_would_imply`` recorded beside it so the demotion
stays auditable. ``compute`` returns DIAGNOSTIC and ``consensus_direction`` does
not let diagnostics vote. To promote it back, get forward discounts for the
basket, implement the cross-sectional sort, and re-run ``backtest_signals.py``:
the directional rule is still ``decide``, and the backtest still measures it.

Why this was not built until now
--------------------------------
The signal layer shipped without carry because there was no verified source for
the non-US legs, and half a carry signal built on guessed series IDs would look
like the paper's result while measuring something else. A FRED probe settled it,
prompted by the news analyst discovering ECBMRRFR on its own mid-run.

What the probe found, and what it costs us
------------------------------------------
Only USD, EUR and GBP publish a rate fresh enough to call current (1-2 days).
Every other leg is monthly and runs about two months behind. A policy rate is a
step function and mostly flat, so a two-month-old value is usually still right --
but it is wrong precisely when a central bank has just moved, which is also when
the differential matters most. Every leg therefore reports its own age, and a
stale leg is said out loud rather than silently averaged in.

The consistent-measure plan also failed. The OECD ``IR3TIB01`` family would have
given one instrument across all currencies, which is what a differential wants,
but it is stale for EUR and GBP. So the basket mixes overnight rates with
3-month rates for CHF and NZD, and that tenor mismatch is recorded as a
deviation rather than hidden.
"""

from __future__ import annotations

import re
from datetime import date

from tradingagents.signals.base import Direction, Signal, SignalResult

NAME = "carry_rate_differential"

# Series verified against FRED by probe_fred.py, with the tenor each one
# measures. Overnight is preferred wherever it exists so the legs are
# comparable: DFF, ECBDFR and SONIA are all overnight rates, and the ECB deposit
# facility (not the main refi rate) is the one euro overnight rates actually sit
# against, which is why ECBDFR is chosen over ECBMRRFR despite both serving.
RATE_SERIES: dict[str, tuple[str, str]] = {
    "USD": ("DFF", "overnight"),
    "EUR": ("ECBDFR", "overnight"),
    "GBP": ("IUDSOIA", "overnight"),
    "JPY": ("IRSTCI01JPM156N", "overnight"),
    "CAD": ("IRSTCI01CAM156N", "overnight"),
    "AUD": ("IRSTCI01AUM156N", "overnight"),
    "CHF": ("IR3TIB01CHM156N", "3-month"),
    "NZD": ("IR3TIB01NZM156N", "3-month"),
}

# A differential this small is not a carry trade. Below it the expected pickup
# is inside the bid-offer and financing noise of actually holding the position,
# so the honest reading is no edge rather than a direction.
MIN_DIFFERENTIAL_PCT = 0.50

# Past this, a leg is reported as stale. A policy rate is persistent, so an old
# value is usually still correct -- but a central bank meeting inside the gap
# makes it wrong, and 45 days spans at least one meeting for every major.
STALE_AFTER_DAYS = 45

_LATEST = re.compile(r"\*\*Latest:\*\*\s*(-?[\d.]+)\s*\((\d{4}-\d{2}-\d{2})\)")


def parse_latest(report: str) -> tuple[float, str] | None:
    """(value, observation date) from a get_macro_data report, or None.

    ``get_macro_data`` returns guidance prose rather than raising on a bad
    series, so an unparseable report means no usable rate -- which is an answer,
    not an error to swallow.
    """
    match = _LATEST.search(report or "")
    if not match:
        return None
    return float(match.group(1)), match.group(2)


def _fetch_rate(currency: str, as_of_date: str) -> tuple[float, str, str] | None:
    """(rate, observation date, series id) for one currency, point-in-time."""
    from tradingagents.dataflows.vendors.fred import get_macro_data

    entry = RATE_SERIES.get(currency)
    if entry is None:
        return None
    series_id, _tenor = entry
    parsed = parse_latest(get_macro_data(series_id, as_of_date, 400))
    if parsed is None:
        return None
    value, obs_date = parsed
    return value, obs_date, series_id


def _age_days(obs_date: str, as_of_date: str) -> int | None:
    try:
        return (date.fromisoformat(as_of_date) - date.fromisoformat(obs_date)).days
    except (ValueError, TypeError):
        return None


def legs_of(ticker: str) -> tuple[str, str] | None:
    """(base, quote) currency codes for a forex pair, or None.

    Returns None rather than raising on anything that is not a pair. This runs
    before the analysts on whatever ticker the run was started with, and a
    classifier that throws is a classifier that ends the run.
    """
    if not isinstance(ticker, str):
        return None
    pair = ticker.upper().replace("=X", "").replace("/", "")
    if len(pair) != 6 or not pair.isalpha():
        return None
    return pair[:3], pair[3:]


def decide(
    base: str, quote: str, base_rate: float, quote_rate: float
) -> tuple[Direction, float]:
    """Direction implied by the differential, and the differential itself.

    Long the base when the base pays more: you earn the base rate and fund at the
    quote rate. For EURUSD with EUR at 2.50 and USD at 3.88 the differential is
    -1.38, so carry pays you to be short the euro and long the dollar.
    """
    differential = base_rate - quote_rate
    if abs(differential) < MIN_DIFFERENTIAL_PCT:
        return Direction.FLAT, differential
    return (Direction.LONG if differential > 0 else Direction.SHORT), differential


def compute(ticker: str, as_of_date: str) -> SignalResult:
    legs = legs_of(ticker)
    if legs is None:
        return SignalResult.unavailable(NAME, f"{ticker} is not a six-letter currency pair")
    base, quote = legs

    missing = [c for c in (base, quote) if c not in RATE_SERIES]
    if missing:
        return SignalResult.unavailable(
            NAME,
            f"no verified rate series for {', '.join(missing)}; a differential "
            f"built on a proxy would look like a carry signal while measuring "
            f"something else",
        )

    fetched = {}
    for currency in (base, quote):
        result = _fetch_rate(currency, as_of_date)
        if result is None:
            return SignalResult.unavailable(
                NAME, f"the {currency} rate ({RATE_SERIES[currency][0]}) returned no value"
            )
        fetched[currency] = result

    base_rate, base_date, base_series = fetched[base]
    quote_rate, quote_date, quote_series = fetched[quote]
    # The rule still runs, and ``implied`` is what it says. It is reported as
    # context rather than acted on -- see the demotion note at the top of this
    # module and ``our_history`` below.
    implied, differential = decide(base, quote, base_rate, quote_rate)

    caveats = [
        "This is context, not a trade. The differential is real and it is what "
        "the rule implies, but the rule did not survive our own history "
        "(pooled Sharpe -0.21, t=-1.04 over 25 years on seven majors), so it "
        "does not set direction. Use it to understand what holding the "
        "position costs or earns in financing, not as a reason to take it.",
    ]
    for currency, (_rate, obs_date, series_id) in fetched.items():
        age = _age_days(obs_date, as_of_date)
        if age is not None and age > STALE_AFTER_DAYS:
            caveats.append(
                f"the {currency} rate ({series_id}) is from {obs_date}, {age} days "
                f"before this analysis date -- if that central bank moved in the "
                f"gap, this differential is wrong"
            )

    tenors = {RATE_SERIES[base][1], RATE_SERIES[quote][1]}
    if len(tenors) > 1:
        caveats.append(
            f"the legs are measured at different tenors ({RATE_SERIES[base][1]} "
            f"for {base}, {RATE_SERIES[quote][1]} for {quote}), so part of this "
            f"spread is a term premium rather than a policy difference"
        )
    if implied is Direction.FLAT:
        caveats.append(
            f"a {differential:+.2f}% differential is inside the cost of holding "
            f"the position, so there is no carry worth naming either way here"
        )

    return SignalResult(
        name=NAME,
        # DIAGNOSTIC, not the direction the rule implies. ``consensus_direction``
        # does not let diagnostics vote, which is the whole point: the
        # differential informs the debate and cannot decide it.
        direction=Direction.DIAGNOSTIC,
        value=differential,
        detail={
            "differential_pct": round(differential, 4),
            # Kept so the demotion is auditable and reversible: this is what the
            # rule would have said, visible to a reader without being acted on.
            "rule_would_imply": implied.value,
            f"{base}_rate": base_rate,
            f"{quote}_rate": quote_rate,
            f"{base}_series": base_series,
            f"{quote}_series": quote_series,
            f"{base}_as_of": base_date,
            f"{quote}_as_of": quote_date,
        },
        caveats=tuple(caveats),
    )


SIGNAL = Signal(
    name=NAME,
    asset_types=frozenset({"forex"}),
    paper=(
        "Lustig, Roussanov and Verdelhan, 'Common Risk Factors in Currency "
        "Markets', Review of Financial Studies (2011), and the forward-premium "
        "literature. 37 currencies, November 1983 to March 2008."
    ),
    mechanism=(
        "Uncovered interest parity says a higher-yielding currency should "
        "depreciate by its rate advantage; empirically it has not, so being "
        "long the higher-yielding currency has earned a premium. The paper "
        "argues this is payment for a specific risk rather than free money: "
        "high-rate currencies fall hard when global risk rises, so the trade is "
        "short a disaster and the premium is the price of that insurance."
    ),
    definition=(
        "The short-rate differential between the two legs of the pair. The "
        f"underlying rule -- long the base when its rate exceeds the quote's by "
        f"more than {MIN_DIFFERENTIAL_PCT}%, short when lower by that much, flat "
        "in between -- is still computed and reported as `rule_would_imply`, but "
        "it does NOT set direction: it failed on our own history. What this "
        "contributes is the financing cost of holding the position, as context."
    ),
    deviations=(
        "The paper sorts 37 currencies into portfolios on forward discounts and "
        "trades the spread between the top and bottom. We take the differential "
        "on a single pair, so none of the cross-sectional diversification that "
        "produced the paper's return is present.",
        "Forward discounts embed the actual tradable financing cost; policy and "
        "overnight rates do not. The gap between them widens exactly when "
        "funding markets are stressed, which is when carry matters most.",
        "Only USD, EUR and GBP publish a rate fresh enough to call current. "
        "Every other leg runs about two months behind, and each one reports its "
        "own age rather than being quietly averaged in.",
        "CHF and NZD have only a 3-month rate where the others are overnight, "
        "so for those pairs part of the spread is a term premium rather than a "
        "policy difference.",
    ),
    fails_when=(
        "Global risk-off. This is the paper's central finding, not a footnote: "
        "high-rate currencies depreciate sharply when global risk rises, so "
        "carry loses most of what it made in short, violent episodes. The paper "
        "names the 1997 Asian crisis, LTCM in 1998, and the 1998 Russian "
        "default.",
        "A central bank moving between a stale leg's observation date and the "
        "analysis date, which inverts the differential without warning.",
        "Pegged or heavily managed currencies, where a wide differential is a "
        "policy stance rather than a return on offer.",
    ),
    our_history=(
        "Measured on the seven dollar majors, 25 years of daily closes to "
        "October 2026, rebalanced every 21 trading days with costs charged: "
        "pooled Sharpe -0.21, t=-1.04. Three of seven pairs were positive, and "
        "the book lost to holding the basket as well. t=-1.04 is not "
        "significantly negative, so the honest statement is that there is NO "
        "EVIDENCE this pays on our instruments -- not that it reliably loses. "
        "This is the predictable price of the deviations listed above rather "
        "than a surprise: the paper's premium comes from sorting 37 currencies "
        "on forward discounts and trading the spread between the extremes, and "
        "a single-pair differential built from policy rates keeps neither the "
        "cross-sectional diversification nor the tradable financing cost that "
        "produced it. On that evidence this signal was DEMOTED to a diagnostic: "
        "it reports the differential and no longer picks a side, so a wide "
        "differential is context about financing cost and not a reason for "
        "conviction. Note also that five of the eight rate series publish "
        "about 65 days late (AUD, CAD, CHF, JPY, NZD), so those legs trade on a "
        "two-month-old rate in live runs exactly as they did in this test."
    ),
    compute=compute,
)
