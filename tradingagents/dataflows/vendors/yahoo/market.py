import logging
from datetime import datetime
from typing import Annotated

import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta
from stockstats import wrap

from tradingagents.dataflows.errors import NoMarketDataError, VendorError
from tradingagents.dataflows.symbols import crypto_base, normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry
from tradingagents.dataflows.vendors.yahoo.ohlcv import _assert_ohlcv_not_stale, load_ohlcv

logger = logging.getLogger(__name__)


# Indicators computed from volume. stockstats does not report that volume was
# missing: on a zero-volume frame it returns an all-NaN series for ``vwma`` and
# a CONSTANT 0.5 for ``mfi``. The 0.5 is the dangerous one, because the
# indicator is described to the agent with 20/80 oversold/overbought
# thresholds, so a permanent 0.5 reads as a standing oversold signal on every
# bar of every spot forex pair and cash index -- a fabricated reading that
# looks like a real one. Verified directly against stockstats with Volume=0 and
# with Volume=NaN; both produce the same values.
_VOLUME_INDICATORS = frozenset({"vwma", "mfi"})

# Every indicator the vendor can serve, with the description handed to the
# agent alongside the values. Module level so it is built once rather than
# per call, and so a test can assert that any volume-derived indicator added
# here is also listed in _VOLUME_INDICATORS and therefore guarded.
INDICATOR_DESCRIPTIONS = {
    # Moving Averages
    "close_50_sma": (
        "50 SMA: A medium-term trend indicator. "
        "Usage: Identify trend direction and serve as dynamic support/resistance. "
        "Tips: It lags price; combine with faster indicators for timely signals."
    ),
    "close_200_sma": (
        "200 SMA: A long-term trend benchmark. "
        "Usage: Confirm overall market trend and identify golden/death cross setups. "
        "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
    ),
    "close_10_ema": (
        "10 EMA: A responsive short-term average. "
        "Usage: Capture quick shifts in momentum and potential entry points. "
        "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
    ),
    # MACD Related
    "macd": (
        "MACD: Computes momentum via differences of EMAs. "
        "Usage: Look for crossovers and divergence as signals of trend changes. "
        "Tips: Confirm with other indicators in low-volatility or sideways markets."
    ),
    "macds": (
        "MACD Signal: An EMA smoothing of the MACD line. "
        "Usage: Use crossovers with the MACD line to trigger trades. "
        "Tips: Should be part of a broader strategy to avoid false positives."
    ),
    "macdh": (
        "MACD Histogram: Shows the gap between the MACD line and its signal. "
        "Usage: Visualize momentum strength and spot divergence early. "
        "Tips: Can be volatile; complement with additional filters in fast-moving markets."
    ),
    # Momentum Indicators
    "rsi": (
        "RSI: Measures momentum to flag overbought/oversold conditions. "
        "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
        "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
    ),
    # Volatility Indicators
    "boll": (
        "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
        "Usage: Acts as a dynamic benchmark for price movement. "
        "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
    ),
    "boll_ub": (
        "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
        "Usage: Signals potential overbought conditions and breakout zones. "
        "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
    ),
    "boll_lb": (
        "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
        "Usage: Indicates potential oversold conditions. "
        "Tips: Use additional analysis to avoid false reversal signals."
    ),
    "atr": (
        "ATR: Averages true range to measure volatility. "
        "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
        "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
    ),
    # Volume-Based Indicators
    "vwma": (
        "VWMA: A moving average weighted by volume. "
        "Usage: Confirm trends by integrating price action with volume data. "
        "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
    ),
    "mfi": (
        "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
        "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
        "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
    ),
}


def _reports_volume(data) -> bool:
    """True when the frame carries volume a volume indicator could use.

    Decided from the data rather than from the asset type, because the split is
    not along asset-class lines: spot forex (``EURUSD=X``) and cash indices
    (``DX-Y.NYB``) are quoted without volume, while the gold *future* (``GC=F``)
    reports it. Only the frame knows.
    """
    if data is None or "Volume" not in getattr(data, "columns", ()):
        return False
    volume = pd.to_numeric(data["Volume"], errors="coerce")
    return bool((volume.fillna(0) > 0).any())


