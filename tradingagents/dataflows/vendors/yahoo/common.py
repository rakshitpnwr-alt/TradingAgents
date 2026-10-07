"""What every Yahoo request shares: its retry, and how a failure or an empty answer is reported."""

import logging
import time

import yfinance as yf
from yfinance.exceptions import YFPricesMissingError, YFRateLimitError

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.net import vendor_reachable

logger = logging.getLogger(__name__)

YAHOO_HOST = "https://query2.finance.yahoo.com"


def read_cached_csv(path: str):
    """A cached CSV, or None when the file cannot serve as a cache hit.

    A cache file can be truncated to nothing -- an interrupted write, a full
    disk, something outside the process. ``pd.read_csv`` raises
    ``EmptyDataError`` on a file with not even a header row, which happens
    before any ``.empty`` check the caller makes and so escapes as an exception
    from what should be a cache miss. Returning None puts a zero-byte file in
    the same category as an empty one: refetch, rather than fail or serve it.
    """
    import pandas as pd

    try:
        return pd.read_csv(path, on_bad_lines="skip", encoding="utf-8")
    except pd.errors.EmptyDataError:
        logger.warning("Cache file %s is empty; refetching.", path)
        return None
    except (OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        logger.warning("Cache file %s is unreadable (%s); refetching.", path, exc)
        return None


def raise_for_empty(symbol: str, canonical: str, what: str) -> None:
    """Report an empty Yahoo answer as an absence, or as an outage if it is one.

    An empty answer from a Yahoo that cannot be reached is not an answer about
    the symbol, so the host is probed before "this symbol has no {what}" is said.
    """
    if not vendor_reachable(YAHOO_HOST):
        raise VendorUnavailableError(f"Yahoo Finance is unreachable; no {what} was retrieved")
    raise NoMarketDataError(symbol, canonical, f"no {what}")


# yfinance answers some failed requests with an empty result, which would read as
# "no data" for a symbol nobody checked; raised, the failure is reported as one.
yf.config.debug.hide_exceptions = False


def _answered_empty(exc: Exception) -> bool:
    """Whether Yahoo answered that it has nothing: no such symbol (HTTP 404), or a
    price window with no prices in it.

    yfinance raises YFPricesMissingError for that answer and also for an answer
    that was an error (an error status, or Yahoo describing a failure), so only
    a chart with no prices, or Yahoo saying the data does not exist, counts.
    A missing time zone is not an answer: yfinance reports a failed lookup the same way.
    """
    if isinstance(exc, YFPricesMissingError):
        reason = exc.yahoo_reason
        return "status_code" not in (exc.debug_info or "") and (
            reason is None or reason.startswith("Data doesn't exist"))
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally, so this wrapper retries them. A rate limit that
    outlasts the retries, or any other exception, is raised as
    VendorUnavailableError: it failed in transit and says nothing about the
    symbol. Yahoo answering that it has nothing for the symbol returns None,
    an empty answer. ``func`` should build its own Ticker and make the request
    itself: a Ticker keeps a failed ``info`` fetch as done, so asking the same
    one again reads an empty profile.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError as exc:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise VendorUnavailableError(
                    f"Yahoo Finance rate limited after {max_retries} retries: {exc}"
                ) from exc
        except Exception as exc:
            if _answered_empty(exc):
                return None
            raise VendorUnavailableError(f"Yahoo Finance request failed: {type(exc).__name__}") from exc


# How many decimals a price needs to stay informative at a given magnitude.
#
# A flat 2 decimals was applied to every instrument. For an equity or gold that
# is finer than the quoted increment, but for a non-JPY forex pair it erases the
# market: EURUSD trades in pips of 0.0001, so a real 2026-09 series moving
# 1.13372 / 1.13418 / 1.13295 / 1.13511 / 1.13337 rendered as 1.13 / 1.13 /
# 1.13 / 1.14 / 1.13 -- five distinct closes collapsed to two, a 22-pip range
# reported as 100 pips, and a smooth drift reported as a staircase. The market
# analyst was told to treat that as the source of truth for exact price claims.
#
# Chosen by magnitude rather than by a currency-convention table, so it needs no
# maintenance and covers equities, crypto, metals and indices by the same rule.
# At 100+ the quoted increment really is 0.01 -- which is also the pip for the
# JPY pairs, the one FX family this does not need to widen.
PRICE_DECIMALS_BY_MAGNITUDE = ((100.0, 2), (1.0, 5))
PRICE_DECIMALS_SUB_UNIT = 6


def price_decimals(reference) -> int:
    """Decimals to round prices to, given a representative price level.

    ``reference`` should be a typical price for the instrument (the median close
    of the frame, not one bar), so a single odd print cannot change the
    precision of the whole series. Unusable input falls back to 2, the
    historical behaviour.
    """
    try:
        magnitude = abs(float(reference))
    except (TypeError, ValueError):
        return 2
    if magnitude != magnitude or magnitude == 0:  # NaN or zero: no information
        return 2
    for threshold, decimals in PRICE_DECIMALS_BY_MAGNITUDE:
        if magnitude >= threshold:
            return decimals
    return PRICE_DECIMALS_SUB_UNIT
