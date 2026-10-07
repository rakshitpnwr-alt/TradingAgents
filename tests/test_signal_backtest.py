"""Tests for the signal backtest.

The tests that matter here are the ones that would catch a backtest lying, so
they are written as constructions where the right answer is known before the
code runs: a series mutated after a reading was formed (look-ahead), a position
whose start bar is counted by hand (timing), a direction flip whose cost is two
units of turnover by arithmetic (costs), and a drawdown of exactly half.

A passing performance number proves nothing. A reading that changes when the
future changes proves the whole module is worthless, which is why that test is
first.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradingagents.signals import backtest as bt
from tradingagents.signals import carry, dollar_factor, tsmom
from tradingagents.signals.base import Direction, SignalResult


def series(closes, start="2015-01-01") -> pd.DataFrame:
    """A history frame from a list of closes, on consecutive calendar days."""
    dates = pd.date_range(start, periods=len(closes), freq="D")
    return pd.DataFrame({"Date": dates, "Close": [float(c) for c in closes]})


def rising(n=400, daily=0.001, start=100.0) -> pd.DataFrame:
    """A series that rises at a constant rate, with a little noise so it has variance."""
    rng = np.random.default_rng(7)
    steps = daily + rng.normal(0, 0.004, n)
    return series(start * np.cumprod(1.0 + steps))


# ---------------------------------------------------------------------------
# Look-ahead: the one failure that makes every other number meaningless
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_reading_does_not_change_when_the_future_changes():
    """Mutate every bar after the reading date; the reading must be identical.

    This is the test the whole module exists to pass. If a reading moves when
    data it should never have seen moves, every performance figure downstream is
    a measurement of the future.
    """
    frame = rising(400)
    cut = 300
    readings = bt.walk(frame.iloc[: cut + 1], bt.tsmom_reader, "X", every_n_days=50)

    tampered = frame.copy()
    tampered.loc[cut + 1:, "Close"] *= 10.0   # a violent, impossible future
    after = bt.walk(tampered, bt.tsmom_reader, "X", every_n_days=50)

    assert readings, "the walk produced no readings to compare"
    for before, now in zip(readings, after):
        assert before.bar == now.bar
        assert before.direction == now.direction
        assert before.scalar == pytest.approx(now.scalar), (
            f"the reading on bar {before.bar} moved when bar {cut + 1} onward moved"
        )


@pytest.mark.unit
def test_the_walk_slices_through_the_reading_bar_and_no_further():
    """Each reading equals the signal applied by hand to closes 0..bar."""
    frame = rising(300)
    for reading in bt.walk(frame, bt.tsmom_reader, "X", every_n_days=40):
        expected = tsmom.compute_from_closes(frame["Close"].iloc[: reading.bar + 1], instrument="X")
        assert reading.direction == expected.direction
        if expected.available:
            assert reading.scalar == pytest.approx(
                expected.detail.get("position_scalar", 1.0)
            )


@pytest.mark.unit
def test_the_reader_calls_the_signals_own_function():
    """A backtest of a reimplementation of the rule tests the reimplementation."""
    calls = []
    original = tsmom.compute_from_closes

    def spy(closes, instrument=""):
        calls.append(len(closes))
        return original(closes, instrument=instrument)

    tsmom.compute_from_closes = spy
    try:
        bt.walk(rising(260), bt.tsmom_reader, "X", every_n_days=100)
    finally:
        tsmom.compute_from_closes = original
    assert calls, "tsmom_reader did not call tsmom.compute_from_closes"


# ---------------------------------------------------------------------------
# Timing: which bar's return a reading earns
# ---------------------------------------------------------------------------


def one_reading(bar, direction=Direction.LONG, scalar=1.0):
    return [bt.Reading(bar=bar, date="2015-01-01", direction=direction, scalar=scalar)]


@pytest.mark.unit
def test_a_position_starts_one_bar_after_execution():
    """Formed on bar 10, executed at the close of 11, earns bar 12 onward."""
    frame = series(range(100, 120))
    exposure = bt.exposure_series(frame, one_reading(10), lag=1)
    assert exposure.iloc[:12].isna().all(), "a position was held before it was executed"
    assert (exposure.iloc[12:] == 1.0).all()


@pytest.mark.unit
def test_zero_lag_earns_the_very_next_bar():
    frame = series(range(100, 120))
    exposure = bt.exposure_series(frame, one_reading(10), lag=0)
    assert exposure.iloc[:11].isna().all()
    assert (exposure.iloc[11:] == 1.0).all()


@pytest.mark.unit
def test_the_lag_costs_exactly_one_bar_of_return():
    """lag=1 must differ from lag=0, or the parameter is not doing anything."""
    frame = rising(300)
    readings = bt.walk(frame, bt.tsmom_reader, "X", every_n_days=21)
    lagged = bt.exposure_series(frame, readings, lag=1)
    prompt = bt.exposure_series(frame, readings, lag=0)
    assert not lagged.equals(prompt)
    # The lagged series is the prompt one shifted forward by one bar.
    assert lagged.iloc[1:].to_numpy().tolist() == pytest.approx(
        prompt.iloc[:-1].to_numpy().tolist(), nan_ok=True
    )


@pytest.mark.unit
def test_a_reading_is_replaced_by_the_next_one():
    frame = series(range(100, 140))
    readings = [
        bt.Reading(bar=5, date="d", direction=Direction.LONG, scalar=1.0),
        bt.Reading(bar=20, date="d", direction=Direction.SHORT, scalar=2.0),
    ]
    exposure = bt.exposure_series(frame, readings, lag=1)
    assert (exposure.iloc[7:22] == 1.0).all()
    assert (exposure.iloc[22:] == -2.0).all()


@pytest.mark.unit
def test_a_reading_too_late_to_act_on_holds_nothing():
    frame = series(range(100, 110))
    exposure = bt.exposure_series(frame, one_reading(9), lag=1)
    assert exposure.isna().all()


# ---------------------------------------------------------------------------
# Costs and turnover
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_entering_from_nothing_is_charged_once():
    frame = series([100.0, 101.0, 102.0, 103.0])
    exposure = pd.Series([np.nan, 1.0, 1.0, 1.0])
    rows = bt.returns_frame(frame, exposure, cost_bps=10.0)
    assert rows["turnover"].tolist() == pytest.approx([0.0, 1.0, 0.0, 0.0])
    assert rows["cost"].iloc[1] == pytest.approx(10.0 / 10_000)
    # Net is the asset return on that exposure, less the cost of getting there.
    assert rows["net"].iloc[1] == pytest.approx(0.01 - 0.001)
    assert rows["net"].iloc[2] == pytest.approx(101.0 / 101.0 * (102.0 / 101.0 - 1.0))


@pytest.mark.unit
def test_a_direction_flip_pays_twice():
    """Long-one to short-one is two units of notional traded, not one."""
    frame = series([100.0, 101.0, 102.0, 103.0])
    rows = bt.returns_frame(frame, pd.Series([np.nan, 1.0, -1.0, -1.0]), cost_bps=10.0)
    assert rows["turnover"].iloc[2] == pytest.approx(2.0)
    assert rows["cost"].iloc[2] == pytest.approx(2 * 10.0 / 10_000)


@pytest.mark.unit
def test_net_is_gross_minus_cost_everywhere_a_position_was_held():
    frame = rising(120)
    rows = bt.returns_frame(frame, pd.Series([np.nan] * 10 + [1.5] * 110), cost_bps=4.0)
    held = rows[rows["exposure"].notna()]
    assert (held["net"] - (held["gross"] - held["cost"])).abs().max() == pytest.approx(0.0)


@pytest.mark.unit
def test_bars_with_no_position_are_not_zero_return_days():
    """A bar the rule had no view on must be NaN, not a free flat day.

    Counted as zero it would add days without adding variance, which raises the
    Sharpe of any rule with a long warm-up period.
    """
    frame = rising(60)
    rows = bt.returns_frame(frame, pd.Series([np.nan] * 30 + [1.0] * 30), cost_bps=0.0)
    assert rows["net"].iloc[:30].isna().all()
    assert rows["net"].iloc[31:].notna().all()


@pytest.mark.unit
def test_cost_defaults_follow_the_instrument():
    assert bt.cost_bps_for("EURUSD=X") == bt.COST_BPS_PER_SIDE["forex"]
    assert bt.cost_bps_for("GC=F") == bt.COST_BPS_PER_SIDE["commodity"]
    assert bt.cost_bps_for("BTC-USD") == bt.COST_BPS_PER_SIDE["crypto"]
    assert bt.cost_bps_for("AAPL") == bt.COST_BPS_PER_SIDE["stock"]


@pytest.mark.unit
def test_costs_reduce_the_measured_return():
    frame = rising(400)
    free = bt.backtest_instrument("X", tsmom.NAME, frame=frame, cost_bps=0.0)
    dear = bt.backtest_instrument("X", tsmom.NAME, frame=frame, cost_bps=50.0)
    assert free.net.cagr > dear.net.cagr
    assert dear.net.annual_cost_drag > free.net.annual_cost_drag


# ---------------------------------------------------------------------------
# The statistics, against hand-computed answers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_max_drawdown_of_a_halving():
    """+50% then -50%: equity 1.5 then 0.75, a drawdown of exactly half."""
    rows = pd.DataFrame({"net": [0.5, -0.5], "exposure": [1.0, 1.0]})
    assert bt.measure(rows).max_drawdown == pytest.approx(-0.5)


@pytest.mark.unit
def test_sharpe_uses_the_papers_annualisation_and_a_sample_standard_deviation():
    values = [0.01, -0.005, 0.004, -0.002, 0.008, -0.009, 0.003]
    rows = pd.DataFrame({"net": values, "exposure": [1.0] * len(values)})
    expected = (np.mean(values) / np.std(values, ddof=1)) * math.sqrt(261)
    assert bt.measure(rows).sharpe == pytest.approx(expected)
    assert bt.TRADING_DAYS_PER_YEAR == 261


@pytest.mark.unit
def test_t_stat_is_the_sharpe_scaled_by_the_sample_length():
    values = [0.01, -0.005, 0.004, -0.002, 0.008, -0.009, 0.003]
    rows = pd.DataFrame({"net": values, "exposure": [1.0] * len(values)})
    got = bt.measure(rows)
    assert got.t_stat == pytest.approx(got.sharpe * math.sqrt(len(values) / 261))


@pytest.mark.unit
def test_a_higher_sharpe_carries_a_larger_standard_error():
    """Lo (2002): a big estimate is not a more certain one."""
    small = pd.DataFrame({"net": [0.001, -0.001] * 100, "exposure": [1.0] * 200})
    big = pd.DataFrame({"net": [0.02, 0.001] * 100, "exposure": [1.0] * 200})
    assert bt.measure(big).sharpe > bt.measure(small).sharpe
    assert bt.measure(big).sharpe_standard_error > bt.measure(small).sharpe_standard_error


@pytest.mark.unit
def test_the_hurdle_is_three_not_two():
    values = [0.001] * 200 + [-0.0005] * 200
    rows = pd.DataFrame({"net": values, "exposure": [1.0] * len(values)})
    got = bt.measure(rows)
    assert bt.SIGNIFICANCE_HURDLE_T == 3.0
    # Whatever this sample says, the two properties must not agree by accident.
    assert got.clears_hurdle is (abs(got.t_stat) >= 3.0)
    assert got.clears_conventional is (abs(got.t_stat) >= 2.0)


@pytest.mark.unit
def test_a_constant_return_stream_has_no_sharpe_rather_than_a_zero_one():
    """No variance means nothing was measured, which must not read as no edge."""
    rows = pd.DataFrame({"net": [0.001] * 50, "exposure": [1.0] * 50})
    got = bt.measure(rows)
    assert got.sharpe is None
    assert got.t_stat is None
    assert got.annual_volatility is None
    assert got.cagr is not None   # the return itself is still a fact


@pytest.mark.unit
def test_an_empty_stream_measures_nothing_without_raising():
    got = bt.measure(pd.DataFrame())
    assert got.days == 0
    assert got.sharpe is None
    assert got.exposure_fraction is None


@pytest.mark.unit
def test_a_missing_column_is_not_an_exception():
    got = bt.measure(pd.DataFrame({"other": [1.0, 2.0]}), column="net")
    assert got.days == 0


@pytest.mark.unit
def test_cagr_compounds_rather_than_averaging():
    """261 days of +0.1% compounds to more than 26.1%."""
    rows = pd.DataFrame({"net": [0.001] * 261, "exposure": [1.0] * 261})
    assert bt.measure(rows).cagr == pytest.approx(1.001 ** 261 - 1.0)


@pytest.mark.unit
def test_a_total_loss_reports_no_cagr_rather_than_a_complex_number():
    rows = pd.DataFrame({"net": [-1.0, 0.5, 0.5], "exposure": [1.0] * 3})
    assert bt.measure(rows).cagr is None


@pytest.mark.unit
def test_a_short_sample_is_flagged_as_not_inferable():
    values = [0.001, -0.001] * 50
    rows = pd.DataFrame({"net": values, "exposure": [1.0] * len(values)})
    assert bt.measure(rows, periods=10).inferable is False
    assert bt.measure(rows, periods=bt.MIN_PERIODS_FOR_INFERENCE).inferable is True


# ---------------------------------------------------------------------------
# Hit rate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_hit_rate_is_per_holding_period_not_per_day():
    """Two stretches, one up and one down: 50%, whatever the day count."""
    rows = pd.DataFrame({
        "net": [0.01, 0.01, 0.01, -0.01, -0.01, -0.01],
        "exposure": [1.0, 1.0, 1.0, -1.0, -1.0, -1.0],
    })
    assert bt.measure(rows, periods=2).hit_rate == pytest.approx(0.5)


@pytest.mark.unit
def test_a_flat_stretch_is_neither_a_hit_nor_a_miss():
    """Doing nothing earns zero, and must not be scored as having been right."""
    rows = pd.DataFrame({
        "net": [0.01, 0.01, 0.0, 0.0],
        "exposure": [1.0, 1.0, 0.0, 0.0],
    })
    assert bt.measure(rows, periods=2).hit_rate == pytest.approx(1.0)


@pytest.mark.unit
def test_a_buy_and_hold_baseline_reports_no_hit_rate():
    """One unchanging stretch is one period; 100% on a sample of one is not a rate."""
    rows = pd.DataFrame({"net": [0.01] * 10, "exposure": [1.0] * 10})
    assert bt.measure(rows, periods=5, hit_periods=False).hit_rate is None


@pytest.mark.unit
def test_hit_rate_compounds_within_a_stretch():
    """A stretch that ends up is a hit even if some days inside it were down."""
    rows = pd.DataFrame({
        "net": [0.10, -0.01, -0.01],
        "exposure": [1.0, 1.0, 1.0],
    })
    assert bt.measure(rows, periods=1).hit_rate == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Rebalancing and the walk's bookkeeping
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rebalance_bars_are_evenly_spaced_in_trading_days():
    assert bt.rebalance_bars(10, every_n_days=3) == [0, 3, 6, 9]
    assert bt.rebalance_bars(10, every_n_days=3, warmup=4) == [4, 7]
    assert bt.rebalance_bars(0) == []


@pytest.mark.unit
def test_rebalance_bars_rejects_a_zero_interval():
    with pytest.raises(ValueError, match="at least 1"):
        bt.rebalance_bars(10, every_n_days=0)


@pytest.mark.unit
def test_a_reader_that_raises_does_not_end_the_walk():
    frame = rising(200)

    def exploding(window, as_of, ticker):
        if len(window) > 100:
            raise RuntimeError("vendor fell over")
        return SignalResult(name="x", direction=Direction.LONG, detail={"position_scalar": 1.0})

    readings = bt.walk(frame, exploding, "X", every_n_days=50)
    assert len(readings) == len(bt.rebalance_bars(len(frame), 50))
    broken = [r for r in readings if r.direction is Direction.UNAVAILABLE]
    assert broken and "vendor fell over" in broken[0].unavailable_reason


@pytest.mark.unit
def test_an_unavailable_reading_is_unmeasured_not_a_flat_day():
    """The rule could not speak, so those bars must leave the statistics entirely.

    Scored as zeros they are free flat days: a twelve-month rule has no reading
    for its first year, and hundreds of motionless days lower the measured
    volatility without lowering the return, which raises the Sharpe of every
    rule in the file. This is the mistake the first version of this module made.
    """
    frame = series(range(100, 140))
    readings = [bt.Reading(bar=5, date="d", direction=Direction.UNAVAILABLE,
                           scalar=0.0, unavailable_reason="no data")]
    assert bt.exposure_series(frame, readings, lag=1).isna().all()


@pytest.mark.unit
def test_a_diagnostic_reading_is_a_flat_day_rather_than_an_unmeasured_one():
    """It ran and implies no position, which is a decision with a real outcome."""
    frame = series(range(100, 140))
    readings = [bt.Reading(bar=5, date="d", direction=Direction.DIAGNOSTIC, scalar=1.0)]
    assert bt.exposure_series(frame, readings, lag=1).iloc[7:].eq(0.0).all()


@pytest.mark.unit
def test_flat_and_unavailable_do_not_look_alike_in_the_arithmetic():
    """Direction draws the distinction; if the numbers lose it, it was decorative."""
    frame = series(range(100, 140))
    flat = bt.exposure_series(frame, [bt.Reading(5, "d", Direction.FLAT, 1.0)], lag=1)
    absent = bt.exposure_series(
        frame, [bt.Reading(5, "d", Direction.UNAVAILABLE, 0.0, "no data")], lag=1)
    assert flat.iloc[7:].notna().all()
    assert absent.iloc[7:].isna().all()


@pytest.mark.unit
def test_the_warmup_period_does_not_enter_the_measured_days():
    """A 12-month rule on 600 bars has about 210 bars it cannot speak for."""
    frame = rising(600)
    result = bt.backtest_instrument("X", tsmom.NAME, frame=frame, cost_bps=0.0)
    assert result.warmup_unavailable > 0
    assert result.net.days < len(frame) - tsmom.MIN_HISTORY_DAYS + 10
    assert result.rows["net"].iloc[:tsmom.MIN_HISTORY_DAYS - 1].isna().all()


@pytest.mark.unit
def test_warmup_gaps_are_counted_separately_from_mid_sample_gaps():
    """A twelve-month rule has no reading in its first months by definition.

    A hole after it has started speaking is missing data, and a backtest run
    only on the dates where data happened to exist is not a backtest. The two
    must not be added together.
    """
    result = bt.InstrumentResult(ticker="X", signal="s", readings=[
        bt.Reading(0, "d", Direction.UNAVAILABLE, 0.0, "warming up"),
        bt.Reading(1, "d", Direction.UNAVAILABLE, 0.0, "warming up"),
        bt.Reading(2, "d", Direction.LONG, 1.0),
        bt.Reading(3, "d", Direction.UNAVAILABLE, 0.0, "vendor hole"),
        bt.Reading(4, "d", Direction.SHORT, 1.0),
    ])
    assert result.warmup_unavailable == 2
    assert result.mid_sample_unavailable == 1
    assert result.directional_readings == 2


@pytest.mark.unit
def test_a_negative_or_infinite_scalar_cannot_become_a_position():
    assert bt._scalar_of(SignalResult("x", Direction.LONG, detail={"position_scalar": -2})) == 0.0
    assert bt._scalar_of(SignalResult("x", Direction.LONG, detail={"position_scalar": float("inf")})) == 0.0
    assert bt._scalar_of(SignalResult("x", Direction.LONG, detail={"position_scalar": "junk"})) == 1.0
    assert bt._scalar_of(SignalResult("x", Direction.LONG)) == 1.0


# ---------------------------------------------------------------------------
# One instrument, end to end
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_rising_series_is_held_long_and_makes_money():
    """The sanity floor: momentum on a trend must not lose on the trend."""
    result = bt.backtest_instrument("X", tsmom.NAME, frame=rising(600), cost_bps=0.0)
    assert result.error is None
    assert result.net.cagr > 0
    assert result.net.sharpe > 0
    exposures = result.rows["exposure"].dropna()
    assert (exposures > 0).mean() > 0.9, "a steadily rising series was not held long"


@pytest.mark.unit
def test_a_falling_series_is_held_short_and_makes_money():
    frame = rising(600)
    frame["Close"] = frame["Close"].iloc[::-1].to_numpy()   # the same path, reversed
    result = bt.backtest_instrument("X", tsmom.NAME, frame=frame, cost_bps=0.0)
    exposures = result.rows["exposure"].dropna()
    assert (exposures < 0).mean() > 0.8, "a steadily falling series was not held short"


@pytest.mark.unit
def test_the_always_long_baseline_is_measured_over_the_same_bars():
    """Otherwise the comparison is between two different decades."""
    result = bt.backtest_instrument("X", tsmom.NAME, frame=rising(600), cost_bps=0.0)
    assert result.always_long.days == result.net.days


@pytest.mark.unit
def test_the_baseline_return_is_the_assets_own_return():
    frame = rising(600)
    result = bt.backtest_instrument("X", tsmom.NAME, frame=frame, cost_bps=0.0)
    held = result.rows[result.rows["exposure"].notna()]
    assert result.always_long.cagr == pytest.approx(
        bt.measure(pd.DataFrame({"net": held["asset"]}), "net").cagr
    )


@pytest.mark.unit
def test_an_instrument_with_no_history_is_an_error_not_a_zero():
    result = bt.backtest_instrument("X", tsmom.NAME, frame=pd.DataFrame())
    assert result.error
    assert result.net is None


@pytest.mark.unit
def test_a_frame_with_a_non_default_index_gives_identical_results():
    """A caller's slice keeps its old labels, and must change nothing.

    Aligning a fresh exposure series against surviving index labels would
    produce NaN for every bar and an empty, innocent-looking result.
    """
    sliced = rising(400).iloc[50:].copy()     # index now starts at 50
    renumbered = sliced.reset_index(drop=True)
    a = bt.backtest_instrument("X", tsmom.NAME, frame=sliced, cost_bps=0.0)
    b = bt.backtest_instrument("X", tsmom.NAME, frame=renumbered, cost_bps=0.0)
    assert a.error is None and b.error is None
    assert a.rows["exposure"].notna().any()
    pd.testing.assert_frame_equal(a.rows, b.rows)
    assert a.net == b.net


@pytest.mark.unit
def test_a_dead_vendor_is_reported_per_instrument_rather_than_ending_the_sweep(monkeypatch):
    def dead(symbol, years=25):
        raise RuntimeError("Yahoo is unreachable")

    monkeypatch.setattr(bt, "load_history", dead)
    result = bt.backtest_instrument("EURUSD=X", tsmom.NAME)
    assert "unreachable" in result.error
    assert result.net is None


# ---------------------------------------------------------------------------
# Carry's rate history, where a look-ahead would hide
# ---------------------------------------------------------------------------


def rates(**series) -> bt.RateHistory:
    """A rate history with handmade series and their publication lags."""
    history = bt.RateHistory(end="2020-12-31")
    for currency, (points, lag) in series.items():
        history.series[currency] = points
        history.publication_lag_days[currency] = lag
    return history


@pytest.mark.unit
def test_a_rate_is_not_readable_until_it_has_been_published():
    """A monthly series dated inside June is not known until August.

    Reading it by observation date would hand a run made in June the month's own
    average rate, which is the month's policy news before the month ended.
    """
    history = rates(JPY=([("2020-06-01", -0.05), ("2020-07-01", -0.07)], 60))
    assert history.rate_as_of("JPY", "2020-06-15") is None
    assert history.rate_as_of("JPY", "2020-07-31")[0] == pytest.approx(-0.05)
    assert history.rate_as_of("JPY", "2020-09-15")[0] == pytest.approx(-0.07)


@pytest.mark.unit
def test_a_daily_series_is_readable_almost_at_once():
    history = rates(USD=([("2020-06-01", 0.05), ("2020-06-02", 0.07)], 1))
    assert history.rate_as_of("USD", "2020-06-02")[0] == pytest.approx(0.05)
    assert history.rate_as_of("USD", "2020-06-03")[0] == pytest.approx(0.07)


@pytest.mark.unit
def test_the_latest_published_value_wins_not_the_first():
    history = rates(USD=([("2020-01-01", 1.0), ("2020-02-01", 2.0), ("2020-03-01", 3.0)], 0))
    assert history.rate_as_of("USD", "2020-02-15")[0] == pytest.approx(2.0)


@pytest.mark.unit
def test_a_date_before_the_series_starts_has_no_rate():
    history = rates(USD=([("2020-06-01", 1.0)], 0))
    assert history.rate_as_of("USD", "2019-01-01") is None


@pytest.mark.unit
def test_the_publication_lag_is_measured_from_the_series_latest_observation(monkeypatch):
    """How far behind the series runs now, not how old its first point is.

    Measured from the start of a twenty-year history the lag would be decades,
    and no observation would ever be readable.
    """
    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.fred.get_series_observations",
        lambda sid, start, end, vintage=None: [
            ("2000-01-01", 0.10), ("2010-01-01", 0.20), ("2020-10-01", 0.25),
        ],
    )
    history = bt.RateHistory(end="2020-12-01")
    history.load("USD")
    assert history.publication_lag_days["USD"] == 61   # 1 Oct to 1 Dec
    # And it must still be possible to read a rate, which it would not be if the
    # lag had been taken from the series' first observation instead.
    assert history.rate_as_of("USD", "2020-12-01")[0] == pytest.approx(0.25)


@pytest.mark.unit
def test_a_dead_rate_series_is_recorded_rather_than_raised(monkeypatch):
    def boom(sid, start, end, vintage=None):
        raise RuntimeError("FRED is down")

    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.fred.get_series_observations", boom)
    history = bt.RateHistory(end="2020-12-01")
    assert history.rate_as_of("USD", "2020-06-01") is None
    assert "FRED is down" in history.errors["USD"]


@pytest.mark.unit
def test_a_series_is_fetched_once_however_many_dates_ask_for_it(monkeypatch):
    """Eight currencies over three hundred dates must not be five thousand requests."""
    calls = []

    def counting(sid, start, end, vintage=None):
        calls.append(sid)
        return [("2019-01-01", 1.0)]

    monkeypatch.setattr(
        "tradingagents.dataflows.vendors.fred.get_series_observations", counting)
    history = bt.RateHistory(end="2020-12-01")
    for day in range(1, 29):
        history.rate_as_of("USD", f"2020-06-{day:02d}")
    assert len(calls) == 1


@pytest.mark.unit
def test_carry_applies_the_signals_own_rule_not_a_copy_of_it():
    """EUR at 2.50 against USD at 3.88 is a short, by carry.decide's own arithmetic."""
    history = rates(EUR=([("2020-01-01", 2.50)], 0), USD=([("2020-01-01", 3.88)], 0))
    result = bt.read_carry(history, "2020-06-01", "EURUSD=X")
    assert result.direction is Direction.SHORT
    assert result.value == pytest.approx(2.50 - 3.88)
    assert result.direction == carry.decide("EUR", "USD", 2.50, 3.88)[0]


