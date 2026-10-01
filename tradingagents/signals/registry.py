"""The signal registry, and the block of ground truth handed to the agents.

Running order matters: signals are computed BEFORE the analysts, and their
output is given to the agents the same way the verified market snapshot is --
as something to explain and size, not something to argue with. The research
rule picks the side; the debate decides conviction and size, or says the rule's
preconditions are absent.

That hierarchy is the point. The pipeline produced three different verdicts for
one ticker on one date because nothing anchored it. A rule that returns the same
number every time it is asked is the anchor.
"""

from __future__ import annotations

from tradingagents.signals import carry, dollar_factor, tsmom
from tradingagents.signals.base import Direction, Signal, SignalResult, run_signal

# Every signal that has cleared the screen. Order is report order.
REGISTRY: tuple[Signal, ...] = (
    tsmom.SIGNAL,
    carry.SIGNAL,
    dollar_factor.SIGNAL,
)


def signals_for(asset_type: str) -> tuple[Signal, ...]:
    return tuple(s for s in REGISTRY if s.applies_to(asset_type))


def evaluate(ticker: str, as_of_date: str, asset_type: str) -> list[SignalResult]:
    """Every applicable signal's reading, in registry order."""
    return [run_signal(s, ticker, as_of_date) for s in signals_for(asset_type)]


def consensus_direction(results: list[SignalResult]) -> tuple[Direction, str]:
    """The side the directional signals agree on, and why.

    Diagnostics do not vote. Disagreement between directional signals resolves
    to FLAT rather than to a tie-break: when two research rules point opposite
    ways, the honest reading is that neither has a claim on the position, and
    inventing a winner would manufacture conviction the evidence does not carry.
    """
    directional = [r for r in results if r.sets_direction]
    if not directional:
        return Direction.UNAVAILABLE, "no directional signal could be computed"

    sides = {r.direction for r in directional}
    names = ", ".join(r.name for r in directional)
    if sides == {Direction.LONG}:
        return Direction.LONG, f"long on {names}"
    if sides == {Direction.SHORT}:
        return Direction.SHORT, f"short on {names}"
    if sides == {Direction.FLAT}:
        return Direction.FLAT, f"no edge on {names}"
    if Direction.LONG in sides and Direction.SHORT in sides:
        return Direction.FLAT, (
            f"directional signals disagree ({names}); no side has a claim on "
            f"the position"
        )
    # A mix of one side and FLAT: the side with an actual reading carries it.
    side = Direction.LONG if Direction.LONG in sides else Direction.SHORT
    return side, f"{side.value} on the signals that formed a view ({names})"


def _format_result(result: SignalResult) -> list[str]:
    if not result.available:
        return [f"**{result.name}**: not available -- {result.unavailable_reason}", ""]
    lines = [f"**{result.name}**: {result.direction.value}"]
    if result.detail:
        lines.append(
            "  - " + "; ".join(f"{k} = {v}" for k, v in result.detail.items())
        )
    for caveat in result.caveats:
        lines.append(f"  - caveat: {caveat}")
    lines.append("")
    return lines


def build_signal_block(ticker: str, as_of_date: str, asset_type: str) -> str:
    """The research-signal section handed to the agents as ground truth."""
    results = evaluate(ticker, as_of_date, asset_type)
    if not results:
        return (
            "## Research signals\n\n"
            f"No research-derived signal in the registry applies to a "
            f"{asset_type} instrument. The decision rests on the analysts' "
            "reading alone for this run, which is weaker evidence than a run "
            "with a signal, not stronger.\n"
        )

    direction, why = consensus_direction(results)
    lines = [
        "## Research signals (deterministic -- computed before this analysis)",
        "",
        f"**Signal direction for {ticker}: {direction.value.upper()}** ({why})",
        "",
        "These are rules derived from published research, computed from price "
        "and macro history. They are reproducible: the same inputs give the "
        "same answer every time, which the rest of this pipeline is not.",
        "",
        "Your task is NOT to pick a direction. The signal above has done that. "
        "You decide conviction and size, and you say so plainly if the "
        "conditions the rule depends on are absent today -- each signal lists "
        "what makes it fail. If you believe the signal is wrong, say that "
        "explicitly and give the evidence; do not quietly trade against it.",
        "",
    ]
    for result in results:
        lines += _format_result(result)

    lines += ["### Provenance", ""]
    for signal in signals_for(asset_type):
        lines.append(signal.provenance())
        lines.append("")
    return "\n".join(lines)
