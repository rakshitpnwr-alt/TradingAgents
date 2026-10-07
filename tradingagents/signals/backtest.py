"""The fourth bar: does a signal survive on the instruments we actually trade?

``signals/base.py`` sets four bars for a signal reaching the registry --
mechanism, data, deviation, and "our history". Three were cleared by reading
the papers carefully and being honest about our departures from them. The
fourth was never cleared at all. Four signals shipped with good provenance and
no evidence whatsoever that they pay on our own prices, and that gap widened
with each one added. This closes it.

What this is not
----------------
``tradingagents/backtest.py`` sweeps the whole agent graph over a grid of
tickers and dates and scores the ratings it produced. That costs a model call
per cell and measures the pipeline. This measures a *rule*: it is
deterministic, free, and gives the same answer every time, which is the entire
reason the signal layer exists. The two are not substitutes. A signal that does
not pay here cannot be rescued by agents sizing it well.

It is also not a portfolio simulator. There is no cash ledger, no margin, no
position carried between instruments. Each instrument is walked on its own and
the pooled result is an equal-weighted average of those walks, which is the
closest honest analogue of the paper's "positive for all 58 contracts" claim.

The three ways a backtest lies, and what is done about each
-----------------------------------------------------------
*Look-ahead.* A reading at bar ``t`` is formed from closes ``0..t`` and nothing
else, by calling the same function the live signal calls. It then earns the
return from bar ``t + lag + 1`` onward. Nothing the rule could not have known is
in its input, and nothing it earns overlaps the window it was formed from.

*Costless trading.* Every change in exposure is charged. A monthly-rebalanced
rule that flips direction pays the spread twice, and a backtest that skips this
is reporting a return nobody could have collected. Gross and net are both
reported so the cost drag is visible rather than assumed.

*Choosing the instruments afterwards.* ``OUR_INSTRUMENTS`` is fixed here, in
source, chosen because they are what Rakshit trades -- not because they are
what worked. The pooled figure and the count of instruments with a positive
Sharpe are the headline; the best single instrument is not a result.

What the numbers can and cannot support
---------------------------------------
Five to twenty years of daily data on under a dozen instruments is a small
sample for this question. A Sharpe ratio estimated from it carries a standard
error large enough that most plausible edges are indistinguishable from zero,
so every figure here is reported with its t-statistic against the roughly 3.0
hurdle Harvey, Liu and Zhu argue for once the published-anomaly multiple-testing
problem is accounted for -- not the conventional 2.0. A signal that clears 2.0
and not 3.0 has not been shown to work. Saying so is the point of the exercise.

Sharpe, not return, is the headline. Our volatility target is a choice we made
(10%, against the paper's 40%), and it scales the return arbitrarily. It does
not scale the Sharpe.

Where this differs from what the live system does
-------------------------------------------------
*Gaps are dropped here and filled there.* ``load_ohlcv`` carries prices across
a gap because indicators need a continuous series. This drops the gap instead,
because a filled bar is a day of invented zero return: it adds days without
adding movement, which lowers the measured volatility and raises the Sharpe of
every rule in the file. The cost of dropping them is that volatility here runs
slightly above what the live signal sees, so the position scalar it would have
used is slightly larger than the one measured. Sharpe is unaffected by that
scaling; reported return is not.

*Rebalances land on trading days.* The live system can be asked for any
calendar date. This walks bars, so every holding period is the same number of
sessions and none begins on a closed market.

*Execution is one bar after formation.* The paper's is not. See
``EXECUTION_LAG_DAYS``.

*Carry's rates are fetched whole rather than one vintage at a time.* The live
signal asks FRED for a single value with the data vintage pinned to the run
date; doing that per rebalance would be some five thousand rate-limited
requests. ``RateHistory`` explains what that costs and the publication lag that
pays most of it back.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from tradingagents.dataflows.symbols import (
    KIND_COMMODITY,
    KIND_CRYPTO,
    KIND_FOREX,
    KIND_STOCK,
    instrument_kind,
)
from tradingagents.dataflows.vendors.yahoo.history import load_history
from tradingagents.signals import carry, dollar_factor, tsmom
from tradingagents.signals.base import Direction, SignalResult
from tradingagents.signals.tsmom import TRADING_DAYS_PER_YEAR

logger = logging.getLogger(__name__)

# The instruments this system is for. Fixed here rather than passed in, because
# a list chosen after seeing results is the oldest way to manufacture an edge:
# the majors Rakshit trades, gold as the metal he asked for, and BTC as the one
# non-forex holding the pipeline has actually been run on.
OUR_INSTRUMENTS: tuple[str, ...] = (
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X",
    "USDCHF=X", "USDCAD=X", "NZDUSD=X",
    "GC=F",
    "BTC-USD",
)

# As much history as the vendor will serve. The live path deliberately fetches
# five years -- an analyst reading a chart does not need 1990 -- but five years
# of monthly rebalances is about sixty observations, which cannot distinguish a
# real edge from luck. This asks for far more and reports what it actually got.
HISTORY_YEARS = 25

# Trading days between rebalances. The paper rebalances monthly; twenty-one
# trading days is that, counted in bars so a rebalance never lands on a day the
# market was shut.
REBALANCE_EVERY_N_DAYS = 21

# Bars between forming a reading and holding the resulting position. The paper
# forms its signal on the month's last close and holds from there, which assumes
# you can trade at a close you are still computing from. One bar later is what a
# person could actually have done, so it is the default; the runner reports zero
# as a sensitivity, and the difference between them is a measure of how much of
# any edge lives in that fiction.
EXECUTION_LAG_DAYS = 1

# Round-trip cost charged on every unit of exposure traded, in basis points of
# notional, per side. Retail spot majors run about a pip on EURUSD, which is
# under a basis point; gold futures and especially crypto are dearer. Erring
# high is the safe direction for a backtest, so these are the wide end of
# plausible rather than the keen end.
COST_BPS_PER_SIDE: dict[str, float] = {
    KIND_FOREX: 1.5,
    KIND_COMMODITY: 4.0,
    KIND_CRYPTO: 15.0,
    KIND_STOCK: 3.0,
}
DEFAULT_COST_BPS_PER_SIDE = 5.0

# Harvey, Liu and Zhu (2016): with hundreds of published factors competing for
# the same data, a t-statistic of 2.0 no longer means what it means in a single
# test. They argue for roughly 3.0. We test rules that are already in the
# literature, so we inherit that problem rather than escaping it.
SIGNIFICANCE_HURDLE_T = 3.0

# Below this many rebalance periods the statistics are reported but should not
# be believed: the standard error on a Sharpe swamps anything they would show.
MIN_PERIODS_FOR_INFERENCE = 30


# ----------------------------------------------------------------------------
# Readings
# ----------------------------------------------------------------------------

# A reader turns a point-in-time slice of history plus its as-of date into the
# signal's own reading. It must call the signal's code, never reimplement it:
# a backtest of a copy of the rule tests the copy.
Reader = Callable[[pd.DataFrame, str, str], SignalResult]


def tsmom_reader(frame: pd.DataFrame, as_of: str, ticker: str) -> SignalResult:
    """Time-series momentum on the closes available at ``as_of``.

    ``tsmom.compute`` is exactly ``compute_from_closes`` applied to
    ``load_ohlcv(ticker, as_of)["Close"]``, so calling the pure function on a
    slice is the live rule, not an imitation of it.
    """
    return tsmom.compute_from_closes(frame["Close"], instrument=ticker)


@dataclass
class RateHistory:
    """Each currency's short rate over the whole window, fetched once.

    ``carry.compute`` asks FRED for one value per call with the data vintage
    pinned to the as-of date, which is exactly right for a live run and
    impossible for a backtest: eight currencies over three hundred rebalance
    dates is some five thousand HTTP requests, which FRED rate-limits into an
    hour of waiting. So the series are fetched whole, once each, and read
    locally.

    What that costs, and how it is paid back
    ----------------------------------------
    Fetching whole loses the vintage pin, and the pin was doing real work. A
    monthly OECD series carries an observation dated inside a month that was not
    *published* until roughly two months later, so reading it by observation
    date would hand a June reading to a run made in June -- knowing the month's
    average rate before the month had finished. That is look-ahead, and on the
    JPY, CAD, AUD, CHF and NZD legs it would be worth about two months of policy
    news.

    So each series carries a publication lag, measured from the data itself: how
    far behind its own last observation runs today. An observation is then only
    readable once that many days have passed, which reproduces what the live
    signal would have been able to see. The remaining assumption is that a
    series published at the same speed throughout its history as it does now,
    and where it did not, the lag is wrong by the difference.

    Revisions are the other loss and the smaller one. Overnight policy rates --
    DFF, ECBDFR, SONIA -- are not revised, so the latest value and the
    originally published value are the same number. The monthly OECD series can
    be revised, and for those legs this reads a revised figure where the live
    signal read the original.
    """

    start: str = "1990-01-01"
    end: str = ""
    series: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    publication_lag_days: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.end = self.end or pd.Timestamp.today().strftime("%Y-%m-%d")

    def load(self, currency: str) -> list[tuple[str, float]]:
        """This currency's observations, fetched on first ask and kept."""
        if currency in self.series or currency in self.errors:
            return self.series.get(currency, [])
        entry = carry.RATE_SERIES.get(currency)
        if entry is None:
            self.errors[currency] = f"no verified rate series for {currency}"
            return []
        series_id = entry[0]
        try:
            from tradingagents.dataflows.vendors.fred import get_series_observations

            points = get_series_observations(series_id, self.start, self.end)
        except Exception as exc:  # noqa: BLE001 -- one dead series is not the sweep
            self.errors[currency] = f"{series_id}: {type(exc).__name__}: {exc}"
            return []
        if not points:
            self.errors[currency] = f"{series_id} returned no observations"
            return []
        self.series[currency] = points
        # The lag the series runs at now, taken as the lag it ran at throughout.
        self.publication_lag_days[currency] = max(
            (pd.Timestamp(self.end) - pd.Timestamp(points[-1][0])).days, 0
        )
        return points

    def rate_as_of(self, currency: str, as_of: str) -> tuple[float, str] | None:
        """The latest rate that had been published by ``as_of``, and its date."""
        points = self.load(currency)
        if not points:
            return None
        lag = self.publication_lag_days.get(currency, 0)
        try:
            visible_through = (pd.Timestamp(as_of) - pd.Timedelta(days=lag)).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None
        # ISO dates sort lexically, so the last observation at or before the
        # cutoff is the last one whose date string does not exceed it.
        usable = [p for p in points if p[0] <= visible_through]
        if not usable:
            return None
        value, obs_date = usable[-1][1], usable[-1][0]
        return value, obs_date