@pytest.mark.unit
def test_carry_is_flat_inside_its_own_minimum_differential():
    narrow = carry.MIN_DIFFERENTIAL_PCT / 2
    history = rates(EUR=([("2020-01-01", 1.0 + narrow)], 0),
                    USD=([("2020-01-01", 1.0)], 0))
    assert bt.read_carry(history, "2020-06-01", "EURUSD=X").direction is Direction.FLAT


@pytest.mark.unit
def test_carry_refuses_a_pair_it_has_no_series_for():
    history = rates(USD=([("2020-01-01", 1.0)], 0))
    assert bt.read_carry(history, "2020-06-01", "USDINR=X").direction is Direction.UNAVAILABLE
    assert bt.read_carry(history, "2020-06-01", "GC=F").direction is Direction.UNAVAILABLE


@pytest.mark.unit
def test_carry_refuses_rather_than_guessing_when_a_leg_has_not_published():
    history = rates(EUR=([("2020-06-01", 2.5)], 60), USD=([("2020-06-01", 3.9)], 1))
    result = bt.read_carry(history, "2020-06-15", "EURUSD=X")
    assert result.direction is Direction.UNAVAILABLE
    assert "EUR" in result.unavailable_reason


@pytest.mark.unit
def test_carry_trades_a_stale_leg_and_says_so():
    """The live signal trades it with a loud caveat, so the backtest must too.

    Refusing those dates here would measure a stricter rule than the one running.
    """
    history = rates(EUR=([("2020-01-01", 2.5)], 0), USD=([("2020-06-01", 3.9)], 0))
    result = bt.read_carry(history, "2020-06-02", "EURUSD=X")
    assert result.direction is Direction.SHORT
    assert result.detail["stale_legs"] == 1
    assert result.caveats


