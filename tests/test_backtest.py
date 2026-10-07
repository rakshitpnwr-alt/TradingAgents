"""Backtesting: many single-shot decisions, scored by the memory log.

A run already records its rating and later settles it with realized and alpha
return against the regional benchmark. A backtest is that machinery over a grid
of tickers and dates, aggregated. It evaluates decision quality; it does not
simulate a portfolio, so there is no execution, no fees and no equity curve.
"""

from __future__ import annotations

import pytest

from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.memory import TradingMemoryLog

DECISION = "Rating: Buy\n\nbuy it"


@pytest.mark.unit
def test_grid_spacing_and_canonical_dates():
    assert iter_grid("2026-01-05", "2026-01-20", every_n_days=7) == ["2026-01-05", "2026-01-12", "2026-01-19"]


@pytest.mark.unit
def test_grid_stops_at_today(monkeypatch):
    import tradingagents.backtest as bt

    monkeypatch.setattr(bt, "get_current_date", lambda: "2026-01-10")
    assert iter_grid("2026-01-05", "2026-02-20", every_n_days=5) == ["2026-01-05", "2026-01-10"]


@pytest.mark.unit
def test_grid_rejects_a_non_canonical_date():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        iter_grid("2026-1-5", "2026-01-20")


class _FakeGraph:
    """Stands in for TradingAgentsGraph, writing to the log the harness gave it."""

    instances: list = []
    fail_on: set = set()

    def __init__(self, selected_analysts=None, config=None, **kw):
        self.analysts = list(selected_analysts) if selected_analysts else None
        self.config = config
        self.memory_log = TradingMemoryLog(config)
        self.calls = []
        self.settled = []
        _FakeGraph.instances.append(self)

    def propagate(self, ticker, trade_date, asset_type="stock", portfolio=None):
        self.calls.append((ticker, trade_date))
        if (ticker, trade_date) in _FakeGraph.fail_on:
            raise RuntimeError("vendor exploded")
        self.memory_log.store_decision(ticker, trade_date, DECISION)
        return {"final_trade_decision": DECISION}, "Buy"

    def settle_pending(self, ticker):
        self.settled.append(ticker)


@pytest.fixture(autouse=True)
def _fake_graph(monkeypatch, tmp_path):
    import tradingagents.backtest as bt

    _FakeGraph.instances = []
    _FakeGraph.fail_on = set()
    monkeypatch.setattr(bt, "TradingAgentsGraph", _FakeGraph)
    return _FakeGraph


def _config(tmp_path):
    return {"results_dir": str(tmp_path / "results"),
            "memory_log_path": str(tmp_path / "live_trading_memory.md")}


@pytest.mark.unit
def test_the_live_memory_log_is_never_written(tmp_path):
    config = _config(tmp_path)
    result = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], config)

    assert not (tmp_path / "live_trading_memory.md").exists()
    assert result.log_path.exists() and result.cells_run == 2


@pytest.mark.unit
def test_a_cell_already_in_the_log_is_not_run_again(tmp_path):
    config = _config(tmp_path)
    first = run_backtest(["NVDA"], ["2026-01-05"], config)

    again = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], config, run_id=first.run_id)

    assert again.cells_run == 1 and again.skipped == 1
    assert _FakeGraph.instances[-1].calls == [("NVDA", "2026-01-12")]


@pytest.mark.unit
def test_every_ticker_is_settled_after_the_grid(tmp_path):
    """Settlement runs at the start of the next same-ticker run, so the last
    date of each ticker would stay pending without an explicit pass."""
    run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path))
    assert sorted(_FakeGraph.instances[-1].settled) == ["AAPL", "NVDA"]


@pytest.mark.unit
def test_a_failed_cell_does_not_abort_the_sweep(tmp_path):
    _FakeGraph.fail_on = {("NVDA", "2026-01-05")}
    result = run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _config(tmp_path))

    assert result.cells_run == 1
    assert result.failures == [("NVDA", "2026-01-05", "vendor exploded")]


# --- reading the result ------------------------------------------------------

def _log_with(tmp_path, rows):
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    for ticker, date, decision, outcome in rows:
        log.store_decision(ticker, date, decision)
        if outcome is not None:
            log.update_with_outcome(ticker, date, outcome[0], outcome[1], 5, "note", "2026-02-01")
    return tmp_path / "m.md"