def read_carry(rates: RateHistory, as_of: str, ticker: str) -> SignalResult:
    """The carry rule on a locally held rate history.

    The *rule* is carry's own: ``legs_of`` splits the pair, ``RATE_SERIES`` says
    which series each leg uses, and ``decide`` applies the differential and the
    minimum that makes it a trade. Only the fetch is different, which is the
    one part that has to be.

    This deliberately reads a DIRECTION from a signal whose ``compute`` now
    returns DIAGNOSTIC. That is not an inconsistency to tidy up. ``compute``
    describes what the live pipeline is allowed to act on; this describes what
    the rule says, and the rule is what gets measured. Carry was demoted
    *because* this measured it, and promoting it back has to be answerable with
    a number rather than an argument -- which it cannot be if the backtest
    stops testing the rule the moment the registry stops trusting it.

    So carry belongs in ``READERS``, not in ``NOT_BACKTESTABLE``. The dollar
    factor is in ``NOT_BACKTESTABLE`` for a different reason: its rule produces
    no direction at all, at any level. "We chose not to act on this direction"
    and "there is no direction here" are not the same statement.
    """
    legs = carry.legs_of(ticker)
    if legs is None:
        return SignalResult.unavailable(
            carry.NAME, f"{ticker} is not a six-letter currency pair")
    base, quote = legs
    missing = [c for c in (base, quote) if c not in carry.RATE_SERIES]
    if missing:
        return SignalResult.unavailable(
            carry.NAME, f"no verified rate series for {', '.join(missing)}")

    readings = {}
    for currency in (base, quote):
        reading = rates.rate_as_of(currency, as_of)
        if reading is None:
            return SignalResult.unavailable(
                carry.NAME,
                rates.errors.get(currency)
                or f"no {currency} rate published by {as_of}",
            )
        readings[currency] = reading

    (base_rate, base_date), (quote_rate, quote_date) = readings[base], readings[quote]
    direction, differential = carry.decide(base, quote, base_rate, quote_rate)
    # Staleness is reported, not acted on, because the live signal also trades a
    # stale leg -- it says so loudly in a caveat and carries on. A backtest that
    # refused those dates would be measuring a stricter rule than the one running.
    caveats = tuple(
        f"the {currency} rate is from {date}, "
        f"{(pd.Timestamp(as_of) - pd.Timestamp(date)).days} days before this date"
        for currency, (_v, date) in readings.items()
        if (pd.Timestamp(as_of) - pd.Timestamp(date)).days > carry.STALE_AFTER_DAYS
    )
    return SignalResult(
        name=carry.NAME, direction=direction, value=differential,
        detail={
            "differential_pct": round(differential, 4),
            f"{base}_rate": base_rate, f"{quote}_rate": quote_rate,
            f"{base}_as_of": base_date, f"{quote}_as_of": quote_date,
            "stale_legs": len(caveats),
        },
        caveats=caveats,
    )


