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
    with open(module_path) as handle:
        source = handle.read()
    assert "get_signal_block_from_state(state)" in source, "never read"
    assert "{signal_block}" in source, "read but never rendered into the prompt"
    assert "conviction and size, not direction" in source, "hierarchy not stated"


def test_the_run_state_carries_a_signal_block_field():
    from tradingagents.graph.propagation import Propagator

    state = Propagator().create_initial_state(
        "EURUSD=X", "2026-09-30", signal_block="## Research signals\n\nLONG"
    )
    assert state["signal_block"].endswith("LONG")


# ---------------------------------------------------------------------------
# Carry (Lustig, Roussanov and Verdelhan 2011 and the forward-premium literature)
# ---------------------------------------------------------------------------

from tradingagents.signals import carry  # noqa: E402


@pytest.mark.parametrize("ticker,legs", [
    ("EURUSD=X", ("EUR", "USD")), ("EURUSD", ("EUR", "USD")),
    ("usdjpy", ("USD", "JPY")), ("EUR/USD", ("EUR", "USD")),
])
def test_carry_reads_the_pair_legs(ticker, legs):
    assert carry.legs_of(ticker) == legs


@pytest.mark.parametrize("junk", ["GC=F", "", None, "ABCDEFG", "EUR", "BTC-USD", 123])
def test_carry_refuses_anything_that_is_not_a_pair(junk):
    assert carry.legs_of(junk) is None


@pytest.mark.parametrize("base,quote,base_rate,quote_rate,expected", [
    # Real numbers from the FRED probe, 2026-10-01.
    ("EUR", "USD", 2.500, 3.880, Direction.SHORT),   # euro pays less: short EURUSD
    ("USD", "JPY", 3.880, 0.977, Direction.LONG),    # dollar pays more: long USDJPY
    ("USD", "CAD", 3.880, 2.250, Direction.LONG),
    # Inside the threshold: a differential this thin is eaten by the cost of
    # holding the position, so it is no edge rather than a weak direction.
    ("AUD", "USD", 4.350, 3.880, Direction.FLAT),    # +0.47%
    ("GBP", "USD", 3.732, 3.880, Direction.FLAT),    # -0.15%
])
def test_carry_direction(base, quote, base_rate, quote_rate, expected):
    direction, _ = carry.decide(base, quote, base_rate, quote_rate)
    assert direction is expected


def test_long_the_base_means_the_base_pays_more():
    """Getting this backwards would systematically trade the wrong way on every
    pair, while still producing a confident-looking number."""
    direction, differential = carry.decide("USD", "JPY", 3.880, 0.977)
    assert differential > 0 and direction is Direction.LONG
    direction, differential = carry.decide("JPY", "USD", 0.977, 3.880)
    assert differential < 0 and direction is Direction.SHORT


def test_the_threshold_is_symmetric():
    just_under = carry.MIN_DIFFERENTIAL_PCT - 0.01
    just_over = carry.MIN_DIFFERENTIAL_PCT + 0.01
    assert carry.decide("A", "B", just_under, 0.0)[0] is Direction.FLAT
    assert carry.decide("A", "B", -just_under, 0.0)[0] is Direction.FLAT
    assert carry.decide("A", "B", just_over, 0.0)[0] is Direction.LONG
    assert carry.decide("A", "B", -just_over, 0.0)[0] is Direction.SHORT


def test_carry_parses_a_real_fred_report():
    assert carry.parse_latest("**Latest:** -0.045 (2026-08-01) | **Change**") == (-0.045, "2026-08-01")
    assert carry.parse_latest("**Latest:** 3.880 (2026-09-29) |") == (3.880, "2026-09-29")
    assert carry.parse_latest("'XYZ' is not a known macro alias") is None
    assert carry.parse_latest("") is None and carry.parse_latest(None) is None


def test_every_rate_series_declares_its_tenor():
    """The legs are not all the same instrument. CHF and NZD have only a 3-month
    rate where the others are overnight, and a spread between different tenors
    is partly a term premium rather than a policy difference."""
    for ccy, (series_id, tenor) in carry.RATE_SERIES.items():
        assert series_id and tenor in {"overnight", "3-month"}, ccy
    assert carry.RATE_SERIES["CHF"][1] == "3-month"
    assert carry.RATE_SERIES["USD"][1] == "overnight"


def test_carry_uses_the_deposit_facility_for_the_euro():
    """Both ECB series serve. The deposit facility is the one euro overnight
    rates actually sit against, so it is comparable to DFF and SONIA; the main
    refi rate is not."""
    assert carry.RATE_SERIES["EUR"][0] == "ECBDFR"
    assert carry.RATE_SERIES["USD"][0] == "DFF"      # daily, not the monthly FEDFUNDS
    assert carry.RATE_SERIES["GBP"][0] == "IUDSOIA"


