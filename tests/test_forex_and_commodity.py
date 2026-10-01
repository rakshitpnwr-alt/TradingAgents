"""Forex and commodity support: classification, search terms, the volume guard,
the cash benchmark, and the prompt reframing.

The headline regression here is the volume guard. Before it, asking for ``mfi``
on a spot forex pair returned a constant 0.5 for every bar — not an error and
not an N/A, but a plausible-looking number that the analyst is told to read
against 20/80 oversold/overbought thresholds. Every FX run that picked it would
have read a standing, fabricated oversold signal. ``vwma`` failed more honestly
(all N/A) but still spent one of the analyst's eight indicator slots.
"""

import pandas as pd
import pytest

from cli.models import AnalystType, AssetType
from cli.prompts import detect_asset_type, filter_analysts_for_asset_type, parse_analysts
from tradingagents.agents.analysts.market_analyst import volume_section_for
from tradingagents.agents.context import (
    build_instrument_context,
    debate_key_points,
    debate_subject,
    debate_thesis,
    fundamentals_report_label,
)
from tradingagents.dataflows.symbols import instrument_kind, search_term
from tradingagents.dataflows.vendors import reddit
from tradingagents.dataflows.vendors.yahoo import market
from tradingagents.memory.settlement import resolve_benchmark

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,kind", [
    # Spot forex, however the user spells it. The probe confirmed Yahoo serves
    # every major and cross as PAIR=X.
    ("EURUSD", "forex"), ("EURUSD=X", "forex"), ("eurusd", "forex"),
    ("USDJPY", "forex"), ("GBPJPY", "forex"), ("USDINR", "forex"),
    # Gold has no spot pair on Yahoo (XAUUSD=X 404s), so it resolves to the
    # COMEX future and classifies as a contract, not a share.
    ("XAUUSD", "commodity"), ("XAUUSD+", "commodity"), ("GC=F", "commodity"),
    ("GOLD", "commodity"), ("XAGUSD", "commodity"), ("CL=F", "commodity"),
    ("BTC-USD", "crypto"), ("BTCUSD", "crypto"), ("BTC-USDT", "crypto"),
    ("AAPL", "stock"), ("SPY", "stock"), ("RELIANCE.NS", "stock"),
    # Gold ETFs are shares in a fund: a company-shaped instrument that does
    # report volume, so it must not be swept up as a commodity.
    ("GLD", "stock"), ("IAU", "stock"),
    # The dollar index is a cash index; it has no volume but it is not a pair.
    ("DX-Y.NYB", "stock"),
])
def test_instrument_kind(raw, kind):
    assert instrument_kind(raw) == kind


@pytest.mark.parametrize("raw", ["", "   ", None, 123, [], {}])
def test_instrument_kind_never_raises_on_junk(raw):
    """A classifier is not the place to fail a run; unknown input is a stock."""
    assert instrument_kind(raw) == "stock"


@pytest.mark.parametrize("raw,term", [
    ("BTC-USD", "BTC"), ("BTCUSD", "BTC"), ("SOL-USD", "SOL"),
    ("EURUSD=X", "EURUSD"), ("EURUSD", "EURUSD"), ("USDJPY", "USDJPY"),
    ("XAUUSD", "gold"), ("GC=F", "gold"), ("SI=F", "silver"), ("CL=F", "crude oil"),
    ("AAPL", "AAPL"),
])
def test_search_term_is_what_people_actually_type(raw, term):
    """A vendor symbol is not a search term. Searching for "EURUSD=X" or "GC=F"
    matches almost nothing, and the sentiment analyst reads that empty result as
    an absence of opinion rather than a query that never had a chance."""
    assert search_term(raw) == term


@pytest.mark.parametrize("raw", ["=X", "=F"])
def test_search_term_never_returns_an_empty_query(raw):
    """A degenerate symbol strips to nothing, and an empty query is not a search
    — it returns no posts, which reads as no discussion."""
    assert search_term(raw).strip()


@pytest.mark.parametrize("raw,expected", [
    ("EURUSD", AssetType.FOREX),
    ("XAUUSD", AssetType.COMMODITY),
    ("GC=F", AssetType.COMMODITY),
    ("BTC-USD", AssetType.CRYPTO),
    ("AAPL", AssetType.STOCK),
])
def test_cli_asset_type_follows_the_data_layer(raw, expected):
    assert detect_asset_type(raw) == expected


