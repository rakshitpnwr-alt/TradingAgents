"""The research-derived signal layer.

The layer exists to anchor a pipeline that produced three different verdicts for
one ticker on one date. So the property that matters most is not that any signal
is profitable -- that is what the backtest is for -- but that it is
*deterministic*, that it never fabricates a reading when it cannot compute one,
and that a diagnostic can never masquerade as a trade.
"""

import numpy as np
import pandas as pd
import pytest

from tradingagents.signals import dollar_factor, tsmom
from tradingagents.signals.base import Direction, Signal, SignalResult, run_signal
from tradingagents.signals.registry import (
    REGISTRY,
    build_signal_block,
    consensus_direction,
    evaluate,
    signals_for,
)


def _series(start, end, periods=300):
    idx = pd.bdate_range("2025-09-01", periods=periods)
    return pd.Series(np.linspace(start, end, periods), index=idx)


# ---------------------------------------------------------------------------
# The type refuses to lose the difference between "no edge" and "could not run"
# ---------------------------------------------------------------------------


def test_an_unavailable_signal_must_say_why():
    """An unexplained absence reads downstream as a neutral reading."""
    with pytest.raises(ValueError, match="must say why"):
        SignalResult(name="x", direction=Direction.UNAVAILABLE)


def test_the_three_kinds_of_no_trade_stay_distinct():
    flat = SignalResult("x", Direction.FLAT)
    diagnostic = SignalResult("x", Direction.DIAGNOSTIC)
    missing = SignalResult.unavailable("x", "no data")

    assert flat.available and flat.sets_direction          # ran, found no edge
    assert diagnostic.available and not diagnostic.sets_direction  # ran, informs only
    assert not missing.available and not missing.sets_direction    # did not run


def test_a_signal_that_raises_becomes_an_explicit_unavailable():
    """A research hypothesis wired to live vendor data fails in ways that are not
    enumerable; what must never happen is the run dying, or the failure being
    smoothed into a neutral reading."""
    def boom(ticker, as_of_date):
        raise RuntimeError("vendor exploded")

    broken = Signal(
        name="broken", asset_types=frozenset({"forex"}), paper="", mechanism="",
        definition="", deviations=(), fails_when=(), compute=boom,
    )
    result = run_signal(broken, "EURUSD=X", "2026-09-30")
    assert result.direction is Direction.UNAVAILABLE
    assert "vendor exploded" in result.unavailable_reason


def test_a_signal_returning_the_wrong_type_is_rejected():
    stray = Signal(
        name="stray", asset_types=frozenset({"forex"}), paper="", mechanism="",
        definition="", deviations=(), fails_when=(), compute=lambda t, d: "long",
    )
    assert run_signal(stray, "EURUSD=X", "2026-09-30").direction is Direction.UNAVAILABLE


def test_a_signal_with_no_implementation_does_not_vote():
    bare = Signal(
        name="bare", asset_types=frozenset({"forex"}), paper="", mechanism="",
        definition="", deviations=(), fails_when=(),
    )
    assert run_signal(bare, "EURUSD=X", "2026-09-30").direction is Direction.UNAVAILABLE


# ---------------------------------------------------------------------------
# Time-series momentum (Moskowitz, Ooi and Pedersen 2012)
# ---------------------------------------------------------------------------


def test_momentum_direction_follows_the_trailing_year():
    assert tsmom.compute_from_closes(_series(100, 130)).direction is Direction.LONG
    assert tsmom.compute_from_closes(_series(130, 100)).direction is Direction.SHORT


def test_a_round_trip_year_is_flat_not_long():
    """sign() would call a zero trailing return long, inventing a position from
    a tie. The series has to oscillate, because a dead-flat one is refused
    earlier for having no volatility to size against."""
    # Exactly one lookback window long, so the trailing return spans the whole
    # series, and a full sine cycle within it: real daily variation that ends
    # exactly where it began.
    n = tsmom.LOOKBACK_TRADING_DAYS + 1
    idx = pd.bdate_range("2025-09-01", periods=n)
    closes = pd.Series(100 + 5 * np.sin(np.linspace(0, 2 * np.pi, n)), index=idx)
    result = tsmom.compute_from_closes(closes)
    assert result.value == pytest.approx(0.0, abs=1e-9)
    assert result.direction is Direction.FLAT


