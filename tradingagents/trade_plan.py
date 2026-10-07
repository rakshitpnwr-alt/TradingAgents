"""The executable instruction, read back out of a decision, and scored.

Why this exists
---------------
Settlement scores a *rating*: it buys at the decision date's close, holds for a
fixed number of days, and compares the return to a benchmark. That is not what
the system tells you to do. A live EURUSD run produced "enter at 1.13, stop at
1.153, risk 1.2-1.5% of portfolio, scale in on a bounce rather than chasing the
low" -- a limit entry, a stop, and a size. Three things the rating-based score
knows nothing about, and each of them changes the outcome:

  - A limit entry may never fill. A plan that never executes is not a flat
    trade, it is an absent one, and counting it as zero return silently credits
    the system for discipline it was not tested on.
  - A stop converts a drawdown into a realised loss. Trend-following into a
    mean-reverting bounce is precisely where a stop turns a winning holding
    period into a losing trade, so a stopless score flatters any trend rule.
  - Size turns a price move into a portfolio outcome, and the system states it
    as risk rather than notional.

So the measured 0.55 Sharpe of the momentum rule does not describe the agents'
behaviour. This module closes that gap: it scores the instruction as issued.

The unit is R
-------------
A stop-based trade is naturally measured in multiples of its own risk. One R is
the distance from entry to stop; a stopped-out trade is exactly -1R. R is
comparable across instruments and across position sizes, and it does not depend
on parsing a size out of free text -- which is why it is the headline here and
portfolio return is secondary.

What is assumed, and which way each assumption leans
----------------------------------------------------
Every one of these is chosen to be pessimistic, because the purpose is to find
out whether there is an edge, not to produce a flattering number.

  - Trading starts the bar AFTER the decision date. The decision is formed from
    that day's close, so it cannot also be executed at it.
  - A gap through the entry fills at the open, which is a better price than the
    limit. That one favours the trade and is unavoidable: it is what would
    really have happened.
  - A gap through the stop exits at the open, which is worse than the stop. Also
    what would really have happened.
  - If the fill bar's own range also reaches the stop, the stop is assumed hit.
    Within one daily bar the order of the high and the low is unknown, so the
    losing order is assumed.
  - No slippage or commission beyond that. This module measures decision
    quality; instrument-level costs belong to whatever executes it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import pandas as pd

# Long for the bullish half of the scale, short for the bearish half. Hold is
# not a trade and is scored as one only by a system that wants more rows.
_DIRECTION_BY_RATING = {
    "buy": 1, "overweight": 1, "hold": 0, "underweight": -1, "sell": -1,
}

# The render in ``schemas.render_pm_decision`` is deterministic, so these read a
# fixed shape rather than guessing at prose. ``not provided`` is part of that
# contract: it distinguishes a level the decision declined to give from one
# nobody asked for.
_NUMBER = r"([-+]?\d[\d,]*\.?\d*)"


def _labelled(label: str) -> re.Pattern:
    return re.compile(rf"\*\*{label}\*\*\s*:\s*\**\s*{_NUMBER}", re.IGNORECASE)


_ENTRY_RE = _labelled("Entry Price")
_STOP_RE = _labelled("Stop Loss")
_RISK_RE = _labelled("Risk Percent")


def _read_number(pattern: re.Pattern, text: str) -> float | None:
    match = pattern.search(text or "")
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class TradePlan:
    """What a decision actually instructs, and what it failed to state.

    ``missing`` is the point of the type. A plan without a stop and a plan with
    a stop are different trades, and a scorer that silently defaults the absent
    one has measured a strategy nobody proposed.
    """

    direction: int
    entry: float | None = None
    stop: float | None = None
    risk_fraction: float | None = None
    missing: tuple[str, ...] = ()
    invalid_reason: str | None = None

    @property
    def is_trade(self) -> bool:
        return self.direction != 0 and self.invalid_reason is None

    @property
    def executable(self) -> bool:
        """Whether this is a complete instruction: a side, a level and a stop."""
        return self.is_trade and self.entry is not None and self.stop is not None

    @property
    def risk_per_unit(self) -> float | None:
        """Distance from entry to stop, in price. One R."""
        if self.entry is None or self.stop is None:
            return None
        return abs(self.entry - self.stop)


def parse_plan(decision_text: str, rating: str | None = None) -> TradePlan:
    """Read the executable instruction out of a rendered decision.

    ``rating`` gives the side; without one it is read from the text. A rating
    that cannot be read is not a Hold -- it is a decision nobody can act on, so
    it comes back as invalid rather than as a flat position.
    """
    from tradingagents.agents.rating import extract_rating

    label = (rating or extract_rating(decision_text) or "").strip().lower()
    if label not in _DIRECTION_BY_RATING:
        return TradePlan(direction=0, invalid_reason=f"no readable rating ({rating!r})")

    direction = _DIRECTION_BY_RATING[label]
    entry = _read_number(_ENTRY_RE, decision_text)
    stop = _read_number(_STOP_RE, decision_text)
    risk = _read_number(_RISK_RE, decision_text)

    if direction == 0:
        # A Hold instructs nothing, so absent levels are correct, not missing.
        return TradePlan(direction=0)

    missing = tuple(
        name for name, value in (("entry", entry), ("stop", stop), ("risk_percent", risk))
        if value is None
    )
    plan = TradePlan(
        direction=direction, entry=entry, stop=stop,
        risk_fraction=(risk / 100.0 if risk is not None else None),
        missing=missing,
    )
    return _validated(plan)


def _validated(plan: TradePlan) -> TradePlan:
    """Reject a plan whose own numbers contradict it.

    A stop on the wrong side of the entry is not a conservative trade with an
    odd shape; it is an instruction to close at a profit and hold a loss
    forever. Scoring it would produce a number, and the number would be
    meaningless.
    """
    entry, stop = plan.entry, plan.stop
    if entry is not None and entry <= 0:
        return _invalid(plan, f"entry price {entry} is not a positive price")
    if stop is not None and stop <= 0:
        return _invalid(plan, f"stop {stop} is not a positive price")
    if entry is not None and stop is not None:
        if entry == stop:
            return _invalid(plan, "entry and stop are the same price, so the trade risks nothing")
        wrong_side = (plan.direction > 0 and stop > entry) or (plan.direction < 0 and stop < entry)
        if wrong_side:
            side = "long" if plan.direction > 0 else "short"
            return _invalid(
                plan,
                f"stop {stop} is on the wrong side of entry {entry} for a {side}",
            )
    if plan.risk_fraction is not None and not 0 < plan.risk_fraction <= 1:
        return _invalid(plan, f"risk of {plan.risk_fraction * 100:.2f}% of portfolio is not usable")
    return plan


def _invalid(plan: TradePlan, reason: str) -> TradePlan:
    return TradePlan(direction=plan.direction, entry=plan.entry, stop=plan.stop,
                     risk_fraction=plan.risk_fraction, missing=plan.missing,
                     invalid_reason=reason)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# Trading days a limit entry stays live before the plan is abandoned. The
# decisions are formed on a roughly monthly cadence, so a level that has not
# been reached within a week is a level the market moved away from.
DEFAULT_FILL_WINDOW_DAYS = 5

# Trading days a filled position is held before being closed at the market, when
# no stop intervenes. The agents state a horizon in prose ("3-6 months") that is
# not reliably a number, so this is ours and is reported as such.
DEFAULT_HORIZON_DAYS = 21

# A plan the market never came back for. A real answer about the trade.
NO_FILL = "no_fill"
STOPPED = "stopped"
HORIZON = "horizon"
# Could not be measured at all: an incomplete instruction, missing bars, or a
# holding period still running. Deliberately NOT ``NO_FILL``: one is a finding
# about the market, the other is the absence of a finding, and the signal layer
# already taught this lesson once with FLAT against UNAVAILABLE.
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TradeOutcome:
    """What happened to one instruction, and why it ended."""

    outcome: str
    r_multiple: float | None = None
    raw_return: float | None = None
    portfolio_return: float | None = None
    fill_price: float | None = None
    fill_date: str | None = None
    exit_price: float | None = None
    exit_date: str | None = None
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if (self.outcome == UNAVAILABLE) != (self.unavailable_reason is not None):
            raise ValueError(
                f"{self.outcome!r}: an unmeasurable outcome must say why, and a "
                f"measured one must not claim to be unavailable"
            )

    @property
    def scored(self) -> bool:
        return self.unavailable_reason is None

    @property
    def filled(self) -> bool:
        return self.outcome in (STOPPED, HORIZON)


def _unavailable(reason: str) -> TradeOutcome:
    return TradeOutcome(outcome=UNAVAILABLE, unavailable_reason=reason)


def score_plan(
    plan: TradePlan,
    bars: pd.DataFrame,
    fill_window: int = DEFAULT_FILL_WINDOW_DAYS,
    horizon: int = DEFAULT_HORIZON_DAYS,
    market_entry: bool = False,
    use_stop: bool = True,
) -> TradeOutcome:
    """Score one instruction against the bars that followed it.

    ``bars`` must be the OHLC bars strictly AFTER the decision date, oldest
    first; the caller slices them, so this stays a pure function testable on a
    constructed sequence where the answer is known by hand.

    ``market_entry`` ignores the stated level and enters at the first bar's
    open instead, keeping the same stop. Comparing the two answers a question
    worth asking on its own: whether the agents' entry timing is worth anything,
    or whether waiting for a better price mostly costs them the trade.

    ``use_stop=False`` holds to the horizon regardless of the stop, which is what
    settlement scores today. The stop is still required and still sets the risk
    unit, so all three variants are measured in the same R -- the only thing that
    changes is whether the stop is honoured. That isolates what the stop itself
    contributes, which for a trend rule entering against an oversold bounce is
    the question that decides whether the stop helps or just realises losses.
    """
    if not plan.is_trade:
        return _unavailable(plan.invalid_reason or "the decision instructs no trade")
    if not plan.executable:
        return _unavailable(
            f"incomplete instruction: no {', '.join(plan.missing) or 'levels'}")
    required = {"Open", "High", "Low", "Close"}
    if bars is None or not required.issubset(getattr(bars, "columns", ())):
        return _unavailable("no OHLC bars to score against")
    bars = bars.dropna(subset=["Open", "High", "Low", "Close"])
    if bars.empty:
        return _unavailable("no OHLC bars to score against")

    long = plan.direction > 0
    fill_index, fill_price = _find_fill(plan, bars, fill_window, long, market_entry)
    if fill_index is None:
        # Not a loss and not a win. A plan the market never came back for is a
        # trade that did not happen, and averaging it in as zero would reward
        # the system for the trades it failed to get into.
        return TradeOutcome(outcome=NO_FILL)

    exit_index, exit_price, reason = _find_exit(
        plan, bars, fill_index, horizon, long, use_stop)
    if exit_index is None:
        return _unavailable(
            f"filled on {_date_of(bars, fill_index)} but the holding period has "
            f"not finished trading yet")

    risk = plan.risk_per_unit
    signed = (exit_price - fill_price) * (1 if long else -1)
    return TradeOutcome(
        outcome=reason,
        r_multiple=signed / risk if risk else None,
        raw_return=signed / fill_price if fill_price else None,
        portfolio_return=((signed / risk) * plan.risk_fraction
                          if risk and plan.risk_fraction is not None else None),
        fill_price=fill_price, fill_date=_date_of(bars, fill_index),
        exit_price=exit_price, exit_date=_date_of(bars, exit_index),
    )


def _find_fill(plan, bars, fill_window, long, market_entry):
    """(bar index, price) where the entry filled, or (None, None).

    A bar that opens through the limit fills at the open, which is a better
    price than asked for. That is what would really have happened, so it is what
    is scored even though it favours the trade.
    """
    if market_entry:
        return 0, float(bars["Open"].iloc[0])
    window = min(int(fill_window), len(bars)) if fill_window else len(bars)
    for i in range(window):
        low, high, open_ = (float(bars["Low"].iloc[i]), float(bars["High"].iloc[i]),
                            float(bars["Open"].iloc[i]))
        if long and low <= plan.entry:
            return i, min(plan.entry, open_)
        if not long and high >= plan.entry:
            return i, max(plan.entry, open_)
    return None, None


def _find_exit(plan, bars, fill_index, horizon, long, use_stop=True):
    """(bar index, price, reason) for the exit, or (None, None, None) if unfinished.

    The stop is checked from the fill bar onward, including the fill bar itself:
    a daily bar does not say whether its high or its low came first, so the
    order that loses money is assumed.
    """
    last = min(fill_index + int(horizon), len(bars) - 1)
    for i in range(fill_index, last + 1) if use_stop else ():
        low, high, open_ = (float(bars["Low"].iloc[i]), float(bars["High"].iloc[i]),
                            float(bars["Open"].iloc[i]))
        if long and low <= plan.stop:
            # Gapped below the stop: you are filled at the open, not the stop.
            return i, min(plan.stop, open_), STOPPED
        if not long and high >= plan.stop:
            return i, max(plan.stop, open_), STOPPED
    if fill_index + int(horizon) > len(bars) - 1:
        return None, None, None      # the holding period has not finished
    return last, float(bars["Close"].iloc[last]), HORIZON


def _date_of(bars: pd.DataFrame, index: int) -> str | None:
    if "Date" in getattr(bars, "columns", ()):
        try:
            return pd.Timestamp(bars["Date"].iloc[index]).strftime("%Y-%m-%d")
        except (ValueError, TypeError, IndexError):
            return None
    return None
