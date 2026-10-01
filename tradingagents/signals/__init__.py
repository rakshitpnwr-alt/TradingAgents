"""Research-derived, deterministic trading signals.

See ``base`` for what a signal must carry and the screen it has to clear.
"""

from tradingagents.signals.base import Direction, Signal, SignalResult, run_signal

__all__ = ["Direction", "Signal", "SignalResult", "run_signal"]