# ---------------------------------------------------------------------------
# No issuer means no fundamentals analyst
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset_type", [
    AssetType.FOREX, AssetType.COMMODITY, AssetType.CRYPTO,
])
def test_fundamentals_analyst_withheld_without_an_issuer(asset_type):
    """There is no income statement for a currency pair or a futures contract.
    Offering the analyst produces a report assembled from nothing."""
    available = filter_analysts_for_asset_type(list(AnalystType), asset_type)
    assert AnalystType.FUNDAMENTALS not in available
    assert AnalystType.MARKET in available  # the rest are untouched


def test_fundamentals_analyst_still_offered_for_equities():
    available = filter_analysts_for_asset_type(list(AnalystType), AssetType.STOCK)
    assert AnalystType.FUNDAMENTALS in available


@pytest.mark.parametrize("asset_type", [AssetType.FOREX, AssetType.COMMODITY])
def test_asking_for_fundamentals_is_rejected_not_silently_dropped(asset_type):
    with pytest.raises(ValueError, match="fundamentals"):
        parse_analysts("market,fundamentals", asset_type)


# ---------------------------------------------------------------------------
# The volume guard
# ---------------------------------------------------------------------------


def _frame(volume, rows: int = 60) -> pd.DataFrame:
    """An OHLCV frame shaped like the one load_ohlcv returns."""
    dates = pd.bdate_range("2026-07-01", periods=rows)
    price = pd.Series([1.10 + 0.0005 * i for i in range(rows)])
    return pd.DataFrame({
        "Date": dates,
        "Open": price, "High": price * 1.001, "Low": price * 0.999, "Close": price,
        "Volume": volume,
    })


def test_stockstats_fabricates_a_reading_when_volume_is_missing():
    """Why the guard exists, pinned so the premise cannot rot silently.

    This asserts third-party behaviour on purpose: if a future stockstats starts
    returning NaN for mfi on a zero-volume frame, the guard becomes belt-only
    and this test tells us the braces changed.
    """
    from stockstats import wrap

    wrapped = wrap(_frame(volume=0).set_index("Date"))
    mfi = wrapped["mfi"]
    assert mfi.notna().all(), "mfi returned values, not N/A, for a zero-volume frame"
    assert mfi.nunique() == 1, "mfi was constant, which is the fabricated reading"
    # A constant in oversold territory, on an instrument described to the agent
    # with 20/80 thresholds.
    assert mfi.iloc[-1] < 20


@pytest.mark.parametrize("volume,reports", [
    (0, False),              # spot forex: yfinance returns zeros
    (float("nan"), False),   # the other shape a vendor can return
    (1_000_000, True),       # a real equity or futures bar
])
def test_reports_volume(volume, reports):
    assert market._reports_volume(_frame(volume=volume)) is reports


def test_reports_volume_without_a_volume_column():
    assert market._reports_volume(_frame(volume=0).drop(columns=["Volume"])) is False
    assert market._reports_volume(None) is False


@pytest.mark.parametrize("column,reports", [
    (["1000", "2000"], True),    # numeric strings: a cache CSV round-trip
    (["abc", "def"], False),     # unparseable: not a volume reading
    ([-5, -10], False),          # negative is not volume
    ([None, None], False),
    ([], False),
])
def test_reports_volume_against_malformed_columns(column, reports):
    assert market._reports_volume(pd.DataFrame({"Volume": column})) is reports


@pytest.mark.parametrize("not_a_frame", ["nope", 42, [], None, pd.DataFrame()])
def test_reports_volume_never_raises(not_a_frame):
    """It is consulted on the error path, where the frame may be anything."""
    assert market._reports_volume(not_a_frame) is False


def test_mixed_volume_counts_as_reported():
    """A single holiday or half-day bar of zero volume is not an instrument that
    does not report volume."""
    frame = _frame(volume=1_000)
    frame.loc[0:5, "Volume"] = 0
    assert market._reports_volume(frame) is True


@pytest.mark.parametrize("indicator", ["vwma", "mfi"])
def test_volume_indicator_refused_with_the_reason(monkeypatch, indicator):
    monkeypatch.setattr(market, "load_ohlcv", lambda *a, **k: _frame(volume=0))
    out = market.get_stock_stats_indicators_window(
        "EURUSD=X", indicator, "2026-09-30", 30
    )
    assert indicator in out
    assert "no trading volume" in out
    # The agent must not read absence as a bearish reading.
    assert "not a volume reading" in out.lower()
    # And it must not retry: no vendor reports volume for spot forex.
    assert "another vendor" in out
    # Above all, no number it could quote as a level.
    assert "0.5" not in out


