from enum import StrEnum


class AnalystType(StrEnum):
    MARKET = "market"
    # Wire value stays "social" for saved-config and string-keyed-caller
    # back-compat; the user-facing label is "Sentiment Analyst".
    SOCIAL = "social"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"


class AssetType(StrEnum):
    # Values match ``tradingagents.dataflows.symbols.instrument_kind``, which is
    # the single rule that decides an instrument's kind; this enum is the CLI's
    # typed view of the same vocabulary.
    STOCK = "stock"
    CRYPTO = "crypto"
    FOREX = "forex"
    COMMODITY = "commodity"
