"""The long-history loader: a backtest's frame, not a running system's.

Two properties matter more than they look like they should, and both have a test
that states why: gaps are dropped rather than filled, because a carried-forward
close is a day of invented zero return that raises the Sharpe of everything
computed on it; and the cache file is not the live one, because a twenty-five
year download landing in the live cache would change what live runs read.
"""

from __future__ import annotations

from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows.vendors.yahoo import history


@pytest.mark.unit
def test_clean_history_sorts_deduplicates_and_drops_unusable_rows():
    raw = pd.DataFrame({
        "Date": ["2020-01-03", "2020-01-01", "2020-01-02", "2020-01-02",
                 "2020-01-04", "2020-01-05"],
        "Close": [103.0, 101.0, 999.0, 102.0, 0.0, None],
    })
    cleaned = history.clean_history(raw)
    assert cleaned["Date"].is_monotonic_increasing
    assert cleaned["Close"].tolist() == [101.0, 102.0, 103.0]   # zero and null dropped
    assert list(cleaned.columns) == ["Date", "Close"]


@pytest.mark.unit
def test_a_gap_is_dropped_rather_than_carried_forward():
    """A filled close is a motionless day that never happened.

    It adds days without adding movement, so it lowers the measured volatility
    while leaving the return alone -- which raises the Sharpe ratio of every
    rule computed on the series. ``load_ohlcv`` fills because indicators need a
    continuous series; measurement must not.
    """
    raw = pd.DataFrame({
        "Date": ["2020-01-01", "2020-01-02", "2020-01-03"],
        "Close": [100.0, None, 102.0],
    })
    cleaned = history.clean_history(raw)
    assert len(cleaned) == 2
    assert cleaned["Close"].tolist() == [100.0, 102.0]
    assert "2020-01-02" not in [str(d.date()) for d in cleaned["Date"]]


@pytest.mark.unit
def test_clean_history_keeps_each_bars_own_local_date():
    """Unifying mixed offsets with utc=True would move a bar to the previous day."""
    raw = pd.DataFrame({
        "Date": ["2020-06-01 00:00:00+05:30", "2020-06-02 00:00:00+01:00"],
        "Close": [1.0, 2.0],
    })
    cleaned = history.clean_history(raw)
    assert [str(d.date()) for d in cleaned["Date"]] == ["2020-06-01", "2020-06-02"]


@pytest.mark.unit
def test_clean_history_finds_the_date_under_any_of_its_names():
    for name in ("Date", "Datetime", "date", "index"):
        cleaned = history.clean_history(pd.DataFrame({name: ["2020-01-01"], "Close": [1.0]}))
        assert len(cleaned) == 1, f"{name} was not recognised as the date column"


@pytest.mark.unit
def test_clean_history_returns_empty_rather_than_raising_on_a_frame_with_no_prices():
    assert history.clean_history(pd.DataFrame({"a": [1]})).empty


@pytest.mark.unit
def test_the_index_is_renumbered_so_callers_can_slice_positionally():
    raw = pd.DataFrame({
        "Date": ["2020-01-03", "2020-01-01", "2020-01-02"],
        "Close": [3.0, 1.0, 2.0],
    })
    assert history.clean_history(raw).index.tolist() == [0, 1, 2]


@pytest.mark.unit
def test_the_history_cache_is_not_the_live_cache(tmp_path, monkeypatch):
    """A twenty-five-year file written into the live cache would change live runs."""
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    path = history.history_cache_path("EURUSD=X", 25)
    assert "history-25y" in path
    assert not path.endswith("EURUSD=X-YFin-data.csv")


@pytest.mark.unit
def test_different_window_lengths_do_not_share_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    assert history.history_cache_path("GC=F", 5) != history.history_cache_path("GC=F", 25)


@pytest.mark.unit
def test_the_cache_path_refuses_a_ticker_that_would_escape_the_cache_directory(
    tmp_path, monkeypatch
):
    """Rejected loudly rather than sanitised: a quietly rewritten path would
    cache one instrument's history under another instrument's name."""
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    with pytest.raises(ValueError, match="not allowed in a filesystem path"):
        history.history_cache_path("../../etc/passwd", 5)
    assert str(tmp_path) in history.history_cache_path("EURUSD=X", 5)


