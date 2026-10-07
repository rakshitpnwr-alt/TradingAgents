"""The runner's report must render before anyone waits on a download for it.

``backtest_signals.py`` needs Yahoo and FRED, so it is only ever run on a
machine with network. That makes a formatting bug expensive in a way a unit
test is not: it surfaces after the data has been fetched, at the end of the
run, with nothing to show for the wait. These drive the whole report on
injected data so every branch of it has been printed once already -- including
the ones that only appear when something went wrong.
"""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest

from tradingagents.signals import backtest as bt
from tradingagents.signals.base import Direction, SignalResult


@pytest.fixture
def runner():
    return importlib.import_module("backtest_signals")


def synthetic(n=900, daily=0.0004, seed=3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100.0 * np.cumprod(1.0 + daily + rng.normal(0, 0.006, n))
    return pd.DataFrame({"Date": pd.date_range("2002-01-01", periods=n, freq="D"),
                         "Close": closes})


@pytest.fixture
def offline(monkeypatch):
    """Every instrument priced, every rate served, nothing over the network."""
    monkeypatch.setattr(bt, "load_history",
                        lambda symbol, years=25: synthetic(seed=abs(hash(symbol)) % 100))
    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.fred.get_series_observations",
        lambda sid, start, end, vintage=None: [
            ("2002-01-01", 1.0), ("2010-01-01", 2.0), ("2020-01-01", 3.5),
        ],
    )
    monkeypatch.setattr(bt, "_RATE_HISTORY", None)
    return True


@pytest.mark.unit
def test_the_whole_report_renders(runner, offline, capsys):
    assert runner.main() == 0
    out = capsys.readouterr().out
    assert "SIGNAL BACKTEST" in out
    assert "POOLED (equal weight)" in out
    assert "VERDICT:" in out
    assert "sensitivity: execution lag" in out


@pytest.mark.unit
def test_the_report_names_every_registered_signal(runner, offline, capsys):
    """A signal that quietly vanished from the table would read as untested."""
    from tradingagents.signals.registry import REGISTRY

    runner.main()
    out = capsys.readouterr().out
    for signal in REGISTRY:
        assert signal.name in out, f"{signal.name} is missing from the report"


@pytest.mark.unit
def test_the_report_refuses_the_dollar_factor_in_words(runner, offline, capsys):
    runner.main()
    out = capsys.readouterr().out
    assert "NOT BACKTESTABLE AS A STRATEGY" in out
    assert "DIAGNOSTIC" in out


@pytest.mark.unit
def test_the_report_states_the_three_point_oh_hurdle(runner, offline, capsys):
    runner.main()
    out = capsys.readouterr().out
    assert "3.0" in out
    assert "not the conventional 2.0" in out


@pytest.mark.unit
def test_the_report_shows_gross_against_net(runner, offline, capsys):
    """Costless is the number a backtest flatters itself with; both must appear."""
    runner.main()
    out = capsys.readouterr().out
    assert "gross of costs" in out
    assert "cost drag" in out


@pytest.mark.unit
def test_the_report_survives_a_dead_price_vendor(runner, monkeypatch, capsys):
    """A download that fails must leave a readable report, not a traceback."""
    def dead(symbol, years=25):
        raise RuntimeError("Yahoo is unreachable")

    monkeypatch.setattr(bt, "load_history", dead)
    monkeypatch.setattr(bt, "_RATE_HISTORY", None)
    assert runner.main() == 0
    out = capsys.readouterr().out
    assert "Yahoo is unreachable" in out
    assert "nothing measured" in out


@pytest.mark.unit
def test_a_dead_rate_series_is_named_rather_than_left_as_dashes(
    runner, monkeypatch, capsys
):
    """Prices fine, rates gone: carry must say why, not render an empty row.

    An instrument whose rate series 404'd and one whose rule never formed a
    view produce the same row of dashes. They are not the same finding.
    """
    monkeypatch.setattr(bt, "load_history", lambda symbol, years=25: synthetic())
    monkeypatch.setattr(bt, "_RATE_HISTORY", None)
    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.fred.get_series_observations",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("FRED is unreachable")))
    assert runner.main() == 0
    out = capsys.readouterr().out
    assert "FRED is unreachable" in out
    assert "rate series that did not serve" in out
    # Momentum needs no rates, so it must still have produced a real result.
    assert "POOLED (equal weight)" in out


