"""Scoring the instruction the system actually issues.

Settlement scores a rating held for a fixed window. The system issues an entry,
a stop and a size. These tests pin the difference, and in particular pin the
direction every assumption leans: each one is meant to be pessimistic, so a test
that passes because the code was generous to the trade is a test that has failed
at its job.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.trade_plan import (
    HORIZON,
    NO_FILL,
    STOPPED,
    TradePlan,
    parse_plan,
    score_plan,
)


def bars(rows) -> pd.DataFrame:
    """OHLC bars from (date, open, high, low, close) tuples."""
    return pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close"])


def flat(n, price=1.12, start=7):
    return bars([(f"2026-10-{start + i:02d}", price, price, price, price) for i in range(n)])


def rendered(**kw) -> str:
    base = dict(rating="Underweight", executive_summary="s", investment_thesis="t")
    return render_pm_decision(PortfolioDecision(**{**base, **kw}))


SHORT = TradePlan(direction=-1, entry=1.13, stop=1.153, risk_fraction=0.0135)
LONG = TradePlan(direction=1, entry=1.13, stop=1.107, risk_fraction=0.0135)


# ---------------------------------------------------------------------------
# Parsing the instruction out of a real rendered decision
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_instruction_round_trips_through_the_render():
    """The render is the parsing contract; the two must not drift apart."""
    plan = parse_plan(rendered(entry_price=1.13, stop_loss=1.153, risk_percent=1.35))
    assert plan.direction == -1
    assert plan.entry == pytest.approx(1.13)
    assert plan.stop == pytest.approx(1.153)
    assert plan.risk_fraction == pytest.approx(0.0135)
    assert plan.executable
    assert plan.missing == ()


@pytest.mark.unit
def test_risk_percent_is_read_as_percent_not_as_a_fraction():
    """1.35 means 1.35% of portfolio. Read as 135% it would size every trade to ruin."""
    plan = parse_plan(rendered(entry_price=1.13, stop_loss=1.153, risk_percent=1.35))
    assert plan.risk_fraction == pytest.approx(0.0135)


@pytest.mark.unit
@pytest.mark.parametrize("rating,direction", [
    ("Buy", 1), ("Overweight", 1), ("Hold", 0), ("Underweight", -1), ("Sell", -1),
])
def test_every_rating_maps_to_its_side(rating, direction):
    plan = parse_plan(rendered(rating=rating, entry_price=1.13,
                               stop_loss=1.107 if direction > 0 else 1.153,
                               risk_percent=1.0))
    assert plan.direction == direction


@pytest.mark.unit
def test_a_hold_instructs_nothing_and_is_not_missing_its_levels():
    """Absent levels on a Hold are correct, not an incomplete instruction."""
    plan = parse_plan(rendered(rating="Hold"))
    assert plan.direction == 0
    assert plan.is_trade is False
    assert plan.missing == ()


@pytest.mark.unit
def test_an_unreadable_rating_is_not_treated_as_a_hold():
    """A decision nobody can read is not a decision to do nothing."""
    plan = parse_plan("the market looks interesting", rating=None)
    assert plan.invalid_reason and "no readable rating" in plan.invalid_reason
    assert plan.is_trade is False


@pytest.mark.unit
def test_a_missing_level_is_named_rather_than_defaulted():
    """A plan with no stop and a plan with a stop are different trades."""
    plan = parse_plan(rendered(entry_price=1.13))
    assert "stop" in plan.missing
    assert "risk_percent" in plan.missing
    assert plan.executable is False


@pytest.mark.unit
def test_not_provided_reads_as_absent_rather_than_as_a_number():
    plan = parse_plan(rendered(entry_price=1.13, stop_loss=1.153))
    assert plan.risk_fraction is None
    assert "risk_percent" in plan.missing


@pytest.mark.unit
def test_a_thousands_separator_does_not_become_a_different_price():
    """An instrument quoted at 24,310 must not parse as 24."""
    plan = parse_plan("**Rating**: Buy\n\n**Entry Price**: 24,310.5\n\n"
                      "**Stop Loss**: 23,900\n\n**Risk Percent**: 1.0")
    assert plan.entry == pytest.approx(24310.5)
    assert plan.stop == pytest.approx(23900.0)


@pytest.mark.unit
def test_markdown_emphasis_around_the_value_is_tolerated():
    plan = parse_plan("**Rating**: Sell\n\n**Entry Price**: **1.13**\n\n"
                      "**Stop Loss**: *1.153*\n\n**Risk Percent**: 1.0")
    assert plan.entry == pytest.approx(1.13)
    assert plan.stop == pytest.approx(1.153)


# ---------------------------------------------------------------------------
# Plans whose own numbers contradict them
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_stop_on_the_wrong_side_is_refused_rather_than_scored():
    """It instructs closing at a profit and holding a loss forever.

    Scoring it would produce a number, and the number would be meaningless.
    """
    plan = parse_plan(rendered(rating="Sell", entry_price=1.13, stop_loss=1.10,
                               risk_percent=1.0))
    assert plan.invalid_reason and "wrong side" in plan.invalid_reason
    assert plan.is_trade is False


@pytest.mark.unit
def test_a_long_stop_above_entry_is_also_refused():
    plan = parse_plan(rendered(rating="Buy", entry_price=1.13, stop_loss=1.16,
                               risk_percent=1.0))
    assert plan.invalid_reason and "wrong side" in plan.invalid_reason


@pytest.mark.unit
def test_an_entry_equal_to_the_stop_risks_nothing_and_is_refused():
    plan = parse_plan(rendered(entry_price=1.13, stop_loss=1.13, risk_percent=1.0))
    assert plan.invalid_reason and "risks nothing" in plan.invalid_reason


@pytest.mark.unit
def test_an_impossible_risk_percentage_is_refused():
    plan = parse_plan(rendered(entry_price=1.13, stop_loss=1.153, risk_percent=250.0))
    assert plan.invalid_reason and "not usable" in plan.invalid_reason


@pytest.mark.unit
def test_a_negative_price_is_refused():
    plan = parse_plan(rendered(entry_price=-1.13, stop_loss=1.153, risk_percent=1.0))
    assert plan.invalid_reason and "not a positive price" in plan.invalid_reason


# ---------------------------------------------------------------------------
# The definition of R
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_stopped_trade_is_exactly_minus_one_r():
    """This is the definition of the unit. If it drifts, nothing else means anything."""
    moved = bars([("2026-10-07", 1.1240, 1.1310, 1.1235, 1.1305),
                  ("2026-10-08", 1.1305, 1.1560, 1.1300, 1.1550)])
    outcome = score_plan(SHORT, moved, horizon=5)
    assert outcome.outcome == STOPPED
    assert outcome.r_multiple == pytest.approx(-1.0)


@pytest.mark.unit
def test_a_stopped_long_is_also_exactly_minus_one_r():
    moved = bars([("2026-10-07", 1.1310, 1.1320, 1.1290, 1.1300),
                  ("2026-10-08", 1.1300, 1.1305, 1.1060, 1.1070)])
    outcome = score_plan(LONG, moved, horizon=5)
    assert outcome.outcome == STOPPED
    assert outcome.r_multiple == pytest.approx(-1.0)


@pytest.mark.unit
def test_the_portfolio_loss_on_a_stop_equals_the_stated_risk():
    """'Risk 1.35% of portfolio' has to mean exactly that when the stop is hit."""
    moved = bars([("2026-10-07", 1.1240, 1.1310, 1.1235, 1.1305),
                  ("2026-10-08", 1.1305, 1.1560, 1.1300, 1.1550)])
    outcome = score_plan(SHORT, moved, horizon=5)
    assert outcome.portfolio_return == pytest.approx(-0.0135)


@pytest.mark.unit
def test_r_scales_with_the_move_not_with_the_instrument():
    """Two instruments at different price levels, same move in risk units."""
    cheap = TradePlan(direction=1, entry=1.00, stop=0.90)
    dear = TradePlan(direction=1, entry=1000.0, stop=900.0)
    cheap_bars = bars([("2026-10-07", 1.00, 1.20, 0.99, 1.20)] + [("2026-10-08", 1.20, 1.20, 1.20, 1.20)])
    dear_bars = bars([("2026-10-07", 1000.0, 1200.0, 990.0, 1200.0)] + [("2026-10-08", 1200.0, 1200.0, 1200.0, 1200.0)])
    assert score_plan(cheap, cheap_bars, horizon=1).r_multiple == pytest.approx(
        score_plan(dear, dear_bars, horizon=1).r_multiple)


@pytest.mark.unit
def test_portfolio_return_is_r_times_the_risk_fraction():
    moved = bars([("2026-10-07", 1.1300, 1.1310, 1.1290, 1.1300)]
                 + [("2026-10-08", 1.1200, 1.1210, 1.1150, 1.1185)])
    outcome = score_plan(SHORT, moved, horizon=1)
    assert outcome.portfolio_return == pytest.approx(outcome.r_multiple * 0.0135)


# ---------------------------------------------------------------------------
# Which way every assumption leans
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_gap_through_the_stop_exits_at_the_open_not_at_the_stop():
    """You do not get the stop price when the market gaps past it overnight.

    This loses MORE than 1R, which is the honest answer and the unflattering one.
    """
    gapped = bars([("2026-10-07", 1.1240, 1.1310, 1.1235, 1.1305),
                   ("2026-10-08", 1.1700, 1.1750, 1.1690, 1.1740)])
    outcome = score_plan(SHORT, gapped, horizon=5)
    assert outcome.outcome == STOPPED
    assert outcome.exit_price == pytest.approx(1.1700)
    assert outcome.r_multiple < -1.0


@pytest.mark.unit
def test_a_gap_through_the_entry_fills_at_the_open():
    """A better price than asked for. It favours the trade, and it is what happened."""
    gapped = bars([("2026-10-07", 1.1400, 1.1420, 1.1380, 1.1390)]
                  + [("2026-10-08", 1.1390, 1.1395, 1.1380, 1.1385)])
    outcome = score_plan(SHORT, gapped, horizon=1)
    assert outcome.fill_price == pytest.approx(1.1400)


@pytest.mark.unit
def test_a_stop_reached_on_the_fill_bar_is_assumed_hit():
    """A daily bar does not say whether its high or its low came first.

    The order that loses money is assumed, because the alternative is a backtest
    that quietly wins every ambiguous bar.
    """
    wide = bars([("2026-10-07", 1.1240, 1.1600, 1.1200, 1.1250)]
                + [("2026-10-08", 1.1250, 1.1260, 1.1200, 1.1210)])
    outcome = score_plan(SHORT, wide, horizon=5)
    assert outcome.outcome == STOPPED
    assert outcome.r_multiple == pytest.approx(-1.0)


@pytest.mark.unit
def test_trading_starts_on_the_bars_the_caller_supplies():
    """The caller slices off the decision date, so bar zero is already the next day.

    A decision formed from a close cannot also be executed at that close.
    """
    outcome = score_plan(SHORT, flat(6, price=1.13), horizon=3)
    assert outcome.fill_date == "2026-10-07"


# ---------------------------------------------------------------------------
# A plan the market never came back for
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_unfilled_plan_is_not_a_flat_trade():
    """Counting it as zero return credits the system for a trade it never got.

    A strategy where most plans never execute is a different strategy from one
    that executes, and the two must not average together.
    """
    away = flat(7, price=1.1200)
    outcome = score_plan(SHORT, away, horizon=5)
    assert outcome.outcome == NO_FILL
    assert outcome.r_multiple is None
    assert outcome.portfolio_return is None
    assert outcome.filled is False
    assert outcome.scored is True      # a real answer, just not a trade


@pytest.mark.unit
def test_the_fill_window_closes():
    """A level reached only after the window has passed is still a no-fill."""
    late = bars([(f"2026-10-{7 + i:02d}", 1.12, 1.1250, 1.1180, 1.12) for i in range(5)]
                + [("2026-10-12", 1.12, 1.1400, 1.1180, 1.1350)]
                + [(f"2026-10-{13 + i:02d}", 1.13, 1.1340, 1.1280, 1.13) for i in range(6)])
    assert score_plan(SHORT, late, fill_window=5, horizon=5).outcome == NO_FILL
    assert score_plan(SHORT, late, fill_window=10, horizon=5).filled


@pytest.mark.unit
def test_a_long_fills_on_a_dip_to_its_level():
    dip = bars([("2026-10-07", 1.1350, 1.1360, 1.1280, 1.1300)]
               + [("2026-10-08", 1.1300, 1.1320, 1.1290, 1.1310)])
    outcome = score_plan(LONG, dip, horizon=1)
    assert outcome.filled
    assert outcome.fill_price == pytest.approx(1.13)


# ---------------------------------------------------------------------------
# Horizon exits and unfinished windows
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_position_that_never_stops_closes_at_the_horizon():
    drift = bars([("2026-10-07", 1.1300, 1.1310, 1.1290, 1.1300)]
                 + [(f"2026-10-{8 + i:02d}", 1.125, 1.1260, 1.1200, 1.1210)
                    for i in range(5)])
    outcome = score_plan(SHORT, drift, horizon=3)
    assert outcome.outcome == HORIZON
    assert outcome.r_multiple > 0


@pytest.mark.unit
def test_an_unfinished_holding_period_is_unavailable_rather_than_scored_early():
    """Settling on a partial window reports a return nobody has earned yet."""
    short_history = bars([("2026-10-07", 1.1300, 1.1310, 1.1290, 1.1300),
                          ("2026-10-08", 1.1280, 1.1290, 1.1270, 1.1275)])
    outcome = score_plan(SHORT, short_history, horizon=21)
    assert outcome.scored is False
    assert "not finished trading" in outcome.unavailable_reason


@pytest.mark.unit
def test_a_stop_hit_inside_an_unfinished_window_still_scores():
    """The outcome is known the moment the stop goes, horizon or no horizon."""
    stopped = bars([("2026-10-07", 1.1300, 1.1310, 1.1290, 1.1300),
                    ("2026-10-08", 1.1400, 1.1600, 1.1390, 1.1550)])
    outcome = score_plan(SHORT, stopped, horizon=21)
    assert outcome.outcome == STOPPED
    assert outcome.scored


# ---------------------------------------------------------------------------
# Market entry, for isolating the value of the agents' entry timing
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_market_entry_ignores_the_level_but_keeps_the_stop():
    away = flat(7, price=1.1200)
    limit = score_plan(SHORT, away, horizon=5)
    market = score_plan(SHORT, away, horizon=5, market_entry=True)
    assert limit.outcome == NO_FILL
    assert market.filled
    assert market.fill_price == pytest.approx(1.1200)


@pytest.mark.unit
def test_market_entry_still_respects_the_stop():
    rally = bars([("2026-10-07", 1.1200, 1.1250, 1.1180, 1.1240),
                  ("2026-10-08", 1.1240, 1.1600, 1.1230, 1.1580)])
    outcome = score_plan(SHORT, rally, horizon=5, market_entry=True)
    assert outcome.outcome == STOPPED


@pytest.mark.unit
def test_market_entry_enters_on_the_first_available_bar():
    outcome = score_plan(SHORT, flat(6, price=1.1200), horizon=3, market_entry=True)
    assert outcome.fill_date == "2026-10-07"


# ---------------------------------------------------------------------------
# Refusals rather than invented numbers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_incomplete_instruction_is_refused_with_its_reason():
    plan = parse_plan(rendered(entry_price=1.13))
    outcome = score_plan(plan, flat(10, price=1.13))
    assert outcome.scored is False
    assert "stop" in outcome.unavailable_reason


@pytest.mark.unit
def test_a_hold_is_refused_rather_than_scored_as_a_flat_return():
    outcome = score_plan(parse_plan(rendered(rating="Hold")), flat(10))
    assert outcome.scored is False
    assert "no trade" in outcome.unavailable_reason


@pytest.mark.unit
def test_bars_without_ohlc_are_refused():
    closes = pd.DataFrame({"Date": ["2026-10-07"], "Close": [1.13]})
    assert score_plan(SHORT, closes).scored is False


@pytest.mark.unit
def test_empty_bars_are_refused():
    assert score_plan(SHORT, pd.DataFrame()).scored is False
    assert score_plan(SHORT, None).scored is False


@pytest.mark.unit
def test_rows_with_missing_prices_are_dropped_rather_than_scored():
    holey = bars([("2026-10-07", None, None, None, None),
                  ("2026-10-08", 1.1300, 1.1310, 1.1290, 1.1300),
                  ("2026-10-09", 1.1200, 1.1260, 1.1150, 1.1180)])
    outcome = score_plan(SHORT, holey, horizon=1)
    assert outcome.filled
    assert outcome.fill_date == "2026-10-08"


@pytest.mark.unit
class TestNoFillIsNotTheSameAsUnmeasurable:
    """One is a finding about the market, the other is the absence of a finding.

    The signal layer learned this with FLAT against UNAVAILABLE; the first
    version of this module repeated the mistake by giving an unmeasurable
    outcome the no-fill label, so a still-running holding period read as a
    level the market never reached.
    """

    def test_a_running_holding_period_is_unavailable_not_a_no_fill(self):
        from tradingagents.trade_plan import UNAVAILABLE

        running = bars([("2026-10-07", 1.1300, 1.1310, 1.1290, 1.1300),
                        ("2026-10-08", 1.1280, 1.1290, 1.1270, 1.1275)])
        outcome = score_plan(SHORT, running, horizon=21)
        assert outcome.outcome == UNAVAILABLE
        assert outcome.outcome != NO_FILL

    def test_a_genuine_no_fill_claims_no_reason(self):
        outcome = score_plan(SHORT, flat(7, price=1.1200), horizon=5)
        assert outcome.outcome == NO_FILL
        assert outcome.unavailable_reason is None
        assert outcome.scored

    def test_an_outcome_cannot_claim_both_or_neither(self):
        from tradingagents.trade_plan import TradeOutcome, UNAVAILABLE

        with pytest.raises(ValueError, match="must say why"):
            TradeOutcome(outcome=UNAVAILABLE)
        with pytest.raises(ValueError, match="must say why"):
            TradeOutcome(outcome=NO_FILL, unavailable_reason="but also fine")


@pytest.mark.unit
class TestTheLongSideLeansTheSameWay:
    """Every pessimistic assumption has two branches, and both must hold.

    The short path was tested first and the long path was not, so a mutation
    that paid the stop price on a long gap survived the suite. Symmetry is not
    something to assume in code that decides which side of a fill you get.
    """

    def test_a_long_gapping_through_its_stop_exits_at_the_open(self):
        """Below the stop overnight: you get the open, and lose more than 1R."""
        gapped = bars([("2026-10-07", 1.1310, 1.1320, 1.1290, 1.1300),
                       ("2026-10-08", 1.0900, 1.0950, 1.0880, 1.0920)])
        outcome = score_plan(LONG, gapped, horizon=5)
        assert outcome.outcome == STOPPED
        assert outcome.exit_price == pytest.approx(1.0900)
        assert outcome.r_multiple < -1.0

    def test_a_long_gapping_through_its_entry_fills_at_the_open(self):
        """A better price than the limit, which is what would really have happened."""
        gapped = bars([("2026-10-07", 1.1100, 1.1150, 1.1080, 1.1120)]
                      + [("2026-10-08", 1.1120, 1.1130, 1.1110, 1.1125)])
        outcome = score_plan(LONG, gapped, horizon=1)
        assert outcome.fill_price == pytest.approx(1.1100)
        assert outcome.fill_price < LONG.entry

    def test_a_long_does_not_fill_on_a_rally_away_from_its_limit(self):
        """A limit buy below the market is not filled by price running up."""
        rally = bars([(f"2026-10-{7 + i:02d}", 1.1400, 1.1500, 1.1380, 1.1450)
                      for i in range(6)])
        assert score_plan(LONG, rally, horizon=3).outcome == NO_FILL

    def test_a_long_stop_reached_on_the_fill_bar_is_assumed_hit(self):
        wide = bars([("2026-10-07", 1.1350, 1.1360, 1.1000, 1.1340)]
                    + [("2026-10-08", 1.1340, 1.1350, 1.1300, 1.1330)])
        outcome = score_plan(LONG, wide, horizon=5)
        assert outcome.outcome == STOPPED
        assert outcome.r_multiple == pytest.approx(-1.0)
