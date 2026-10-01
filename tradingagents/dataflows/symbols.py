"""Symbol normalization for vendor calls, and ticker values safe to use in a path.

Yahoo Finance (the default vendor) uses specific ticker conventions that
differ from the broker / TradingView / MT5 style symbols users often type:

    user types        Yahoo wants       why
    ---------------   ---------------   -----------------------------------
    XAUUSD, XAUUSD+   GC=F              gold has no forex pair on Yahoo;
                                        it is quoted as a COMEX future
    EURUSD            EURUSD=X          spot forex pairs take a ``=X`` suffix
    BTCUSD            BTC-USD           crypto pairs use a ``-`` separator
    SPX500, US500     ^GSPC             index CFDs map to Yahoo index symbols
    09992.HK, 700.HK  9992.HK, 0700.HK  HK codes are zero-padded to 4 digits
    600519.SH         600519.SS         Yahoo spells Shanghai ``.SS``

Passing the raw broker symbol to Yahoo returns an empty result, which the
agents previously received as free text and could hallucinate a price
around (see issue #781). Centralizing the mapping here means every yfinance
entry point resolves symbols the same way, and new instruments are added by
appending a table row rather than editing call sites.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ISO-4217 codes common enough to appear in retail forex pairs. A bare
# six-letter symbol whose halves are BOTH in this set is treated as a spot
# forex pair and given Yahoo's ``=X`` suffix.
_FOREX_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
        "CNY", "CNH", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN",
        "MXN", "ZAR", "TRY", "INR", "KRW", "BRL", "RUB", "THB",
    }
)

# Crypto bases that brokers quote against USD without a separator.
_CRYPTO_BASES = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "DOT", "AVAX", "LINK"}
)

# Explicit aliases for instruments whose broker symbol does not map to a
# Yahoo symbol by rule. Metals/energy resolve to their front-month future;
# index CFD names resolve to the underlying Yahoo index symbol. Extend by
# adding rows — no call site changes required.
_ALIASES = {
    # Precious metals (spot names -> COMEX/NYMEX futures)
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "XAG": "SI=F", "SILVER": "SI=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Energy
    "WTICOUSD": "CL=F", "USOIL": "CL=F", "WTI": "CL=F",
    "BCOUSD": "BZ=F", "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F", "XNGUSD": "NG=F",
    "COPPER": "HG=F", "XCUUSD": "HG=F",
    # Index CFDs -> Yahoo index symbols
    "SPX500": "^GSPC", "US500": "^GSPC", "SPX": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX", "USTEC": "^NDX",
    "US30": "^DJI", "DJI30": "^DJI", "WS30": "^DJI",
    "GER40": "^GDAXI", "GER30": "^GDAXI", "DE40": "^GDAXI",
    "UK100": "^FTSE", "JP225": "^N225", "JPN225": "^N225",
    "FRA40": "^FCHI", "EU50": "^STOXX50E", "HK50": "^HSI",
}

# HKEX codes as Yahoo spells them: the number zero-padded to 4 digits (#957).
_HK_CODE = re.compile(r"^(\d{1,5})\.HK$")
_SHANGHAI_SH = re.compile(r"^(\d{6})\.SH$")


# Crypto quote currencies that all map to Yahoo's USD pair. Yahoo lists only
# ``<BASE>-USD`` (not the USDT/USDC stablecoin pairs), so a broker symbol quoted
# in any of these resolves to ``-USD`` (#982). Longest first so ``USDT``/``USDC``
# match before the ``USD`` substring.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def crypto_base(raw: str) -> str | None:
    """Return the crypto base (e.g. ``BTC``) for a known USD/USDT/USDC-quoted
    crypto symbol in any form the pipeline may hold — ``BTC-USD``, ``BTCUSD``,
    ``BTC-USDT`` — or None for non-crypto symbols. Purely syntactic.
    """
    if not isinstance(raw, str):
        return None
    compact = raw.strip().upper().rstrip("+").replace("-", "")
    for quote in _CRYPTO_QUOTES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            return base if base in _CRYPTO_BASES else None
    return None


def _normalize_crypto(s: str) -> str | None:
    """Return ``<BASE>-USD`` for a known USD/USDT/USDC-quoted crypto, else None."""
    base = crypto_base(s)
    return f"{base}-USD" if base else None


def normalize_symbol(raw: str) -> str:
    """Map a user/broker symbol to its canonical Yahoo Finance symbol.

    Resolution order (first match wins):
      1. Explicit alias table (metals, energy, index CFDs).
      2. Crypto rule: a known crypto base quoted in USD/USDT/USDC (dashed or
         not) -> ``BASE-USD``.
      3. Forex rule: six letters that are two ISO currency codes -> ``PAIR=X``.
      4. HK rule: a numeric ``.HK`` code -> Yahoo's 4-digit padding
         (``09992.HK`` -> ``9992.HK``, ``700.HK`` -> ``0700.HK``).
      5. Shanghai rule: ``600519.SH`` -> ``600519.SS``.
      6. Otherwise the upper-cased symbol is returned unchanged (plain
         equities, ETFs, Yahoo-native symbols like ``GC=F`` or ``^GSPC``).

    A trailing ``+`` (broker CFD marker, e.g. ``XAUUSD+``) is stripped before
    matching. The function is purely syntactic — it performs no network
    calls — so it is safe to apply on every request.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw

    s = raw.strip().upper()
    # Broker CFD/qualifier suffixes Yahoo never uses.
    s = s.rstrip("+")

    crypto = _normalize_crypto(s)
    if s in _ALIASES:
        canonical = _ALIASES[s]
    elif crypto is not None:
        canonical = crypto
    elif len(s) == 6 and s[:3] in _FOREX_CURRENCIES and s[3:] in _FOREX_CURRENCIES:
        canonical = f"{s}=X"
    elif hk := _HK_CODE.match(s):
        canonical = f"{int(hk.group(1)):04d}.HK"
    elif sh := _SHANGHAI_SH.match(s):
        canonical = f"{sh.group(1)}.SS"
    else:
        canonical = s

    if canonical != raw.strip().upper():
        logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
    return canonical


# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Instrument classification and search terms
#
# Both rules live here, beside ``normalize_symbol``, so the CLI, the agents and
# the vendors cannot disagree about what an instrument is or what to search for.
# ---------------------------------------------------------------------------

# Yahoo suffixes that say what kind of instrument a canonical symbol is: ``=X``
# is a spot forex pair (``EURUSD=X``), ``=F`` an exchange-traded future
# (``GC=F``). Purely syntactic, and applied to the CANONICAL symbol, so a broker
# spelling classifies the same as the Yahoo one -- ``XAUUSD`` and ``GC=F`` are
# both a future, ``EURUSD`` and ``EURUSD=X`` both spot forex.
_FOREX_SUFFIX = "=X"
_FUTURES_SUFFIX = "=F"

# Futures roots -> the word a news or forum search should actually use. A vendor
# symbol is not a search term: a query for ``GC=F`` matches essentially nothing,
# which is the same failure that left crypto sentiment empty until ``BTC-USD``
# was searched as ``BTC``.
_FUTURES_SEARCH_TERMS = {
    "GC": "gold",
    "SI": "silver",
    "PL": "platinum",
    "PA": "palladium",
    "HG": "copper",
    "CL": "crude oil",
    "BZ": "Brent crude",
    "NG": "natural gas",
}

# The kinds ``instrument_kind`` can return. Kept as plain strings because the
# data layer must not import the CLI's enum; ``AssetType`` is built from these
# values, so the two stay in step by construction.
KIND_STOCK = "stock"
KIND_CRYPTO = "crypto"
KIND_FOREX = "forex"
KIND_COMMODITY = "commodity"


def instrument_kind(raw: str) -> str:
    """Classify ``raw`` as ``crypto``, ``forex``, ``commodity`` or ``stock``.

    Classification runs on the canonical symbol, so it agrees with what the data
    path will actually fetch rather than with how the user happened to type it.

    ``=F`` is read as a commodity because that is what the alias table maps to
    it (metals and energy) and what callers use it for. A financial future such
    as ``ES=F`` also lands here; the classification is still right about the two
    things the pipeline does with it -- there are no company filings to fetch,
    and the price is a rolled futures series rather than a cash quote.

    Unknown or empty input classifies as ``stock``: that is the pipeline's
    historical default, and a classifier is not the place to raise.
    """
    if not isinstance(raw, str) or not raw.strip():
        return KIND_STOCK
    canonical = normalize_symbol(raw)
    if crypto_base(canonical) is not None:
        return KIND_CRYPTO
    if canonical.endswith(_FOREX_SUFFIX):
        return KIND_FOREX
    if canonical.endswith(_FUTURES_SUFFIX):
        return KIND_COMMODITY
    return KIND_STOCK


def search_term(raw: str) -> str:
    """The text to search news and forums for, for the instrument ``raw`` names.

    Vendor symbols make poor queries. ``BTC-USD``, ``EURUSD=X`` and ``GC=F``
    each match almost no discussion, and the sentiment analyst reads that empty
    result as an absence of opinion rather than as a query that never had a
    chance. Returns the base coin (``BTC``), the bare pair (``EURUSD``), or the
    commodity's name (``gold``); anything else is returned canonicalised and
    unchanged.
    """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    canonical = normalize_symbol(raw)
    base = crypto_base(canonical)
    if base:
        return base
    term = canonical
    if canonical.endswith(_FOREX_SUFFIX):
        term = canonical[: -len(_FOREX_SUFFIX)]
    elif canonical.endswith(_FUTURES_SUFFIX):
        root = canonical[: -len(_FUTURES_SUFFIX)]
        term = _FUTURES_SEARCH_TERMS.get(root, root)
    # A degenerate symbol (a bare suffix like "=X") strips to nothing, and an
    # empty query is not a search -- it comes back with no posts, which reads as
    # no discussion. Fall back to the canonical symbol: a query that finds
    # nothing is still better than one that asked nothing.
    return term.strip() or canonical