@pytest.mark.unit
def test_carry_is_held_at_unit_exposure_because_it_does_not_size():
    history = rates(EUR=([("2020-01-01", 2.5)], 0), USD=([("2020-01-01", 3.9)], 0))
    assert bt._scalar_of(bt.read_carry(history, "2020-06-01", "EURUSD=X")) == 1.0


@pytest.mark.unit
def test_a_carry_walk_holds_the_direction_the_differential_implies():
    frame = rising(300)
    history = rates(EUR=([("2014-01-01", 2.5)], 0), USD=([("2014-01-01", 3.9)], 0))
    readings = bt.walk(frame, lambda f, a, t: bt.read_carry(history, a, t),
                       "EURUSD=X", every_n_days=50)
    assert readings and all(r.direction is Direction.SHORT for r in readings)
    exposure = bt.exposure_series(frame, readings, lag=1).dropna()
    assert (exposure == -1.0).all()


# ---------------------------------------------------------------------------
# What cannot be backtested
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_dollar_factor_refuses_with_a_reason():
    """It returns DIAGNOSTIC; there is no position, so there is no return."""
    result = bt.backtest_instrument("EURUSD=X", dollar_factor.NAME, frame=rising(600))
    assert result.net is None
    assert "DIAGNOSTIC" in result.error
    assert dollar_factor.NAME in bt.NOT_BACKTESTABLE


