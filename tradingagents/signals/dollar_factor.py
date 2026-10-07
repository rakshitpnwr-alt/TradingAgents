"""The dollar factor, from Lustig, Roussanov and Verdelhan (2011).

The paper builds two factors from currency portfolios. The one we can compute
honestly today is RX, the dollar factor: the equal-weighted average excess
return across all foreign currencies against the dollar, described in the paper
as "the currency 'market' return in dollars available to a US investor".

This exists because the agents asked for it twice, unprompted. Analysing EURUSD,
the market analyst wrote that it "cannot decompose whether this is EUR weakness
or USD strength -- that requires comparing EUR crosses against USD crosses", and
the portfolio manager asked for "a quick USD-cross check". They then improvised
the test by eyeballing sterling, and reasoned from a single comparison. The
decomposition they were reaching for is a published construct, and this makes it
an arithmetic fact of the run rather than a narrative an agent assembles.

The decomposition
-----------------
Every pair's move has two candidate causes. Expressing each pair as its foreign
currency's return against the dollar:

    foreign_leg = dollar_factor + residual

``dollar_factor`` is what the whole basket did against the dollar, so it is the
part of the move that is about the dollar. ``residual`` is what this currency did
that the basket did not, so it is the part that is about that currency. A move
that is nearly all dollar factor is a dollar story; a large residual is a
currency-specific story. That is the question the agents kept asking.

The second factor, HML-FX (carry), is deliberately absent. It sorts currencies on
forward discounts, which requires interest rates for every leg of the basket, and
we do not have a verified source for the non-US legs. Half of a carry signal
built on proxies would be worse than no carry signal, because it would look like
the paper's result while measuring something else.
"""

from __future__ import annotations

import pandas as pd

from tradingagents.signals.base import Direction, Signal, SignalResult

NAME = "dollar_factor_decomposition"

# The majors, all confirmed to serve full weekday coverage on the vendor. The
# basket must be fixed rather than "whatever loaded today": a factor whose
# membership changes with vendor availability is not comparable between runs,
# and the whole purpose of this layer is a number that does not move when
# nothing moved.
BASKET: tuple[str, ...] = (
    "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "NZDUSD=X",
    "USDJPY=X", "USDCHF=X", "USDCAD=X",
)

DEFAULT_WINDOW_DAYS = 21          # about a month, the horizon the agents discuss
MIN_BASKET_FOR_A_FACTOR = 4       # below this the "market" is a couple of pairs
# A residual this much larger than the dollar move is what makes a story
# currency-specific rather than dollar-driven. Our threshold, not the paper's --
# LRV builds a factor, it does not classify individual moves.
CURRENCY_SPECIFIC_RATIO = 1.0


def foreign_leg_series(symbol: str, closes: pd.Series) -> pd.Series | None:
    """The foreign currency's value in dollars, whichever way the pair is quoted.

    ``EURUSD=X`` is already dollars per euro, so it is the euro's value in
    dollars. ``USDJPY=X`` is yen per dollar, so the yen's value in dollars is its
    reciprocal. Getting this backwards would flip the sign of half the basket and
    produce a dollar factor near zero for every date, which would look like a
    calm market rather than a bug.
    """
    pair = (symbol or "").upper().replace("=X", "")
    closes = pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
    closes = closes[closes > 0]
    if len(closes) < 2 or len(pair) != 6:
        return None
    if pair.endswith("USD"):
        return closes
    if pair.startswith("USD"):
        return 1.0 / closes
    return None  # a cross with no dollar leg says nothing about the dollar


def _window_return(series: pd.Series, window: int) -> float | None:
    series = series.dropna()
    if len(series) < 2:
        return None
    sliced = series.iloc[-(window + 1):] if len(series) > window else series
    first, last = float(sliced.iloc[0]), float(sliced.iloc[-1])
    if first <= 0:
        return None
    return last / first - 1.0