def _volume_indicator_refusal(symbol: str, indicator: str, as_of_date: str) -> str | None:
    """Why ``indicator`` cannot be computed for ``symbol``, or None to proceed.

    Returns prose instead of raising so the router does not re-ask the next
    vendor: no vendor reports volume for spot forex, so a fallback would spend
    another call and then report a generic failure in place of the real reason.
    A failed load returns None so the normal path runs and raises its own typed
    error -- this helper only ever answers the volume question.
    """
    try:
        data = load_ohlcv(symbol, as_of_date)
    except Exception:
        return None
    if _reports_volume(data):
        return None
    canonical = normalize_symbol(symbol)
    return (
        f"## {indicator} is not available for {canonical}\n\n"
        f"The vendor reports no trading volume for this instrument, and "
        f"{indicator} is computed from volume. Spot forex pairs and cash indices "
        f"are quoted without volume. This is a property of the instrument, not "
        f"an outage, so it will not resolve on a retry or at another vendor -- "
        f"use a price-based indicator instead (close_50_sma, close_200_sma, "
        f"close_10_ema, macd, macds, macdh, rsi, boll, boll_ub, boll_lb, atr).\n\n"
        f"Absent volume is not a volume reading. It is neither high nor low, and "
        f"no conclusion about participation, liquidity or conviction follows from "
        f"it -- do not describe this instrument as lacking volume confirmation."
    )


def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):

    datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # Resolve broker/forex symbols to Yahoo's convention (XAUUSD+ -> GC=F).
    canonical = normalize_symbol(symbol)

    # yfinance treats ``end`` as EXCLUSIVE, so it would drop the requested
    # end_date row (and the current day when end_date is today). Request one day
    # past end_date so the requested range is actually inclusive (#986/#987).
    end_inclusive = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")
    data = yf_retry(lambda: yf.Ticker(canonical).history(start=start_date, end=end_inclusive))

    # Empty result means the symbol is unknown/delisted. Raise a typed error
    # instead of returning prose: the routing layer turns it into a single
    # unambiguous "no data" signal so the agent never fabricates a price.
    if data is None or data.empty:
        raise_for_empty(symbol, canonical, f"rows between {start_date} and {end_date}")

    # Remove timezone info from index for cleaner output
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    # Reject a stale frame (e.g. a year-old partial response) before it is
    # formatted into the report. Raises NoMarketDataError, which the router
    # turns into one clear unavailable signal (#1021).
    _assert_ohlcv_not_stale(data, end_date, symbol, canonical)

    # Round numerical values to 2 decimal places for cleaner display
    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_columns:
        if col in data.columns:
            data[col] = data[col].round(2)

    csv_string = data.to_csv()

    # Name the resolved symbol when it differs, so the reader sees which
    # instrument was priced.
    label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n\n"

    return header + csv_string


def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:


    if indicator not in INDICATOR_DESCRIPTIONS:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(INDICATOR_DESCRIPTIONS.keys())}"
        )

    if indicator in _VOLUME_INDICATORS:
        refusal = _volume_indicator_refusal(symbol, indicator, as_of_date)
        if refusal:
            return refusal

    end_date = as_of_date
    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    before = as_of_dt - relativedelta(days=look_back_days)

    # Optimized: Get stock data once and calculate indicators for all dates
    try:
        indicator_data = _get_stock_stats_bulk(symbol, indicator, as_of_date)

        # Generate the date range we need
        current_dt = as_of_dt
        date_values = []

        while current_dt >= before:
            date_str = current_dt.strftime('%Y-%m-%d')

            # Look up the indicator value for this date
            if date_str in indicator_data:
                indicator_value = indicator_data[date_str]
            else:
                indicator_value = _missing_value_label(symbol, date_str)

            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)

        ind_string = ""
        for date_str, value in date_values:
            ind_string += f"{date_str}: {value}\n"

    except VendorError:
        raise  # Unknown/delisted symbol — let the router emit the sentinel
    except Exception as e:
        logger.warning("Bulk stockstats fetch failed, falling back per-day: %s", e)
        # Fallback to original implementation if bulk method fails
        ind_string = ""
        as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
        while as_of_dt >= before:
            indicator_value = get_stockstats_indicator(
                symbol, indicator, as_of_dt.strftime("%Y-%m-%d")
            )
            ind_string += f"{as_of_dt.strftime('%Y-%m-%d')}: {indicator_value}\n"
            as_of_dt = as_of_dt - relativedelta(days=1)

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + INDICATOR_DESCRIPTIONS.get(indicator, "No description available.")
    )

    return result_str