@pytest.mark.unit
def test_every_registered_signal_is_either_backtestable_or_excused():
    """A signal added later must not silently vanish from the results table."""
    from tradingagents.signals.registry import REGISTRY

    for signal in REGISTRY:
        assert signal.name in bt.READERS or signal.name in bt.NOT_BACKTESTABLE, (
            f"{signal.name} has no reader and no recorded reason for not having one"
        )


@pytest.mark.unit
def test_an_unknown_signal_name_is_an_error_not_an_empty_result():
    result = bt.backtest_instrument("X", "something_invented", frame=rising(300))
    assert "no reader registered" in result.error


# ---------------------------------------------------------------------------
# Pooling across instruments
# ---------------------------------------------------------------------------


def handmade(ticker, nets, assets=None, costs=None):
    """An InstrumentResult with a return stream chosen so the pooled answer is known."""
    n = len(nets)
    rows = pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="D"),
        "asset": list(assets) if assets else list(nets),
        "exposure": [1.0] * n,
        "turnover": [0.0] * n,
        "cost": list(costs) if costs else [0.0] * n,
        "gross": list(nets),
        "net": list(nets),
    })
    return bt.InstrumentResult(ticker=ticker, signal=tsmom.NAME, rows=rows,
                               net=bt.measure(rows, "net", periods=n))


