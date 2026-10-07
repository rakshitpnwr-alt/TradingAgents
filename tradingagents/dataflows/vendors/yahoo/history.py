"""Long price history, for measuring a rule rather than reading a chart.

``ohlcv.load_ohlcv`` serves the running system: five years to today, filtered to
the as-of date, with gaps carried forward so indicators compute on a continuous
series. That is the right frame for an analyst reading a chart and the wrong one
for a backtest, in two ways.

*Five years is not enough.* Five years of monthly rebalances is about sixty
observations, which cannot tell a real edge from luck. This asks for as much as
the vendor will serve.

*Filling gaps flatters every result.* A carried-forward close is a day of
invented zero return. It adds days without adding movement, which lowers the
measured volatility while leaving the return alone, and so raises the Sharpe
ratio of anything computed on it. Here the gap is dropped instead: a day with no
close is a day on which no position could have been marked.

The two also keep separate cache files. A twenty-five-year download written into
the live cache would change what live runs read, and a bug in either would then
be invisible in the other.
"""

from __future__ import annotations

import logging
import os

import pandas as pd

from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.common import (
    raise_for_empty,
    read_cached_csv,
    yf_retry,
)

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_YEARS = 25


def history_cache_path(symbol: str, years: int = DEFAULT_HISTORY_YEARS) -> str:
    """Where a long history is cached, deliberately not where the live one is."""
    config = get_config()
    os.makedirs(config["data_cache_dir"], exist_ok=True)
    return os.path.join(
        config["data_cache_dir"],
        f"{safe_ticker_component(normalize_symbol(symbol))}-YFin-history-{int(years)}y.csv",
    )


def _written_today(path: str) -> bool:
    return pd.Timestamp.fromtimestamp(os.path.getmtime(path)).date() == pd.Timestamp.today().date()


def load_history(symbol: str, years: int = DEFAULT_HISTORY_YEARS) -> pd.DataFrame:
    """Every daily close the vendor will serve for ``symbol``, oldest first.

    Returns ``Date`` and ``Close`` only, with unusable rows dropped rather than
    filled -- see the module docstring for why that matters more than it looks
    like it should.
    """
    import yfinance as yf

    canonical = normalize_symbol(symbol)
    path = history_cache_path(symbol, years)
    frame = None
    if os.path.exists(path) and _written_today(path):
        # Reused within the day so a sweep over many instruments and signals
        # downloads each series once, and so two runs on the same day are
        # comparable. Not reused across days: the last bars would go stale, and
        # a backtest quietly ending last week is a backtest of last week.
        cached = read_cached_csv(path)
        if cached is not None and not cached.empty and "Close" in cached.columns:
            frame = cached

    if frame is None:
        start = (pd.Timestamp.today() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
        end = (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        downloaded = yf_retry(lambda: yf.Ticker(canonical).history(
            start=start, end=end, auto_adjust=True, actions=False,
        ))
        if downloaded is None or downloaded.empty:
            raise_for_empty(symbol, canonical, "price history")
        downloaded = downloaded.reset_index()
        if "Close" not in downloaded.columns:
            raise_for_empty(symbol, canonical, "price history")
        replace_file(path, lambda tmp: downloaded.to_csv(tmp, index=False, encoding="utf-8"))
        frame = downloaded

    return clean_history(frame)


def clean_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize a downloaded or cached frame to sorted ``Date``/``Close`` rows.

    Separated from the download so a caller can be tested on a constructed
    series with no vendor in the picture.
    """
    data = frame.copy()
    for candidate in ("Date", "Datetime", "date", "index"):
        if candidate in data.columns:
            data = data.rename(columns={candidate: "Date"})
            break
    if "Date" not in data.columns or "Close" not in data.columns:
        return pd.DataFrame(columns=["Date", "Close"])

    # Each timestamp keeps its own local date: a long history spans daylight
    # saving changes, and unifying with utc=True would shift a non-US market's
    # bars to the previous day.
    data["Date"] = pd.to_datetime(
        pd.Series(data["Date"]).map(_naive_midnight), errors="coerce"
    )
    data["Close"] = pd.to_numeric(data["Close"], errors="coerce")
    data = data.dropna(subset=["Date", "Close"])
    data = data[data["Close"] > 0]
    data = data.sort_values("Date").drop_duplicates(subset=["Date"], keep="last")
    return data[["Date", "Close"]].reset_index(drop=True)


def _naive_midnight(value):
    if pd.isna(value):
        return pd.NaT
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)
    return ts.normalize()