def test_a_motionless_series_is_refused_before_it_is_sized():
    """Zero volatility divides into an infinite position, so the refusal has to
    come before the direction is read -- there is nothing to size."""
    flat = pd.Series([100.0] * 300, index=pd.bdate_range("2025-09-01", periods=300))
    result = tsmom.compute_from_closes(flat)
    assert result.direction is Direction.UNAVAILABLE
    assert "volatility" in result.unavailable_reason


def test_momentum_is_deterministic():
    """The reason this layer exists: ask twice, get the same answer."""
    closes = _series(100, 130)
    first = tsmom.compute_from_closes(closes)
    second = tsmom.compute_from_closes(closes)
    assert (first.direction, first.value) == (second.direction, second.value)


def test_momentum_refuses_without_enough_history():
    result = tsmom.compute_from_closes(_series(100, 130, periods=50))
    assert result.direction is Direction.UNAVAILABLE
    assert "12-month" in result.unavailable_reason


@pytest.mark.parametrize("closes", [
    pd.Series([], dtype=float),
    pd.Series([float("nan")] * 300),
    pd.Series([0.0] * 300),          # non-positive prices are not prices
    pd.Series([-5.0] * 300),
])
def test_momentum_never_raises_on_degenerate_prices(closes):
    assert tsmom.compute_from_closes(closes).direction is Direction.UNAVAILABLE


def test_volatility_estimator_matches_the_papers_shape():
    """60-day center of mass, annualised by sqrt(261) -- the paper's constants,
    kept rather than re-tuned on our much shorter sample."""
    rng = np.random.default_rng(0)
    daily = pd.Series(rng.normal(0, 0.01, 1000))
    vol = tsmom.ex_ante_volatility(daily)
    # 1% daily vol annualises to roughly 16%.
    assert 0.12 < vol < 0.20


def test_volatility_returns_none_rather_than_zero():
    """A zero volatility divides into an infinite position."""
    assert tsmom.ex_ante_volatility(pd.Series([0.0] * 100)) is None
    assert tsmom.ex_ante_volatility(pd.Series([0.01])) is None


def test_position_scalar_is_capped():
    """The paper's 40% target assumes 58 diversified positions; taken literally
    on one instrument with suppressed volatility, 1/sigma explodes."""
    assert tsmom.position_scalar(0.0001) == tsmom.MAX_POSITION_SCALAR
    assert tsmom.position_scalar(0.0) == 0.0
    assert tsmom.position_scalar(0.20) == pytest.approx(0.5)


def test_a_near_zero_trend_is_flagged_as_weak():
    gentle = _series(100.0, 100.5)
    result = tsmom.compute_from_closes(gentle)
    assert any("coin flip" in c for c in result.caveats)


def test_momentum_records_its_departures_from_the_paper():
    """Every deviation is a place the published evidence may not transfer, so
    the entry has to carry them rather than imply the paper's result is ours."""
    deviations = " ".join(tsmom.SIGNAL.deviations).lower()
    assert "interest differential" in deviations   # spot vs excess returns
    assert "40%" in deviations                      # the volatility target
    assert "monthly" in deviations                  # the rebalance
    assert tsmom.SIGNAL.fails_when                  # and when it is known to fail


# ---------------------------------------------------------------------------
# The dollar factor (Lustig, Roussanov and Verdelhan 2011)
# ---------------------------------------------------------------------------


def test_the_foreign_leg_is_oriented_correctly():
    """The sign trap: USDJPY rising means the DOLLAR rose and the yen fell. Get
    this backwards and half the basket inverts, producing a dollar factor near
    zero on every date -- which looks like a calm market, not a bug."""
    eur = dollar_factor.foreign_leg_series("EURUSD=X", pd.Series([1.10, 1.20]))
    assert eur.pct_change().iloc[-1] > 0        # euro stronger

    jpy = dollar_factor.foreign_leg_series("USDJPY=X", pd.Series([150.0, 160.0]))
    assert jpy.pct_change().iloc[-1] < 0        # yen weaker, not stronger