@pytest.mark.unit
def test_pooling_averages_rather_than_adding():
    """Two instruments at +2%/+4% and 0%/+2% make a book at +1%/+3%, not +2%/+6%."""
    pooled = bt.pool([handmade("A", [0.02, 0.04]), handmade("B", [0.00, 0.02])], tsmom.NAME)
    one_day = (1.01 * 1.03) ** (261 / 2) - 1.0
    assert pooled.net.cagr == pytest.approx(one_day)
    assert pooled.net.days == 2


@pytest.mark.unit
def test_pooling_equal_weights_only_the_instruments_holding_that_day():
    """A book of one instrument is that instrument, not that instrument halved."""
    a = handmade("A", [0.01, 0.02, 0.03])
    b = handmade("B", [np.nan, np.nan, 0.05])
    pooled = bt.pool([a, b], tsmom.NAME)
    stream = (1.0 + pd.Series([0.01, 0.02, 0.04])).prod() ** (261 / 3) - 1.0
    assert pooled.net.cagr == pytest.approx(stream)


@pytest.mark.unit
def test_pooling_equal_weights_the_instruments_daily_returns():
    a = bt.backtest_instrument("A", tsmom.NAME, frame=rising(400), cost_bps=0.0)
    b = bt.backtest_instrument("B", tsmom.NAME, frame=rising(400, daily=-0.001), cost_bps=0.0)
    pooled = bt.pool([a, b], tsmom.NAME)
    assert pooled.net is not None
    assert len(pooled.usable) == 2
    # The book's worst day cannot be worse than the worse of the two instruments'.
    worst = min(a.rows["net"].min(), b.rows["net"].min())
    assert pooled.net.max_drawdown >= worst * 2