@pytest.mark.unit
def test_the_report_survives_an_instrument_with_too_little_history(runner, monkeypatch, capsys):
    monkeypatch.setattr(bt, "load_history", lambda symbol, years=25: synthetic(n=30))
    monkeypatch.setattr(bt, "_RATE_HISTORY", None)
    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.fred.get_series_observations",
        lambda *a, **k: [("2002-01-01", 1.0)])
    assert runner.main() == 0
    assert "SIGNAL BACKTEST" in capsys.readouterr().out


@pytest.mark.unit
def test_mid_sample_gaps_are_surfaced(runner, monkeypatch, capsys):
    """A hole after the rule started speaking is missing data, and must be said."""
    frame = synthetic(n=900)
    monkeypatch.setattr(bt, "load_history", lambda symbol, years=25: frame)
    monkeypatch.setattr(bt, "_RATE_HISTORY", None)

    calls = {"n": 0}

    def holey(window, as_of, ticker):
        calls["n"] += 1
        if calls["n"] % 7 == 0:
            return SignalResult.unavailable("time_series_momentum", "a vendor hole")
        return SignalResult(name="time_series_momentum", direction=Direction.LONG,
                            value=0.1, detail={"position_scalar": 1.0})

    monkeypatch.setitem(bt.READERS, "time_series_momentum", holey)
    runner.report_signal("time_series_momentum", lag=1)
    assert "MID-SAMPLE DATA GAPS" in capsys.readouterr().out


@pytest.mark.unit
class TestTheVerdictIsBlunt:
    """Each verdict must say what the sample does and does not support."""

    def _perf(self, **kw):
        base = dict(days=5000, periods=200, cagr=0.05, annual_volatility=0.1,
                    sharpe=0.5, sharpe_standard_error=0.1, t_stat=2.2,
                    max_drawdown=-0.2, hit_rate=0.55, annual_turnover=4.0,
                    annual_cost_drag=0.002, exposure_fraction=1.0)
        return bt.Performance(**{**base, **kw})

    def test_clearing_three_is_stated_as_surviving(self, runner):
        assert "CLEARS" in runner.verdict(self._perf(t_stat=3.4))

    def test_clearing_only_two_is_not_established(self, runner):
        text = runner.verdict(self._perf(t_stat=2.2))
        assert "NOT ESTABLISHED" in text
        assert "2.0" in text and "3.0" in text

    def test_a_positive_but_insignificant_result_is_called_luck(self, runner):
        assert "luck" in runner.verdict(self._perf(t_stat=0.6))

    def test_a_significantly_negative_result_is_stated_plainly(self, runner):
        assert "NEGATIVE" in runner.verdict(self._perf(sharpe=-0.6, t_stat=-3.1))

    def test_a_losing_rule_is_never_reported_as_clearing_the_hurdle(self, runner):
        """t=-3.1 is distinguishable from zero in the direction that loses money.

        The first version of this function read the hurdle as an |t| question
        and called that a success.
        """
        text = runner.verdict(self._perf(sharpe=-0.6, t_stat=-3.1))
        assert "CLEARS" not in text
        assert "survives" not in text

    def test_a_short_sample_is_not_dressed_up_as_a_result(self, runner):
        """A high Sharpe on 12 periods must not read as success."""
        text = runner.verdict(self._perf(periods=12, sharpe=2.0, t_stat=3.9))
        assert "NOT ESTABLISHED" in text
        assert "CLEARS" not in text

    def test_nothing_measured_says_so(self, runner):
        assert "NOT MEASURED" in runner.verdict(self._perf(sharpe=None, t_stat=None))
        assert "NOT MEASURED" in runner.verdict(None)