def test_a_cross_with_no_dollar_leg_is_excluded():
    assert dollar_factor.foreign_leg_series("EURGBP=X", pd.Series([0.85, 0.86])) is None
    assert dollar_factor.foreign_leg_series("NONSENSE", pd.Series([1.0, 2.0])) is None


def test_a_broad_dollar_move_leaves_no_residual():
    broad = dict.fromkeys(dollar_factor.BASKET, -0.03)
    detail = dollar_factor.decompose(broad, "EURUSD=X").detail
    assert detail["dollar_factor"] == pytest.approx(-0.03)
    assert detail["residual"] == pytest.approx(0.0, abs=1e-9)
    assert detail["attribution"] == "mostly a dollar move"


def test_a_single_currency_move_shows_up_as_residual():
    """This is the test the agents improvised by eyeballing sterling."""
    specific = dict.fromkeys(dollar_factor.BASKET, 0.0)
    specific["EURUSD=X"] = -0.03
    detail = dollar_factor.decompose(specific, "EURUSD=X").detail
    assert detail["residual"] < detail["dollar_factor"]
    assert detail["attribution"] == "mostly currency-specific"


def test_the_decomposition_never_claims_a_direction():
    """It says which leg moved, not which way the pair goes next. A diagnostic
    allowed to vote would let an explanation masquerade as a trade."""
    broad = dict.fromkeys(dollar_factor.BASKET, -0.03)
    result = dollar_factor.decompose(broad, "EURUSD=X")
    assert result.direction is Direction.DIAGNOSTIC
    assert not result.sets_direction


def test_a_thin_basket_refuses_to_be_a_market():
    thin = {"EURUSD=X": -0.03, "GBPUSD=X": -0.02}
    result = dollar_factor.decompose(thin, "EURUSD=X")
    assert result.direction is Direction.UNAVAILABLE
    assert "basket" in result.unavailable_reason


def test_a_pair_outside_the_basket_is_refused_not_guessed():
    broad = dict.fromkeys(dollar_factor.BASKET, -0.03)
    result = dollar_factor.decompose(broad, "EURGBP=X")
    assert result.direction is Direction.UNAVAILABLE
    assert "dollar" in result.unavailable_reason


# ---------------------------------------------------------------------------
# Registry and the hierarchy the user chose: signal decides, agents size
# ---------------------------------------------------------------------------


def test_signals_are_scoped_to_asset_types_they_were_tested_on():
    assert dollar_factor.SIGNAL in signals_for("forex")
    assert dollar_factor.SIGNAL not in signals_for("commodity")  # no dollar leg
    assert tsmom.SIGNAL in signals_for("commodity")              # 24 commodities in the paper


def test_diagnostics_do_not_vote():
    only_diagnostic = [SignalResult("d", Direction.DIAGNOSTIC)]
    direction, why = consensus_direction(only_diagnostic)
    assert direction is Direction.UNAVAILABLE
    assert "no directional signal" in why


def test_disagreement_resolves_to_flat_not_to_a_tie_break():
    """When two research rules point opposite ways, neither has a claim on the
    position. Inventing a winner manufactures conviction the evidence lacks."""
    results = [SignalResult("a", Direction.LONG), SignalResult("b", Direction.SHORT)]
    direction, why = consensus_direction(results)
    assert direction is Direction.FLAT
    assert "disagree" in why


def test_agreement_carries_through():
    results = [SignalResult("a", Direction.LONG), SignalResult("b", Direction.LONG)]
    assert consensus_direction(results)[0] is Direction.LONG


def test_one_view_plus_one_flat_keeps_the_view():
    results = [SignalResult("a", Direction.SHORT), SignalResult("b", Direction.FLAT)]
    assert consensus_direction(results)[0] is Direction.SHORT


def test_every_registered_signal_carries_its_provenance():
    """A number with no provenance is a guess with a decimal point."""
    for signal in REGISTRY:
        assert signal.paper and signal.mechanism and signal.definition
        assert signal.fails_when, f"{signal.name} claims no failure conditions"
        assert signal.asset_types
        rendered = signal.provenance()
        assert "Source:" in rendered and "Mechanism:" in rendered