def test_carry_refuses_a_currency_with_no_verified_series(monkeypatch):
    """A differential built on a guessed series would look like the paper's
    result while measuring something else."""
    result = carry.compute("SEKUSD=X", "2026-09-30")
    assert result.direction is Direction.UNAVAILABLE
    assert "SEK" in result.unavailable_reason


def _fake_fred(values):
    """values: {currency: (rate, obs_date)}"""
    def fetch(currency, as_of_date):
        if currency not in values:
            return None
        rate, obs = values[currency]
        return rate, obs, carry.RATE_SERIES[currency][0]
    return fetch


def test_carry_flags_a_stale_leg(monkeypatch):
    """A policy rate is persistent, so an old value is usually still right --
    but it is wrong exactly when a central bank has just moved, which is when
    the differential matters most."""
    monkeypatch.setattr(carry, "_fetch_rate", _fake_fred({
        "USD": (3.880, "2026-09-29"),
        "JPY": (0.977, "2026-08-01"),   # 60 days behind
    }))
    result = carry.compute("USDJPY=X", "2026-09-30")
    assert result.direction is Direction.DIAGNOSTIC
    assert result.detail["rule_would_imply"] == "long"
    assert any("JPY" in c and "days before" in c for c in result.caveats)


def test_carry_does_not_flag_fresh_legs(monkeypatch):
    monkeypatch.setattr(carry, "_fetch_rate", _fake_fred({
        "EUR": (2.500, "2026-09-30"), "USD": (3.880, "2026-09-29"),
    }))
    result = carry.compute("EURUSD=X", "2026-09-30")
    assert result.direction is Direction.DIAGNOSTIC
    assert result.detail["rule_would_imply"] == "short"
    assert result.detail["differential_pct"] == pytest.approx(-1.38)
    assert not any("days before" in c for c in result.caveats)


def test_carry_flags_a_tenor_mismatch(monkeypatch):
    monkeypatch.setattr(carry, "_fetch_rate", _fake_fred({
        "USD": (3.880, "2026-09-29"), "CHF": (-0.045, "2026-09-29"),
    }))
    result = carry.compute("USDCHF=X", "2026-09-30")
    assert any("different tenors" in c for c in result.caveats)


def test_carry_reports_a_missing_rate_rather_than_assuming_zero(monkeypatch):
    """A missing leg read as zero would invent an enormous differential."""
    monkeypatch.setattr(carry, "_fetch_rate", _fake_fred({"EUR": (2.5, "2026-09-30")}))
    result = carry.compute("EURUSD=X", "2026-09-30")
    assert result.direction is Direction.UNAVAILABLE
    assert "USD" in result.unavailable_reason


def test_carry_records_the_crash_risk_that_is_the_whole_point(monkeypatch):
    """LRV's central finding is that the premium is payment for crash risk. A
    carry entry that did not say so would present insurance income as free
    money."""
    fails = " ".join(carry.SIGNAL.fails_when).lower()
    assert "risk-off" in fails or "global risk" in fails
    assert "depreciate" in fails
    deviations = " ".join(carry.SIGNAL.deviations).lower()
    assert "forward discount" in deviations      # we use policy rates instead
    assert "two months behind" in deviations     # the staleness limit


def test_carry_is_registered_for_forex_only():
    from tradingagents.signals.registry import signals_for
    assert carry.SIGNAL in signals_for("forex")
    assert carry.SIGNAL not in signals_for("commodity")
    assert carry.SIGNAL not in signals_for("crypto")


def test_momentum_and_carry_agreeing_carries_through():
    """For EURUSD on 2026-09-30 both said short, which is the configuration that
    should produce a confident signal rather than a hedged one."""
    results = [
        SignalResult("time_series_momentum", Direction.SHORT, value=-0.025),
        SignalResult("carry_rate_differential", Direction.SHORT, value=-1.38),
        SignalResult("dollar_factor_decomposition", Direction.DIAGNOSTIC, value=0.0008),
    ]
    direction, why = consensus_direction(results)
    assert direction is Direction.SHORT
    assert "dollar_factor" not in why      # the diagnostic did not vote