def test_price_based_indicators_are_untouched_without_volume(monkeypatch):
    """The guard must be narrow: rsi and the moving averages need no volume and
    must still compute for a forex pair."""
    monkeypatch.setattr(market, "load_ohlcv", lambda *a, **k: _frame(volume=0))
    out = market.get_stock_stats_indicators_window("EURUSD=X", "rsi", "2026-09-30", 5)
    assert "no trading volume" not in out
    assert "2026-09-30:" in out


def test_volume_indicators_still_compute_when_volume_exists(monkeypatch):
    """GC=F is a future and does report volume, so the guard must not fire on it
    — the split is per instrument, not per asset class."""
    monkeypatch.setattr(market, "load_ohlcv", lambda *a, **k: _frame(volume=1_000))
    out = market.get_stock_stats_indicators_window("GC=F", "vwma", "2026-09-30", 5)
    assert "no trading volume" not in out


def test_single_date_path_is_guarded_too(monkeypatch):
    """The per-day fallback reaches stockstats through get_stock_stats, so a
    guard only at the windowed entry point would be bypassed the moment the bulk
    path failed."""
    monkeypatch.setattr(market, "load_ohlcv", lambda *a, **k: _frame(volume=0))
    out = market.get_stock_stats("EURUSD=X", "mfi", "2026-09-30")
    assert "needs volume" in str(out)
    assert "0.5" not in str(out)


def test_refusal_does_not_mask_a_genuine_data_failure(monkeypatch):
    """A failed load must not be reported as "this instrument has no volume"."""
    def boom(*a, **k):
        raise RuntimeError("vendor down")

    monkeypatch.setattr(market, "load_ohlcv", boom)
    assert market._volume_indicator_refusal("EURUSD=X", "vwma", "2026-09-30") is None


def test_every_volume_derived_indicator_is_guarded():
    """Forward guard: an indicator added later whose description mentions volume
    must be listed in _VOLUME_INDICATORS, or it escapes the check and starts
    fabricating readings again."""
    suspect = {
        name for name, text in market.INDICATOR_DESCRIPTIONS.items()
        if "volume" in text.lower()
    }
    assert suspect <= set(market._VOLUME_INDICATORS), (
        f"volume-derived but unguarded: {sorted(suspect - set(market._VOLUME_INDICATORS))}"
    )


def test_market_analyst_withholds_the_volume_block_for_forex():
    assert "vwma" not in volume_section_for("forex")
    # A future reports volume, and a cash index that classifies as "stock" is
    # caught by the vendor-side guard instead.
    assert "vwma" in volume_section_for("commodity")
    assert "vwma" in volume_section_for("stock")
    assert "vwma" in volume_section_for("crypto")


# ---------------------------------------------------------------------------
# Benchmark: an absolute-return position is measured against cash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ticker,benchmark", [
    ("EURUSD=X", "BIL"), ("EURUSD", "BIL"), ("USDJPY", "BIL"),
    ("GC=F", "BIL"), ("XAUUSD", "BIL"), ("SI=F", "BIL"),
    # Unchanged for everything that does carry equity-index beta.
    ("AAPL", "SPY"), ("BRK.B", "SPY"),
    ("RELIANCE.NS", "^NSEI"), ("0700.HK", "^HSI"), ("7203.T", "^N225"),
])
def test_benchmark_resolution(ticker, benchmark):
    """FX and futures have no index to carry beta to: measuring a long EURUSD
    against the S&P credits or blames it for a market it is not exposed to. The
    honest hurdle is the cash the margin earns while the position is open."""
    from tradingagents.default_config import DEFAULT_CONFIG

    assert resolve_benchmark(ticker, DEFAULT_CONFIG) == benchmark


def test_explicit_benchmark_still_overrides():
    assert resolve_benchmark("EURUSD=X", {"benchmark_ticker": "DX-Y.NYB"}) == "DX-Y.NYB"


# ---------------------------------------------------------------------------
# Subreddits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind,expected", [
    ("forex", reddit.FOREX_SUBREDDITS),
    ("commodity", reddit.COMMODITY_SUBREDDITS),
    ("crypto", reddit.CRYPTO_SUBREDDITS),
    ("stock", reddit.DEFAULT_SUBREDDITS),
    ("nonsense", reddit.DEFAULT_SUBREDDITS),  # falls back, never raises
])
def test_resolve_subreddits(kind, expected, monkeypatch):
    monkeypatch.setattr(
        "tradingagents.dataflows.config.get_config", lambda: {}, raising=False
    )
    assert reddit.resolve_subreddits(kind) == tuple(expected)