def test_the_agent_block_tells_them_not_to_pick_a_direction(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.signals.registry.evaluate",
        lambda *a, **k: [SignalResult("time_series_momentum", Direction.SHORT, value=-0.1)],
    )
    block = build_signal_block("EURUSD=X", "2026-09-30", "forex")
    assert "SHORT" in block
    assert "Your task is NOT to pick a direction" in block
    assert "conviction and size" in block
    # and it must not pretend a disagreement is a mandate
    assert "do not quietly trade against it" in block


def test_the_block_says_plainly_when_nothing_applies(monkeypatch):
    block = build_signal_block("AAPL", "2026-09-30", "nonexistent-type")
    assert "No research-derived signal" in block
    # An absent signal is weaker evidence, and must not read as a clean slate.
    assert "weaker evidence" in block


def test_an_unavailable_signal_is_reported_with_its_reason(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.signals.registry.evaluate",
        lambda *a, **k: [SignalResult.unavailable("time_series_momentum", "only 40 rows")],
    )
    block = build_signal_block("EURUSD=X", "2026-09-30", "forex")
    assert "not available" in block and "only 40 rows" in block


def test_evaluate_never_raises_whatever_the_ticker(monkeypatch):
    """It runs before the analysts; an exception here kills the whole run."""
    monkeypatch.setattr(
        "tradingagents.signals.tsmom.compute",
        lambda t, d: (_ for _ in ()).throw(ValueError("bad ticker")),
    )
    monkeypatch.setattr(
        "tradingagents.signals.dollar_factor.compute",
        lambda t, d: (_ for _ in ()).throw(ValueError("bad ticker")),
    )
    # The registry holds the Signal objects, whose compute is bound at import;
    # rebuild the two entries so the patched functions are the ones called.
    from dataclasses import replace

    import tradingagents.signals.registry as reg
    patched = tuple(
        replace(s, compute=(lambda t, d: (_ for _ in ()).throw(ValueError("bad"))))
        for s in reg.REGISTRY
    )
    monkeypatch.setattr(reg, "REGISTRY", patched)
    results = evaluate("???", "not-a-date", "forex")
    assert results and all(r.direction is Direction.UNAVAILABLE for r in results)


# ---------------------------------------------------------------------------
# The wiring: a signal that never reaches an agent is a signal that does nothing
# ---------------------------------------------------------------------------


def test_the_signal_block_reaches_the_state_accessor():
    from tradingagents.agents.context import get_signal_block_from_state

    assert "SHORT" in get_signal_block_from_state({"signal_block": "SHORT xyz"})


def test_a_missing_signal_block_is_not_presented_as_a_clean_slate():
    """A run with no research anchor is the configuration that produced three
    verdicts for one ticker on one date. The agent deciding should know that."""
    from tradingagents.agents.context import get_signal_block_from_state

    for empty in ({}, {"signal_block": ""}, {"signal_block": "   "}, {"signal_block": None}):
        block = get_signal_block_from_state(empty)
        assert "No deterministic research signal" in block
        assert "less reliable" in block


@pytest.mark.parametrize("module_path", [
    "tradingagents/agents/managers/research_manager.py",
    "tradingagents/agents/managers/portfolio_manager.py",
    "tradingagents/agents/trader/trader.py",
])
def test_each_decision_agent_both_reads_and_renders_the_signal(module_path):
    """Binding the variable but never interpolating it is a silent no-op: the
    signal would be computed every run and seen by nobody."""
    source = open(module_path).read()
    assert "get_signal_block_from_state(state)" in source, "never read"
    assert "{signal_block}" in source, "read but never rendered into the prompt"
    assert "conviction and size, not direction" in source, "hierarchy not stated"


def test_the_run_state_carries_a_signal_block_field():
    from tradingagents.graph.propagation import Propagator

    state = Propagator().create_initial_state(
        "EURUSD=X", "2026-09-30", signal_block="## Research signals\n\nLONG"
    )
    assert state["signal_block"].endswith("LONG")
