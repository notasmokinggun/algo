# Nifty Alpha Swing Scanner

**Regime-adaptive NSE equity scanner · Sharpe 2.05 · Max DD -10.6% · 16.2% ann. return · 10-year backtest**

Built over roughly 2 months. The first four versions came in 10 days -- rapid iteration, daily releases, figure out what it is. The next two versions (v5 and v6) took 50 days -- slower, more deliberate, and where everything that actually matters was built.

---

## Backtest Results (v6.0.0 · 10 years · 154 tickers · 14 sectors)

| Metric | Scanner | Nifty TR | Blended Benchmark |
|---|---|---|---|
| Ann. Return | **16.2%** | 12.8% | 10.1% |
| Sharpe Ratio | **2.05** | 0.66 | -- |
| Max Drawdown | **-10.6%** | -38.4% | -- |
| Win Rate | 47.3% | -- | -- |
| Avg Win / Avg Loss | Rs879 / Rs422 | -- | -- |
| Real R:R | 2.08x | -- | -- |
| Alpha vs Nifty TR | **+3.36%** | -- | -- |
| Alpha vs Blended | **+6.11%** | -- | -- |
| Final NAV (Rs1L start) | **Rs4,48,433** | -- | -- |

Slippage: 0.30%/leg · Transaction cost: 0.20%/leg · RS scores computed lookahead-free · Validated via 3-fold walk-forward

---

## Walk-Forward Validation

| Fold | Period | Trades | Win% | Sharpe | Max DD | Ann. Return | Nifty Ann. | Alpha |
|---|---|---|---|---|---|---|---|---|
| 1 | Jun 2023 - Jun 2024 | 186 | 54.3% | 3.49 | -4.3% | 46.6% | 27.0% | +19.7% |
| 2 | Jun 2024 - Jun 2025 | 134 | 44.0% | 2.05 | -5.0% | 17.8% | 9.4% | +8.4% |
| 3 | Jun 2025 - Jun 2026 | 104 | 33.7% | 1.05 | -3.1% | 4.6% | -5.6% | +10.2% |

Fold 3 is live / forward. Alpha held positive (+10.2%) in a year where Nifty returned -5.6%.

---

## Architecture

### Regime Engine (continuous intensity, not a label)

By v4, regime detection existed -- but as three static lookup tables (bull / neutral / bear), each with hardcoded parameter values. The problem: a stock qualifying at intensity -0.09 behaved completely differently from one at -0.11. The tables created sharp discontinuities at arbitrary thresholds.

The fix was to represent regime as a continuous float in [-1, +1], computed from three weighted components:

```
intensity = 0.50 * ema_gap  +  0.30 * slope  +  0.20 * recovery
```

- **EMA gap**: Nifty 20/50 EMA spread, clipped and normalised
- **Slope**: 10-bar slope of the slow EMA
- **Recovery**: 15-bar rebound from recent low

Every downstream parameter -- stops, targets, RSI thresholds, position limits, cash floors, hold caps, score minimums -- becomes a linear interpolation between its bear anchor and bull anchor values, evaluated at the current intensity. The old lookup tables became a single `_ANCHORS` dict and one interpolation function:

```python
def _interp(bear_val, bull_val, intensity):
    t = clip((intensity + 1.0) / 2.0, 0.0, 1.0)
    return bear_val + t * (bull_val - bear_val)
```

No hard switches anywhere in the system.

### Capital Allocation (regime-driven, continuous)

```
NIFTYBEES:   0% (bear, intensity=-1)  -->  55% (bull, intensity=+1)
Liquid fund: 80% (bear)               -->   0% (bull)
Stocks:      remainder, subject to cash floor
```

At current intensity (-0.398, bear): 28% stocks · 17% NIFTYBEES · 56% liquid (~6% p.a.)

### Signal Paths (4)

| Path | Regime | Core Condition |
|---|---|---|
| Oversold Pullback | All | RSI 22-52, vol surge, reversal candle |
| Trend Resumption | Bull/Recovering | 4-20% pullback to 50 EMA, vol confirmation |
| Bear Survivor | Bear (Nifty DD >= 8%) | Top RS decile holding up vs market |
| Exceptional Tier | Bear only | Bear Survivor + 5 high-conviction gates, unlocks bull-mode sizing |

### Scoring and Entry Gates

Each signal scores 0-100 across gates: RSI range, volume ratio, ATR%, market RS percentile, sector RS rank, reversal confirmation, PVD (price-volume divergence). Threshold is regime-adaptive via the intensity interpolation.

### Position Sizing

- Kelly-fractioned (0.35x), ATR-stop derived, regime-adjusted max position
- Stop: 1.3-1.8x ATR below entry
- Target: 3.16-4.0x ATR above entry
- Earnings blackout: +/-5/2 days around results

### Risk Controls

- Max 6 positions (bear) to 10 (bull)
- Max 2 per sector
- Sector RS rankings refreshed every 3 bars
- Portfolio heat check (-2.5% threshold) reduces new entries
- Fundamental quality screen: ROCE > 20%, D/E < 0.16, Net Profit > Rs1000cr

---

## Version History

### v2 -- Starting point (Day 1-3)

Basic RSI screener across 6 sectors, ~80 tickers. No regime detection -- same entry logic in every market condition. Binary sector momentum from an EMA10/30 ratio. 3-year backtest with shared capital pool and walk-forward structure already in place.