def decompose(
    pair_returns: dict[str, float], ticker: str
) -> SignalResult:
    """Split the analysed pair's move into a dollar part and a residual.

    ``pair_returns`` maps each basket symbol to its foreign leg's return against
    the dollar over the window. Pure, so it can be tested on constructed cases
    where the right answer is known by construction.
    """
    usable = {s: r for s, r in pair_returns.items() if r is not None and r == r}
    if len(usable) < MIN_BASKET_FOR_A_FACTOR:
        return SignalResult.unavailable(
            NAME,
            f"only {len(usable)} of {len(BASKET)} basket pairs priced; a dollar "
            f"factor needs at least {MIN_BASKET_FOR_A_FACTOR} to mean anything",
        )

    canonical = (ticker or "").upper()
    if not canonical.endswith("=X"):
        canonical = f"{canonical}=X"
    if canonical not in usable:
        return SignalResult.unavailable(
            NAME, f"{ticker} is not a dollar pair in the basket, so it has no dollar leg"
        )

    dollar_factor = sum(usable.values()) / len(usable)
    foreign_leg = usable[canonical]
    residual = foreign_leg - dollar_factor

    # Which leg is driving, in the terms the agents kept asking for.
    if abs(dollar_factor) < 1e-9:
        attribution = "currency-specific (the basket did essentially nothing)"
    elif abs(residual) > abs(dollar_factor) * CURRENCY_SPECIFIC_RATIO:
        attribution = "mostly currency-specific"
    else:
        attribution = "mostly a dollar move"

    caveats = [
        "This is a decomposition, not a trade: it says which leg moved, not "
        "which way the pair goes next.",
    ]
    if len(usable) < len(BASKET):
        caveats.append(
            f"computed on {len(usable)} of {len(BASKET)} basket pairs, so the "
            f"factor is noisier than usual"
        )

    return SignalResult(
        name=NAME,
        direction=Direction.DIAGNOSTIC,
        value=residual,
        detail={
            "dollar_factor": round(dollar_factor, 6),
            "foreign_leg_return": round(foreign_leg, 6),
            "residual": round(residual, 6),
            "attribution": attribution,
            "basket_size": len(usable),
            "basket": sorted(usable),
        },
        caveats=tuple(caveats),
    )


def compute(ticker: str, as_of_date: str, window: int = DEFAULT_WINDOW_DAYS) -> SignalResult:
    """Price the basket point-in-time and decompose the analysed pair's move."""
    from tradingagents.dataflows.symbols import normalize_symbol
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    canonical = normalize_symbol(ticker)
    symbols = set(BASKET) | {canonical}
    returns: dict[str, float] = {}
    for symbol in sorted(symbols):
        try:
            frame = load_ohlcv(symbol, as_of_date)
        except Exception:  # noqa: BLE001 -- one missing pair must not sink the factor
            continue
        if frame is None or "Close" not in getattr(frame, "columns", ()):
            continue
        leg = foreign_leg_series(symbol, frame["Close"])
        if leg is None:
            continue
        value = _window_return(leg, window)
        if value is not None:
            returns[symbol] = value

    return decompose(returns, canonical)


SIGNAL = Signal(
    name=NAME,
    asset_types=frozenset({"forex"}),
    paper=(
        "Lustig, Roussanov and Verdelhan, 'Common Risk Factors in Currency "
        "Markets', Review of Financial Studies (2011). 37 currencies, "
        "November 1983 to March 2008."
    ),
    mechanism=(
        "Currency returns against the dollar share a common component -- the "
        "dollar factor, the average return of the basket -- so any single "
        "pair's move is partly about the dollar and partly about that currency. "
        "Separating them is what tells a broad-dollar move from a story about "
        "one economy."
    ),
    definition=(
        "RX is the equal-weighted average return of the foreign legs of a fixed "
        "basket of dollar pairs. The analysed pair's foreign leg minus RX is the "
        "residual: the part of its move the basket does not explain."
    ),
    deviations=(
        "The paper uses excess returns from forward contracts; we use spot "
        "returns, which omit the interest differential. For the decomposition "
        "this matters less than it would for a carry signal, because the "
        "omission is largely common across the basket and partly cancels.",
        "The paper builds factors from six portfolios sorted on forward "
        "discounts across 37 currencies. We use seven majors unsorted, so this "
        "is the dollar factor only -- a cruder instrument on a narrower basket.",
        "Classifying a single pair's move as dollar-driven or currency-specific "
        "is our construction. The paper builds a risk factor; it does not "
        "attribute individual moves.",
    ),
    fails_when=(
        "The basket is itself dominated by one currency's crisis, which makes "
        "the 'market' return a story about that currency.",
        "Windows short enough that the residual is mostly noise.",
        "Pegged or heavily managed currencies, whose residual reflects a policy "
        "choice rather than a market view.",
    ),
    our_history=(
        "Not measurable as a strategy, and that is a property of the signal "
        "rather than a gap in the testing: it returns DIAGNOSTIC and holds no "
        "position, so there is no return stream to score. Scoring it would mean "
        "inventing a trading rule it does not make and reporting that "
        "invention's performance under the paper's name. What it offers is the "
        "decomposition itself, which is arithmetic on prices rather than a "
        "claim about future returns. One measured fact does bear on it: across "
        "the nine-instrument book, volatility pooled to 6.1% where each "
        "position targeted 10% -- nine independent bets would have pooled to "
        "about 3.3%, so a basket of dollar pairs is substantially one bet. That "
        "is the thing this signal exists to measure."
    ),
    compute=compute,
)
