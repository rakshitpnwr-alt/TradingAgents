## BTC-USD Technical Analysis Report — as of 2026-09-29 (latest verified data: 2026-09-28 close)

### Market Context
BTC-USD has undergone a powerful bull run since mid-August 2026, rallying from the low-$60,000s to a peak near $87,363 (intraday high, 2026-09-21) before consolidating in the low-to-mid $80,000s. The most recent verified close (2026-09-28) was **$83,502.61**, down from the prior day's $84,458.09 — a pullback of roughly 1.1% within an otherwise firmly bullish structure.

---

### 1. Trend Structure (Moving Averages)
- **close_10_ema (83,294.79)** sits just below the current price (83,502.61), confirming short-term momentum remains constructive, though the EMA has flattened over the last 3–4 sessions (83,248 → 83,295) after a steep rise from ~77,000 in mid-September — signaling the sharp rally is losing acceleration.
- **close_50_sma (76,477.71)** is well below price, and has been rising steadily (from ~63,300 in early August to ~76,478 now), confirming a strong intermediate-term uptrend.
- **close_200_sma (71,137.77)** is also rising and sits far below both faster averages. The 50 SMA > 200 SMA (a "golden cross" configuration established weeks ago) confirms a structurally bullish long-term backdrop.
- **Takeaway:** All three MAs are stacked bullishly (price > 10 EMA ≈ price > 50 SMA > 200 SMA), but the flattening 10 EMA versus a still-fast-rising price earlier this month suggests the parabolic leg (Sep 18–22) is cooling into consolidation/pullback mode.

### 2. Momentum (MACD & RSI)
- **MACD (2,309.07) vs Signal (2,235.02)**: MACD is still above signal line (positive histogram +74.04), but both MACD and histogram have been **declining steadily since peaking near 3,878 on 8/30 and again near 2,486 on 9/24** — a clear bearish divergence forming as price made new highs (9/21–9/22 peak) while MACD momentum waned. This is an early warning of weakening upside thrust, not yet a confirmed bearish crossover.
- **RSI (60.92)**: Down from a hot overbought reading of 73.85 on 9/21 (when price hit its peak) to a more neutral 60.92 now. This retreat from overbought territory alongside a price pullback is consistent with a healthy cooling-off rather than a trend reversal — RSI remains above the 50 midline, preserving bullish bias.
- **Takeaway:** Momentum is decelerating from an overbought extreme but hasn't broken down. Watch for RSI to hold above ~50 and MACD histogram to stop shrinking as confirmation the uptrend resumes; a MACD bearish crossover (MACD < signal) combined with RSI below 50 would flag a more serious correction.

### 3. Volatility (Bollinger Bands & ATR)
- **Bollinger Bands:** Middle band (20 SMA) = 80,682.86; Upper = 88,261.99; Lower = 73,103.73. Price (83,502.61) sits comfortably between the middle and upper band, roughly 39% of the way from middle to upper band — not stretched to the extreme, unlike 9/21 when price approached the upper band amid the parabolic spike.
- Band width has expanded significantly since late August (was ~72,469 mid to ~86,718 upper on 8/31, now 80,683 mid to 88,262 upper), reflecting the sharp rise in realized volatility during the rally.
- **ATR (2,233.56)**, down slightly from a peak of ~2,510 on 9/23, indicates volatility remains elevated relative to the July–August range (ATR was typically 2,100–2,300 then too, but price levels were much lower, meaning volatility-as-%-of-price has effectively decreased slightly) — still, an ATR of ~$2,233 implies typical daily swings of ~2.7% at current price levels, which should inform stop-loss sizing (e.g., a 1.5–2x ATR stop ≈ $3,300–4,500 below entry).

### 4. Volume Confirmation (VWMA)
- **VWMA (82,227.28)** is below the current close (83,502.61) and below the 10 EMA, and has been rising in lockstep with price throughout the rally — confirming that the upward move has been supported by genuine volume, not a low-volume drift. Notably, the largest volume spikes (Sep 3: 40.5B, Sep 21: 57.7B, Sep 18: 40.4B) all coincided with the sharpest up-days, reinforcing that buying pressure has been the dominant force behind each breakout leg.
- **Takeaway:** No volume-based divergence is visible yet; the trend has institutional/participant conviction behind it.

---

### Synthesis & Actionable Insights
1. **Primary trend remains bullish** — price above all key MAs, golden-cross structure intact, volume-confirmed uptrend (VWMA rising with price).
2. **Short-term momentum is cooling from overbought extremes** — RSI down from 73.9 to 60.9, MACD histogram shrinking, price pulled back ~1.1% on 9/28 from the 9/27 close. This looks like consolidation after the 8/19–9/22 surge rather than trend reversal, but traders should watch closely.
3. **Key levels to watch:** Bollinger middle band (~$80,683) and the rising 10 EMA (~$83,295) as near-term dynamic support; a break below the 10 EMA with rising volume would increase odds of a deeper retracement toward the 50 SMA (~$76,478) or VWMA (~$82,227). Upper band (~$88,262) remains resistance/breakout target if momentum reaccelerates.
4. **Risk management:** With ATR at ~$2,234, use stops sized at 1.5–2x ATR (~$3,300–$4,500) to accommodate normal volatility swings without premature stop-outs.
5. **Divergence watch:** The MACD/RSI decline against a still-elevated price is the most important signal to track over the next several sessions — if it resolves with a shallow pullback and re-acceleration in momentum, that reinforces the bull case; a break below the 50 SMA on high volume would be the first serious bearish signal.

---

### Discrepancy Note
The `get_stock_data` daily OHLCV table only extends through 2026-09-28 (no row for 2026-09-29 yet), consistent with the verified snapshot's use of 2026-09-28 as the latest available row. All indicator tool calls likewise returned "N/A" for 2026-09-29, confirming data hasn't posted yet for the current date — no conflicting values were found between tools.

---

### Summary Table

| Category | Indicator | Latest Value (2026-09-28) | Signal |
|---|---|---:|---|
| Trend | close_10_ema | 83,294.79 | Price just above; short-term uptrend flattening |
| Trend | close_50_sma | 76,477.71 | Rising steadily; medium-term bullish |
| Trend | close_200_sma | 71,137.77 | Rising; long-term bullish, golden cross intact |
| Momentum | macd / macds / macdh | 2,309.07 / 2,235.02 / 74.04 | Bullish but histogram shrinking — momentum divergence |
| Momentum | rsi | 60.92 | Cooling from overbought (73.9 on 9/21) but still >50 |
| Volatility | boll (mid) | 80,682.86 | Near-term support zone |
| Volatility | boll_ub | 88,261.99 | Upside breakout target/resistance |
| Volatility | boll_lb | 73,103.73 | Downside band, far from current price |
| Volatility | atr | 2,233.56 | Elevated; use for stop sizing (~1.5-2x ATR) |
| Volume | vwma | 82,227.28 | Below price, confirms volume-backed uptrend |

**Overall stance: Bullish primary trend with short-term consolidation/cooling momentum — constructive but requires confirmation that the MACD/RSI divergence resolves upward rather than breaking down.**