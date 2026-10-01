"""Prompt context shared by the agents: instrument identity, output language and portfolio."""

import functools
import logging
from collections.abc import Mapping
from typing import Any

from tradingagents.dataflows.date_window import is_historical
from tradingagents.dataflows.vendors.yahoo.fundamentals import get_company_profile

logger = logging.getLogger(__name__)


def get_language_instruction() -> str:
    """Return a prompt instruction for the configured output language.

    Returns empty string when English (default), so no extra tokens are used.
    Applied to every agent whose output reaches the saved report —
    analysts, researchers, debaters, research manager, trader, and
    portfolio manager — so a non-English run produces a fully localized
    report rather than a mix of languages.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    # The labelled lines are read by the program, so they keep their English
    # label and value: a translated rating line leaves the reader prose to
    # search, where a negated rating ("not a Sell") reads as the call (#1435).
    return (
        f" Write your entire response in {lang}, except the labelled lines the format"
        f" asks for (the \"**Rating**:\" line, \"FINAL TRANSACTION PROPOSAL:\"):"
        f" keep their label and value in English, exactly as specified."
    )


def opponent_argument_or_opening(text: str, opponent: str) -> str:
    """Opponent's latest argument, or an explicit opening marker when empty.

    The first speaker in each debate round receives an empty opponent response;
    interpolating it into a "refute the opponent" prompt makes the model
    fabricate the other side's position. Returning a clear "has not spoken yet"
    marker instead lets it open with its own case (#1176).
    """
    text = (text or "").strip()
    if text:
        return text
    return f"(The {opponent} has not spoken yet — open the debate with your own case.)"


def _clean_identity_value(value: Any) -> str | None:
    """Return a trimmed string, or None for empty / placeholder-ish values."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "n/a", "nan", "null"}:
        return None
    return cleaned


def resolve_instrument_identity(ticker: str) -> dict:
    """Resolve deterministic identity metadata (company name, sector, …) for a ticker.

    This exists to stop the pipeline from hallucinating a *different* company
    when a chart pattern suggests a different industry than the real one
    (#814): without a ground-truth name, the market analyst would pattern-match
    the price action to a narrative and invent an identity that then cascaded
    through every downstream agent.

    Best-effort by design: if yfinance is unavailable, rate-limited, or doesn't
    recognise the ticker, we return ``{}`` and the caller falls back to
    ticker-only context rather than failing before analysis starts. An answer
    is cached for the process; a failed lookup is asked again next time.

    Identity resolves for the same instrument the price path fetches
    (``XAUUSD`` -> ``GC=F``, #983).
    """
    try:
        return _identity(ticker)
    except Exception as exc:  # noqa: BLE001 — fail open, never block the run
        logger.debug("Could not resolve instrument identity for %s: %s", ticker, exc)
        return {}


@functools.lru_cache(maxsize=256)
def _identity(ticker: str) -> dict:
    """The vendor's identity fields for ``ticker``; raises if the lookup fails."""
    info = get_company_profile(ticker)
    identity: dict[str, str] = {}
    company_name = _clean_identity_value(info.get("longName")) or _clean_identity_value(
        info.get("shortName")
    )
    if company_name:
        identity["company_name"] = company_name
    for source_key, target_key in (
        ("sector", "sector"),
        ("industry", "industry"),
        ("exchange", "exchange"),
        ("quoteType", "quote_type"),
    ):
        value = _clean_identity_value(info.get(source_key))
        if value:
            identity[target_key] = value
    return identity


# What to call the thing being analysed. "instrument" is the neutral default;
# each of the others is what the thing actually is, which is also the first
# correction an agent needs -- a currency pair is not a company, and a futures
# contract is not a share.
_INSTRUMENT_LABELS = {
    "stock": "instrument",
    "crypto": "asset",
    "forex": "currency pair",
    "commodity": "futures contract",
}

# Per-asset-type guidance appended to the instrument context.
#
# Without it the downstream prompts carry equity assumptions into markets that
# do not have them: the bull researcher is asked to argue from "revenue
# projections" and "strong branding", which for EURUSD produces invented
# corporate narrative about a currency. Each block states what the instrument
# is, what actually moves it, and which readings do not exist for it.
_ASSET_GUIDANCE = {
    "crypto": (
        " Treat it as a crypto asset rather than a company, and do not "
        "assume company fundamentals are available."
    ),
    "forex": (
        " Treat it as a spot currency pair, not a company: it has no earnings,"
        " revenue, balance sheet or shares outstanding, and no issuer"
        " fundamentals exist to look up.\n"
        " - Direction: the first three letters are the base currency and the"
        " last three the quote currency, and the price is how many units of the"
        " quote one unit of the base buys. A Buy is long the base and short the"
        " quote; a Sell is the reverse. For USDJPY a Buy is long the dollar"
        " against the yen, not a view on Japan by itself.\n"
        " - Two-sided moves: every move has two candidate causes, base strength"
        " or quote weakness. Say which leg you think is driving it and what"
        " would tell the two apart, rather than writing about the pair as a"
        " single thing that rose or fell.\n"
        " - What moves it: the expected policy-rate path and short-rate"
        " differential between the two currencies, inflation and real-yield"
        " differentials, growth and labour data measured against expectations"
        " rather than against zero, terms of trade, risk appetite (which drives"
        " the low-yielding funding currencies), central-bank communication, and"
        " intervention risk where an authority has signalled one.\n"
        " - No volume: the vendor reports no trading volume for spot forex."
        " That is not a low-volume reading, and no claim about participation,"
        " liquidity or conviction follows from it.\n"
        " - Benchmark: this is an absolute-return position with no equity-index"
        " beta, so it is measured against cash rather than against a stock market."
    ),
    "commodity": (
        " Treat it as an exchange-traded futures contract, not a company: it has"
        " no earnings, revenue or balance sheet, and no issuer fundamentals"
        " exist to look up.\n"
        " - Futures, not spot: the price is the exchange-traded contract. For"
        " gold, `GC=F` is the COMEX future and differs from spot XAUUSD by"
        " carry, so do not present its level as a spot quote.\n"
        " - Rolls: the series is a continuous front-month series, which splices"
        " successive expiries together. A level shift where one contract hands"
        " over to the next is an artefact of the splice, not a market move --"
        " do not read one as a gap, a breakout or a trend change.\n"
        " - What moves precious metals: the real yield on long-dated government"
        " debt, which is the discount rate for an asset that pays no cash flow"
        " (take the 10-year nominal yield minus 10-year inflation expectations,"
        " available from the macro tool as `10y_treasury` and"
        " `inflation_expectations`); the dollar; central-bank reserve buying;"
        " ETF flows; and hedging demand in geopolitical or inflation stress."
        " For energy and industrial metals: inventories, physical supply and"
        " demand, and the shape of the forward curve.\n"
        " - Benchmark: this is an absolute-return position with no equity-index"
        " beta, so it is measured against cash rather than against a stock market."
    ),
}


def build_instrument_context(
    ticker: str,
    asset_type: str = "stock",
    identity: Mapping[str, str] | None = None,
    trade_date: str | None = None,
) -> str:
    """Describe the exact instrument so agents preserve identity and ticker.

    When ``identity`` is provided (resolved deterministically via
    :func:`resolve_instrument_identity`), the company name and business
    classification are injected so agents anchor to the real company rather
    than pattern-matching the price chart to a wrong one (#814).

    That profile carries no historical vintage: it describes the company today.
    A run dated earlier gets the current name alone, as a way to tell the
    company apart from others rather than as what it was called then; a sector,
    industry or exchange it holds today is not given, since it may not have held
    on the analysis date.
    """
    is_company = asset_type == "stock"
    instrument_label = _INSTRUMENT_LABELS.get(asset_type, "instrument")
    context = (
        f"The {instrument_label} to analyze is `{ticker}`. "
        "The tools serve this instrument; refer to it by this exact ticker in every report and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`, `-USD`)."
    )

    identity = identity or {}
    name = identity.get("company_name") or identity.get("name")
    label = "Company" if is_company else "Name"
    details = []
    if is_historical(trade_date):
        if name:
            details.append(
                f"{label}: {name} (its current name, given only to identify it; "
                f"on {trade_date} it may have been named differently)"
            )
    else:
        if name:
            details.append(f"{label}: {name}")
        sector, industry = identity.get("sector"), identity.get("industry")
        if sector and industry:
            details.append(f"Business classification: {sector} / {industry}")
        elif sector:
            details.append(f"Sector: {sector}")
        elif industry:
            details.append(f"Industry: {industry}")
        if identity.get("exchange"):
            details.append(f"Exchange: {identity['exchange']}")

    if details:
        context += (
            f" Resolved identity: {'; '.join(details)}. "
            "Do not substitute a different company or ticker unless a tool "
            "result explicitly disproves this resolved identity."
        )

    guidance = _ASSET_GUIDANCE.get(asset_type)
    if guidance:
        context += guidance
    return context


# What the debate is about, per asset type. "stock" and "asset" are the wordings
# the pipeline already used, kept so equity and crypto runs read exactly as
# before.
_DEBATE_SUBJECT = {
    "stock": "stock",
    "crypto": "asset",
    "forex": "currency pair",
    "commodity": "futures contract",
}

# The first three bullets of each researcher's brief. The remaining two
# (counterpoints, engagement) are the same whatever is being traded.
#
# The equity wording is not transferable: asked to argue for EURUSD from
# "revenue projections", "scalability" and "strong branding", a bull analyst
# writes corporate narrative about a currency, and the bear answers it. Each
# asset type gets the case its market actually turns on.
_BULL_POINTS = {
    "stock": """- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.""",
    "crypto": """- Adoption and Network: Argue from usage, active addresses, developer activity, settlement volume, or the ecosystem built on it — not from revenue or margins, which do not exist.
- Supply and Flows: Emphasize issuance schedule, holdings that are not circulating, ETF or treasury accumulation, and exchange balances.
- Positive Indicators: Use trend and momentum evidence from the market report, plus regulatory or infrastructure news that widens access.""",
    "forex": """- Rate and Policy Case: Argue where the short-rate differential and the expected policy path favour being long the base currency against the quote. Use data measured against what was expected, not against zero.
- Flows and Positioning: Emphasize terms of trade, current-account and reserve flows, the carry on offer, and whether positioning on this side is still uncrowded.
- Confirmation: Use trend and momentum evidence from the market report, and say which leg — base strength or quote weakness — the macro and news evidence actually supports.""",
    "commodity": """- Macro Case: Argue from the real yield on long-dated government debt (the discount rate for an asset paying no cash flow), the dollar, and the inflation or geopolitical stress that drives hedging demand.
- Physical and Official Demand: Emphasize central-bank reserve buying, ETF flows, inventories, mine or field supply, and a forward curve that rewards holding the position.
- Confirmation: Use trend and momentum evidence from the market report, distinguishing a genuine move from a contract-roll artefact in the continuous series.""",
}

_BEAR_POINTS = {
    "stock": """- Risks and Challenges: Highlight factors like market saturation, financial instability, or macroeconomic threats that could hinder the stock's performance.
- Competitive Weaknesses: Emphasize vulnerabilities such as weaker market positioning, declining innovation, or threats from competitors.
- Negative Indicators: Use evidence from financial data, market trends, or recent adverse news to support your position.""",
    "crypto": """- Risks and Challenges: Highlight regulatory exposure, custody and counterparty risk, concentration of holdings, protocol or bridge failure, and dependence on liquidity that can leave quickly.
- Structural Weaknesses: Emphasize issuance or unlock overhang, thin real usage relative to valuation, and competition from assets with the same use case.
- Negative Indicators: Use trend and momentum evidence from the market report, plus adverse regulatory or infrastructure news.""",
    "forex": """- Rate and Policy Risk: Argue where the short-rate differential and the expected policy path favour being short the base currency against the quote, including the case where the market has already priced the bull's story.
- Flows, Carry and Intervention: Emphasize a deteriorating terms of trade or external balance, a carry that pays you to be on the other side, crowded positioning vulnerable to an unwind, and intervention risk where an authority has signalled one.
- Negative Indicators: Use trend and momentum evidence from the market report, and say which leg — base weakness or quote strength — the macro and news evidence actually supports.""",
    "commodity": """- Macro Risk: Argue from rising real yields, a stronger dollar, or fading inflation and geopolitical stress removing the hedging demand the bull relies on.
- Supply and Positioning: Emphasize supply response, rising inventories, demand destruction at these prices, a forward curve that charges you to hold the position, and crowded speculative length.
- Negative Indicators: Use trend and momentum evidence from the market report, distinguishing a genuine move from a contract-roll artefact in the continuous series.""",
}


# The one-line thesis in each researcher's opening sentence. Stock and crypto
# keep the wording the pipeline already used, so equity and crypto runs are
# unchanged; the other two drop the equity vocabulary that does not apply.
_BULL_THESIS = {
    "stock": "growth potential, competitive advantages, and positive market indicators",
    "crypto": "growth potential, competitive advantages, and positive market indicators",
    "forex": "the rate and policy case for the base currency, supportive flows, and positive market indicators",
    "commodity": "the macro case, supportive physical and official demand, and positive market indicators",
}

_BEAR_THESIS = {
    "stock": "risks, challenges, and negative indicators",
    "crypto": "risks, challenges, and negative indicators",
    "forex": "the rate and policy case against the base currency, adverse flows and intervention risk, and negative indicators",
    "commodity": "adverse macro conditions, supply and positioning risk, and negative indicators",
}


def debate_thesis(asset_type: str, side: str) -> str:
    """The thesis phrase for the bull or bear opening sentence."""
    table = _BULL_THESIS if side == "bull" else _BEAR_THESIS
    return table.get(asset_type, table["stock"])


def debate_subject(asset_type: str) -> str:
    """What to call the thing under debate ("stock", "currency pair", ...)."""
    return _DEBATE_SUBJECT.get(asset_type, "asset")


def debate_key_points(asset_type: str, side: str) -> str:
    """The asset-appropriate opening bullets for the bull or bear brief.

    ``side`` is ``"bull"`` or ``"bear"``. An unknown asset type falls back to the
    equity wording, which is what the pipeline used before the split.
    """
    table = _BULL_POINTS if side == "bull" else _BEAR_POINTS
    return table.get(asset_type, table["stock"])


def fundamentals_report_label(asset_type: str) -> str:
    """Heading for the fundamentals slot, saying plainly when none can exist.

    The old wording named crypto specifically, so a forex or commodity run
    labelled an absent report "may be unavailable for crypto" — a heading that
    tells the reader the wrong thing about why it is empty.
    """
    if asset_type == "stock":
        return "Company fundamentals report"
    return (
        f"Fundamentals report (a {debate_subject(asset_type)} has no issuer "
        f"filings, so expect this to be absent; its absence is not a negative "
        f"finding)"
    )


def get_instrument_context_from_state(state: Mapping[str, Any]) -> str:
    """Return the instrument context for the current run.

    Prefers the identity-resolved context computed once at run start and
    stored on the state (see ``TradingAgentsGraph.resolve_instrument_context``).
    Falls back to a ticker-only context — with no network lookup — when the
    state was constructed without it (bare programmatic states, tests), so a
    consumer is never forced to make a yfinance call mid-graph.
    """
    context = state.get("instrument_context")
    if isinstance(context, str) and context.strip():
        return context
    return build_instrument_context(
        str(state["company_of_interest"]),
        state.get("asset_type", "stock"),
    )


def report_or_absent(text: str, source: str) -> str:
    """An analyst's report, or a marker saying it was never produced.

    A report is empty when its analyst was not selected, refused, or returned
    nothing. Interpolating that into a labelled section presents an absence as a
    blank finding, and the reading agent fills it in from nothing, the same way
    an empty opponent argument used to invite an invented rebuttal (#1176).
    """
    text = (text or "").strip()
    if text:
        return text
    return f"(No {source} report in this run: it is not available, not an empty finding.)"


def get_portfolio_context_from_state(state: Mapping[str, Any]) -> str:
    """Return the caller's portfolio block, or a notice that none was given.

    A run without portfolio context must not read as a flat book: the agents
    would otherwise size as if the caller held nothing, which is a claim about
    an account we were never told about.
    """
    context = state.get("portfolio_context")
    if isinstance(context, str) and context.strip():
        return context
    return (
        "Portfolio context: not provided. You do not know the caller's current "
        "holdings or cash, so do not assume a flat book; give direction and "
        "sizing guidance in terms the caller can apply to their own position."
    )