_RATE_HISTORY: RateHistory | None = None


def rate_history() -> RateHistory:
    """The shared rate history, built on first use so importing costs nothing."""
    global _RATE_HISTORY
    if _RATE_HISTORY is None:
        _RATE_HISTORY = RateHistory()
    return _RATE_HISTORY


def carry_reader(frame: pd.DataFrame, as_of: str, ticker: str) -> SignalResult:
    return read_carry(rate_history(), as_of, ticker)


READERS: dict[str, Reader] = {
    tsmom.NAME: tsmom_reader,
    carry.NAME: carry_reader,
}

# Signals that cannot be backtested as strategies, and why. Recorded rather
# than omitted: a missing row in a results table reads as an oversight, and
# "we did not test it" and "it is not the kind of thing that can be tested
# this way" are different statements.
# Keyed on signals whose RULE yields no direction -- not on signals we have
# chosen not to trade. A demoted signal keeps its reader so the demotion stays
# reversible on evidence; see ``read_carry``.
NOT_BACKTESTABLE: dict[str, str] = {
    dollar_factor.NAME: (
        "it returns DIAGNOSTIC, not a direction. It decomposes a move into a "
        "dollar part and a currency-specific part; there is no position to hold "
        "and therefore no return to measure. Backtesting it would mean inventing "
        "a trading rule it does not make, and then reporting that invention's "
        "performance under the paper's name."
    ),
}