@pytest.mark.unit
def test_summary_scores_resolved_cells_and_keeps_pending_out_of_the_average(tmp_path):
    log = _log_with(tmp_path, [
        ("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04)),
        ("NVDA", "2026-01-12", "Rating: Buy\n\nx", (-0.02, -0.02)),
        ("AAPL", "2026-01-05", "Rating: Sell\n\nx", None),
    ])

    summary = summarize(log)

    assert summary.resolved == 2 and summary.pending == 1
    buys = summary.by_rating["Buy"]
    assert buys.count == 2 and buys.hit_rate == 0.5 and round(buys.mean_alpha, 4) == 0.01
    assert "Sell" not in summary.by_rating  # unsettled: nothing to score yet


@pytest.mark.unit
def test_summary_states_what_it_cannot_prove(tmp_path):
    text = summarize(_log_with(tmp_path, [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))])).render()
    assert "not archived" in text
    assert "one" in text.lower() and "sampl" in text.lower()


@pytest.mark.unit
def test_the_analyst_set_under_test_is_the_one_that_runs(tmp_path):
    """A backtest of a two-analyst setup must not silently run four."""
    run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), selected_analysts=["market", "news"])
    assert _FakeGraph.instances[-1].analysts == ["market", "news"]


@pytest.mark.unit
def test_a_run_id_cannot_escape_the_results_directory(tmp_path):
    """run_id becomes a path segment, so it is validated like a ticker is."""
    with pytest.raises(ValueError):
        run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), run_id="../../escaped")
    with pytest.raises(ValueError):
        run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path), run_id="/etc/cron.d/x")


@pytest.mark.unit
def test_a_failed_settlement_does_not_lose_the_remaining_tickers(tmp_path, monkeypatch):
    """Settlement reflects with an LLM, so it can fail; the sweep still returns
    its result and every other ticker still gets settled."""
    settled = []

    def _settle(self, ticker):
        if ticker == "NVDA":
            raise RuntimeError("reflector timed out")
        settled.append(ticker)

    monkeypatch.setattr(_FakeGraph, "settle_pending", _settle, raising=False)
    result = run_backtest(["NVDA", "AAPL"], ["2026-01-05"], _config(tmp_path))

    assert result.cells_run == 2
    assert settled == ["AAPL"]
    assert result.settlement_failures == [("NVDA", "reflector timed out")]


@pytest.mark.unit
def test_pending_note_appears_only_when_something_is_pending(tmp_path):
    settled = [("NVDA", "2026-01-05", DECISION, (0.1, 0.05))]
    assert "Pending" not in summarize(_log_with(tmp_path, settled)).render()
    assert "Pending" in summarize(_log_with(tmp_path, settled + [("AAPL", "2026-01-05", DECISION, None)])).render()


# --- scoring reads the direction the rating claimed ---------------------------

def _scored(tmp_path, rows):
    log = _log_with(tmp_path, rows)
    return summarize(log).by_rating


@pytest.mark.unit
def test_a_bearish_call_that_fell_counts_as_right(tmp_path):
    """Alpha below the benchmark is the outcome a Sell predicted; scoring it as
    a miss reported the system as wrong exactly when it was right."""
    scores = _scored(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Sell\n\nx", (-0.08, -0.05)),
        ("AAPL", "2026-01-05", "**Rating**: Underweight\n\nx", (-0.03, -0.02)),
    ])
    assert scores["Sell"].hit_rate == 1.0
    assert scores["Underweight"].hit_rate == 1.0


@pytest.mark.unit
def test_a_bearish_call_that_rose_counts_as_wrong(tmp_path):
    scores = _scored(tmp_path, [("NVDA", "2026-01-05", "**Rating**: Sell\n\nx", (0.08, 0.05))])
    assert scores["Sell"].hit_rate == 0.0


@pytest.mark.unit
def test_a_bullish_call_is_scored_the_same_way_as_before(tmp_path):
    scores = _scored(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Buy\n\nx", (0.10, 0.04)),
        ("AAPL", "2026-01-05", "**Rating**: Buy\n\nx", (-0.02, -0.02)),
    ])
    assert scores["Buy"].hit_rate == 0.5


@pytest.mark.unit
def test_hold_claims_no_direction_so_it_gets_no_hit_rate(tmp_path):
    scores = _scored(tmp_path, [("NVDA", "2026-01-05", "**Rating**: Hold\n\nx", (0.01, 0.005))])
    assert scores["Hold"].hit_rate is None
    assert scores["Hold"].mean_alpha == 0.005


@pytest.mark.unit
def test_the_report_names_the_window_the_scores_cover(tmp_path):
    text = summarize(_log_with(tmp_path, [
        ("NVDA", "2026-01-05", "**Rating**: Buy\n\nx", (0.1, 0.05))])).render()
    assert "5" in text and "day" in text.lower()
    assert "Hold" not in text or "no direction" in text.lower()