def _missing_value_label(symbol: str, date_str: str) -> str:
    """Say why a date has no value, without asserting a reason we do not know.

    The old wording called every gap a weekend or holiday. Crypto trades every
    day of the year, so for a crypto pair that claim is always false, and it
    presented a real data gap to the agents as a market closure they should
    expect (a missing Tuesday bar for BTC-USD read as a holiday). Only a
    genuine weekend on a non-24/7 instrument is named as one.
    """
    from datetime import date

    if crypto_base(symbol) is None:
        try:
            y, mth, d = (int(part) for part in str(date_str).split("-"))
            weekend = date(y, mth, d).weekday() >= 5
        except (ValueError, TypeError):
            # An unparseable date is not evidence of a weekend. Fall through to
            # the claim that asserts nothing rather than raising from inside a
            # helper whose entire purpose is to describe absent data calmly.
            weekend = False
        if weekend:
            return "N/A: Not a trading day (weekend)"
    return "N/A: no data for this date (the vendor returned no row)"


def _get_stock_stats_bulk(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to calculate"],
    as_of_date: Annotated[str, "current date for reference"]
) -> dict:
    """
    Optimized bulk calculation of stock stats indicators.
    Fetches data once and calculates indicator for all available dates.
    Returns dict mapping date strings to indicator values.
    """
    from stockstats import wrap

    data = load_ohlcv(symbol, as_of_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    df[indicator]  # This triggers stockstats to calculate the indicator

    result_dict = {}
    for _, row in df.iterrows():
        date_str = row["Date"]
        indicator_value = row[indicator]

        if pd.isna(indicator_value):
            result_dict[date_str] = "N/A"
        else:
            result_dict[date_str] = str(indicator_value)

    return result_dict


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    as_of_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
) -> str:

    as_of_dt = datetime.strptime(as_of_date, "%Y-%m-%d")
    as_of_date = as_of_dt.strftime("%Y-%m-%d")

    try:
        indicator_value = get_stock_stats(
            symbol,
            indicator,
            as_of_date,
        )
    except VendorError:
        raise  # Unknown/delisted symbol — let the router emit the sentinel
    except Exception as e:
        # An empty string renders as "2026-05-08: " in the indicator table, which
        # reads as no value that day rather than a read that failed. Raise so the
        # router can try the next vendor or report the series unavailable.
        raise NoMarketDataError(
            symbol, symbol, f"{indicator} could not be read for {as_of_date}: {e}"
        ) from e

    return str(indicator_value)


def get_closes(symbol: str, start_date: str, end_date: str) -> pd.Series:
    """Daily closes from ``start_date`` up to, not including, ``end_date``."""
    canonical = normalize_symbol(symbol)
    history = yf_retry(lambda: yf.Ticker(canonical).history(start=start_date, end=end_date))
    return history["Close"] if history is not None and "Close" in history else pd.Series(dtype=float)


def get_stock_stats(
    symbol: Annotated[str, "ticker symbol for the company"],
    indicator: Annotated[
        str, "quantitative indicators based off of the stock data for the company"
    ],
    as_of_date: Annotated[
        str, "curr date for retrieving stock price data, YYYY-mm-dd"
    ],
):
    data = load_ohlcv(symbol, as_of_date)
    # Guarded here too: the per-day fallback in
    # ``get_stock_stats_indicators_window`` reaches stockstats through this
    # function, so a guard only at the windowed entry point would be bypassed
    # the moment the bulk path failed.
    if indicator in _VOLUME_INDICATORS and not _reports_volume(data):
        return (
            f"N/A: {indicator} needs volume, which the vendor does not report "
            f"for this instrument (this is not a low-volume reading)"
        )
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
    as_of_str = pd.to_datetime(as_of_date).strftime("%Y-%m-%d")

    df[indicator]  # trigger stockstats to calculate the indicator
    matching_rows = df[df["Date"].str.startswith(as_of_str)]

    if not matching_rows.empty:
        indicator_value = matching_rows[indicator].values[0]
        return indicator_value
    else:
        return _missing_value_label(symbol, as_of_str)