# ----------------------------------------------------------------------------
# The walk
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One signal reading in the walk, and the bar it was formed on."""

    bar: int
    date: str
    direction: Direction
    scalar: float
    unavailable_reason: str | None = None

    @property
    def exposure(self) -> float:
        """Signed size this reading implies, or NaN when the rule could not speak.

        The distinction ``Direction`` draws has to survive into the arithmetic or
        it was decorative. FLAT and DIAGNOSTIC are readings: the rule ran and
        implies no position, so those bars earn zero and that zero is a real
        outcome of a real decision, which belongs in the statistics.

        UNAVAILABLE is not a reading. Those bars must be NaN and drop out of the
        measurement entirely. Scored as zeros they would be hundreds of free
        flat days -- a twelve-month rule has no reading for its first year -- and
        days with no movement added to a return stream lower its measured
        volatility without lowering its return, which raises the Sharpe of every
        rule in the file. That is the single most flattering mistake a backtest
        of a slow signal can make.
        """
        if self.direction is Direction.LONG:
            return self.scalar
        if self.direction is Direction.SHORT:
            return -self.scalar
        if self.direction is Direction.UNAVAILABLE:
            return float("nan")
        return 0.0


def rebalance_bars(
    n_rows: int, every_n_days: int = REBALANCE_EVERY_N_DAYS, warmup: int = 0
) -> list[int]:
    """Bar indices to form a reading on, counted in trading days.

    Counted in bars rather than calendar days so a rebalance never lands on a
    closed market and every holding period is the same number of sessions. The
    live system can be asked on any calendar date; this is a deviation, and a
    small one, since the rule's inputs only change on days that trade.
    """
    if every_n_days < 1:
        raise ValueError("every_n_days must be at least 1")
    if n_rows <= 0:
        return []
    return list(range(max(int(warmup), 0), n_rows, int(every_n_days)))


def walk(
    frame: pd.DataFrame,
    reader: Reader,
    ticker: str,
    every_n_days: int = REBALANCE_EVERY_N_DAYS,
) -> list[Reading]:
    """Form a reading at every rebalance bar, each from that bar's history only.

    The slice is ``frame.iloc[: bar + 1]`` -- through the bar's own close and not
    one row further. That single expression is where look-ahead would live, so
    it is the one line in this module worth re-reading.
    """
    readings: list[Reading] = []
    for bar in rebalance_bars(len(frame), every_n_days):
        as_of = frame["Date"].iloc[bar].strftime("%Y-%m-%d")
        window = frame.iloc[: bar + 1]
        try:
            result = reader(window, as_of, ticker)
        except Exception as exc:  # noqa: BLE001 -- one bad date must not end the walk
            logger.warning("Reading %s on %s failed: %s", ticker, as_of, exc)
            readings.append(Reading(bar, as_of, Direction.UNAVAILABLE, 0.0,
                                    f"{type(exc).__name__}: {exc}"))
            continue
        scalar = _scalar_of(result)
        readings.append(Reading(
            bar=bar, date=as_of, direction=result.direction, scalar=scalar,
            unavailable_reason=result.unavailable_reason,
        ))
    return readings


def _scalar_of(result: SignalResult) -> float:
    """Position size a reading implies, or 1.0 when the signal does not size.

    A signal with no sizing rule is held at unit exposure. That is a decision,
    not a neutral default: it means carry's results are the direction's results
    and say nothing about how large the position should have been.
    """
    try:
        scalar = float(result.detail.get("position_scalar", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(scalar) or scalar < 0:
        return 0.0
    return scalar


def exposure_series(
    frame: pd.DataFrame, readings: list[Reading], lag: int = EXECUTION_LAG_DAYS
) -> pd.Series:
    """The signed exposure earning each bar's return, or NaN where none applies.

    A reading formed on bar ``t`` is executed at the close of ``t + lag`` and so
    earns the return of bar ``t + lag + 1`` onward -- the return from the close it
    was executed at to the next close. Bars before the first reading takes effect
    are NaN rather than zero: no position was held because the rule had not
    spoken yet, which is different from the rule saying to hold nothing.
    """
    exposure = pd.Series(np.nan, index=range(len(frame)), dtype=float)
    lag = max(int(lag), 0)
    starts = [r.bar + lag + 1 for r in readings]
    for index, reading in enumerate(readings):
        start = starts[index]
        if start >= len(frame):
            break
        stop = next((s for s in starts[index + 1:] if s > start), len(frame))
        exposure.iloc[start:min(stop, len(frame))] = reading.exposure
    return exposure


def cost_bps_for(ticker: str) -> float:
    return COST_BPS_PER_SIDE.get(instrument_kind(ticker), DEFAULT_COST_BPS_PER_SIDE)


def returns_frame(
    frame: pd.DataFrame, exposure: pd.Series, cost_bps: float
) -> pd.DataFrame:
    """Per-bar asset return, exposure, turnover, cost, and strategy return.

    Everything here reads positionally, through ``to_numpy``, so a frame that
    arrived pre-filtered keeps working: aligning a fresh exposure series against
    a caller's surviving index labels would silently produce NaN for every bar.

    The cost is charged on the bar the exposure changes, against the notional
    actually traded: going from short-one to long-one is two units of turnover
    and pays twice, which is the expensive thing trend rules do and the thing a
    gross-only backtest hides.

    Turnover and cost are columns rather than quantities derived later, because
    the pooled book cannot recover them from its own average exposure -- a book
    whose instruments flip from long to short in opposite directions shows no
    change in its mean exposure while every instrument in it paid a spread.

    One cost is knowingly not charged: closing a position because the rule went
    UNAVAILABLE. Those bars carry no return to charge it against, and putting a
    cost on a bar with no position would be its own fiction. The understatement
    is one exit per mid-sample gap plus one at the end of the sample, so
    ``InstrumentResult.mid_sample_unavailable`` is what says how much of it
    there is -- zero gaps means the only uncharged exit is the last one.
    """
    closes = pd.Series(frame["Close"].to_numpy(), dtype=float)
    asset = closes.pct_change()
    exposure = pd.Series(np.asarray(exposure, dtype=float))
    # Turnover against the exposure held on the previous bar; an unset previous
    # exposure (the first position of the walk) is a trade from nothing.
    turnover = (exposure.fillna(0.0) - exposure.shift(1).fillna(0.0)).abs()
    cost = turnover * (float(cost_bps) / 10_000.0)
    gross = exposure * asset
    return pd.DataFrame({
        "date": frame["Date"].to_numpy(),
        "asset": asset.to_numpy(),
        "exposure": exposure.to_numpy(),
        "turnover": turnover.to_numpy(),
        "cost": cost.to_numpy(),
        "gross": gross.to_numpy(),
        # NaN wherever no position applied, so a bar the rule had no view on
        # cannot enter the statistics as a zero-return day.
        "net": (gross - cost).where(exposure.notna()).to_numpy(),
    })


# ----------------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Performance:
    """What a stream of daily returns did, with the precision it can support."""

    days: int
    periods: int
    cagr: float | None
    annual_volatility: float | None
    sharpe: float | None
    sharpe_standard_error: float | None
    t_stat: float | None
    max_drawdown: float | None
    hit_rate: float | None
    # How many holding periods the hit rate was computed over. A rate without
    # its denominator is not a statistic: carry flips direction so rarely that
    # an instrument can have two stretches, and "0%" on a sample of two reads
    # exactly like "0%" on a sample of two hundred.
    hit_periods_counted: int = 0
    annual_turnover: float | None = None
    annual_cost_drag: float | None = None
    exposure_fraction: float | None = None

    @property
    def clears_hurdle(self) -> bool:
        return self.t_stat is not None and abs(self.t_stat) >= SIGNIFICANCE_HURDLE_T

    @property
    def clears_conventional(self) -> bool:
        return self.t_stat is not None and abs(self.t_stat) >= 2.0

    @property
    def inferable(self) -> bool:
        """Whether the sample is long enough for the statistics to mean anything."""
        return self.periods >= MIN_PERIODS_FOR_INFERENCE


def _max_drawdown(net: pd.Series) -> float | None:
    """Worst peak-to-trough fall of the compounded curve, as a negative fraction."""
    clean = pd.to_numeric(net, errors="coerce").dropna()
    if clean.empty:
        return None
    equity = (1.0 + clean).cumprod()
    peak = equity.cummax()
    drawdown = equity / peak - 1.0
    return float(drawdown.min())


def _numeric_column(rows: pd.DataFrame, column: str) -> pd.Series:
    """One column as floats, or an empty series when it is not there at all."""
    if rows is None or not len(rows) or column not in getattr(rows, "columns", ()):
        return pd.Series(dtype=float)
    return pd.to_numeric(rows[column], errors="coerce")


def measure(
    rows: pd.DataFrame,
    column: str = "net",
    periods: int = 0,
    hit_periods: bool = True,
) -> Performance:
    """Summarize a return stream, refusing to compute what the data cannot support.

    Every ratio here is None rather than zero when its inputs are absent. A
    Sharpe of 0.0 reported for a stream with no variance reads as "no edge
    found" when the truth is "nothing was measured", and those must not look
    alike in a results table.

    ``hit_periods`` is off for a buy-and-hold baseline, whose exposure never
    changes: grouping it into holding periods yields one period, and a hit rate
    of 100% or 0% on a sample of one is not a statistic.
    """
    series = _numeric_column(rows, column).dropna()
    days = int(len(series))
    exposure = _numeric_column(rows, "exposure")
    exposure_fraction = (
        float((exposure.notna() & (exposure != 0)).mean())
        if len(exposure) and exposure.notna().any() else None
    )

    costs = _numeric_column(rows, "cost").dropna()
    moves = _numeric_column(rows, "turnover").dropna()
    per_year = TRADING_DAYS_PER_YEAR / days if days else None
    cost_drag = float(costs.sum()) * per_year if days and len(costs) else None
    turnover = float(moves.sum()) * per_year if days and len(moves) else None

    if days < 2:
        return Performance(
            days=days, periods=periods, cagr=None, annual_volatility=None,
            sharpe=None, sharpe_standard_error=None, t_stat=None,
            max_drawdown=_max_drawdown(series), hit_rate=None,
            annual_turnover=turnover, annual_cost_drag=cost_drag,
            exposure_fraction=exposure_fraction,
        )

    mean, std = float(series.mean()), float(series.std(ddof=1))
    total = float((1.0 + series).prod())
    cagr = (total ** (TRADING_DAYS_PER_YEAR / days) - 1.0) if total > 0 else None
    annual_vol = std * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else None
    sharpe = (mean / std) * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else None
    # Lo (2002): the standard error of an estimated Sharpe grows with the Sharpe
    # itself, so a high estimate is not a more certain one.
    se = (math.sqrt((1.0 + 0.5 * sharpe ** 2) / days) * math.sqrt(TRADING_DAYS_PER_YEAR)
          if sharpe is not None else None)
    t_stat = (mean / (std / math.sqrt(days))) if std > 0 else None

    # Hit rate is per holding period, not per day. Daily hit rate on a
    # trend-following rule is near a coin flip by construction and says nothing.
    hit_rate, hit_counted = (
        _period_hit_rate(rows, column) if (periods and hit_periods) else (None, 0))

    return Performance(
        days=days, periods=periods, cagr=cagr, annual_volatility=annual_vol,
        sharpe=sharpe, sharpe_standard_error=se, t_stat=t_stat,
        max_drawdown=_max_drawdown(series), hit_rate=hit_rate,
        hit_periods_counted=hit_counted,
        annual_turnover=turnover, annual_cost_drag=cost_drag,
        exposure_fraction=exposure_fraction,
    )


def _period_hit_rate(rows: pd.DataFrame, column: str) -> tuple[float | None, int]:
    """Fraction of held stretches that made money, compounded within each stretch.

    A stretch is a run of bars with one unchanged exposure -- a holding period.
    Flat stretches are excluded: a period with no position cannot be a hit or a
    miss, and counting its zero return as a win would reward doing nothing.
    """
    exposure = _numeric_column(rows, "exposure")
    values = _numeric_column(rows, column)
    if exposure.empty or values.empty:
        return None, 0
    # A change of exposure opens a new stretch. Compared on the filled values so
    # a run of unset bars is one stretch rather than one per bar (NaN != NaN).
    filled = exposure.fillna(0.0)
    stretches = (filled != filled.shift(1)).cumsum()
    results = []
    for _, chunk in pd.DataFrame({"e": filled, "v": values}).groupby(stretches.to_numpy()):
        if (chunk["e"] == 0).all():
            continue
        clean = chunk["v"].dropna()
        if clean.empty:
            continue
        results.append(float((1.0 + clean).prod()) - 1.0)
    if not results:
        return None, 0
    return sum(r > 0 for r in results) / len(results), len(results)


# ----------------------------------------------------------------------------
# Per-instrument and pooled results
# ----------------------------------------------------------------------------


@dataclass
class InstrumentResult:
    ticker: str
    signal: str
    rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    readings: list[Reading] = field(default_factory=list)
    net: Performance | None = None
    gross: Performance | None = None
    always_long: Performance | None = None
    first_date: str = ""
    last_date: str = ""
    cost_bps: float = 0.0
    error: str | None = None

    @property
    def warmup_unavailable(self) -> int:
        """Leading readings the rule could not form, which is expected, not a fault."""
        count = 0
        for reading in self.readings:
            if reading.direction is not Direction.UNAVAILABLE:
                break
            count += 1
        return count

    @property
    def mid_sample_unavailable(self) -> int:
        """Readings the rule could not form after it had already formed one.

        Distinguished from the warm-up block because they mean different things.
        A twelve-month rule has no reading in its first twelve months by
        definition; a hole in the middle is missing data, and a backtest run
        only on the dates where the data happened to be there is not a backtest.
        """
        tail = self.readings[self.warmup_unavailable:]
        return sum(1 for r in tail if r.direction is Direction.UNAVAILABLE)

    @property
    def directional_readings(self) -> int:
        return sum(1 for r in self.readings if r.direction in (Direction.LONG, Direction.SHORT))

    @property
    def why_nothing_was_measured(self) -> str | None:
        """Why this instrument produced no return stream, when it produced none.

        An instrument whose rate series 404s and one whose rule genuinely never
        formed a view both end up with an empty frame and a row of dashes. The
        readings know the difference and said so at the time, so the reason is
        carried out here rather than left in a list nobody prints.
        """
        if self.error:
            return self.error
        if self.net is not None and self.net.days:
            return None
        reasons = [r.unavailable_reason for r in self.readings if r.unavailable_reason]
        if not reasons:
            return "the rule formed no position on any rebalance date"
        # The last one, not the first: the first is usually "warming up", which
        # is expected and not the reason the whole instrument came back empty.
        return reasons[-1]


def backtest_instrument(
    ticker: str,
    signal_name: str,
    frame: pd.DataFrame | None = None,
    every_n_days: int = REBALANCE_EVERY_N_DAYS,
    lag: int = EXECUTION_LAG_DAYS,
    cost_bps: float | None = None,
    years: int = HISTORY_YEARS,
) -> InstrumentResult:
    """Walk one signal over one instrument's history and measure what it did."""
    if signal_name in NOT_BACKTESTABLE:
        return InstrumentResult(ticker=ticker, signal=signal_name,
                                error=NOT_BACKTESTABLE[signal_name])
    reader = READERS.get(signal_name)
    if reader is None:
        return InstrumentResult(ticker=ticker, signal=signal_name,
                                error=f"no reader registered for {signal_name}")

    if frame is None:
        try:
            frame = load_history(ticker, years)
        except Exception as exc:  # noqa: BLE001 -- one dead symbol must not end the sweep
            return InstrumentResult(ticker=ticker, signal=signal_name,
                                    error=f"{type(exc).__name__}: {exc}")
    if frame is None or frame.empty or len(frame) < 2:
        return InstrumentResult(ticker=ticker, signal=signal_name,
                                error="no usable price history")

    cost = cost_bps_for(ticker) if cost_bps is None else float(cost_bps)
    readings = walk(frame, reader, ticker, every_n_days)
    exposure = exposure_series(frame, readings, lag)
    rows = returns_frame(frame, exposure, cost)
    held = rows[rows["exposure"].notna()]
    periods = sum(1 for r in readings if r.direction is not Direction.UNAVAILABLE)

    result = InstrumentResult(
        ticker=ticker, signal=signal_name, rows=rows, readings=readings,
        net=measure(held, "net", periods), gross=measure(held, "gross", periods),
        cost_bps=cost,
        first_date=str(frame["Date"].iloc[0].date()),
        last_date=str(frame["Date"].iloc[-1].date()),
    )
    # Always-long over the same bars: the baseline a directional rule has to
    # beat before it has earned the complexity. Measured on the same window so
    # the comparison is not a comparison of two different decades.
    if len(held):
        long_rows = held.assign(
            exposure=1.0, net=held["asset"], cost=0.0, turnover=0.0,
        )
        result.always_long = measure(long_rows, "net", periods, hit_periods=False)
    return result


