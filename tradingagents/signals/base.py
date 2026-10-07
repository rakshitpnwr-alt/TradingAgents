"""The research-derived signal layer.

Why this exists
---------------
Until now every run reasoned from scratch off one week of news and a handful of
indicators, with no priors. That is why the same ticker on the same date
produced three different verdicts across runs: nothing anchored it. A signal
here is a deterministic function of price and macro history, so it returns the
same answer every time it is asked, and the agents' job becomes to size and
contextualise a number they cannot invent.

What a signal must carry
------------------------
A number with no provenance is not a signal, it is a guess with a decimal point.
Each entry therefore states the paper it came from, the economic mechanism it
claims, the conditions under which the paper itself says it fails, and -- the
field that matters most here -- how OUR implementation deviates from the paper.
Research is a starting point for rules, not something to copy one to one, and
every deviation is a place the published result may not transfer.

The screen
----------
Published anomalies replicate poorly as a class: most fail under consistent
re-testing, significance hurdles are understated because only winners are
published, and measured effects decay after publication. So a signal reaching
this registry has to clear four bars, recorded on the entry itself:

  mechanism   -- is there an economic reason this pays, or only a correlation
  data        -- can our actual data compute it, point-in-time, without proxies
                 that quietly change what is being measured
  deviation   -- are our departures from the paper small enough to inherit its
                 evidence, and stated plainly where they are not
  our history -- does it survive on the instruments we actually trade

A signal that cannot compute says so. It never degrades to a default direction,
because a fabricated "flat" is indistinguishable from a real one downstream.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class Direction(StrEnum):
    """What the rule says to do. ``FLAT`` is a reading; ``UNAVAILABLE`` is not.

    The distinction is the whole point of the type. ``FLAT`` means the rule ran
    and found no edge. ``UNAVAILABLE`` means the rule could not run, and the
    agents must be told that rather than handed a neutral-looking number.
    """

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    # Ran successfully and deliberately implies no position: a decomposition or
    # a context reading. Distinct from FLAT, which is a rule finding no edge,
    # and from UNAVAILABLE, which is a rule that could not run at all. Three
    # different things that all look like "no trade" if they share one value.
    DIAGNOSTIC = "diagnostic"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SignalResult:
    """One signal's reading for one instrument on one date.

    ``value`` is the raw quantity the rule computed (a trailing return, a
    residual, a real yield). ``direction`` is what that quantity implies.
    ``detail`` carries the inputs, so a reader can check the arithmetic rather
    than trust it, and ``caveats`` carries anything that should temper it.
    """

    name: str
    direction: Direction
    value: float | None = None
    detail: dict = field(default_factory=dict)
    caveats: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.direction is Direction.UNAVAILABLE and not self.unavailable_reason:
            raise ValueError(
                f"{self.name}: an unavailable signal must say why -- an unexplained "
                f"absence reads downstream as a neutral reading"
            )

    @property
    def available(self) -> bool:
        return self.direction is not Direction.UNAVAILABLE

    @property
    def sets_direction(self) -> bool:
        """Whether this reading may decide long/short, rather than only inform.

        The agents size and contextualise; only a directional signal picks a
        side. A diagnostic that was allowed to vote would let a decomposition
        masquerade as a trade.
        """
        return self.direction in (Direction.LONG, Direction.SHORT, Direction.FLAT)

    @classmethod
    def unavailable(cls, name: str, reason: str) -> SignalResult:
        return cls(name=name, direction=Direction.UNAVAILABLE, unavailable_reason=reason)


@dataclass(frozen=True)
class Signal:
    """A rule derived from research, with everything needed to judge it.

    ``compute`` takes (ticker, as_of_date) and returns a ``SignalResult``. It
    must not raise: a signal that blows up mid-run would take the whole analysis
    with it, so the registry converts any escape into an explicit unavailable.
    """

    name: str
    asset_types: frozenset[str]
    paper: str
    mechanism: str
    definition: str
    deviations: tuple[str, ...]
    fails_when: tuple[str, ...]
    # The fourth bar, measured rather than argued: what this rule actually did
    # on the instruments we trade. Required, and never empty -- a signal that
    # has not been tested must say so in words, because an absent field reads
    # downstream as a signal with nothing against it rather than one with
    # nothing for it. ``backtest_signals.py`` produces the numbers that go here.
    our_history: str = ""
    compute: object = None

    def applies_to(self, asset_type: str) -> bool:
        return asset_type in self.asset_types

    def provenance(self) -> str:
        """The signal's card, rendered for a human or an agent to read."""
        lines = [
            f"### {self.name}",
            f"- Source: {self.paper}",
            f"- Mechanism: {self.mechanism}",
            f"- Definition: {self.definition}",
        ]
        if self.deviations:
            lines.append("- How our implementation differs from the paper:")
            lines += [f"  - {d}" for d in self.deviations]
        if self.fails_when:
            lines.append("- Known to fail when:")
            lines += [f"  - {f}" for f in self.fails_when]
        # Last, and deliberately so: it is the only line here that is evidence
        # about our own instruments rather than about someone else's sample.
        lines.append(f"- Measured on our own history: "
                     f"{self.our_history or 'NOT YET MEASURED on our instruments'}")
        return "\n".join(lines)


def run_signal(signal: Signal, ticker: str, as_of_date: str) -> SignalResult:
    """Evaluate one signal, converting any failure into an explicit unavailable.

    Deliberately broad: a signal is a research hypothesis wired to live vendor
    data, and the ways that can fail are not enumerable. What must never happen
    is a raised exception ending the run, or -- worse -- a failure being
    smoothed into a neutral reading the agents then treat as information.
    """
    if signal.compute is None:
        return SignalResult.unavailable(signal.name, "no implementation registered")
    try:
        result = signal.compute(ticker, as_of_date)
    except Exception as exc:  # noqa: BLE001 -- see docstring
        logger.warning("Signal %s failed for %s on %s: %s", signal.name, ticker, as_of_date, exc)
        return SignalResult.unavailable(
            signal.name, f"could not be computed ({type(exc).__name__}: {exc})"
        )
    if not isinstance(result, SignalResult):
        return SignalResult.unavailable(
            signal.name, f"returned {type(result).__name__}, not a SignalResult"
        )
    return result