@pytest.mark.unit
def test_pooling_counts_how_many_instruments_were_positive():
    """The paper's claim was 'positive for all 58', not 'the best one worked'."""
    a = bt.backtest_instrument("A", tsmom.NAME, frame=rising(400), cost_bps=0.0)
    b = bt.backtest_instrument("B", tsmom.NAME, frame=rising(400), cost_bps=0.0)
    pooled = bt.pool([a, b], tsmom.NAME)
    assert pooled.positive_sharpe == sum(1 for i in pooled.usable if i.net.sharpe > 0)


@pytest.mark.unit
def test_pooling_survives_an_instrument_that_failed():
    good = bt.backtest_instrument("A", tsmom.NAME, frame=rising(400), cost_bps=0.0)
    bad = bt.InstrumentResult(ticker="B", signal=tsmom.NAME, error="no data")
    pooled = bt.pool([good, bad], tsmom.NAME)
    assert len(pooled.usable) == 1
    assert pooled.net is not None


@pytest.mark.unit
def test_pooling_nothing_usable_reports_nothing_rather_than_raising():
    pooled = bt.pool([bt.InstrumentResult(ticker="A", signal="s", error="no data")], "s")
    assert pooled.net is None
    assert pooled.positive_sharpe == 0


@pytest.mark.unit
def test_the_books_cost_is_averaged_from_the_instruments_own_costs():
    """Rederiving it from the book's mean exposure would net offsetting flips to zero."""
    a = bt.backtest_instrument("A", tsmom.NAME, frame=rising(400), cost_bps=100.0)
    b = bt.backtest_instrument("B", tsmom.NAME, frame=rising(400, daily=-0.001), cost_bps=100.0)
    pooled = bt.pool([a, b], tsmom.NAME)
    assert pooled.net.annual_cost_drag > 0