@dataclass
class PooledResult:
    signal: str
    instruments: list[InstrumentResult] = field(default_factory=list)
    net: Performance | None = None
    gross: Performance | None = None
    always_long: Performance | None = None

    @property
    def usable(self) -> list[InstrumentResult]:
        return [i for i in self.instruments if i.error is None and i.net is not None]

    @property
    def positive_sharpe(self) -> int:
        return sum(1 for i in self.usable if (i.net.sharpe or 0) > 0)


def pool(results: list[InstrumentResult], signal_name: str) -> PooledResult:
    """Equal-weight the instruments' daily net returns into one book.

    This is the closest analogue of the paper's claim, which was about a
    diversified portfolio across many contracts and not about any single one.
    Weighting is equal across whichever instruments have a position on a given
    day, so the early years -- when only some series exist -- are less
    diversified than the late ones. That is a real property of our data and is
    reported rather than smoothed over by truncating to a common start.
    """
    pooled = PooledResult(signal=signal_name, instruments=list(results))
    usable = pooled.usable
    if not usable:
        return pooled

    def stack(column: str) -> pd.DataFrame:
        series = {}
        for item in usable:
            rows = item.rows
            held = rows[rows["exposure"].notna()]
            if held.empty:
                continue
            series[item.ticker] = pd.Series(
                pd.to_numeric(held[column], errors="coerce").values,
                index=pd.to_datetime(held["date"].values),
            )
        return pd.DataFrame(series) if series else pd.DataFrame()

    nets = stack("net")
    if nets.empty:
        return pooled
    grosses, assets = stack("gross"), stack("asset")
    periods = sum(i.net.periods for i in usable)

    # Equal weight across whichever instruments had a position that day, which
    # is what ``mean`` over a frame with holes does. Turnover and cost are
    # averaged from the instruments' own columns rather than rederived from the
    # book's average exposure, which would net offsetting flips to nothing.
    frame = pd.DataFrame({
        "net": nets.mean(axis=1),
        "gross": grosses.mean(axis=1) if not grosses.empty else nets.mean(axis=1),
        # Absolute exposure, so a day on which two instruments hold opposite
        # positions reads as a day the book was invested, not a day it was flat.
        "exposure": stack("exposure").abs().mean(axis=1),
        "turnover": stack("turnover").mean(axis=1),
        "cost": stack("cost").mean(axis=1),
    })
    # Holding periods are per instrument, so the book has no single stretch
    # structure to compute a hit rate over; the per-instrument rates carry that.
    pooled.net = measure(frame, "net", periods, hit_periods=False)
    pooled.gross = measure(frame, "gross", periods, hit_periods=False)
    if not assets.empty:
        pooled.always_long = measure(
            pd.DataFrame({"net": assets.mean(axis=1), "exposure": 1.0}),
            "net", periods, hit_periods=False,
        )
    return pooled


def run(
    signal_name: str,
    tickers: tuple[str, ...] = OUR_INSTRUMENTS,
    every_n_days: int = REBALANCE_EVERY_N_DAYS,
    lag: int = EXECUTION_LAG_DAYS,
    years: int = HISTORY_YEARS,
    frames: dict[str, pd.DataFrame] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> PooledResult:
    """Backtest one signal across the instruments it applies to.

    Instruments the signal does not claim are skipped silently -- carry has
    nothing to say about gold, and recording that as a failure would make the
    table unreadable.
    """
    from tradingagents.signals.registry import REGISTRY

    signal = next((s for s in REGISTRY if s.name == signal_name), None)
    applicable = [t for t in tickers
                  if signal is None or signal.applies_to(instrument_kind(t))]
    results = []
    for index, ticker in enumerate(applicable, 1):
        if progress:
            progress(index, len(applicable), ticker)
        results.append(backtest_instrument(
            ticker, signal_name,
            frame=(frames or {}).get(ticker),
            every_n_days=every_n_days, lag=lag, years=years,
        ))
    return pool(results, signal_name)
