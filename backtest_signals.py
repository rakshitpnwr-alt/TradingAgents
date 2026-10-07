"""Backtest the signal layer on our own price history, and report it honestly.

Why this exists: ``signals/base.py`` sets four bars a signal must clear before
it reaches the registry, and the fourth -- "does it survive on the instruments
we actually trade" -- had never been tested for any of them. Four signals
shipped with carefully-read papers and zero evidence they pay here.

Run:  source .venv/bin/activate && python backtest_signals.py

This needs network: Yahoo for ~25 years of daily closes per instrument, and
FRED for one rate series per currency. Both are cached for the day, so a second
run is fast and gives the same answer.

Reading it
  Sharpe is the headline, not return. Our 10% volatility target is a choice we
  made and it scales the return arbitrarily; it does not scale the Sharpe.

  t is the t-statistic on the mean daily return. The hurdle is 3.0, not 2.0:
  Harvey, Liu and Zhu argue that with hundreds of published factors competing
  for the same data, 2.0 no longer means what it means in a single test, and
  these are already-published rules so we inherit that problem.

  POOLED is an equal-weighted book across the instruments, which is the closest
  analogue of the paper's actual claim. It is the number to read. The best
  single instrument is not a result.

  long-only is the same instrument held long over the same bars. A directional
  rule that does not beat it has not earned its complexity.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from tradingagents.signals import backtest as bt  # noqa: E402
from tradingagents.signals.registry import REGISTRY  # noqa: E402

BAR = "━"


def pct(value, digits=1) -> str:
    return "—" if value is None else f"{value * 100:+.{digits}f}%"


def plain_pct(value, digits=0) -> str:
    """A proportion, unsigned. A hit rate is not a gain or a loss."""
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def num(value, digits=2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def performance_row(label: str, p, extra: str = "") -> str:
    return (f"  {label:<26} {p.days:>6} {num(p.sharpe):>7} {num(p.sharpe_standard_error):>7}"
            f" {num(p.t_stat):>6} {pct(p.cagr):>8} {pct(p.annual_volatility):>7}"
            f" {pct(p.max_drawdown):>8} {plain_pct(p.hit_rate):>6}"
            f" {('n=' + str(p.hit_periods_counted)) if p.hit_rate is not None else '':>6}"
            f"  {extra}")


def header() -> str:
    return (f"  {'instrument':<26} {'days':>6} {'Sharpe':>7} {'±se':>7} {'t':>6}"
            f" {'CAGR':>8} {'vol':>7} {'maxDD':>8} {'hit':>6} {'(n)':>6}")


def verdict(p, baseline=None, per_instrument=None) -> str:
    """What the sample does and does not support. Deliberately blunt.

    ``baseline`` is the always-long book over the same bars. A directional rule
    that does not beat holding the basket has not earned its complexity,
    whatever its t-statistic says, so that comparison outranks significance
    here: a rule can be reliably no better than buy-and-hold.

    ``per_instrument`` is every instrument's Sharpe, used to say whether the
    pooled figure is broad or is one instrument. Read after the fact it is
    diagnosis, not a second test -- but a pooled number that comes from one
    holding should never be reported as though it came from nine.
    """
    if p is None or not p.days:
        return "NOT MEASURED — no usable return stream"
    # A rule that never took a position and a rule with no data both come back
    # without a Sharpe. The first is a finding about the rule, the second is a
    # finding about the vendor, and they must not share a sentence.
    if p.exposure_fraction == 0:
        return (f"NO VIEW — flat on every one of {p.periods} rebalance dates. "
                f"Nothing was risked, so nothing was measured about whether it pays")
    if p.sharpe is None:
        return "NOT MEASURED — the return stream has no variance to measure"
    if not p.inferable:
        return (f"NOT ESTABLISHED — only {p.periods} holding periods; the standard "
                f"error on this Sharpe (±{p.sharpe_standard_error:.2f}) swamps it")

    # The sign is read BEFORE the hurdle. ``clears_hurdle`` asks whether the
    # result is distinguishable from zero, which is an |t| question and the
    # right one for significance -- but a rule at t=-3.1 is distinguishable
    # from zero in the direction that loses money, and reporting it as having
    # cleared the hurdle would call a reliably losing rule a success. That was
    # the first version of this function.
    if p.sharpe < 0:
        if abs(p.t_stat) >= 2.0:
            return (f"NEGATIVE on our history — Sharpe {p.sharpe:.2f} at "
                    f"t={p.t_stat:.2f}, significantly the wrong way")
        # Nor does an insignificant negative establish that the rule loses.
        # Overstating in this direction is the same error as overstating in the
        # other, and it is the one that deletes a rule on evidence that could
        # not have established its sign.
        return (f"NO EVIDENCE IT PAYS — Sharpe {p.sharpe:.2f} at t={p.t_stat:.2f}, "
                f"which is not significantly negative either; this sample cannot "
                f"establish the sign")

    if p.clears_hurdle:
        return (f"CLEARS the 3.0 hurdle (t={p.t_stat:.2f}) — survives on our history, "
                f"net of costs")
    if p.clears_conventional:
        return (f"NOT ESTABLISHED — t={p.t_stat:.2f} clears the conventional 2.0 but "
                f"not the 3.0 hurdle these rules need")
    return (f"NOT ESTABLISHED — positive but t={p.t_stat:.2f}; indistinguishable "
            f"from luck on this sample")


def earns_its_complexity(p, baseline) -> str | None:
    """Whether the rule beat simply holding the basket, over the same bars."""
    if p is None or baseline is None or p.sharpe is None or baseline.sharpe is None:
        return None
    if p.sharpe > baseline.sharpe:
        return (f"BEATS buy-and-hold: {p.sharpe:.2f} against {baseline.sharpe:.2f} "
                f"over the same bars")
    return (f"DOES NOT BEAT buy-and-hold: {p.sharpe:.2f} against {baseline.sharpe:.2f} "
            f"over the same bars. Whatever the t-statistic says, a directional rule "
            f"that loses to holding the basket has not earned its complexity")


def concentration(per_instrument) -> str | None:
    """How much of a pooled result is one instrument.

    The median Sharpe next to the pooled one tells the story without excluding
    anything: a pooled 0.55 beside a median of 0.06 is not a broad effect.
    """
    sharpes = sorted(s for s in per_instrument if s is not None)
    if len(sharpes) < 3:
        return None
    middle = len(sharpes) // 2
    median = (sharpes[middle] if len(sharpes) % 2
              else (sharpes[middle - 1] + sharpes[middle]) / 2)
    best, worst = sharpes[-1], sharpes[0]
    return (f"spread across {len(sharpes)} instruments: median Sharpe {median:+.2f}, "
            f"best {best:+.2f}, worst {worst:+.2f}")


def report_signal(signal_name: str, lag: int) -> None:
    signal = next((s for s in REGISTRY if s.name == signal_name), None)
    print(f"\n{BAR * 3} {signal_name} {BAR * (72 - len(signal_name))}")
    if signal is not None:
        print(f"  {signal.paper}")

    if signal_name in bt.NOT_BACKTESTABLE:
        print(f"\n  NOT BACKTESTABLE AS A STRATEGY\n  {bt.NOT_BACKTESTABLE[signal_name]}")
        return

    print(f"  rebalance every {bt.REBALANCE_EVERY_N_DAYS} trading days · "
          f"execution lag {lag} bar(s) · costs charged per side\n")

    pooled = bt.run(signal_name, lag=lag,
                    progress=lambda i, n, t: print(f"  … {i}/{n} {t}", file=sys.stderr))
    if not pooled.instruments:
        print("  no instrument in OUR_INSTRUMENTS matches this signal's asset types")
        return

    print(header())
    for item in pooled.instruments:
        # A row of dashes says nothing about why. An instrument whose rate
        # series 404'd and one whose rule never formed a view look identical
        # until the reason is printed, and they are not the same finding.
        reason = item.why_nothing_was_measured
        if reason:
            print(f"  {item.ticker:<26} nothing measured — {reason[:60]}")
            continue
        baseline = (f"long-only Sh {num(item.always_long.sharpe)}"
                    if item.always_long else "")
        print(performance_row(item.ticker, item.net, baseline))

    usable = pooled.usable
    if not usable or pooled.net is None:
        print("\n  nothing usable to pool")
        return

    print(f"\n{header()}")
    print(performance_row("POOLED (equal weight)", pooled.net,
                          f"long-only Sh {num(pooled.always_long.sharpe) if pooled.always_long else '—'}"))
    measured = [i for i in usable if i.net.sharpe is not None]
    if measured:
        print(f"\n  instruments with a positive Sharpe: {pooled.positive_sharpe} "
              f"of {len(measured)} measured")
    else:
        # "0 of 7" reads as seven failures when the truth is seven non-results.
        print(f"\n  instruments with a measurable Sharpe: none of {len(usable)}")
    print(f"  holding periods pooled: {pooled.net.periods}")
    print(f"  gross of costs: Sharpe {num(pooled.gross.sharpe)} "
          f"→ net {num(pooled.net.sharpe)}  "
          f"(cost drag {pct(pooled.net.annual_cost_drag, 2)}/yr, "
          f"turnover {num(pooled.net.annual_turnover, 1)}x/yr)")
    sharpes = [i.net.sharpe for i in usable]
    spread = concentration(sharpes)
    if spread:
        print(f"  {spread}")
    print(f"\n  VERDICT: {verdict(pooled.net)}")
    earned = earns_its_complexity(pooled.net, pooled.always_long)
    if earned:
        print(f"  BASELINE: {earned}")

    gaps = [(i.ticker, i.mid_sample_unavailable) for i in usable
            if i.mid_sample_unavailable]
    if gaps:
        print("\n  MID-SAMPLE DATA GAPS (dates the rule could not be formed after it "
              "had started):")
        for ticker, count in gaps:
            print(f"    {ticker}: {count}")
    short = [i.ticker for i in usable if not i.net.inferable]
    if short:
        print(f"\n  TOO SHORT TO INFER FROM: {', '.join(short)}")

    windows = {(i.first_date[:4], i.last_date[:4]) for i in usable}
    print(f"  history spans: {min(w[0] for w in windows)} to {max(w[1] for w in windows)}")


def sensitivity(signal_name: str) -> None:
    """How much of any edge lives in trading at a close you are still computing from."""
    print(f"\n{BAR * 3} sensitivity: execution lag {BAR * 56}")
    for lag in (0, 1, 2):
        pooled = bt.run(signal_name, lag=lag)
        if pooled.net is None or pooled.net.sharpe is None:
            print(f"  lag {lag} bar(s): not measured")
            continue
        note = "  ← the paper's convention: trades at the close it computed from" if lag == 0 else (
               "  ← ours" if lag == bt.EXECUTION_LAG_DAYS else "")
        print(f"  lag {lag} bar(s): pooled Sharpe {num(pooled.net.sharpe)}, "
              f"t={num(pooled.net.t_stat)}{note}")


def main() -> int:
    print(f"{BAR * 78}")
    print("  SIGNAL BACKTEST — the fourth bar: does it survive on our history")
    print(f"  instruments: {', '.join(bt.OUR_INSTRUMENTS)}")
    print(f"  significance hurdle: t ≥ {bt.SIGNIFICANCE_HURDLE_T} "
          f"(Harvey, Liu and Zhu, not the conventional 2.0)")
    print(f"{BAR * 78}")

    for signal in REGISTRY:
        try:
            report_signal(signal.name, lag=bt.EXECUTION_LAG_DAYS)
        except Exception as exc:  # noqa: BLE001 -- one signal must not end the sweep
            print(f"\n  {signal.name} FAILED: {type(exc).__name__}: {exc}")

    try:
        sensitivity(REGISTRY[0].name)
    except Exception as exc:  # noqa: BLE001
        print(f"\n  sensitivity FAILED: {type(exc).__name__}: {exc}")

    rates = bt.rate_history()
    if rates.errors:
        print(f"\n{BAR * 3} rate series that did not serve {BAR * 44}")
        for currency, reason in sorted(rates.errors.items()):
            print(f"  {currency}: {reason}")
    if rates.publication_lag_days:
        print("\n  publication lags measured from the data (an observation is only "
              "readable once this many days have passed):")
        for currency, lag in sorted(rates.publication_lag_days.items()):
            # A series that stopped publishing shows up as an enormous lag, and
            # then nothing is ever readable and carry goes quietly unavailable.
            # That is the safe failure, but it must not be a silent one.
            flag = "   ← looks discontinued; carry cannot use this leg" if lag > 120 else ""
            print(f"    {currency}: {lag}d{flag}")

    print(f"\n{BAR * 78}")
    print("  Read the POOLED line, not the best instrument. A rule that clears 2.0")
    print("  and not 3.0 has not been shown to work.")
    print(f"{BAR * 78}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