# ---------------------------------------------------------------------------
# Instrument selection, and the run entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_instrument_list_is_fixed_in_source():
    """Chosen before the results existed, which is the only time it can be."""
    assert "EURUSD=X" in bt.OUR_INSTRUMENTS
    assert "GC=F" in bt.OUR_INSTRUMENTS
    assert "BTC-USD" in bt.OUR_INSTRUMENTS
    assert len(set(bt.OUR_INSTRUMENTS)) == len(bt.OUR_INSTRUMENTS)


@pytest.mark.unit
def test_run_skips_instruments_the_signal_does_not_claim():
    """Carry has nothing to say about gold, and that is not a failure."""
    seen = []
    pooled = bt.run(carry.NAME, tickers=("EURUSD=X", "GC=F", "BTC-USD"),
                    frames={"EURUSD=X": pd.DataFrame()},
                    progress=lambda i, n, t: seen.append(t))
    assert seen == ["EURUSD=X"]
    assert [i.ticker for i in pooled.instruments] == ["EURUSD=X"]


@pytest.mark.unit
def test_run_applies_momentum_to_every_asset_class():
    seen = []
    bt.run(tsmom.NAME, tickers=("EURUSD=X", "GC=F", "BTC-USD"),
           frames={t: pd.DataFrame() for t in ("EURUSD=X", "GC=F", "BTC-USD")},
           progress=lambda i, n, t: seen.append(t))
    assert seen == ["EURUSD=X", "GC=F", "BTC-USD"]