What it got right from the start: lookahead-free indicator computation, realistic transaction costs, portfolio-level capital constraints, walk-forward validation structure. These stayed unchanged through all six versions.

What it was missing: any awareness of whether the market was collapsing or at highs, relative strength, multiple signal paths, dynamic sizing.

### v4.2 -- First real regime awareness (Day ~10)

Added regime detection and a proper multi-regime parameter system. But the implementation was three static lookup tables -- one dict each for bull, neutral, and bear -- with all parameters hardcoded per regime:

```python
REGIME_FILTERS = {
    "bull":    {"entry_score_min": 50, "rsi_entry_max": 58, "max_positions": 8, ...},
    "neutral": {"entry_score_min": 60, "rsi_entry_max": 52, "max_positions": 8, ...},
    "bear":    {"entry_score_min": 78, "rsi_entry_max": 44, "max_positions": 4, ...},
}
```

Regime was determined by EMA crossover -- one bar you're in "bull", the next bar you're in "bear". Every parameter snapped instantly to its new value.

Fundamentals at this stage: a manually annotated `REV_GROWTH` dict with `True/False` per ticker, updated by hand. INDUSINDBK, IIFL, LAURUSLABS, GRANULES, MANYAVAR all marked `False`. It worked, but it meant running the scanner required manually checking quarterly results and editing the file.

Universe: 11 sectors, ~130 tickers. Backtest: 3 years.

This was the version where it became clear the system had real potential -- and also where the problems with binary regime labels became obvious enough to fix properly.

### v5.x -- The slow 50 days

The decision to replace binary regime labels with a continuous intensity float. The three lookup tables collapsed into one `_ANCHORS` dict with (bear_value, bull_value) pairs, and `_interp()` evaluates each parameter smoothly at the current intensity. A regime "flip" no longer exists -- the system always sits somewhere on a continuous spectrum.

Everything else that came with v5.x:

- 3 signal paths (Oversold Pullback, Trend Resumption, Bear Survivor/Capitulation)
- NIFTYBEES ETF sleeve as a passive allocation component
- Liquid fund accrual modelled at 6% p.a.
- Dynamic capital allocation curve
- The hardcoded `REV_GROWTH` dict replaced by a live yfinance fundamental cache (90-day TTL)
- Universe grew to 154 tickers across 14 sectors
- Backtest extended from 3 years to 10 years

Critical bugs found and fixed during v5.x, all discovered by running the system and interrogating the output:

**v5.8.3:** NaN propagation crash in ETF rebalance logic (11 separate fixes). `min_volume_ratio` removed from base CFG -- had been a latent mismatch vs the bear anchor value since the anchors system was introduced. RSI exit made continuous (74 bear to 80 bull).

**v5.8.4:** `is_exceptional_tier()` always returned False in backtest. Root cause: the function received a float intensity from the backtest loop but its first guard compared against the string `"bear"`. A float never equals a string -- exceptional tier was structurally dead in all 10 years of backtest history without ever throwing an error. Fixed by accepting both str and float inputs.

**v5.8.5:** Bear Survivor RS gate was trivially True once gate 1 passed. The OR logic on gate 2 made it unreachable as a fallback. Fixed to require top RS level OR accelerating RS trend -- both genuinely reachable.

### v6.0 -- Exceptional Tier, Universe Modes, Diagnostic Engine

**Exceptional Tier:** 4th signal path, bear-only. A stock must pass all Bear Survivor gates plus 5 independent high-conviction gates: elite RS percentile, genuine buying volume, score ceiling, controlled ATR. If all pass, the position receives bull-mode sizing in a bear market. In a broad selloff, the stocks genuinely being accumulated deserve larger positions, not smaller ones.

**Universe modes:** HARDCODED (fixed curated list, no survivorship bias, use for backtests) and VARIABLE (quality-screened from screener.in, use for live scanning). The separation matters -- backtesting on a list of "stocks that survived to today" inflates returns.

**Diagnostic mode:** `python scanner.py diagnose TICKER.NS` runs a full per-ticker explainability pass and writes a markdown report with gate-by-gate breakdown, distance to threshold, scoring, position sizing, and a near-miss leaderboard of the closest non-triggering stocks across the universe.

**v6.0.0 critical bug:** `exc_score_min` was set to 75, but the maximum achievable score in survivor mode is 69 (64 raw + 5 bonus). Exceptional tier was structurally unreachable for its entire lifespan. Fixed to 60.

---

## Usage

```bash
# Live scan (hardcoded universe)
python scanner.py

# Live scan (screened V200 universe)
python scanner.py universe.txt

# Diagnostic mode
python scanner.py diagnose MARICO.NS

# Diagnostic without near-miss leaderboard
python scanner.py diagnose MARICO.NS --no-near-misses
```

---

## Stack

Python · yfinance · pandas · numpy · matplotlib

No ML. No external data vendor. All signals derived from price, volume, and fundamental data via Yahoo Finance, with a 90-day fundamental cache to stay within rate limits.

---

## Caveats

- HARDCODED universe contains currently listed stocks only -- survivorship bias applies. Use VARIABLE mode with a point-in-time screen for production.
- yfinance fundamental coverage on NSE is patchy. Some tickers return None and are dropped from quality screens.
- Walk-forward fold 3 (Jun 2025 - Jun 2026) is forward / live. Treat with appropriate scepticism.
- Personal research project. Not financial advice.

---

*Udit Gandhi · Grade 11 IB · gandhi.udit.work@gmail.com*