@pytest.mark.unit
def test_a_fresh_cache_is_reused_without_a_download(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    path = history.history_cache_path("EURUSD=X", 25)
    pd.DataFrame({"Date": ["2020-01-01", "2020-01-02"], "Close": [1.1, 1.2]}).to_csv(
        path, index=False)

    with mock.patch.object(history, "yf_retry") as fetch:
        frame = history.load_history("EURUSD=X", 25)
    fetch.assert_not_called()
    assert frame["Close"].tolist() == [1.1, 1.2]


@pytest.mark.unit
def test_a_cache_from_an_earlier_day_is_refetched(tmp_path, monkeypatch):
    """Otherwise the last bars go stale and the backtest silently ends last week."""
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    path = history.history_cache_path("EURUSD=X", 25)
    pd.DataFrame({"Date": ["2020-01-01"], "Close": [1.1]}).to_csv(path, index=False)
    monkeypatch.setattr(history, "_written_today", lambda p: False)

    fresh = pd.DataFrame({"Date": pd.to_datetime(["2020-01-01", "2020-01-02"]),
                          "Close": [2.1, 2.2]})
    with mock.patch.object(history, "yf_retry", return_value=fresh) as fetch:
        frame = history.load_history("EURUSD=X", 25)
    fetch.assert_called_once()
    assert frame["Close"].tolist() == [2.1, 2.2]


@pytest.mark.unit
def test_an_empty_download_is_reported_as_an_absence_not_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    calls = []
    monkeypatch.setattr(history, "raise_for_empty",
                        lambda *a: calls.append(a) or (_ for _ in ()).throw(RuntimeError("no data")))
    with mock.patch.object(history, "yf_retry", return_value=None):
        with pytest.raises(RuntimeError, match="no data"):
            history.load_history("NOSUCH=X", 25)
    assert not list(tmp_path.glob("*.csv")), "an empty answer must not be cached"


@pytest.mark.unit
def test_a_poisoned_empty_cache_file_is_treated_as_a_miss(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    path = history.history_cache_path("EURUSD=X", 25)
    pd.DataFrame().to_csv(path, index=False)

    fresh = pd.DataFrame({"Date": pd.to_datetime(["2020-01-01"]), "Close": [1.0]})
    with mock.patch.object(history, "yf_retry", return_value=fresh) as fetch:
        history.load_history("EURUSD=X", 25)
    fetch.assert_called_once()


@pytest.mark.unit
class TestTheSharedCacheReader:
    """``read_cached_csv``: a file that cannot serve as a cache hit is a miss.

    The live loader's own comment says it means to treat a poisoned cache as a
    miss and refetch. A file truncated to nothing -- an interrupted write, a
    full disk -- raised ``EmptyDataError`` inside ``read_csv`` before any
    ``.empty`` check could run, so the intent was there and the zero-byte case
    escaped it as an exception. These pin both loaders to the stated intent.
    """

    def test_a_zero_byte_file_is_a_miss_not_an_exception(self, tmp_path):
        from tradingagents.dataflows.vendors.yahoo.common import read_cached_csv

        path = tmp_path / "empty.csv"
        path.write_text("")
        assert read_cached_csv(str(path)) is None

    def test_a_header_only_file_reads_as_an_empty_frame(self, tmp_path):
        from tradingagents.dataflows.vendors.yahoo.common import read_cached_csv

        path = tmp_path / "headers.csv"
        path.write_text("Date,Close\n")
        frame = read_cached_csv(str(path))
        assert frame is not None and frame.empty

    def test_a_real_file_reads_normally(self, tmp_path):
        from tradingagents.dataflows.vendors.yahoo.common import read_cached_csv

        path = tmp_path / "good.csv"
        path.write_text("Date,Close\n2020-01-01,1.5\n")
        assert read_cached_csv(str(path))["Close"].tolist() == [1.5]

    def test_a_missing_file_is_a_miss(self, tmp_path):
        from tradingagents.dataflows.vendors.yahoo.common import read_cached_csv

        assert read_cached_csv(str(tmp_path / "nope.csv")) is None

    def test_the_live_loader_refetches_a_zero_byte_cache(self, tmp_path, monkeypatch):
        """The same defect, in the path the running system uses every day."""
        from tradingagents.dataflows.vendors.yahoo import ohlcv

        monkeypatch.setattr(ohlcv, "get_config",
                            lambda: {"data_cache_dir": str(tmp_path)})
        (tmp_path / "EURUSD=X-YFin-data.csv").write_text("")
        fresh = pd.DataFrame({
            "Date": pd.to_datetime(["2026-09-29", "2026-09-30"]),
            "Open": [1.1, 1.2], "High": [1.1, 1.2],
            "Low": [1.1, 1.2], "Close": [1.1, 1.2], "Volume": [0, 0],
        })
        with mock.patch.object(ohlcv, "yf_retry", return_value=fresh) as fetch:
            frame = ohlcv.load_ohlcv("EURUSD=X", "2026-09-30")
        fetch.assert_called_once()
        assert frame["Close"].tolist() == [1.1, 1.2]
