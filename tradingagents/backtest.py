"""Run the graph over a grid of tickers and dates, and score what came back.

One run yields one decision, so it cannot say whether the system decides well.
This runs the same machinery over many (ticker, date) cells and reads the
aggregate. The memory log is the results table: every run already records its
rating and later settles it with realized and alpha return against the
instrument's regional benchmark, so there is nothing to record separately.

Scope: this evaluates decision quality. It is not a portfolio simulator, and
must not grow one. Turning a rating into a filled order needs a quantity, a fill
price and a cash ledger, none of which the system has; inventing them here would
put an execution model behind an evaluation tool. Cells are therefore
independent, and a portfolio, when given, is the same standing book for every
cell rather than a position carried forward.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradingagents.agents.rating import RATING_REVIEW
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.memory import TradingMemoryLog

logger = logging.getLogger(__name__)


def iter_grid(start_date: str, end_date: str, every_n_days: int = 1) -> list[str]:
    """Analysis dates from ``start_date``, never past today.

    A future date has no outcome to settle against, and the graph rejects one, so
    the grid stops at the present rather than producing cells that cannot score.
    """
    start, end = _canonical(start_date), _canonical(end_date)
    if every_n_days < 1:
        raise ValueError("every_n_days must be at least 1")
    if end < start:
        raise ValueError(f"the grid ends before it starts: {end_date} is before {start_date}")

    last = min(end, datetime.strptime(get_current_date(), "%Y-%m-%d"))
    dates, cursor = [], start
    while cursor <= last:
        dates.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=every_n_days)
    return dates


def _canonical(date: str) -> datetime:
    """Parse a grid bound, rejecting anything the run date would also reject."""
    try:
        parsed = datetime.strptime(str(date), "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"grid dates must be in YYYY-MM-DD format, got {date!r}") from exc
    if parsed.strftime("%Y-%m-%d") != str(date):
        raise ValueError(f"grid dates must be in YYYY-MM-DD format, got {date!r}")
    return parsed


def _alpha(entry: dict) -> float | None:
    """Alpha return of a settled entry, or None when it has not settled.

    The log stores it as a percentage rounded to one decimal, so aggregates here
    are accurate to 0.1 of a percentage point, not to the raw quote.
    """
    text = (entry.get("alpha") or "").strip().rstrip("%")
    try:
        return float(text) / 100
    except ValueError:
        return None


@dataclass
class BacktestResult:
    run_id: str
    log_path: Path
    cells_run: int = 0
    skipped: int = 0
    failures: list[tuple[str, str, str]] = field(default_factory=list)
    settlement_failures: list[tuple[str, str]] = field(default_factory=list)


# What each rating claims will happen, so an outcome can be scored against it.
# Hold claims no direction, so nothing about alpha proves it right or wrong.
_DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


@dataclass
class RatingScore:
    count: int
    hit_rate: float | None
    mean_alpha: float


@dataclass
class BacktestSummary:
    resolved: int
    pending: int
    by_rating: dict[str, RatingScore]
    unscored: int = 0
    holding: str = ""

    def render(self) -> str:
        lines = [f"Resolved cells: {self.resolved} · pending: {self.pending}"
                 + (f" · unscored: {self.unscored}" if self.unscored else "")]
        for rating, score in self.by_rating.items():
            called = (f"called the direction {score.hit_rate:.0%}"
                      if score.hit_rate is not None else "no direction claimed")
            lines.append(
                f"- {rating}: n={score.count}, {called}, "
                f"mean alpha {score.mean_alpha:+.2%} vs the benchmark"
            )
        lines.append("")
        if self.pending:
            lines.append("Pending cells are not scored above; re-run to settle them.")
        lines.append(
            f"Alpha is measured over {self.holding} after each analysis date. "
            "One model sampling per cell, and text feeds are not archived, so "
            "these figures are indicative rather than repeatable."
        )
        return "\n".join(lines)


def run_backtest(
    tickers: list[str],
    dates: list[str],
    config: dict,
    asset_type: str = "stock",
    portfolio=None,
    selected_analysts=("market", "social", "news", "fundamentals"),
    run_id: str | None = None,
    progress: Callable[[int, int, str, str], None] | None = None,
) -> BacktestResult:
    """Analyze every ticker on every date, into a memory log of this run's own.

    The live log stays untouched: a sweep would otherwise flood the context that
    real runs read back. Cells already in this run's log are skipped, so an
    interrupted sweep resumes by being run again. ``progress(done, total,
    ticker, date)`` is called before each cell that runs.
    """
    # run_id becomes a path segment, so it is validated like a ticker: an
    # absolute or dotted value would otherwise place the run outside results_dir.
    run_id = safe_ticker_component(run_id or datetime.now().strftime("%Y%m%d_%H%M%S"))
    run_dir = Path(config["results_dir"]) / "backtest" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_config = {**config, "results_dir": str(run_dir),
                  "memory_log_path": str(run_dir / "trading_memory.md")}

    graph = TradingAgentsGraph(selected_analysts, config=run_config)
    result = BacktestResult(run_id=run_id, log_path=Path(run_config["memory_log_path"]))
    done = {(e["ticker"], e["date"]) for e in graph.memory_log.load_entries()}

    # A ticker or date given twice is one cell, run and settled once.
    tickers, dates = list(dict.fromkeys(tickers)), list(dict.fromkeys(dates))
    cells = [(ticker, date) for ticker in tickers for date in dates]
    todo = [cell for cell in cells if cell not in done]
    result.skipped = len(cells) - len(todo)
    for index, (ticker, date) in enumerate(todo, 1):
        if progress:
            progress(index, len(todo), ticker, date)
        try:
            graph.propagate(ticker, date, asset_type, portfolio=portfolio)
            result.cells_run += 1
        except Exception as exc:  # one unreachable vendor must not end the sweep
            logger.warning("Backtest cell %s %s failed: %s", ticker, date, exc)
            result.failures.append((ticker, date, str(exc)))

    # Settlement runs at the start of the next run for a ticker, so each ticker's
    # last cell would stay pending without this pass.
    for ticker in tickers:
        try:
            graph.settle_pending(ticker)
        except Exception as exc:  # reflection calls an LLM; one failure is not the sweep's
            logger.warning("Settling %s failed: %s", ticker, exc)
            result.settlement_failures.append((ticker, str(exc)))
    return result


def summarize(source: BacktestResult | str | Path) -> BacktestSummary:
    """Score the settled decisions of a backtest, or of a memory log at a path, by rating."""
    if isinstance(source, BacktestResult):
        path = source.log_path      # a run whose cells all failed wrote no log: nothing to score
    elif Path(source).is_file():
        path = Path(source)
    else:
        raise FileNotFoundError(f"no memory log at {source}")
    entries = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()
    # A decision with no readable rating has no direction, so it can neither
    # count for nor against the system; it is reported as unscored instead.
    resolved = [(e, _alpha(e)) for e in entries
                if not e["pending"] and e["rating"] != RATING_REVIEW]
    resolved = [(e, a) for e, a in resolved if a is not None]
    by_rating: dict[str, RatingScore] = {}
    for rating in dict.fromkeys(e["rating"] for e, _ in resolved):
        alphas = [a for e, a in resolved if e["rating"] == rating]
        direction = _DIRECTION.get(rating, 0)
        by_rating[rating] = RatingScore(
            count=len(alphas),
            hit_rate=(sum(a * direction > 0 for a in alphas) / len(alphas)) if direction else None,
            mean_alpha=sum(alphas) / len(alphas),
        )
    unscored = sum(1 for e in entries if e["rating"] == RATING_REVIEW)
    # Report the window the outcomes were actually measured over, from the log.
    windows = {f"{e['holding'][:-1]} trading days" for e, _ in resolved
               if (e.get("holding") or "").endswith("d")}
    return BacktestSummary(resolved=len(resolved),
                           pending=len(entries) - len(resolved) - unscored,
                           by_rating=by_rating, unscored=unscored,
                           holding=", ".join(sorted(windows)) or "the configured window")


# ---------------------------------------------------------------------------
# Scoring the instruction, not just the rating
# ---------------------------------------------------------------------------
#
# Everything above scores a RATING: buy at the decision date's close, hold a
# fixed window, compare to a benchmark. That is not what the system tells you to
# do. It issues an entry, a stop and a size, and all three change the outcome --
# a limit that never fills, a stop that realises a loss the horizon would have
# recovered, a size that turns a price move into a portfolio number.
#
# The three variants below are scored over the same decisions so the difference
# between them is attributable:
#
#   as_specified  limit entry, stop honoured     -- what the system actually said
#   market_entry  enter at the open, same stop   -- isolates the entry timing
#   rating_only   enter at the open, no stop     -- isolates the stop
#
# All three report in R, the trade's own risk unit, so they are comparable to
# each other and across instruments.

PLAN_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("as_specified", {}),
    ("market_entry", {"market_entry": True}),
    ("rating_only", {"market_entry": True, "use_stop": False}),
)


def forward_bars(ticker: str, decision_date: str, lookahead_days: int = 120):
    """OHLC bars strictly AFTER ``decision_date``, oldest first.

    Looking forward is correct here and nowhere else in this codebase: this is
    settlement, not signal formation. The decision is already fixed; the only
    question is what the price then did to it.

    Raw bars, not gap-filled: a carried-forward bar has no true high or low, and
    feeding one to a stop check would either invent a trigger or hide one.
    """
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    frame = load_ohlcv(ticker, get_current_date(), fill_gaps=False)
    if frame is None or "Date" not in getattr(frame, "columns", ()):
        return None
    cutoff = pd.Timestamp(decision_date).normalize()
    after = frame[pd.to_datetime(frame["Date"]) > cutoff]
    return after.head(int(lookahead_days)).reset_index(drop=True)


@dataclass
class PlanScore:
    """What one variant did across every decision that could be scored."""

    variant: str
    r_multiples: list[float] = field(default_factory=list)
    portfolio_returns: list[float] = field(default_factory=list)
    stopped: int = 0
    horizon_exits: int = 0
    no_fills: int = 0
    unmeasured: int = 0

    @property
    def trades(self) -> int:
        return len(self.r_multiples)

    @property
    def fill_rate(self) -> float | None:
        attempted = self.trades + self.no_fills
        return self.trades / attempted if attempted else None

    @property
    def expectancy_r(self) -> float | None:
        """Mean R per trade taken. The number that decides whether this pays."""
        return sum(self.r_multiples) / self.trades if self.trades else None

    @property
    def win_rate(self) -> float | None:
        if not self.trades:
            return None
        return sum(1 for r in self.r_multiples if r > 0) / self.trades

    @property
    def total_r(self) -> float:
        return sum(self.r_multiples)

    @property
    def t_stat(self) -> float | None:
        """Whether the expectancy is distinguishable from zero.

        On a handful of trades this is near-meaningless, which is the point of
        reporting it rather than the expectancy alone.
        """
        if self.trades < 2:
            return None
        mean = self.expectancy_r
        spread = pd.Series(self.r_multiples).std(ddof=1)
        if not spread or not math.isfinite(spread) or spread <= 0:
            return None
        return float(mean / (spread / math.sqrt(self.trades)))

    @property
    def portfolio_total(self) -> float | None:
        """Compounded portfolio return, when sizes were stated."""
        if not self.portfolio_returns:
            return None
        total = 1.0
        for r in self.portfolio_returns:
            total *= (1.0 + r)
        return total - 1.0


@dataclass
class PlanSummary:
    by_variant: dict[str, PlanScore] = field(default_factory=dict)
    decisions: int = 0
    holds: int = 0
    incomplete: int = 0
    invalid: list[tuple[str, str, str]] = field(default_factory=list)
    horizon_days: int = 0

    def render(self) -> str:
        lines = [
            f"Decisions read: {self.decisions} · holds (no instruction): {self.holds} "
            f"· incomplete instructions: {self.incomplete} · invalid: {len(self.invalid)}",
            "",
        ]
        if self.incomplete:
            lines.append(
                f"{self.incomplete} decision(s) stated a side but not a usable entry and "
                f"stop, so they cannot be scored as trades at all. A system that cannot "
                f"state its own levels cannot be executed, whatever its ratings do."
            )
            lines.append("")
        header = (f"  {'variant':<14} {'trades':>7} {'fills':>7} {'expectancy':>11} "
                  f"{'win':>6} {'totalR':>8} {'t':>6} {'stopped':>8}")
        lines.append(header)
        for name, score in self.by_variant.items():
            exp = ("—" if score.expectancy_r is None
                   else f"{score.expectancy_r:+.3f}R")
            lines.append(
                f"  {name:<14} {score.trades:>7} "
                f"{('—' if score.fill_rate is None else f'{score.fill_rate:.0%}'):>7} "
                f"{exp:>11} "
                f"{('—' if score.win_rate is None else f'{score.win_rate:.0%}'):>6} "
                f"{score.total_r:>+8.2f} "
                f"{('—' if score.t_stat is None else f'{score.t_stat:.2f}'):>6} "
                f"{score.stopped:>8}"
            )
        lines.append("")
        lines.append(
            f"Expectancy is mean R per trade taken; one R is the distance from the "
            f"decision's own entry to its own stop, so a stopped trade is -1R. "
            f"Horizon is {self.horizon_days} trading days."
        )
        specified = self.by_variant.get("as_specified")
        rating_only = self.by_variant.get("rating_only")
        if specified and rating_only and None not in (specified.expectancy_r,
                                                      rating_only.expectancy_r):
            better = specified.expectancy_r > rating_only.expectancy_r
            lines.append("")
            lines.append(
                f"The trade craft {'ADDED' if better else 'COST'} "
                f"{abs(specified.expectancy_r - rating_only.expectancy_r):.3f}R per trade "
                f"against simply taking the rating at the open with no stop "
                f"({specified.expectancy_r:+.3f}R vs {rating_only.expectancy_r:+.3f}R)."
            )
        for date, ticker, reason in self.invalid[:5]:
            lines.append(f"  invalid: {ticker} {date} — {reason}")
        return "\n".join(lines)


def score_log_plans(
    source,
    horizon: int = None,
    fill_window: int = None,
    bars_for=None,
) -> PlanSummary:
    """Score every decision in a memory log as the trade instruction it issued.

    ``bars_for(ticker, date)`` is injectable so the whole aggregation is testable
    on constructed price paths with no vendor in the picture.
    """
    from tradingagents.trade_plan import (
        DEFAULT_FILL_WINDOW_DAYS,
        DEFAULT_HORIZON_DAYS,
        NO_FILL,
        STOPPED,
        parse_plan,
        score_plan,
    )

    horizon = DEFAULT_HORIZON_DAYS if horizon is None else horizon
    fill_window = DEFAULT_FILL_WINDOW_DAYS if fill_window is None else fill_window
    bars_for = bars_for or (lambda t, d: forward_bars(t, d, horizon + fill_window + 40))

    path = source.log_path if isinstance(source, BacktestResult) else Path(source)
    entries = TradingMemoryLog({"memory_log_path": str(path)}).load_entries()
    summary = PlanSummary(
        by_variant={name: PlanScore(variant=name) for name, _ in PLAN_VARIANTS},
        decisions=len(entries), horizon_days=horizon,
    )

    for entry in entries:
        plan = parse_plan(entry.get("decision", ""), entry.get("rating"))
        if plan.direction == 0 and plan.invalid_reason is None:
            summary.holds += 1
            continue
        if plan.invalid_reason:
            summary.invalid.append((entry["date"], entry["ticker"], plan.invalid_reason))
            continue
        if not plan.executable:
            summary.incomplete += 1
            continue

        bars = bars_for(entry["ticker"], entry["date"])
        for name, options in PLAN_VARIANTS:
            score = summary.by_variant[name]
            outcome = score_plan(plan, bars, fill_window=fill_window,
                                 horizon=horizon, **options)
            if not outcome.scored:
                score.unmeasured += 1
            elif outcome.outcome == NO_FILL:
                score.no_fills += 1
            else:
                score.r_multiples.append(outcome.r_multiple)
                if outcome.portfolio_return is not None:
                    score.portfolio_returns.append(outcome.portfolio_return)
                if outcome.outcome == STOPPED:
                    score.stopped += 1
                else:
                    score.horizon_exits += 1
    return summary