@pytest.mark.unit
class TestTheHitRateCarriesItsDenominator:
    """A rate without its sample size is not a statistic.

    Carry flips direction so rarely that an instrument can have two holding
    periods over twenty-five years, and "0%" on a sample of two reads exactly
    like "0%" on a sample of two hundred. The live run printed hit rates of 0%,
    14% and 80% across pairs with no indication that each rested on a handful
    of stretches.
    """

    def test_the_count_is_the_number_of_held_stretches(self):
        rows = pd.DataFrame({
            "net": [0.01, 0.01, -0.01, -0.01, 0.02],
            "exposure": [1.0, 1.0, -1.0, -1.0, 1.0],
        })
        got = bt.measure(rows, periods=3)
        assert got.hit_periods_counted == 3
        assert got.hit_rate == pytest.approx(2 / 3)

    def test_flat_stretches_are_excluded_from_the_count_too(self):
        rows = pd.DataFrame({
            "net": [0.01, 0.0, 0.0, 0.02],
            "exposure": [1.0, 0.0, 0.0, 1.0],
        })
        assert bt.measure(rows, periods=3).hit_periods_counted == 2

    def test_a_two_stretch_sample_reports_two_not_a_bare_percentage(self):
        rows = pd.DataFrame({"net": [-0.01, -0.02], "exposure": [1.0, -1.0]})
        got = bt.measure(rows, periods=2)
        assert got.hit_rate == pytest.approx(0.0)
        assert got.hit_periods_counted == 2

    def test_no_hit_rate_means_no_count(self):
        rows = pd.DataFrame({"net": [0.01] * 10, "exposure": [1.0] * 10})
        assert bt.measure(rows, periods=5, hit_periods=False).hit_periods_counted == 0