@pytest.mark.unit
class TestFormatting:
    def test_a_missing_number_renders_as_a_dash_not_a_zero(self, runner):
        assert runner.pct(None) == "—"
        assert runner.num(None) == "—"

    def test_percentages_carry_their_sign(self, runner):
        assert runner.pct(0.0512) == "+5.1%"
        assert runner.pct(-0.0512) == "-5.1%"

    def test_a_row_renders_with_every_statistic_absent(self, runner):
        """The case that appears when a vendor served two bars and nothing else."""
        empty = bt.Performance(days=1, periods=0, cagr=None, annual_volatility=None,
                               sharpe=None, sharpe_standard_error=None, t_stat=None,
                               max_drawdown=None, hit_rate=None, annual_turnover=None,
                               annual_cost_drag=None, exposure_fraction=None)
        row = runner.performance_row("X", empty)
        assert "X" in row and "—" in row


@pytest.mark.unit
class TestTheReportDistinguishesNoViewFromNoData:
    """Three different non-results that all render as a row of dashes.

    A rule that stayed flat, a rule with no variance, and a vendor that served
    nothing are findings about three different things. Sharing one sentence
    would turn a fact about the rule into a fact about the data, or the reverse.
    """

    def _perf(self, **kw):
        base = dict(days=2000, periods=95, cagr=0.0, annual_volatility=None,
                    sharpe=None, sharpe_standard_error=None, t_stat=None,
                    max_drawdown=0.0, hit_rate=None, annual_turnover=0.0,
                    annual_cost_drag=0.0, exposure_fraction=0.0)
        return bt.Performance(**{**base, **kw})

    def test_flat_on_every_date_is_reported_as_no_view(self, runner):
        text = runner.verdict(self._perf())
        assert "NO VIEW" in text
        assert "95 rebalance dates" in text

    def test_a_held_position_with_no_variance_is_not_called_no_view(self, runner):
        text = runner.verdict(self._perf(exposure_fraction=1.0))
        assert "NO VIEW" not in text
        assert "no variance" in text

    def test_no_days_at_all_is_reported_as_nothing_measured(self, runner):
        assert "NOT MEASURED" in runner.verdict(self._perf(days=0))

    def test_a_flat_rule_is_not_counted_as_a_failed_one(self, runner, monkeypatch, capsys):
        """'0 of 7' reads as seven failures when the truth is seven non-results."""
        monkeypatch.setattr(bt, "load_history", lambda symbol, years=25: synthetic())
        monkeypatch.setattr(bt, "_RATE_HISTORY", None)
        # Identical rates on both legs: a zero differential, flat everywhere.
        # The series has to reach today, or the measured publication lag is
        # years long and every date comes back unavailable instead of flat.
        today = pd.Timestamp.today().strftime("%Y-%m-%d")
        monkeypatch.setattr(
            "tradingagents.dataflows.vendors.fred.get_series_observations",
            lambda *a, **k: [("2002-01-01", 2.0), (today, 2.0)])
        runner.report_signal("carry_rate_differential", lag=1)
        out = capsys.readouterr().out
        assert "none of" in out
        assert "0 of" not in out


@pytest.mark.unit
class TestTheReportFlagsADiscontinuedRateSeries:
    def test_an_enormous_publication_lag_is_flagged(self, runner, monkeypatch, capsys):
        """Nothing is ever readable, carry goes unavailable, and that must be loud."""
        monkeypatch.setattr(bt, "load_history", lambda symbol, years=25: synthetic())
        monkeypatch.setattr(bt, "_RATE_HISTORY", None)
        monkeypatch.setattr(
            "tradingagents.dataflows.vendors.fred.get_series_observations",
            lambda *a, **k: [("2005-01-01", 2.0)])
        runner.main()
        assert "looks discontinued" in capsys.readouterr().out

    def test_a_current_series_is_not_flagged(self, runner, monkeypatch, capsys):
        today = pd.Timestamp.today().strftime("%Y-%m-%d")
        monkeypatch.setattr(bt, "load_history", lambda symbol, years=25: synthetic())
        monkeypatch.setattr(bt, "_RATE_HISTORY", None)
        monkeypatch.setattr(
            "tradingagents.dataflows.vendors.fred.get_series_observations",
            lambda *a, **k: [("2005-01-01", 2.0), (today, 3.0)])
        runner.main()
        assert "looks discontinued" not in capsys.readouterr().out


