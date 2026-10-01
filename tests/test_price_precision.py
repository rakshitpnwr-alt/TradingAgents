"""Price precision, and the difference between a level and a flow.

Two defects found by reading a live EURUSD run against the data it was given:

1. Every price was rounded to 2 decimals before the analyst saw it. For an
   equity or gold that is finer than the quoted increment; for a non-JPY forex
   pair it erases the market. A real two-month EURUSD decline arrived as five
   distinct closes, and the verified snapshot -- which the analyst is instructed
   to treat as the source of truth for exact price claims -- was the least
   precise number in the run, reporting the close as 1.13 and the macd as -0.00.

2. Gap filling forward-filled Volume along with the prices. A price is a level,
   so carrying it across a gap claims only that it did not change. Volume is a
   flow, and carrying it forward invents trading that never happened, which a
   volume-weighted indicator then reports as volume confirmation.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import market, ohlcv
from tradingagents.dataflows.vendors.yahoo.common import price_decimals
from tradingagents.dataflows.vendors.yahoo.ohlcv import _fill_price_gaps
from tradingagents.dataflows.vendors.yahoo.snapshot import _fmt

# Closes taken from a real EURUSD window: five distinct values spanning ~22 pips.
# At 2 decimals they collapse to two values spanning a reported 100 pips.
EURUSD_CLOSES = [1.13372, 1.13418, 1.13295, 1.13511, 1.13337]


@pytest.mark.parametrize("reference,decimals", [
    # At and above 100, 0.01 is already the quoted increment -- equities, crypto
    # majors, gold, and the JPY pairs whose pip IS 0.01.
    (4186.70, 2), (83624.79, 2), (229.14, 2), (157.325, 2), (100.0, 2),
    # Below 100, a non-JPY pair quotes to the pip (0.0001) or finer.
    (1.13372, 5), (54.39, 5), (1.0, 5),
    # Sub-unit instruments need one more again.
    (0.69512, 6), (0.5634, 6),
])
def test_price_decimals_tiers(reference, decimals):
    assert price_decimals(reference) == decimals


@pytest.mark.parametrize("junk", [None, "", "abc", float("nan"), 0, 0.0, [], {}])
def test_price_decimals_falls_back_without_raising(junk):
    """It runs inside the vendor path on whatever the frame holds; unusable input
    keeps the historical 2 decimals rather than failing the run."""
    assert price_decimals(junk) == 2


def test_two_decimals_would_destroy_an_fx_series():
    """The premise, pinned so it cannot quietly stop being true."""
    exact = pd.Series(EURUSD_CLOSES)
    assert exact.nunique() == 5
    assert exact.round(2).nunique() == 2
    # The reported range becomes ~5x the real one.
    assert (exact.max() - exact.min()) * 1e4 == pytest.approx(21.6, abs=0.1)
    assert (exact.round(2).max() - exact.round(2).min()) * 1e4 == pytest.approx(100.0, abs=0.1)


def _history_frame(closes, volume=0):
    index = pd.bdate_range("2026-09-24", periods=len(closes))
    close = pd.Series(closes, index=index)
    return pd.DataFrame(
        {"Open": close, "High": close * 1.0005, "Low": close * 0.9995,
         "Close": close, "Volume": volume},
        index=index,
    )


def _stub_yfinance(monkeypatch, frame):
    monkeypatch.setattr(
        market.yf, "Ticker",
        lambda symbol: SimpleNamespace(history=lambda *a, **k: frame),
    )
    monkeypatch.setattr(market, "_assert_ohlcv_not_stale", lambda *a, **k: None)


def test_fx_csv_keeps_the_pip(monkeypatch):
    """The CSV the market analyst reads must carry the price it was quoted at."""
    _stub_yfinance(monkeypatch, _history_frame(EURUSD_CLOSES))
    csv = market.get_YFin_data_online("EURUSD", "2026-09-24", "2026-09-30")
    for close in EURUSD_CLOSES:
        assert f"{close}" in csv, f"{close} was rounded away"
    # And the staircase is gone: every distinct close survives.
    assert csv.count("1.13") >= 5


def test_equity_csv_precision_is_unchanged(monkeypatch):
    """Equities, crypto and gold keep 2 decimals, so runs already accumulating a
    track record do not shift underneath it."""
    _stub_yfinance(monkeypatch, _history_frame([229.1449, 230.5551, 228.9950]))
    csv = market.get_YFin_data_online("AAPL", "2026-09-24", "2026-09-30")
    assert "229.14" in csv and "230.56" in csv
    assert "229.1449" not in csv


def test_csv_precision_is_set_by_the_median_not_one_bar(monkeypatch):
    """A single bad print must not change the precision of the whole series."""
    closes = EURUSD_CLOSES + [9999.0]  # one absurd outlier
    _stub_yfinance(monkeypatch, _history_frame(closes))
    csv = market.get_YFin_data_online("EURUSD", "2026-09-24", "2026-09-30")
    assert "1.13372" in csv, "the outlier dragged the series down to 2 decimals"


@pytest.mark.parametrize("value,rendered", [
    (1.13372, "1.13372"),      # the pip survives
    (0.69512, "0.69512"),
    (83624.79, "83624.79"),    # and a large price keeps its cents
    (4186.70, "4186.70"),
    (106.0, "106.00"),
    (-0.00581293, "-0.00581293"),   # was "-0.01"
    (-0.00217772, "-0.00217772"),   # was "-0.00"
    (149747.0, "149747"),      # a count, not a price
    (0.0, "0.00"),
])
def test_snapshot_renders_quotable_numbers(value, rendered):
    assert _fmt(value) == rendered


def test_snapshot_still_reports_absence_as_absence():
    assert _fmt(None) == "N/A"
    assert _fmt(float("nan")) == "N/A"


def test_snapshot_is_never_the_least_precise_number_in_the_run():
    """It is declared the source of truth for exact price claims, so it must not
    be coarser than the CSV beside it."""
    for close in EURUSD_CLOSES:
        assert _fmt(close) == f"{close}"


# ---------------------------------------------------------------------------
# A level may be carried across a gap. A flow may not.
# ---------------------------------------------------------------------------


def _gappy_frame():
    return pd.DataFrame({
        "Date": pd.to_datetime(["2026-09-28", "2026-09-29", "2026-09-30"]),
        "Open": [4315.0, 4150.1, float("nan")],
        "High": [4315.6, 4218.1, float("nan")],
        "Low": [4143.1, 4145.2, float("nan")],
        "Close": [4168.4, 4179.7, 4186.7],
        "Volume": [229031.0, 149747.0, float("nan")],
    })


def test_prices_are_still_filled_across_a_gap():
    """Gap filling exists so indicators compute on a continuous series."""
    out = _fill_price_gaps(_gappy_frame())
    assert out["Open"].notna().all()
    assert out["Open"].iloc[-1] == 4150.1  # the last level, carried forward


def test_volume_is_not_filled_across_a_gap():
    """Carrying volume forward invents trading that never happened, and a
    volume-weighted indicator then reports it as volume confirmation."""
    out = _fill_price_gaps(_gappy_frame())
    assert pd.isna(out["Volume"].iloc[-1]), "the unreported volume was filled"
    assert out["Volume"].iloc[0] == 229031.0  # reported volume is untouched
    assert out["Volume"].iloc[1] == 149747.0


def test_an_unfilled_volume_reaches_the_guard_as_missing():
    """The two fixes have to meet: an unreported volume must leave the frame as
    NaN so the market vendor's guard refuses a volume indicator on it rather
    than computing one from a carried-forward count."""
    out = _fill_price_gaps(_gappy_frame())
    # Some volume is real, so the frame as a whole still reports volume ...
    assert market._reports_volume(out) is True
    # ... but the unsettled bar is honestly empty rather than a copy.
    assert pd.isna(out["Volume"].iloc[-1])
    # A frame whose volume is entirely unreported is refused outright.
    none_reported = _gappy_frame()
    none_reported["Volume"] = float("nan")
    assert market._reports_volume(_fill_price_gaps(none_reported)) is False


def test_a_frame_without_volume_still_fills(monkeypatch):
    """Dropping Volume from the fill list must not assume the column exists."""
    frame = _gappy_frame().drop(columns=["Volume"])
    out = _fill_price_gaps(frame)
    assert out["Open"].notna().all()


def test_ohlcv_module_does_not_list_volume_as_a_price():
    """A direct read of the thing that went wrong: Volume was in the list of
    columns treated as prices and forward-filled."""
    import inspect

    source = inspect.getsource(ohlcv._fill_price_gaps)
    price_list = source.split("price_cols = ")[1].split("\n")[0]
    assert "Volume" not in price_list