@pytest.mark.unit
def test_the_window_reported_is_the_one_the_outcomes_used(tmp_path):
    """The log records the window each outcome was measured over; the summary
    must not claim a different one."""
    log = TradingMemoryLog({"memory_log_path": str(tmp_path / "m.md")})
    log.store_decision("NVDA", "2026-01-05", "**Rating**: Buy\n\nx")
    log.update_with_outcome("NVDA", "2026-01-05", 0.1, 0.04, 21, "note", "2026-02-01")

    assert "21 trading days" in summarize(tmp_path / "m.md").render()


@pytest.mark.unit
def test_a_backtest_result_is_summarized_directly(tmp_path):
    """The result names its own log, so a caller never builds the log to score it."""
    from tradingagents.backtest import BacktestResult

    path = _log_with(tmp_path, [("NVDA", "2026-01-05", "Rating: Buy\n\nx", (0.10, 0.04))])

    assert summarize(BacktestResult(run_id="r", log_path=path)).resolved == 1


@pytest.mark.unit
def test_a_log_path_that_does_not_exist_is_an_error_not_an_empty_summary(tmp_path):
    missing = tmp_path / "no-such-dir" / "m.md"

    with pytest.raises(FileNotFoundError):
        summarize(missing)
    assert not missing.parent.exists()


@pytest.mark.unit
def test_a_result_whose_cells_all_failed_summarizes_as_empty(tmp_path):
    from tradingagents.backtest import BacktestResult

    result = BacktestResult(run_id="r", log_path=tmp_path / "never-written.md")

    assert summarize(result).resolved == 0


@pytest.mark.unit
def test_progress_is_reported_before_each_cell(tmp_path):
    seen = []

    run_backtest(["NVDA", "AAPL"], ["2026-01-05", "2026-01-12"], _config(tmp_path),
                 progress=lambda done, total, ticker, date: seen.append((done, total, ticker, date)))

    assert seen == [(1, 4, "NVDA", "2026-01-05"), (2, 4, "NVDA", "2026-01-12"),
                    (3, 4, "AAPL", "2026-01-05"), (4, 4, "AAPL", "2026-01-12")]


@pytest.mark.unit
def test_a_resumed_sweep_reports_only_the_cells_it_runs(tmp_path):
    first = run_backtest(["NVDA"], ["2026-01-05"], _config(tmp_path))
    seen = []

    run_backtest(["NVDA"], ["2026-01-05", "2026-01-12"], _config(tmp_path), run_id=first.run_id,
                 progress=lambda done, total, ticker, date: seen.append((done, total, date)))

    assert seen == [(1, 1, "2026-01-12")]


@pytest.mark.unit
def test_a_ticker_or_date_given_twice_runs_and_settles_once(tmp_path):
    run_backtest(["NVDA", "NVDA"], ["2026-01-05", "2026-01-05"], _config(tmp_path))
    graph = _FakeGraph.instances[-1]
    assert graph.calls == [("NVDA", "2026-01-05")]
    assert graph.settled == ["NVDA"]


# ---------------------------------------------------------------------------
# Scoring the instruction rather than the rating
# ---------------------------------------------------------------------------

import pandas as pd  # noqa: E402

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision  # noqa: E402
import tradingagents.backtest as bt  # noqa: E402
from tradingagents.backtest import PlanScore, score_log_plans  # noqa: E402


def _decision(rating="Sell", entry=1.13, stop=1.153, risk=1.35) -> str:
    return render_pm_decision(PortfolioDecision(
        rating=rating, executive_summary="s", investment_thesis="t",
        entry_price=entry, stop_loss=stop, risk_percent=risk))


def _log(tmp_path, decisions) -> str:
    """A memory log holding the given decisions, written in the stored format."""
    path = tmp_path / "trading_memory.md"
    log = TradingMemoryLog({"memory_log_path": str(path)})
    for i, (ticker, date, text, rating) in enumerate(decisions):
        log.store_decision(ticker=ticker, trade_date=date,
                           final_trade_decision=text, rating=rating)
    return str(path)


