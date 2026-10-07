"""Time-series momentum, from Moskowitz, Ooi and Pedersen (2012).

The paper's construction, taken from the text rather than from memory:

  - Direction is the sign of the instrument's own excess return over the
    preceding 12 months. Long if positive, short if negative.
  - Position size is inversely proportional to ex-ante volatility, estimated as
    an exponentially weighted average of squared daily returns with a 60-day
    center of mass, annualised.
  - Monthly rebalancing.
  - Tested on 58 liquid futures: 24 commodities, 12 currency pairs, 9 equity
    index futures, 13 bond futures. 1985-2009, with 1966-1985 for robustness.
  - 12-month momentum profits were positive for every one of the 58 contracts.
  - The effect partially reverses beyond 12 months.

Why it is a reasonable prior rather than a curve fit: it was documented across
four asset classes, dozens of instruments and decades, which is a far harder
thing to produce by data mining than a single-market result. It is also one of
the few published effects with a mechanism that does not require the market to
be inefficient in a way that should have been arbitraged away -- the paper
attributes it to initial under-reaction followed by delayed over-reaction, with
speculators taking the other side of hedgers.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from tradingagents.signals.base import Direction, Signal, SignalResult

NAME = "time_series_momentum"

# The paper's own parameters, kept rather than re-tuned. Re-optimising a
# published parameter on our much shorter sample is how a prior turns into a
# curve fit, and it would forfeit the out-of-sample evidence that made the
# result worth using at all.
LOOKBACK_TRADING_DAYS = 252      # 12 months
VOLATILITY_CENTER_OF_MASS = 60   # days
TRADING_DAYS_PER_YEAR = 261      # the paper's annualisation constant

# Our deviations, each a place the published evidence may not transfer.
OUR_VOLATILITY_TARGET = 0.10     # the paper targets 40% on a 58-instrument book
MAX_POSITION_SCALAR = 3.0        # a cap the paper does not need and we do
MIN_HISTORY_DAYS = 200           # below this the 12-month signal is not formed


def ex_ante_volatility(daily_returns: pd.Series) -> float | None:
    """Annualised ex-ante volatility, the paper's estimator.

    Exponentially weighted squared daily returns with a 60-day center of mass,
    annualised by the square root of 261. Pandas measures the deviation around
    its own exponentially weighted mean, which is what the paper's equation does
    as well.

    Returns None rather than a number when the input cannot support an estimate:
    a volatility of zero would divide into an infinite position.
    """
    returns = pd.to_numeric(pd.Series(daily_returns), errors="coerce").dropna()
    if len(returns) < 2:
        return None
    sigma = returns.ewm(com=VOLATILITY_CENTER_OF_MASS).std().iloc[-1]
    if sigma is None or not np.isfinite(sigma) or sigma <= 0:
        return None
    return float(sigma * np.sqrt(TRADING_DAYS_PER_YEAR))


def position_scalar(volatility: float, target: float = OUR_VOLATILITY_TARGET) -> float:
    """Volatility-scaled size, capped.

    The paper sizes each position at a 40% annualised volatility target, which
    is only sane because it is one of 58 positions in a diversified book. Taken
    literally on a single instrument it is a instruction to use enormous
    leverage, and on a low-volatility instrument -- a pegged or managed currency
    especially -- the unbounded 1/sigma term explodes. We target 10% and cap the
    scalar; both are departures from the paper and are recorded as such.
    """
    if volatility is None or volatility <= 0:
        return 0.0
    return float(min(target / volatility, MAX_POSITION_SCALAR))


def _trailing_return(closes: pd.Series, lookback: int = LOOKBACK_TRADING_DAYS) -> float | None:
    """Simple return over the trailing window, or None if the window is short."""
    closes = pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
    closes = closes[closes > 0]
    if len(closes) < 2:
        return None
    window = closes.iloc[-(lookback + 1):] if len(closes) > lookback else closes
    first, last = float(window.iloc[0]), float(window.iloc[-1])
    if first <= 0:
        return None
    return last / first - 1.0


def compute_from_closes(closes: pd.Series, instrument: str = "") -> SignalResult:
    """The rule itself, as a pure function of a close series.

    Separated from data loading so it can be tested against a series with known
    properties rather than against whatever the vendor served today.
    """
    closes = pd.to_numeric(pd.Series(closes), errors="coerce").dropna()
    closes = closes[closes > 0]
    if len(closes) < MIN_HISTORY_DAYS:
        return SignalResult.unavailable(
            NAME,
            f"needs about a year of closes to form a 12-month signal; "
            f"{len(closes)} usable rows available"
            + (f" for {instrument}" if instrument else ""),
        )

    trailing = _trailing_return(closes)
    if trailing is None:
        return SignalResult.unavailable(NAME, "trailing return could not be computed")

    daily_returns = closes.pct_change().dropna()
    volatility = ex_ante_volatility(daily_returns)
    if volatility is None:
        return SignalResult.unavailable(
            NAME, "ex-ante volatility is zero or undefined, so the position cannot be sized"
        )

    scalar = position_scalar(volatility)
    # An exactly flat trailing return carries no direction. Treating zero as
    # long -- which sign() would -- invents a position out of a tie.
    if trailing > 0:
        direction = Direction.LONG
    elif trailing < 0:
        direction = Direction.SHORT
    else:
        direction = Direction.FLAT

    caveats = []
    realised_window = min(len(closes) - 1, LOOKBACK_TRADING_DAYS)
    if realised_window < LOOKBACK_TRADING_DAYS:
        caveats.append(
            f"formed on {realised_window} trading days, short of the paper's 12 months"
        )
    if abs(trailing) < 0.02:
        caveats.append(
            "trailing return is close to zero, so the sign is near a coin flip "
            "and the direction should carry little conviction"
        )
    if scalar >= MAX_POSITION_SCALAR:
        caveats.append(
            f"volatility is low enough that the paper's 1/sigma sizing hit our "
            f"cap of {MAX_POSITION_SCALAR}x"
        )

    return SignalResult(
        name=NAME,
        direction=direction,
        value=trailing,
        detail={
            "trailing_12m_return": round(trailing, 6),
            "ex_ante_annualised_volatility": round(volatility, 6),
            "position_scalar": round(scalar, 4),
            "volatility_target": OUR_VOLATILITY_TARGET,
            "observations": len(closes),
        },
        caveats=tuple(caveats),
    )


def compute(ticker: str, as_of_date: str) -> SignalResult:
    """Load point-in-time closes and apply the rule."""
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    frame = load_ohlcv(ticker, as_of_date)
    if frame is None or "Close" not in getattr(frame, "columns", ()):
        return SignalResult.unavailable(NAME, f"no price history for {ticker}")
    return compute_from_closes(frame["Close"], instrument=ticker)


SIGNAL = Signal(
    name=NAME,
    asset_types=frozenset({"forex", "commodity", "crypto", "stock"}),
    paper=(
        "Moskowitz, Ooi and Pedersen, 'Time Series Momentum', Journal of "
        "Financial Economics (2012). 58 futures across four asset classes, "
        "1985-2009."
    ),
    mechanism=(
        "Prices under-react to information and then over-react, so a move "
        "persists for up to twelve months before partially reversing. The "
        "paper attributes the premium to speculators taking the other side of "
        "hedgers' demand."
    ),
    definition=(
        "Long if the trailing 12-month return is positive, short if negative, "
        "with the position scaled inversely to ex-ante annualised volatility "
        "(exponentially weighted squared daily returns, 60-day center of mass)."
    ),
    deviations=(
        "The paper uses excess returns on fully collateralised futures. For a "
        "spot forex pair we only have the spot return, which omits the interest "
        "differential -- for a high-carry pair that is a material part of the "
        "excess return the paper measured, and the omission biases the signal "
        "toward the spot trend.",
        "The paper rebalances monthly; we evaluate on the analysis date, so a "
        "run between rebalances sees a signal the paper would not have acted on "
        "that day.",
        "We target 10% volatility, not the paper's 40%, because that target was "
        "set for a 58-instrument portfolio rather than a single position.",
        f"We cap the volatility scalar at {MAX_POSITION_SCALAR}x; the paper does not.",
    ),
    fails_when=(
        "Horizons beyond twelve months, where the paper finds the effect "
        "partially reverses.",
        "Sharp trend reversals, which is the characteristic drawdown of every "
        "trend-following strategy and is not a malfunction of the signal.",
        "Instruments whose volatility is suppressed by a peg or heavy "
        "management, where 1/sigma sizing implies a position the liquidity "
        "cannot support and the trend itself is a policy choice.",
    ),
    our_history=(
        "Measured on our nine instruments, 25 years of daily closes to October "
        "2026, rebalanced every 21 trading days, costs charged, execution one "
        "bar after formation: pooled Sharpe 0.55, t=2.93. That does NOT clear "
        "the 3.0 hurdle, and it did not beat simply holding the basket, which "
        "returned 0.57 over the same bars. The result is also concentrated "
        "rather than broad: across the seven FX majors the median Sharpe was "
        "+0.06 (range -0.26 to +0.37), gold returned 0.48 against its own "
        "buy-and-hold of 0.69, and BTC-USD alone returned 1.05 at t=4.21 -- in "
        "a market that rose roughly a hundredfold over the sample, where a "
        "trend rule and a long position are hard to tell apart. Robust to the "
        "execution assumption: pooled Sharpe was 0.54 / 0.55 / 0.55 at zero, "
        "one and two bars of lag, so none of it depends on trading at a close "
        "the rule is still computing from. Read this as a consistency anchor "
        "that has not been shown to add return -- it reliably gives the same "
        "answer twice, which is what it was built for, and that is a different "
        "claim from paying."
    ),
    compute=compute,
)