@pytest.mark.unit
class TestTheFourthBarIsEnforced:
    """A signal must carry what it did on OUR instruments, measured not argued.

    ``signals/base.py`` sets four bars for the registry and the fourth -- "does
    it survive on the instruments we actually trade" -- was argued about for
    four signals and never tested. Now it is a required field, so a new signal
    cannot reach the agents with nothing said about it either way.
    """

    def test_every_registered_signal_states_its_measured_result(self):
        from tradingagents.signals.registry import REGISTRY

        for signal in REGISTRY:
            assert signal.our_history.strip(), (
                f"{signal.name} reaches the agents with no measured result. An "
                f"empty field reads as a signal with nothing against it rather "
                f"than one with nothing for it."
            )

    def test_an_untested_signal_says_so_rather_than_saying_nothing(self):
        from tradingagents.signals.base import Signal

        bare = Signal(name="x", asset_types=frozenset({"forex"}), paper="p",
                      mechanism="m", definition="d", deviations=(), fails_when=())
        assert "NOT YET MEASURED" in bare.provenance()

    def test_the_measured_result_reaches_the_provenance_card(self):
        from tradingagents.signals.registry import REGISTRY

        for signal in REGISTRY:
            assert signal.our_history in signal.provenance()

    def test_momentum_records_that_it_did_not_clear_the_hurdle(self):
        """The finding must survive in the card, not just in a terminal."""
        from tradingagents.signals import tsmom

        assert "does NOT clear" in tsmom.SIGNAL.our_history
        assert "did not beat simply holding the basket" in tsmom.SIGNAL.our_history

    def test_carry_records_that_there_is_no_evidence_it_pays(self):
        from tradingagents.signals import carry

        assert "NO EVIDENCE" in carry.SIGNAL.our_history
        assert "not that it reliably loses" in carry.SIGNAL.our_history

    def test_the_dollar_factor_records_why_it_cannot_be_scored(self):
        from tradingagents.signals import dollar_factor

        assert "Not measurable as a strategy" in dollar_factor.SIGNAL.our_history

    def test_the_signal_block_hands_the_agents_the_measured_result(self):
        """The agents size what the rule says; they must see what it has done."""
        from tradingagents.signals.registry import build_signal_block

        block = build_signal_block("EURUSD=X", "2026-09-30", "forex")
        assert "Measured on our own history" in block


@pytest.mark.unit
class TestCarryWasDemotedOnTheEvidence:
    """It failed the fourth bar, so it informs the debate and cannot decide it.

    The demotion has to hold in three places or it is cosmetic: the reading
    itself must not be directional, the registry must not let it vote, and the
    rule must remain measurable so promoting it back is testable rather than a
    matter of opinion.
    """

    def _fresh(self, monkeypatch):
        monkeypatch.setattr(carry, "_fetch_rate", _fake_fred({
            "EUR": (2.500, "2026-09-30"), "USD": (3.880, "2026-09-29"),
        }))

    def test_the_reading_is_diagnostic_and_so_cannot_set_direction(self, monkeypatch):
        self._fresh(monkeypatch)
        result = carry.compute("EURUSD=X", "2026-09-30")
        assert result.direction is Direction.DIAGNOSTIC
        assert result.sets_direction is False

    def test_the_differential_is_still_reported(self, monkeypatch):
        """The financing cost is a real fact and the trader has used it well."""
        self._fresh(monkeypatch)
        result = carry.compute("EURUSD=X", "2026-09-30")
        assert result.value == pytest.approx(-1.38)
        assert result.detail["EUR_rate"] == 2.500

    def test_what_the_rule_would_have_said_is_recorded_for_audit(self, monkeypatch):
        self._fresh(monkeypatch)
        result = carry.compute("EURUSD=X", "2026-09-30")
        assert result.detail["rule_would_imply"] == "short"

    def test_the_reading_says_in_words_that_it_is_not_a_trade(self, monkeypatch):
        self._fresh(monkeypatch)
        result = carry.compute("EURUSD=X", "2026-09-30")
        assert any("context, not a trade" in c for c in result.caveats)
        assert any("did not survive our own history" in c for c in result.caveats)

    def test_it_does_not_vote_in_the_consensus(self, monkeypatch):
        """A diagnostic that was allowed to vote would be a demotion in name only."""
        from tradingagents.signals.base import SignalResult
        from tradingagents.signals.registry import consensus_direction

        momentum = SignalResult(name="time_series_momentum", direction=Direction.SHORT)
        differential = SignalResult(name=carry.NAME, direction=Direction.DIAGNOSTIC)
        side, why = consensus_direction([momentum, differential])
        assert side is Direction.SHORT
        assert carry.NAME not in why

    def test_the_directional_rule_is_still_measurable(self):
        """Promotion back must be testable, not a matter of opinion.

        ``decide`` is untouched and the backtest still reads a direction from
        it, so re-running the sweep after getting forward discounts answers the
        question with a number.
        """
        from tradingagents.signals import backtest as bt

        assert carry.decide("EUR", "USD", 2.5, 3.88)[0] is Direction.SHORT
        assert carry.NAME in bt.READERS
        assert carry.NAME not in bt.NOT_BACKTESTABLE

    def test_the_demotion_is_recorded_where_a_reader_will_find_it(self):
        assert "DEMOTED" in carry.SIGNAL.our_history
        assert "does NOT set direction" in carry.SIGNAL.definition
        assert "no longer sets direction" in carry.__doc__