def test_configured_subreddits_win():
    import tradingagents.dataflows.config as config_module

    original = config_module.get_config
    config_module.get_config = lambda: {"reddit_subreddits": {"forex": ["MyFxSub"]}}
    try:
        assert reddit.resolve_subreddits("forex") == ("MyFxSub",)
    finally:
        config_module.get_config = original


# ---------------------------------------------------------------------------
# Prompt reframing
# ---------------------------------------------------------------------------


def test_forex_context_states_the_direction_convention():
    """Which leg a Buy is long is the single most decision-relevant fact about a
    pair, and nothing in the pipeline said it."""
    context = build_instrument_context("EURUSD=X", "forex")
    assert "base" in context and "quote" in context
    assert "long the base" in context
    assert "USDJPY" in context  # the worked example
    assert "no trading volume" in context
    assert "cash" in context


def test_commodity_context_separates_the_future_from_spot():
    context = build_instrument_context("GC=F", "commodity")
    assert "COMEX" in context
    assert "XAUUSD" in context  # says plainly that this is not the spot quote
    assert "splice" in context  # the roll artefact
    assert "real yield" in context


def test_equity_and_crypto_context_unchanged():
    """Those two runs are accumulating a track record; their prompts must not
    move underneath it."""
    stock = build_instrument_context("AAPL", "stock")
    assert "instrument to analyze is `AAPL`" in stock
    assert "currency" not in stock.lower()

    crypto = build_instrument_context("BTC-USD", "crypto")
    assert "asset to analyze is `BTC-USD`" in crypto
    assert "Treat it as a crypto asset rather than a company" in crypto


@pytest.mark.parametrize("asset_type,subject", [
    ("stock", "stock"), ("crypto", "asset"),
    ("forex", "currency pair"), ("commodity", "futures contract"),
])
def test_debate_subject(asset_type, subject):
    assert debate_subject(asset_type) == subject


@pytest.mark.parametrize("side", ["bull", "bear"])
def test_debate_brief_drops_equity_vocabulary_for_forex(side):
    """Asked to argue EURUSD from "revenue projections" and "strong branding",
    a researcher writes corporate narrative about a currency."""
    points = debate_key_points("forex", side)
    lowered = points.lower()
    for equity_word in ("revenue", "branding", "scalability", "competitors", "products"):
        assert equity_word not in lowered, f"{equity_word!r} still in the {side} brief"
    assert "rate" in lowered and "polic" in lowered


@pytest.mark.parametrize("side", ["bull", "bear"])
def test_equity_debate_brief_unchanged(side):
    points = debate_key_points("stock", side)
    assert "revenue" in points.lower() or "financial" in points.lower()
    assert debate_key_points("unknown-type", side) == points  # safe fallback


def test_commodity_brief_names_the_roll_artefact():
    for side in ("bull", "bear"):
        assert "roll" in debate_key_points("commodity", side).lower()


@pytest.mark.parametrize("asset_type", ["forex", "commodity"])
def test_thesis_phrase_matches_the_asset(asset_type):
    assert "competitive advantages" not in debate_thesis(asset_type, "bull")
    assert debate_thesis(asset_type, "bull") != debate_thesis(asset_type, "bear")


def test_fundamentals_label_explains_the_absence_correctly():
    """The old label told a forex run its missing report "may be unavailable for
    crypto" — the wrong reason, and an invitation to read absence as a finding."""
    label = fundamentals_report_label("forex")
    assert "crypto" not in label
    assert "currency pair" in label
    assert "not a negative finding" in label
    assert fundamentals_report_label("stock") == "Company fundamentals report"


def test_prompt_fragments_carry_no_braces():
    """These strings are interpolated into f-strings and LangChain templates; a
    stray brace raises at format time, mid-run."""
    fragments = [
        build_instrument_context("EURUSD=X", "forex"),
        build_instrument_context("GC=F", "commodity"),
        volume_section_for("forex"),
        volume_section_for("stock"),
    ]
    for asset_type in ("stock", "crypto", "forex", "commodity"):
        for side in ("bull", "bear"):
            fragments.append(debate_key_points(asset_type, side))
            fragments.append(debate_thesis(asset_type, side))
        fragments.append(fundamentals_report_label(asset_type))
    for fragment in fragments:
        assert "{" not in fragment and "}" not in fragment, fragment[:80]