@pytest.mark.unit
def test_a_hit_rate_is_rendered_unsigned(runner):
    """It is a proportion, not a gain: '+52%' reads as a return."""
    assert runner.plain_pct(0.52) == "52%"
    assert runner.plain_pct(None) == "—"


@pytest.mark.unit
class TestTheVerdictDoesNotOverstateInEitherDirection:
    """Overstating a negative is the same error as overstating a positive.

    A Sharpe of -0.21 at t=-1.04 cannot establish that a rule loses money, and
    calling it NEGATIVE is how a rule gets deleted on evidence that could not
    have established its sign either way.
    """

    def _perf(self, sharpe, t):
        return bt.Performance(days=6503, periods=1983, cagr=-0.013,
                              annual_volatility=0.055, sharpe=sharpe,
                              sharpe_standard_error=0.20, t_stat=t,
                              max_drawdown=-0.397, hit_rate=None,
                              hit_periods_counted=0, annual_turnover=0.6,
                              annual_cost_drag=0.0001, exposure_fraction=1.0)

    def test_an_insignificant_negative_is_not_called_negative(self, runner):
        text = runner.verdict(self._perf(-0.21, -1.04))
        assert "NO EVIDENCE IT PAYS" in text
        assert "not significantly negative" in text
        assert "NEGATIVE on our history" not in text

    def test_a_significant_negative_is_called_negative(self, runner):
        assert "NEGATIVE on our history" in runner.verdict(self._perf(-0.6, -3.1))

    def test_the_baseline_comparison_outranks_the_t_statistic(self, runner):
        """A rule can be reliably no better than buy-and-hold."""
        signal, basket = self._perf(0.55, 2.93), self._perf(0.57, 3.0)
        assert "DOES NOT BEAT" in runner.earns_its_complexity(signal, basket)
        assert "BEATS" in runner.earns_its_complexity(self._perf(0.9, 4.1), basket)

    def test_no_baseline_comparison_is_made_without_a_baseline(self, runner):
        assert runner.earns_its_complexity(self._perf(0.5, 2.0), None) is None

    def test_concentration_reports_the_median_beside_the_best(self, runner):
        text = runner.concentration([-0.26, -0.14, -0.06, 0.06, 0.08, 0.19, 0.37, 0.48, 1.05])
        assert "median Sharpe +0.08" in text
        assert "best +1.05" in text
        assert "worst -0.26" in text

    def test_concentration_is_not_reported_on_too_few_instruments(self, runner):
        assert runner.concentration([0.5, 0.6]) is None


@pytest.mark.unit
class TestTheRowShowsTheHitRateSampleSize:
    def _perf(self, hit, n):
        return bt.Performance(days=5925, periods=1983, cagr=-0.024,
                              annual_volatility=0.095, sharpe=-0.21,
                              sharpe_standard_error=0.21, t_stat=-1.01,
                              max_drawdown=-0.487, hit_rate=hit,
                              hit_periods_counted=n, annual_turnover=0.6,
                              annual_cost_drag=0.0001, exposure_fraction=1.0)

    def test_the_denominator_is_printed_beside_the_rate(self, runner):
        """0% on two stretches must not look like 0% on two hundred."""
        assert "n=2" in runner.performance_row("EURUSD=X", self._perf(0.0, 2))
        assert "n=200" in runner.performance_row("EURUSD=X", self._perf(0.0, 200))

    def test_no_denominator_is_printed_when_there_is_no_rate(self, runner):
        assert "n=" not in runner.performance_row("POOLED", self._perf(None, 0))

    def test_the_header_has_a_column_for_it(self, runner):
        assert "(n)" in runner.header()