def _bars(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close"])


STOPPED_PATH = _bars(
    [("2026-10-07", 1.1240, 1.1310, 1.1235, 1.1305),
     ("2026-10-08", 1.1305, 1.1560, 1.1300, 1.1550)]
    + [(f"2026-10-{9 + i:02d}", 1.1000, 1.1060, 1.0950, 1.1000) for i in range(25)])


@pytest.mark.unit
def test_the_three_variants_are_scored_over_the_same_decision(tmp_path):
    """Their difference is what attributes the result to entry, stop or rating."""
    path = _log(tmp_path, [("EURUSD", "2026-10-06", _decision(), "Sell")])
    summary = score_log_plans(path, horizon=5, bars_for=lambda t, d: STOPPED_PATH)
    assert summary.decisions == 1
    assert set(summary.by_variant) == {"as_specified", "market_entry", "rating_only"}
    assert all(v.trades == 1 for v in summary.by_variant.values())


@pytest.mark.unit
def test_the_stop_is_what_separates_as_specified_from_rating_only(tmp_path):
    """Same decision, same path: stopped at -1R, or held through to a profit.

    This is the comparison the whole addition exists to make. A trend call
    entering against an oversold bounce is exactly where honouring the stop
    converts a winning holding period into a realised loss.
    """
    path = _log(tmp_path, [("EURUSD", "2026-10-06", _decision(), "Sell")])
    summary = score_log_plans(path, horizon=5, bars_for=lambda t, d: STOPPED_PATH)
    assert summary.by_variant["as_specified"].expectancy_r == pytest.approx(-1.0)
    assert summary.by_variant["rating_only"].expectancy_r > 0
    assert summary.by_variant["as_specified"].stopped == 1
    assert summary.by_variant["rating_only"].stopped == 0


@pytest.mark.unit
def test_a_limit_that_never_fills_is_counted_as_a_no_fill_not_a_zero(tmp_path):
    """A plan the market never came back for must not average in as flat."""
    away = _bars([(f"2026-10-{7 + i:02d}", 1.1200, 1.1250, 1.1180, 1.1200)
                  for i in range(25)])
    path = _log(tmp_path, [("EURUSD", "2026-10-06", _decision(), "Sell")])
    summary = score_log_plans(path, horizon=5, bars_for=lambda t, d: away)
    specified = summary.by_variant["as_specified"]
    assert specified.no_fills == 1
    assert specified.trades == 0
    assert specified.expectancy_r is None
    # Entering at the market does get the trade, which is what the comparison shows.
    assert summary.by_variant["market_entry"].trades == 1


@pytest.mark.unit
def test_a_hold_is_counted_as_a_hold_rather_than_scored(tmp_path):
    path = _log(tmp_path, [("EURUSD", "2026-10-06",
                            _decision(rating="Hold", entry=None, stop=None, risk=None),
                            "Hold")])
    summary = score_log_plans(path, horizon=5, bars_for=lambda t, d: STOPPED_PATH)
    assert summary.holds == 1
    assert all(v.trades == 0 for v in summary.by_variant.values())


@pytest.mark.unit
def test_a_decision_with_no_levels_is_reported_as_incomplete(tmp_path):
    """A system that cannot state its own levels cannot be executed."""
    path = _log(tmp_path, [("EURUSD", "2026-10-06",
                            _decision(entry=None, stop=None, risk=None), "Sell")])
    summary = score_log_plans(path, horizon=5, bars_for=lambda t, d: STOPPED_PATH)
    assert summary.incomplete == 1
    assert all(v.trades == 0 for v in summary.by_variant.values())
    # Counted once, as incomplete. Falling through to the scorer as well would
    # double-count it as an unmeasurable trade in all three variants.
    assert all(v.unmeasured == 0 for v in summary.by_variant.values())
    assert "cannot be executed" in summary.render()


@pytest.mark.unit
def test_a_contradictory_instruction_is_listed_as_invalid(tmp_path):
    """A stop on the wrong side would still produce a number. It must not."""
    path = _log(tmp_path, [("EURUSD", "2026-10-06",
                            _decision(entry=1.13, stop=1.10), "Sell")])
    summary = score_log_plans(path, horizon=5, bars_for=lambda t, d: STOPPED_PATH)
    assert len(summary.invalid) == 1
    assert "wrong side" in summary.invalid[0][2]


@pytest.mark.unit
def test_the_report_names_whether_the_trade_craft_added_or_cost(tmp_path):
    path = _log(tmp_path, [("EURUSD", "2026-10-06", _decision(), "Sell")])
    text = score_log_plans(path, horizon=5, bars_for=lambda t, d: STOPPED_PATH).render()
    assert ("trade craft ADDED" in text) or ("trade craft COST" in text)


@pytest.mark.unit
class TestPlanScoreArithmetic:
    def test_expectancy_is_the_mean_r_per_trade_taken(self):
        """Per trade taken, not per plan attempted.

        Dividing by attempts would quietly reward a system for the plans the
        market never let it enter, which is the opposite of informative.
        """
        score = PlanScore(variant="x", r_multiples=[-1.0, 2.0, -1.0, 1.0], no_fills=6)
        assert score.expectancy_r == pytest.approx(0.25)
        assert score.total_r == pytest.approx(1.0)

    def test_win_rate_counts_trades_not_days(self):
        score = PlanScore(variant="x", r_multiples=[-1.0, 2.0, -1.0, 1.0])
        assert score.win_rate == pytest.approx(0.5)

    def test_the_fill_rate_has_no_fills_in_its_denominator(self):
        score = PlanScore(variant="x", r_multiples=[1.0, 1.0], no_fills=2)
        assert score.fill_rate == pytest.approx(0.5)

    def test_nothing_taken_reports_nothing_rather_than_zero(self):
        score = PlanScore(variant="x")
        assert score.expectancy_r is None
        assert score.win_rate is None
        assert score.t_stat is None
        assert score.fill_rate is None

    def test_a_single_trade_has_no_t_statistic(self):
        """One observation cannot be distinguished from luck at all."""
        assert PlanScore(variant="x", r_multiples=[3.0]).t_stat is None

    def test_identical_trades_have_no_t_statistic(self):
        """No spread means no inference, not infinite confidence."""
        assert PlanScore(variant="x", r_multiples=[1.0, 1.0, 1.0]).t_stat is None

    def test_portfolio_return_compounds(self):
        score = PlanScore(variant="x", portfolio_returns=[0.01, 0.01])
        assert score.portfolio_total == pytest.approx(1.01 * 1.01 - 1.0)

    def test_no_stated_sizes_means_no_portfolio_figure(self):
        assert PlanScore(variant="x", r_multiples=[1.0]).portfolio_total is None


@pytest.mark.unit
class TestForwardBars:
    """The bars a decision is scored against: strictly after it, and raw.

    Off by one here and the decision is scored partly against the close it was
    formed from, which is the one place in this codebase where looking forward
    is correct and so the one place a boundary error looks like nothing.
    """

    def _frame(self):
        return pd.DataFrame({
            "Date": pd.to_datetime(["2026-10-05", "2026-10-06", "2026-10-07",
                                    "2026-10-08", "2026-10-09"]),
            "Open": [1.0, 1.1, 1.2, 1.3, 1.4],
            "High": [1.0, 1.1, 1.2, 1.3, 1.4],
            "Low": [1.0, 1.1, 1.2, 1.3, 1.4],
            "Close": [1.0, 1.1, 1.2, 1.3, 1.4],
        })

    def test_the_decision_date_itself_is_excluded(self, monkeypatch):
        """The decision is formed from that close, so it cannot trade at it."""
        import tradingagents.dataflows.vendors.yahoo.ohlcv as ohlcv_mod

        monkeypatch.setattr(ohlcv_mod, "load_ohlcv",
                            lambda *a, **k: self._frame())
        bars = bt.forward_bars("EURUSD", "2026-10-06")
        assert [str(d.date()) for d in bars["Date"]] == [
            "2026-10-07", "2026-10-08", "2026-10-09"]

    def test_the_bars_are_requested_raw_rather_than_gap_filled(self, monkeypatch):
        """A carried-forward bar has no true high or low.

        Feeding one to a stop check either invents a trigger or hides one, and
        both are silent.
        """
        import tradingagents.dataflows.vendors.yahoo.ohlcv as ohlcv_mod

        seen = {}

        def spy(ticker, as_of, fill_gaps=True):
            seen["fill_gaps"] = fill_gaps
            return self._frame()

        monkeypatch.setattr(ohlcv_mod, "load_ohlcv", spy)
        bt.forward_bars("EURUSD", "2026-10-06")
        assert seen["fill_gaps"] is False

    def test_the_lookahead_is_capped(self, monkeypatch):
        import tradingagents.dataflows.vendors.yahoo.ohlcv as ohlcv_mod

        monkeypatch.setattr(ohlcv_mod, "load_ohlcv", lambda *a, **k: self._frame())
        assert len(bt.forward_bars("EURUSD", "2026-10-05", lookahead_days=2)) == 2

    def test_a_frame_without_dates_is_refused_rather_than_misread(self, monkeypatch):
        import tradingagents.dataflows.vendors.yahoo.ohlcv as ohlcv_mod

        monkeypatch.setattr(ohlcv_mod, "load_ohlcv",
                            lambda *a, **k: pd.DataFrame({"Close": [1.0]}))
        assert bt.forward_bars("EURUSD", "2026-10-06") is None
