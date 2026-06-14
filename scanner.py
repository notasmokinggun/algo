"""
══════════════════════════════════════════════════════════════════════
  NIFTY ALPHA SWING SCANNER v6.0.0  [FULLY CONTINUOUS REGIME ENGINE]

  WHAT'S NEW IN v6.0.0  —  TWO UNIVERSE MODES (engine unchanged)
  ──────────────────────────────────────────────────────────────
  The SAME selection engine, with exactly two ways to pick the universe:

    MODE 1 — HARDCODED (default):  the curated UNIVERSE dict below.
        python scanner.py
        # use this for BACKTEST and WALK-FORWARD (fixed list = no survivorship)

    MODE 2 — universe.txt:  the live V200 list you scraped from screener.in.
        python scanner.py universe.txt          (or --universe-file universe.txt)
        # use this for the LIVE / forward scan

  The V200 list is produced by the companion scraper fetch_v200_universe.py
  (logs into a free screener.in account, scrapes today's screen, writes
  universe.txt with one TICKER.NS[,SECTOR] per line).

  Helpers in this file: parse_universe_file(), _passes_tradeability()
  (listing history >= 4y, avg daily value >= Rs 10cr, price >= Rs 30),
  build_quality_universe(), set_universe() — which rebuild UNIVERSE /
  ALL_TICKERS / SECTOR_OF from the file.
  NOTE: keep backtests on the HARDCODED list; a screened-today list carries
  survivorship bias in a backtest. Use universe.txt for live scanning.

  WHAT'S NEW vs v5.9.0
  ─────────────────────
  BUGFIX [CRITICAL] exc_score_min=75 was above survivor mode ceiling
    Survivor max raw=64, normalised=64, +5 score_bar bonus=69.
    exc_score_min=75 was 6pts above the absolute maximum. Exceptional
    tier was structurally unreachable for survivor_mode in all history.
    Fix: exc_score_min 75 → 60. Fires on strong survivors (top ~40%
    of qualifying signals) while rejecting marginal ones.

  CARRIED FORWARD from v5.9.0

  WHAT'S NEW vs v5.8.3
  ─────────────────────
  BUGFIX [CRITICAL] exceptional_tier always False in backtest (0 fires)
    Root cause: is_exceptional_tier(row, score, regime_label) received
    a float intensity (e.g. -0.7) from the backtest entry loop, but the
    function's first guard is `if regime_label != "bear": return False`.
    A float never equals the string "bear" → exceptional_tier was
    always False in run_backtest(), even on qualifying stocks.
    Fix: is_exceptional_tier() now accepts str OR float; converts float
    via _intensity_label() before comparing. Both call-sites confirmed.

  BUGFIX [LOGIC] is_exceptional_tier _rs_ok gate was always True
    Line 1729 already required mkt_rs >= exc_mkt_rs_min; line 1730
    then checked _rs_ok = (mkt_rs >= exc_mkt_rs_min) OR (...), which
    was trivially True once line 1729 passed. The OR branch
    (rs_trend >= exc_rs_trend_min) was never reachable as a fallback.
    Fix: gate 1 now checks EITHER elite RS level OR strong trend
    (mkt_rs >= exc_mkt_rs_min OR rs_trend >= exc_rs_trend_min).
    Gate-level comment updated. Semantics: top RS OR accelerating RS
    qualifies for exceptional tier — same spirit as v5.8.5 survivor fix.

  BUGFIX [DIAGNOSTIC] _bf_gate_checks used stale rs_trend threshold
    Survivor gate "RS trend accelerating" still used the pre-v5.8.5
    threshold rs_trend20 >= 6, while the live scorer uses >= 4.
    Fixed to match live logic so diagnostic output is accurate.

  PRINT [CLARITY] run() header updated to v5.9.0

  CARRIED FORWARD from v5.8.3
  ─────────────────────────────
  BUGFIX [EXIT] rsi_exit continuous anchor (74 bear → 80 bull)
  BUGFIX [BMC] tiered bear survivor activation (8% / 10% DD)
  CLEANUP [CFG] min_volume_ratio removed from base CFG
  BUGFIX [BACKTEST] NaN propagation crash in _etf_rebalance (11 fixes)
  FUTUREPROOF [DATA] _sanitise_df() — NaN-clean OHLCV at source
  FEATURE [DIAGNOSTIC] Comprehensive explainability layer
  FEATURE [CORE] Fully continuous regime engine
══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timedelta, date
import json, os, time, warnings, pickle, shutil
from typing import Optional

warnings.filterwarnings("ignore")


def _backup_corrupted_file(path: str) -> None:
    """Move a corrupted cache file aside so the next save does not silently wipe history."""
    try:
        if os.path.exists(path):
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.move(path, f"{path}.corrupted.{stamp}")
    except Exception:
        pass


def _load_pickle_cache(path: str) -> dict:
    """Load a pickle cache; back up the file if it is corrupted."""
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = pickle.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            _backup_corrupted_file(path)
    return {}


# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════
CFG = {
    
# ── CAPITAL & POSITION SIZING ─────────────────────────

    "capital":                 30_000,
    "min_position":              5_000,
    "max_position":             18_000,   # was 20k — smaller max in high-vol bear
    "max_positions":                10,

    "risk_per_trade":            0.008,   # was 0.01RISK # crude @$93+, RBI hold, reduce per-trade risk
    "kelly_fraction":             0.35,   # was 0.40RISK # lower kelly in uncertain macro regime
    "atr_stop_mult":              1.5,    # was 1.8BEAR # tighter stop, gap-down risk is real
    "atr_trail_mult":             1.3,    # was 1.4BEAR # lock gains faster in choppy bear
    "profit_target_mult":         3.5,
    "max_hold_days":               25,

    
# ── PER-REGIME HOLD CAPS ──────────────────────────────

    "max_hold_bear":               16,    # was 12BEAR # IT/pharma leaders need more room
    "max_hold_neutral":            18,
    "max_hold_recovering":         24,    # was 18RECOV # recovery runs longer per historical pattern
    "max_hold_bull":               25,

    
# ── CASH FLOORS BY REGIME ─────────────────────────────

    "cash_floor_bear":             0.80,  # was 0.70BEAR # RBI hold + crude + FII selling = stay heavy cash
    "cash_floor_neutral":          0.50,
    "cash_floor_recovering":       0.25,  # was 0.35RECOV # deploy FAST when regime flips, DII-led rally
    "cash_floor_bull":             0.20,

    
# ── PORTFOLIO HEAT ────────────────────────────────────

    "portfolio_heat_threshold":   -0.025, # was -0.03BEAR # cut exposure sooner in stagflation-lite env
    "portfolio_heat_min_pos":       2,

    
# ── INDICATORS ────────────────────────────────────────

    "rsi_window":                  14,
    "atr_window":                  14,
    "volume_window":               20,
    "pvd_window":                  10,
    "rs_window":                  252,

    
# ── REGIME DETECTION ──────────────────────────────────

    "regime_ema_fast":             20,
    "regime_ema_slow":             50,
    "regime_slope_bars":           10,
    "regime_confirm_bars":          5,
    "regime_confirm_bars_up":       3,    # was 4RECOV # detect regime flip 1 bar earlier, catch the move

    
# ── SECTOR RS ─────────────────────────────────────────

    "sector_rs_top_n":              4,
    "sector_rs_days":              20,

    
# ── ENTRY GATES ───────────────────────────────────────

    "entry_score_min":             58,    # was 53BEAR # fewer but higher quality entries in bear

    
# ── PATH 1: OVERSOLD PULLBACK ─────────────────────────

    "rsi_entry_max":               50,    # was 55BEAR # only properly oversold, not just dipping
    "rsi_entry_min":               22,

    
# ── PATH 2: TREND RESUMPTION ──────────────────────────

    "tr_ema_period":               50,
    "tr_pullback_min_pct":          4.0,
    "tr_pullback_max_pct":         20.0,
    "tr_rsi_min":                  32,    # was 36RECOV # catch early recovering-phase entries
    "tr_rsi_max":                  65,
    "tr_vol_spike_min":             1.15,  # was 1.10RECOV # need cleaner vol confirmation
    "tr_close_upper_half":         True,
    "tr_down_vol_decay_bars":       5,
    "tr_mkt_rs_min":               42,    # was 40
    "tr_score_min":                50,    # was 48

    
# ── PATH 3: BEAR SURVIVOR / CAPITULATION ──────────────

    "bmc_mkt_rs_min":              70,    # was 65BEAR # only absolute relative strength leaders
    "bmc_max_drawdown_pct":        18.0,
    "bmc_nifty_down_pct":          10.0,  # was 8.0BEAR # full activation: capitulation mode + survivor mode at full score_min
    "bmc_nifty_early_dd":           8.0,  # v5.8.3: early tier — both modes allowed but score_min raised to 65
    "bmc_rs_trend_accel_bars":     20,
    "bmc_rs_trend_min":             8.0,
    "bmc_max_atr_pct":              5.0,
    "bmc_min_atr_pct":              0.6,
    "bmc_score_min":               55,    # was 50BEAR # higher quality gate on BMC — avg PnL was ₹16
    "bmc_vol_stable_ratio":         0.75,
    "bmc_max_position":           10_000, # was 12000RISK
    "bmc_profit_target_mult":       2.0,
    "bmc_atr_stop_mult":            1.3,  # was 1.5BEAR

# ── EXCEPTIONAL TIER (v5.8.4) ─────────────────────────────────────────
# Bear-only overlay. Stock must first clear all standard BMC path gates,
# then pass 5 independent high-conviction gates to unlock bull-mode sizing.
# Uses only pre-computed row values — zero new data dependencies.

    "exc_mkt_rs_min":              85,    # top ~15% of universe by market RS
    "exc_rs_trend_min":             5.0,  # 20-bar RS acceleration OR sustained mkt_rs>=85
    "exc_vol_ratio_min":            1.3,  # genuine buying pressure present
    "exc_score_min":               60,    # was 75 — survivor mode max is 69 (64 raw/100 norm + 5 bonus); 75 was above ceiling
    "exc_atr_pct_max":              4.5,  # controlled vol — not a wildcard move
    # Sizing unlocked when all 5 gates pass
    "exc_max_position":           18_000, # bull-anchor max_position value
    "exc_stop_mult":                1.8,  # _ANCHORS["atr_stop_mult"] bull val
    "exc_target_mult":              4.0,  # _ANCHORS["profit_target_mult"] bull val
    "exc_max_hold_days":           22,    # recovering-regime hold, lets winner breathe

    
# ── DISABLED PATHS (RETAINED FOR REFERENCE) ───────────

    "rsi_breakout_min":           50,
    "rsi_breakout_max":           65,
    "mkt_rs_accel_min":          999,
    "bull_momentum_rsi_min":     999,
    "bull_momentum_rsi_max":      72,

    
# ── EXIT RULES ────────────────────────────────────────

    "rsi_exit":                    74,   # fallback only — live value comes from rcfg["rsi_exit"] (continuous anchor 74→80)
    "min_atr_pct":                  1.5,
    "max_atr_pct":                  9.0,
    "vol_decline_window":            4,
    "tr_vol_decline_window":         5,
    "max_per_sector":                2,
    # NOTE: min_volume_ratio removed from base CFG (v5.8.3) — value is
    # always sourced from rcfg via _ANCHORS anchor (1.05 bear → 0.68 bull).
    # Base CFG value of 0.85 was a latent mismatch vs bear anchor of 1.05.

    
# ── UNIVERSE & COSTS ──────────────────────────────────

    "min_avg_daily_value_cr":       5.0,
    "cost_per_leg":              0.0020,
    "slippage_per_leg":          0.0030,

    
# ── BACKTEST & WF ─────────────────────────────────────

    "backtest_years":              10,
    "wf_train_years":               7,
    "wf_test_months":              12,
    "wf_folds":                     3,

    "journal_file":        "journal.json",
    "liquid_fund_annual":           0.06,

# ── DYNAMIC NIFTY / LIQUID ALLOCATION (v5.6.0) ────────────
# Continuous curve: alloc = base + (max-base) * t  where t=(intensity+1)/2
# Bear  (intensity=-1) → t=0 ; Bull (intensity=+1) → t=1
#
#   NIFTYBEES:  0%  at bear  →  55% at bull
#   Liquid:    80%  at bear  →   0% at bull
#   (stock budget = 1 - cash_floor, controlled by cash_floor_* keys above)

    "nifty_alloc_min":              0.00,   # NIFTY share at full bear
    "nifty_alloc_max":              0.55,   # NIFTY share at full bull
    "liquid_alloc_min":             0.00,   # liquid share at full bull
    "liquid_alloc_max":             0.80,   # liquid share at full bear
    # tolerance band for daily ETF rebalance (avoid churn)
    "nifty_alloc_tolerance":        0.03,

    
# ── PVD EXIT ──────────────────────────────────────────

    "pvd_exit_bars":                3,
    "pvd_exit_window":              5,

    
# ── RS TREND ──────────────────────────────────────────

    "rs_trend_window":             10,
    "rs_trend_min":                 5,
    "override_rs_trend_min":       12,

    
# ── NIFTYBEES ETF PATH (DISABLED) ─────────────────────

    "nifty_etf_ticker":    "NIFTYBEES.NS",
    "nifty_score_min":            999,    # was 58KILL # path 4 EV-negative: 25% WR, -₹2891 total
    "nifty_rsi_min":               50,
    "nifty_rsi_max":               75,
    "nifty_etf_cost_per_leg":    0.0010,

    
# ── REGIME INTENSITY ──────────────────────────────────

    "intensity_ema_gap_clip":      0.03,
    "intensity_slope_clip":        0.005,
    "intensity_recovery_clip":     0.05,
    "intensity_w_ema":             0.50,
    "intensity_w_slope":           0.30,
    "intensity_w_recovery":        0.20,
    "intensity_kelly_scale":       0.35,  # was 0.40RISK # scale down kelly in current bear intensity
    "recovery_window":             15,

    
# ── OVERRIDE THRESHOLDS ───────────────────────────────

    "override_mkt_rs_bull":        68,
    "override_mkt_rs_neutral":     74,
    "override_mkt_rs_bear":        84,

    "override_rank_mkt_rs_penalty":      12,
    "override_rank_mkt_rs_penalty_bull": 10,
    "override_rank_score_base":          10,
    "override_rank_score_extra":         15,

    
# ── FUNDAMENTALS & EARNINGS ───────────────────────────

    "fundamental_cache_ttl_days":  90,
    "fundamental_cache_file":  "fundamental_cache.pkl",
    "earnings_blackout_before":     5,
    "earnings_blackout_after":      2,
    "earnings_cache_file":     "earnings_cache.pkl",
    "earnings_cache_ttl_days":     14,
}
BASE_DIR            = os.path.dirname(os.path.abspath(__file__))
JOURNAL_PATH        = os.path.join(BASE_DIR, CFG["journal_file"])
FUND_CACHE_PATH     = os.path.join(BASE_DIR, CFG["fundamental_cache_file"])
EARNINGS_CACHE_PATH = os.path.join(BASE_DIR, CFG["earnings_cache_file"])

MIN_WARMUP_BARS = CFG["rs_window"] + 30  # 282 bars


def _clear_non_journal_json_files():
    """Delete only the three replaceable walk-forward fold outputs."""
    removed = []
    for name in ("fold1.json", "fold2.json", "fold3.json"):
        file_path = os.path.join(BASE_DIR, name)
        try:
            os.remove(file_path)
            removed.append(name)
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            pass
    if removed:
        print(f"  Cleared old walk-forward files: {', '.join(removed)}")


# ══════════════════════════════════════════════════════════════════════
# UNIVERSE
# ══════════════════════════════════════════════════════════════════════

NIFTY = "^NSEI"

UNIVERSE = {
    
    "DEFENCE": [
        "BEL.NS", "HAL.NS", "SOLARINDS.NS", "MAZDOCK.NS", "BDL.NS",
        "COCHINSHIP.NS", "GRSE.NS", "DATAPATTNS.NS", "ZENTEC.NS",
        "BEML.NS", "MTARTECH.NS", "ASTRAMICRO.NS",
        "PARAS.NS", "IDEAFORGE.NS",
    ],
    
    "TECH": [
        "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "TATAELXSI.NS",
        "NEWGEN.NS", "DIXON.NS", "KAYNES.NS", "AMBER.NS",
        "TECHM.NS", "LTTS.NS", "HAPPSTMNDS.NS",
        "TANLA.NS", "KFINTECH.NS",
    ],
    
    "BANKING": [
        "SBIN.NS", "ICICIBANK.NS", "AXISBANK.NS", "INDUSINDBK.NS",
        "FEDERALBNK.NS", "MAHABANK.NS", "CANBK.NS", "MUTHOOTFIN.NS",
        "CHOLAFIN.NS", "MANAPPURAM.NS", "IIFL.NS",
        "INDIANB.NS", "BSE.NS", "AAVAS.NS", "PNBHOUSING.NS",
        "RBLBANK.NS", "YESBANK.NS",
    ],
    "PHARMA": [
        "DIVISLAB.NS", "LUPIN.NS", "DRREDDY.NS", "CIPLA.NS",
        "AUROPHARMA.NS", "ZYDUSLIFE.NS", "GLENMARK.NS", "LAURUSLABS.NS",
        "ALKEM.NS", "IPCALAB.NS", "GRANULES.NS",
        "GLAND.NS", "WOCKPHARMA.NS",
    ],
    
    "CONSUMER": [
        "TATACONSUM.NS", "VBL.NS", "BRITANNIA.NS", "DABUR.NS",
        "EMAMILTD.NS", "GODREJCP.NS", "TRENT.NS", "ETERNAL.NS",
        "DELHIVERY.NS", "NYKAA.NS", "CAMPUS.NS",
    ],
    # Swing rationale: removed safe large caps. Auto ancillaries swing hard on
    # monthly sales data & EV news. OLECTRA is the most volatile EV bus play.
    # TIINDIA, UNOMINDA, SANSERA, BFORGЕ are liquid midcap auto swings.
    "AUTO": [
        "M&M.NS", "ASHOKLEY.NS", "EICHERMOT.NS", "MOTHERSON.NS",
        "SUNDRMFAST.NS", "OLECTRA.NS", "TIINDIA.NS",
        "UNOMINDA.NS", "SANSERA.NS", "BHARATFORG.NS", 
    ],
    
    "FMCG": [
        "HINDUNILVR.NS", "ITC.NS", "NESTLEIND.NS", "MARICO.NS",
        "COLPAL.NS", "PGHH.NS", "BAJAJCON.NS", "JYOTHYLAB.NS",
        "RADICO.NS", "UNITDSPR.NS", "GODFRYPHLP.NS",
    ],
    
    "CHEMICALS": [
        "PIDILITIND.NS", "ATUL.NS", "DEEPAKNTR.NS",
        "VINATIORGA.NS", "NAVINFLUOR.NS", "CLEAN.NS",
        "TATACHEM.NS", "GNFC.NS", "ALKYLAMINE.NS",
        "FLUOROCHEM.NS", "PRIVISCL.NS",
    ],

    "INFRA": [
        "LT.NS", "NTPC.NS", "POWERGRID.NS", "IRFC.NS", "PFC.NS",
        "RECLTD.NS", "NBCC.NS", "RVNL.NS", "IRCON.NS", "RAILTEL.NS",
        "ENGINERSIN.NS", "BHEL.NS", "TITAGARH.NS", "HUDCO.NS",
        "POLYCAB.NS", "KEI.NS", "AIAENG.NS", "KNRCON.NS", "APLAPOLLO.NS",
    ],
    
    "POWER": [
        "SUZLON.NS", "TATAPOWER.NS", "CESC.NS",
        "NHPC.NS", "SJVN.NS", "TORNTPOWER.NS",
        "JSWENERGY.NS", "WAAREEENER.NS", "ADANIGREEN.NS", "VOLTAS.NS",
    ],
    
    "CAPITAL_GOODS": [
        "SIEMENS.NS", "ABB.NS", "CUMMINSIND.NS", "THERMAX.NS",
        "CGPOWER.NS", "GVT&D.NS",
    ],
    
    "HEALTHCARE": [
        "APOLLOHOSP.NS", "FORTIS.NS", "MAXHEALTH.NS", "RAINBOW.NS",
        "KIMS.NS", "METROPOLIS.NS", "LALPATHLAB.NS", "THYROCARE.NS",
        "MEDANTA.NS", "VIJAYA.NS",
    ],
    
    "TEXTILES": [
        "PAGEIND.NS", "KPRMILL.NS", "RAYMOND.NS", "TRIDENT.NS",
        "ARVIND.NS", "WELSPUNLIV.NS", "GOKEX.NS", "MANYAVAR.NS",
    ],
    "INDEX": [
        "NIFTYBEES.NS",
    ],
}

ALL_TICKERS = list({t for tickers in UNIVERSE.values() for t in tickers})
SECTOR_OF   = {t: s for s, tickers in UNIVERSE.items() for t in tickers}
# True only when scanner.py was launched with a universe text file.
# In this mode the external selector is authoritative and sector eligibility
# must not discard an otherwise valid stock-level signal.
UNIVERSE_FILE_MODE = False
_LAST_BUILT_UNIVERSE_SOURCE = "hardcoded"


# ══════════════════════════════════════════════════════════════════════
# UNIVERSE MODE: "hardcoded" (above) OR "variable" (quality-screened)
# ══════════════════════════════════════════════════════════════════════
#
# Two selectable universe paths:
#
#   PATH 1 — HARDCODED  (default):  the curated UNIVERSE dict above.
#       python scanner.py
#
#   PATH 2 — VARIABLE:  a quality-screened universe.
#       python scanner.py --universe variable --universe-file quality.txt
#
# DESIGN (matches the screener.in + yfinance hybrid):
#   * The FUNDAMENTAL quality screen is run on screener.in (its strength),
#     because yfinance cannot supply pledged %, public holding, ROCE or a
#     clean 5-yr ROE. You paste the query below into screener.in, export the
#     passing names into a simple text file (one NSE code per line, optional
#     ",SECTOR"), and point --universe-file at it.
#   * yfinance then applies the TRADEABILITY filters (liquidity, price,
#     listing history) so the engine only ever sees swingable names.
#   * The hand-tuned scanner.py engine then does the actual selection.
#
# SCREENER.IN QUERY (non-banks) — paste into screener.in "Create a screen":
#
#   Net profit > 1000 AND
#   Debt to equity < 0.16 AND
#   Return on capital employed > 20 AND
#   Public holding < 30 AND
#   Pledged percentage < 10 AND
#   Average return on equity 5Years > 15 AND
#   Market Capitalization > 15000
#
# SCREENER.IN QUERY (banks / NBFCs) — run separately and merge:
#
#   Return on equity > 10 AND
#   Net profit > 1000 AND
#   Market Capitalization > 15000
#
# Tradeability defaults (applied here, in code): mcap is already enforced by
# the screen; we additionally require listing history >= 4y, avg daily traded
# value >= Rs 10cr, and price >= Rs 30.

QUALITY_SCREEN_NONBANK = (
    "Net profit > 1000 AND Debt to equity < 0.16 AND "
    "Return on capital employed > 20 AND Public holding < 30 AND "
    "Pledged percentage < 10 AND Average return on equity 5Years > 15 AND "
    "Market Capitalization > 15000"
)
QUALITY_SCREEN_BANK = (
    "Return on equity > 10 AND Net profit > 1000 AND "
    "Market Capitalization > 15000"
)

# Tradeability thresholds for the VARIABLE path
UNIV_MIN_HISTORY_YEARS = 4.0
UNIV_MIN_ADV_CR        = 10.0    # avg daily traded value (Rs crore)
UNIV_MIN_PRICE         = 30.0
UNIV_MIN_MCAP_CR       = 15000.0 # informational; the screener query enforces it
UNIV_LOOKBACK_DAYS     = 120

# ── V200 AUTO-SCREEN (yfinance fundamentals) ─────────────────────────────
# `python scanner.py variable`  auto-screens NSE via yfinance using:
#     non-bank:  Net profit (TTM) > 200cr  AND  ROCE > 20%  AND  D/E < 0.25
#     bank/NBFC: ROE > 10%  AND  Net profit (TTM) > 1000cr
# HONEST CAVEAT: yfinance does NOT expose ROCE — it is COMPUTED from EBIT and
# (Total Assets - Current Liabilities), and yfinance's NSE statement coverage
# is patchy, so many names return None and get dropped. It's also SLOW
# (per-ticker .info + statements, rate-limited) — first full screen over the
# NSE list can take a long time, so results are cached for 90 days.
V200_NP_MIN_CR        = 200.0
V200_ROCE_MIN         = 20.0
V200_DE_MAX           = 0.25
V200_BANK_ROE_MIN     = 10.0
V200_BANK_NP_MIN_CR   = 1000.0
UNIV_FUND_CACHE       = os.path.join(BASE_DIR, "universe_fund_cache.pkl")
UNIV_FUND_TTL_DAYS    = 90
UNIV_SCREEN_LIMIT     = None     # cap candidates for a quick test (None = all)

_BANK_WORDS = ("bank", "finance", "financial", "nbfc", "fin serv", "fincorp",
               "housing finance", "capital", "insurance", "lombard", "life")
_YF_SECTOR_MAP = {
    "financial services": "BANKING", "financial": "BANKING",
    "technology": "TECH", "communication services": "TECH",
    "healthcare": "PHARMA", "consumer defensive": "FMCG",
    "consumer cyclical": "CONSUMER", "basic materials": "CHEMICALS",
    "industrials": "INFRA", "energy": "POWER", "utilities": "POWER",
    "real estate": "INFRA",
}
NSE_EQUITY_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


def _is_bank_name(name: str, sector: str = "") -> bool:
    t = f"{name} {sector}".lower()
    return any(w in t for w in _BANK_WORDS)


def _yf_fundamentals(ticker: str) -> dict:
    """Pull the screen inputs from yfinance. Returns a dict with net_profit_cr
    (TTM), roce (computed %), de (ratio), roe (%), market_cap_cr, sector,
    name, is_bank. Missing values come back as None — caller drops those."""
    out = {"net_profit_cr": None, "roce": None, "de": None, "roe": None,
           "market_cap_cr": None, "sector": None, "name": ticker, "is_bank": False}
    try:
        t = yf.Ticker(ticker)
        try:
            info = t.get_info()
        except Exception:
            info = getattr(t, "info", {}) or {}
        out["name"]   = info.get("longName") or info.get("shortName") or ticker
        out["sector"] = info.get("sector")
        out["is_bank"] = _is_bank_name(out["name"], f"{out['sector']} {info.get('industry','')}")
        mc = info.get("marketCap")
        if mc:
            out["market_cap_cr"] = mc / 1e7
        de = info.get("debtToEquity")
        if de is not None:
            out["de"] = de / 100.0 if de > 3 else de   # yfinance reports a %
        roe = info.get("returnOnEquity")
        if roe is not None:
            out["roe"] = roe * 100.0
        np_ttm = info.get("netIncomeToCommon")
        # prefer summed last-4-quarters net income when available
        try:
            qis = t.quarterly_income_stmt
            if qis is not None and not qis.empty:
                for key in ("Net Income", "Net Income Common Stockholders",
                            "Net Income From Continuing Operation Net Minority Interest"):
                    if key in qis.index:
                        vals = qis.loc[key].dropna().values[:4]
                        if len(vals) >= 1:
                            np_ttm = float(np.nansum(vals)); break
        except Exception:
            pass
        if np_ttm is not None:
            out["net_profit_cr"] = float(np_ttm) / 1e7
        # ROCE = EBIT(TTM) / (Total Assets - Current Liabilities)
        try:
            ebit = None
            qis = t.quarterly_income_stmt
            if qis is not None and not qis.empty:
                for key in ("EBIT", "Operating Income", "Total Operating Income As Reported"):
                    if key in qis.index:
                        vals = qis.loc[key].dropna().values[:4]
                        if len(vals) >= 1:
                            ebit = float(np.nansum(vals)); break
            bs = t.balance_sheet
            cap_emp = None
            if bs is not None and not bs.empty:
                ta = bs.loc["Total Assets"].dropna().iloc[0] if "Total Assets" in bs.index else None
                cl = (bs.loc["Current Liabilities"].dropna().iloc[0]
                      if "Current Liabilities" in bs.index else None)
                if ta is not None and cl is not None and (ta - cl) > 0:
                    cap_emp = float(ta - cl)
            if ebit is not None and cap_emp:
                out["roce"] = ebit / cap_emp * 100.0
        except Exception:
            pass
    except Exception:
        pass
    return out


def _passes_v200(f: dict) -> bool:
    """Apply the V200 fundamental screen to one fetched fundamentals dict."""
    if f.get("is_bank"):
        roe = f.get("roe"); npft = f.get("net_profit_cr")
        return (roe is not None and roe > V200_BANK_ROE_MIN and
                npft is not None and npft > V200_BANK_NP_MIN_CR)
    npft = f.get("net_profit_cr"); roce = f.get("roce"); de = f.get("de")
    return (npft is not None and npft > V200_NP_MIN_CR and
            roce is not None and roce > V200_ROCE_MIN and
            de is not None and de < V200_DE_MAX)


def _fetch_nse_symbols(limit: Optional[int] = None) -> list[str]:
    """Download the live NSE equity list (EQ series). Falls back to the
    QUALITY_SEED names if the download is unavailable."""
    try:
        import urllib.request
        req = urllib.request.Request(NSE_EQUITY_LIST_URL, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        from io import StringIO
        df = pd.read_csv(StringIO(raw))
        df.columns = [c.strip().upper() for c in df.columns]
        col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
        syms = df[col].astype(str).str.strip().tolist()
        if "SERIES" in df.columns:
            syms = df[df["SERIES"].astype(str).str.strip() == "EQ"][col].astype(str).str.strip().tolist()
        print(f"  [universe] NSE list: {len(syms)} equities")
        return syms[:limit] if limit else syms
    except Exception as e:
        seed = [s for syms in QUALITY_SEED.values() for s in syms]
        print(f"  [universe] NSE list unavailable ({e}); using {len(seed)} seed names")
        return seed[:limit] if limit else seed

# Fallback seed used by the VARIABLE path when no --universe-file is given:
# a curated set that plausibly clears the screen above (debt-light, high
# ROCE/ROE, large-cap, low pledge) + the big banks (bank rule). Sectors map
# to the scanner's existing buckets so sector-RS/rotation keeps working.
QUALITY_SEED = {
    "FMCG":      ["NESTLEIND", "HINDUNILVR", "BRITANNIA", "DABUR", "MARICO",
                  "COLPAL", "PGHH"],
    "CONSUMER":  ["TITAN", "PIDILITIND", "ASIANPAINT", "PAGEIND", "BERGEPAINT"],
    "TECH":      ["TCS", "INFY", "HCLTECH", "TATAELXSI", "PERSISTENT", "COFORGE"],
    "PHARMA":    ["DIVISLAB", "ABBOTINDIA", "CIPLA", "SUNPHARMA"],
    "CHEMICALS": ["PIIND", "VINATIORGA", "NAVINFLUOR", "FLUOROCHEM"],
    "AUTO":      ["EICHERMOT", "BAJAJ-AUTO", "TIINDIA"],
    "INFRA":     ["POLYCAB", "APLAPOLLO", "HAVELLS"],
    "BANKING":   ["HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
                  "BAJFINANCE", "BAJAJFINSV", "CHOLAFIN"],
}


def parse_universe_file(path: str) -> list[tuple[str, Optional[str]]]:
    """Read a universe file: one NSE code per line, optional ',SECTOR'.
    Blank lines and '#' comments ignored. Returns [(TICKER, sector|None), ...]."""
    out: list[tuple[str, Optional[str]]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.replace("\t", ",").split(",")]
                sym = parts[0].upper()
                sec = parts[1].upper() if len(parts) > 1 and parts[1] else None
                if sym:
                    out.append((sym, sec))
    except FileNotFoundError:
        print(f"  [universe] file not found: {path}")
    return out


def _passes_tradeability(df: pd.DataFrame,
                         min_adv_cr: float = UNIV_MIN_ADV_CR,
                         min_price: float = UNIV_MIN_PRICE,
                         min_history_years: float = UNIV_MIN_HISTORY_YEARS,
                         lookback: int = UNIV_LOOKBACK_DAYS) -> bool:
    """Pure price/volume tradeability gate (yfinance-derived)."""
    if df is None or df.empty:
        return False
    if len(df) < int(min_history_years * 252 * 0.9):      # listing history
        return False
    last = df.tail(lookback)
    if last.empty:
        return False
    price = float(last["Close"].iloc[-1])
    if not np.isfinite(price) or price < min_price:        # price floor
        return False
    adv_cr = float((last["Close"] * last["Volume"]).mean()) / 1e7
    if not np.isfinite(adv_cr) or adv_cr < min_adv_cr:     # liquidity
        return False
    return True


def _load_fund_universe_cache() -> dict:
    return _load_pickle_cache(UNIV_FUND_CACHE)


def _save_fund_universe_cache(cache: dict) -> None:
    try:
        with open(UNIV_FUND_CACHE, "wb") as f:
            pickle.dump(cache, f)
    except Exception:
        pass


def _auto_screen_v200(limit: Optional[int], force: bool = False
                      ) -> list[tuple[str, Optional[str]]]:
    """AUTO mode: pull fundamentals from yfinance for the NSE list and keep
    names passing the V200 screen. Cached for UNIV_FUND_TTL_DAYS. Returns
    [(symbol, sector), ...] of PASSING names (sector mapped to scanner buckets)."""
    syms  = _fetch_nse_symbols(limit=limit if limit is not None else UNIV_SCREEN_LIMIT)
    cache = _load_fund_universe_cache()
    ttl   = timedelta(days=UNIV_FUND_TTL_DAYS)
    now   = datetime.now()
    passing: list[tuple[str, Optional[str]]] = []
    checked = hits = 0
    print(f"  [universe] V200 auto-screen over {len(syms)} names "
          f"(NP>{V200_NP_MIN_CR:.0f}cr, ROCE>{V200_ROCE_MIN:.0f}%, D/E<{V200_DE_MAX} "
          f"| bank: ROE>{V200_BANK_ROE_MIN:.0f}, NP>{V200_BANK_NP_MIN_CR:.0f}cr)")
    print("  [universe] NOTE: this is slow (yfinance fundamentals, rate-limited) "
          "and ROCE is computed — many names lack data and are skipped.")
    for sym in syms:
        yf_t = sym if sym.endswith(".NS") else f"{sym}.NS"
        ent = cache.get(yf_t)
        if not force and ent and (now - ent[1]) < ttl:
            f = ent[0]
        else:
            f = _yf_fundamentals(yf_t)
            cache[yf_t] = (f, now)
            time.sleep(0.2)                      # be polite to yfinance
        checked += 1
        if _passes_v200(f):
            sec = (_YF_SECTOR_MAP.get(str(f.get("sector") or "").lower(), "OTHER"))
            passing.append((sym, sec))
            hits += 1
        if checked % 100 == 0:
            print(f"    screened {checked}/{len(syms)} ... {hits} passing")
            _save_fund_universe_cache(cache)
    _save_fund_universe_cache(cache)
    print(f"  [universe] V200 screen: {hits} of {checked} names pass")
    return passing


def build_quality_universe(universe_file: Optional[str] = None,
                           auto_screen: bool = False,
                           limit: Optional[int] = None,
                           force_screen: bool = False,
                           min_adv_cr: float = UNIV_MIN_ADV_CR,
                           min_price: float = UNIV_MIN_PRICE,
                           min_history_years: float = UNIV_MIN_HISTORY_YEARS,
                           lookback: int = UNIV_LOOKBACK_DAYS,
                           include_index: bool = True
                           ) -> tuple[dict, dict]:
    """Build {sector: [tickers]} + {ticker: sector} for the VARIABLE path.

    Candidate source (in priority):
      1. --universe-file        -> names you exported from screener.in (no
                                   re-screening; tradeability only)
      2. auto_screen (default)  -> pull fundamentals from yfinance across the
                                   NSE list and keep names passing the V200
                                   screen (NP>200cr, ROCE>20%, D/E<0.25; banks
                                   ROE>10 & NP>1000)
      3. QUALITY_SEED fallback  -> if both above yield nothing
    Whatever the source, every name must then clear the price/volume
    tradeability gate. Returns (UNIVERSE, SECTOR_OF)."""
    global _LAST_BUILT_UNIVERSE_SOURCE
    candidates: list[tuple[str, Optional[str]]] = []
    _LAST_BUILT_UNIVERSE_SOURCE = "quality_seed"
    if universe_file:
        candidates = parse_universe_file(universe_file)
        print(f"  [universe] {len(candidates)} candidates from {universe_file}")
        if candidates:
            _LAST_BUILT_UNIVERSE_SOURCE = "file"
    elif auto_screen:
        candidates = _auto_screen_v200(limit=limit, force=force_screen)
        if candidates:
            _LAST_BUILT_UNIVERSE_SOURCE = "auto_screen"
    if not candidates:
        _LAST_BUILT_UNIVERSE_SOURCE = "quality_seed"
        print("  [universe] no valid symbols, falling back to QUALITY_SEED")
        for sec, syms in QUALITY_SEED.items():
            for s in syms:
                candidates.append((s, sec))
        print(f"  [universe] QUALITY_SEED contains {len(candidates)} names")

    # tradeability filter via yfinance prices
    end   = datetime.now()
    start = end - timedelta(days=int((min_history_years + 0.5) * 365) + lookback)
    universe: dict[str, list[str]] = {}
    kept = dropped = 0
    for sym, sec in candidates:
        yf_t = sym if sym.endswith(".NS") else f"{sym}.NS"
        try:
            df = download(yf_t, start, end)
        except Exception:
            df = pd.DataFrame()
        if _passes_tradeability(df, min_adv_cr, min_price, min_history_years, lookback):
            universe.setdefault((sec or "OTHER").upper(), []).append(yf_t)
            kept += 1
        else:
            dropped += 1
    print(f"  [universe] tradeability: kept {kept}, dropped {dropped}")

    if include_index and universe:
        universe.setdefault("INDEX", [])
        if CFG.get("nifty_etf_ticker", "NIFTYBEES.NS") not in universe["INDEX"]:
            universe["INDEX"].append(CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"))

    sector_of = {t: s for s, ts in universe.items() for t in ts}
    return universe, sector_of


def set_universe(mode: str = "hardcoded", universe_file: Optional[str] = None,
                 **kw) -> None:
    """Select the universe path and (for 'variable') rebuild the globals."""
    global UNIVERSE, ALL_TICKERS, SECTOR_OF, UNIVERSE_FILE_MODE
    if str(mode).lower() in ("variable", "quality", "dynamic"):
        uni, sof = build_quality_universe(universe_file=universe_file, **kw)
        tradeable = {s: ts for s, ts in uni.items() if s != "INDEX"}
        if tradeable:
            UNIVERSE    = uni
            ALL_TICKERS = list({t for ts in UNIVERSE.values() for t in ts})
            SECTOR_OF   = {t: s for s, ts in UNIVERSE.items() for t in ts}
            UNIVERSE_FILE_MODE = (_LAST_BUILT_UNIVERSE_SOURCE == "file")
            print(f"  Universe mode: VARIABLE — {len(ALL_TICKERS)} tickers "
                  f"across {len(UNIVERSE)} sectors")
            if UNIVERSE_FILE_MODE:
                print("  Sector eligibility filtering: DISABLED for universe-file mode")
        else:
            UNIVERSE_FILE_MODE = False
            print("  Variable universe empty (network/filters) — "
                  "keeping HARDCODED universe.")
    else:
        UNIVERSE_FILE_MODE = False
        print(f"  Universe mode: HARDCODED — {len(ALL_TICKERS)} tickers "
              f"across {len(UNIVERSE)} sectors")


# ══════════════════════════════════════════════════════════════════════
# FIX-F1: DYNAMIC FUNDAMENTAL SCREENING
# ══════════════════════════════════════════════════════════════════════

def _load_fund_cache() -> dict:
    return _load_pickle_cache(FUND_CACHE_PATH)


def _save_fund_cache(cache: dict):
    with open(FUND_CACHE_PATH, "wb") as f:
        pickle.dump(cache, f)


def _fetch_revenue_growth(ticker: str) -> Optional[bool]:
    """
    Returns True if revenue is growing YoY (or data unavailable → permissive),
    False if revenue is declining.
    """
    try:
        t    = yf.Ticker(ticker)
        fins = t.quarterly_financials
        if fins is None or fins.empty:
            return None
        if "Total Revenue" in fins.index:
            rev = fins.loc["Total Revenue"].dropna()
        elif "Revenue" in fins.index:
            rev = fins.loc["Revenue"].dropna()
        else:
            return None
        if len(rev) < 4:
            return None
        # Compare most recent quarter vs same quarter last year
        recent = float(rev.iloc[0])
        year_ago = float(rev.iloc[min(4, len(rev)-1)])
        if year_ago <= 0:
            return None
        growth = (recent - year_ago) / abs(year_ago)
        return growth > -0.05  # allow up to -5% as noise
    except Exception:
        return None


def passes_fundamental_gate(ticker: str) -> bool:
    """
    Dynamic fundamental gate. Uses cache with 90-day TTL.
    Falls back to permissive (True) if data unavailable.
    """
    cache = _load_fund_cache()
    ttl   = timedelta(days=CFG["fundamental_cache_ttl_days"])
    now   = datetime.now()

    if ticker in cache:
        cached_val, cached_time = cache[ticker]
        if now - cached_time < ttl:
            return cached_val if cached_val is not None else True

    result = _fetch_revenue_growth(ticker)
    cache[ticker] = (result, now)
    _save_fund_cache(cache)

    return result if result is not None else True


def refresh_fundamentals(tickers: list, force: bool = False):
    """Pre-fetch fundamentals for all tickers. Call once before scan/backtest."""
    cache = _load_fund_cache()
    ttl   = timedelta(days=CFG["fundamental_cache_ttl_days"])
    now   = datetime.now()
    stale = [
        t for t in tickers
        if force or t not in cache or (now - cache[t][1]) >= ttl
    ]
    if not stale:
        print(f"  Fundamentals: all {len(tickers)} tickers cached and fresh.")
        return
    print(f"  Refreshing fundamentals for {len(stale)} tickers...")
    for i, ticker in enumerate(stale):
        result = _fetch_revenue_growth(ticker)
        cache[ticker] = (result, now)
        if (i+1) % 20 == 0:
            print(f"    {i+1}/{len(stale)} done...")
        time.sleep(0.3)
    _save_fund_cache(cache)
    excluded = [t for t, (v, _) in cache.items() if v is False and t in tickers]
    print(f"  Fundamentals done. Excluded (neg revenue): {excluded if excluded else 'none'}")


# ══════════════════════════════════════════════════════════════════════
# FIX-E1: EARNINGS BLACKOUT WINDOW
# ══════════════════════════════════════════════════════════════════════

def _load_earnings_cache() -> dict:
    return _load_pickle_cache(EARNINGS_CACHE_PATH)


def _save_earnings_cache(cache: dict):
    with open(EARNINGS_CACHE_PATH, "wb") as f:
        pickle.dump(cache, f)


def _fetch_earnings_date(ticker: str) -> Optional[date]:
    """Fetch next/most recent earnings date from yfinance."""
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return None

        def _coerce_date(val):
            if val is None:
                return None
            if isinstance(val, (list, tuple)):
                for item in val:
                    coerced = _coerce_date(item)
                    if coerced is not None:
                        return coerced
                return None
            if hasattr(val, "date"):
                try:
                    return val.date()
                except Exception:
                    pass
            try:
                ts = pd.Timestamp(val)
                if pd.isna(ts):
                    return None
                return ts.date()
            except Exception:
                return None

        # yfinance returns dict, DataFrame, Series, or list-like depending on version
        if isinstance(cal, dict):
            for key in ("Earnings Date", "earnings date", "Earnings date", "earnings Date"):
                if key in cal:
                    return _coerce_date(cal.get(key))
            return None

        if isinstance(cal, pd.Series):
            for key in ("Earnings Date", "earnings date", "Earnings date", "earnings Date"):
                if key in cal.index:
                    return _coerce_date(cal.loc[key])
            return None

        if isinstance(cal, pd.DataFrame):
            if cal.empty:
                return None
            idx = pd.Index([str(x).strip() for x in cal.index])
            cal = cal.copy()
            cal.index = idx
            for key in ("Earnings Date", "earnings date", "Earnings date", "earnings Date"):
                if key in cal.index:
                    row = cal.loc[key]
                    if isinstance(row, pd.Series):
                        for val in row.dropna().tolist():
                            out = _coerce_date(val)
                            if out is not None:
                                return out
                    else:
                        out = _coerce_date(row)
                        if out is not None:
                            return out
            return None

        return _coerce_date(cal)
    except Exception:
        return None


def in_earnings_blackout(ticker: str, check_date: Optional[date] = None) -> bool:
    """
    Returns True if check_date is within earnings blackout window.
    Blackout = [earnings_date - before_days, earnings_date + after_days].
    Uses cache with 14-day TTL.
    """
    check_date = check_date or datetime.now().date()
    cache      = _load_earnings_cache()
    ttl        = timedelta(days=CFG["earnings_cache_ttl_days"])
    now        = datetime.now()

    if ticker in cache:
        earnings_dt, cached_time = cache[ticker]
        if now - cached_time >= ttl:
            earnings_dt = _fetch_earnings_date(ticker)
            cache[ticker] = (earnings_dt, now)
            _save_earnings_cache(cache)
    else:
        earnings_dt = _fetch_earnings_date(ticker)
        cache[ticker] = (earnings_dt, now)
        _save_earnings_cache(cache)

    if earnings_dt is None:
        return False

    before = CFG["earnings_blackout_before"]
    after  = CFG["earnings_blackout_after"]

    if isinstance(check_date, datetime):
        check_date = check_date.date()
    if isinstance(earnings_dt, datetime):
        earnings_dt = earnings_dt.date()

    delta = (check_date - earnings_dt).days
    return -before <= delta <= after


def prefetch_earnings(tickers: list):
    """Pre-fetch earnings dates for all tickers before scan/backtest."""
    cache = _load_earnings_cache()
    ttl   = timedelta(days=CFG["earnings_cache_ttl_days"])
    now   = datetime.now()
    stale = [
        t for t in tickers
        if t not in cache or (now - cache[t][1]) >= ttl
    ]
    if not stale:
        print(f"  Earnings: all {len(tickers)} tickers cached and fresh.")
        return
    print(f"  Fetching earnings dates for {len(stale)} tickers...")
    for i, ticker in enumerate(stale):
        ed = _fetch_earnings_date(ticker)
        cache[ticker] = (ed, now)
        if (i+1) % 20 == 0:
            print(f"    {i+1}/{len(stale)} done...")
        time.sleep(0.3)
    _save_earnings_cache(cache)
    print(f"  Earnings dates cached.")


# ══════════════════════════════════════════════════════════════════════
# FLUID REGIME
# ══════════════════════════════════════════════════════════════════════

# ── v5.7.0: EVERYTHING is on the continuous intensity curve ──────────
# Bear anchor = intensity -1.0,  Bull anchor = intensity +1.0
# All values interpolate linearly between the two extremes.
# No discrete snapping except for path eligibility (which is threshold-based).
#
# Reading the table:  (bear_val, bull_val)
# intensity=-1 → bear_val,  intensity=0 → midpoint,  intensity=+1 → bull_val

_ANCHORS: dict[str, tuple] = {
    # ── entry quality gates ──────────────────────────────────────────
    "entry_score_min":          (70,      42),   # tight in bear, loose in bull
    "rsi_entry_max":            (46,      66),   # only deeply OS in bear; allow higher in bull
    "rsi_entry_min":            (22,      22),   # floor stays the same
    "min_volume_ratio":         (1.05,    0.68), # demand vol confirmation in bear
    "min_atr_pct":              (2.2,     1.1),  # need bigger moves to justify bear entries
    # ── RS thresholds ───────────────────────────────────────────────
    "rs_min_strong":            (80,      48),   # only elite leaders in bear
    "rs_min_moderate":          (65,      33),
    "mkt_rs_min_entry":         (62,      28),
    # ── scoring weights ─────────────────────────────────────────────
    "mkt_rs_weight_mult":       (1.9,     0.7),  # RS matters most in bear
    "momentum_weight_mult":     (0.4,     1.4),  # momentum matters most in bull
    "rs_weight_mult":           (1.6,     1.1),
    "pvd_weight_mult":          (1.7,     0.8),
    "exhaust_weight_mult":      (1.7,     0.7),
    "value_weight_mult":        (1.5,     0.9),
    "pvd_strong_thresh":        (0.32,    0.18),
    "pvd_mild_thresh":          (0.13,    0.04),
    # ── position sizing ─────────────────────────────────────────────
    "max_position":             (10_000,  20_000), # tiny max in bear, full in bull
    # ── portfolio construction ──────────────────────────────────────
    "max_positions":            (3,       13),   # continuous: ~3 bear → ~13 bull
    "max_per_sector":           (1,        3),   # 1 per sector in bear, 3 in bull
    "sector_rs_top_n":          (1,        5),   # only #1 sector in bear, top-5 in bull
    # ── hold duration ───────────────────────────────────────────────
    "max_hold_days":            (14,      26),   # shorter holds in bear
    # ── cash floor (stock deployment budget) ────────────────────────
    "cash_floor":               (0.80,    0.18), # 80% idle in bear → 18% idle in bull
    # ── stop / target multipliers ───────────────────────────────────
    "atr_stop_mult":            (1.3,     1.8),  # tighter stops in bear
    "atr_trail_mult":           (1.1,     1.5),
    "profit_target_mult":       (2.8,     4.0),  # smaller targets in bear (take what you can)
    # ── exit RSI ceiling (v5.8.3) ───────────────────────────────────
    # Bear entries start at RSI ~22-46; the old fixed ceiling of 72
    # left only ~26 RSI pts of travel → real R:R 1.86 vs cfg 2.33.
    # Now scales with regime: bear 74 → bull 80, matching entry range.
    "rsi_exit":                 (74,      80),
}

# Path eligibility: threshold-based on intensity (not snapped to label)
# oversold_pullback: all regimes
# trend_resumption:  intensity > -0.10  (neutral or better)
# bear_flush:        intensity < -0.10  (neutral or worse)
# nifty_momentum:    intensity > -0.40  (not deep bear)
PATH_REGIME_MAP = {
    "oversold_pullback":   {"bull", "recovering", "neutral", "bear"},
    "trend_resumption":    {"bull", "recovering"},
    "bear_flush":          {"bear"},
    "nifty_momentum":      {"bull", "recovering", "neutral"},
}

# require_reversal: True only in deep bear (intensity < -0.40)
# reversal_required_below_rs: rs threshold scales with intensity


def _interp(bear_val: float, bull_val: float, intensity: float) -> float:
    t = float(np.clip((float(intensity) + 1.0) / 2.0, 0.0, 1.0))
    return bear_val + t * (bull_val - bear_val)


def _intensity_label(intensity: float) -> str:
    if   intensity >=  0.40: return "bull"
    elif intensity >=  0.10: return "recovering"
    elif intensity >= -0.10: return "neutral"
    else:                    return "bear"


def compute_intensity(nifty_df: pd.DataFrame, as_of=None) -> float:
    if nifty_df is None or nifty_df.empty or len(nifty_df) < 60:
        return 0.0
    df = nifty_df.copy()
    if as_of is not None:
        ts = pd.Timestamp(as_of)
        idx = df.index
        if idx.tz is not None and ts.tz is None: ts = ts.tz_localize("UTC")
        elif idx.tz is None and ts.tz is not None: ts = ts.tz_localize(None)
        df = df[idx <= ts]
    if len(df) < 60:
        return 0.0

    close = df["Close"]
    fast  = close.ewm(span=CFG["regime_ema_fast"],  adjust=False).mean()
    slow  = close.ewm(span=CFG["regime_ema_slow"],  adjust=False).mean()
    sb    = CFG["regime_slope_bars"]
    rw    = CFG.get("recovery_window", 15)

    sl_now  = float(slow.iloc[-1]); sl_prev = float(slow.iloc[-min(sb+1, len(slow))])
    f_now   = float(fast.iloc[-1])
    rl      = float(close.rolling(rw, min_periods=1).min().iloc[-1])
    c_now   = float(close.iloc[-1])

    gc = CFG["intensity_ema_gap_clip"]
    sc = CFG["intensity_slope_clip"]
    rc = CFG["intensity_recovery_clip"]

    sig_gap  = float(np.clip((f_now - sl_now) / max(sl_now, 1e-9) / gc,    -1.0, 1.0))
    sig_slp  = float(np.clip((sl_now - sl_prev) / max(sl_prev, 1e-9) / sc, -1.0, 1.0))
    sig_rec  = float(np.clip((c_now - rl) / max(rl, 1e-9) / rc,            -1.0, 1.0))

    we = CFG["intensity_w_ema"]
    ws = CFG["intensity_w_slope"]
    wr = CFG["intensity_w_recovery"]

    return float(np.clip(we*sig_gap + ws*sig_slp + wr*sig_rec, -1.0, 1.0))


def build_regime_series(nifty_df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    if nifty_df is None or nifty_df.empty or len(nifty_df) < 60:
        return pd.Series(dtype=float), pd.Series(dtype=str)

    close    = nifty_df["Close"]
    fast     = close.ewm(span=CFG["regime_ema_fast"],  adjust=False).mean()
    slow     = close.ewm(span=CFG["regime_ema_slow"],  adjust=False).mean()
    sb       = CFG["regime_slope_bars"]
    rw       = CFG.get("recovery_window", 15)
    roll_low = close.rolling(rw, min_periods=1).min()

    gc = CFG["intensity_ema_gap_clip"]
    sc = CFG["intensity_slope_clip"]
    rc = CFG["intensity_recovery_clip"]
    we = CFG["intensity_w_ema"]
    ws = CFG["intensity_w_slope"]
    wr = CFG["intensity_w_recovery"]

    intensities = []
    for i in range(len(nifty_df)):
        sl_now  = float(slow.iloc[i])
        sl_prev = float(slow.iloc[max(0, i - sb)])
        f_now   = float(fast.iloc[i])
        rl      = float(roll_low.iloc[i])
        c_now   = float(close.iloc[i])

        sig_gap = float(np.clip((f_now - sl_now) / max(sl_now, 1e-9) / gc,    -1, 1))
        sig_slp = float(np.clip((sl_now - sl_prev) / max(sl_prev, 1e-9) / sc, -1, 1))
        sig_rec = float(np.clip((c_now - rl) / max(rl, 1e-9) / rc,            -1, 1))
        intensities.append(float(np.clip(we*sig_gap + ws*sig_slp + wr*sig_rec, -1, 1)))

    intensity_series = pd.Series(intensities, index=nifty_df.index)
    raw_labels       = intensity_series.apply(_intensity_label)

    confirm_dn = CFG["regime_confirm_bars"]
    confirm_up = CFG["regime_confirm_bars_up"]
    rank       = {"bear": 0, "neutral": 1, "recovering": 2, "bull": 3}

    committed  = [raw_labels.iloc[0]]
    streak_val = raw_labels.iloc[0]
    streak_len = 1
    for i in range(1, len(raw_labels)):
        cand    = raw_labels.iloc[i]
        current = committed[-1]
        if cand == streak_val: streak_len += 1
        else: streak_val = cand; streak_len = 1
        if cand == current: committed.append(current); continue
        going_up = rank.get(cand, 1) > rank.get(current, 1)
        if streak_len >= (confirm_up if going_up else confirm_dn):
            committed.append(cand)
        else:
            committed.append(current)

    return intensity_series, pd.Series(committed, index=nifty_df.index)


def _regime_cfg(regime_or_intensity) -> dict:
    """
    v5.7.0 — Every single parameter interpolates continuously on intensity.
    No discrete label snapping. intensity=-1 → full bear, +1 → full bull.
    """
    if isinstance(regime_or_intensity, str):
        _map = {"bull": 0.75, "recovering": 0.25, "neutral": 0.0, "bear": -0.75}
        intensity = _map.get(regime_or_intensity, 0.0)
        label     = regime_or_intensity
    else:
        intensity = float(regime_or_intensity)
        label     = _intensity_label(intensity)

    cfg: dict = {}

    # ── Continuous interpolation for every anchor ──────────────────────
    _int_keys = {"entry_score_min", "rsi_entry_max", "rsi_entry_min",
                 "mkt_rs_min_entry", "rs_min_strong", "rs_min_moderate",
                 "max_position", "max_positions", "max_per_sector",
                 "sector_rs_top_n", "max_hold_days", "rsi_exit"}
    for key, (bear_val, bull_val) in _ANCHORS.items():
        val = _interp(float(bear_val), float(bull_val), intensity)
        if key in _int_keys:
            val = int(round(val))
        cfg[key] = val

    # ── cash_floor: continuous from anchors ───────────────────────────
    # Overrides the old four-step lookup — same value, fully smooth
    cfg["cash_floor"] = float(np.clip(cfg["cash_floor"], 0.05, 0.95))

    # ── require_reversal: continuous threshold ────────────────────────
    # True only when intensity < -0.35 (mid-to-deep bear)
    cfg["require_reversal"] = intensity < -0.35

    # ── reversal_required_below_rs: scales with intensity ─────────────
    # In bear: require RS >= 70. In neutral: RS >= 60. In bull: disabled (None)
    if intensity < -0.10:
        cfg["reversal_required_below_rs"] = int(round(_interp(72, 58, intensity)))
    else:
        cfg["reversal_required_below_rs"] = None

    # ── stop / target mults already interpolated from _ANCHORS ───────
    # (atr_stop_mult, atr_trail_mult, profit_target_mult are in _ANCHORS)
    # Just ensure they exist in cfg — they were set in the loop above.

    cfg["_intensity"] = intensity
    cfg["_label"]     = label
    return cfg


def _cash_floor(label_or_intensity=None, intensity: float = None) -> float:
    """
    v5.7.0 — continuous cash floor from intensity.
    Accepts either a label string (maps to representative intensity)
    or a raw float intensity directly.
    """
    if intensity is not None:
        i = float(intensity)
    elif isinstance(label_or_intensity, str):
        _map = {"bull": 0.75, "recovering": 0.25, "neutral": 0.0, "bear": -0.75}
        i = _map.get(label_or_intensity, 0.0)
    elif label_or_intensity is not None:
        i = float(label_or_intensity)
    else:
        i = 0.0
    bear_floor = float(CFG["cash_floor_bear"])
    bull_floor = float(CFG["cash_floor_bull"])
    return float(np.clip(_interp(bear_floor, bull_floor, i), bull_floor, bear_floor))


def _max_hold(label_or_intensity=None, intensity: float = None) -> int:
    """v5.7.0 — continuous max hold from intensity."""
    if intensity is not None:
        i = float(intensity)
    elif isinstance(label_or_intensity, str):
        _map = {"bull": 0.75, "recovering": 0.25, "neutral": 0.0, "bear": -0.75}
        i = _map.get(label_or_intensity, 0.0)
    elif label_or_intensity is not None:
        i = float(label_or_intensity)
    else:
        i = 0.0
    return int(round(_interp(
        float(CFG["max_hold_bear"]),
        float(CFG["max_hold_bull"]),
        i
    )))


def _nifty_trend_state(nifty_df: Optional[pd.DataFrame], as_of=None) -> dict:
    """Compact market regime context from the index itself."""
    out = {
        "nifty_below_50": False,
        "nifty_below_200": False,
        "nifty_reclaimed_50": False,
        "nifty_reclaimed_200": False,
        "nifty_drawdown_60": 0.0,
        "nifty_close": np.nan,
        "nifty_ema50": np.nan,
        "nifty_ema200": np.nan,
    }
    if nifty_df is None or nifty_df.empty:
        return out
    try:
        df = nifty_df.copy()
        if as_of is not None:
            ts = pd.Timestamp(as_of)
            idx = df.index
            if idx.tz is not None and ts.tz is None:
                ts = ts.tz_localize("UTC")
            elif idx.tz is None and ts.tz is not None:
                ts = ts.tz_localize(None)
            df = df[idx <= ts]
        if len(df) < 20:
            return out
        close = df["Close"]
        ema50 = close.ewm(span=50, adjust=False).mean()
        ema200 = close.ewm(span=200, adjust=False).mean()
        last_close = float(close.iloc[-1])
        last_ema50 = float(ema50.iloc[-1])
        last_ema200 = float(ema200.iloc[-1])
        out.update({
            "nifty_below_50": last_close < last_ema50,
            "nifty_below_200": last_close < last_ema200,
            "nifty_reclaimed_50": last_close > last_ema50 and (float(close.iloc[-2]) <= float(ema50.iloc[-2]) if len(close) > 1 else True),
            "nifty_reclaimed_200": last_close > last_ema200 and (float(close.iloc[-2]) <= float(ema200.iloc[-2]) if len(close) > 1 else True),
            "nifty_drawdown_60": _nifty_drawdown_pct(df, lookback_days=60),
            "nifty_close": last_close,
            "nifty_ema50": last_ema50,
            "nifty_ema200": last_ema200,
        })
    except Exception:
        pass
    return out


def compute_market_context(ticker_dfs: dict, as_of, nifty_df: Optional[pd.DataFrame] = None) -> dict:
    """Breadth / leadership / compression context for index scoring and regime protection."""
    ts = pd.Timestamp(as_of)
    if nifty_df is not None and not nifty_df.empty:
        idx = nifty_df.index
        if idx.tz is not None and ts.tz is None:
            ts = ts.tz_localize("UTC")
        elif idx.tz is None and ts.tz is not None:
            ts = ts.tz_localize(None)
    breadth_hits = breadth_total = 0
    leadership_hits = leadership_total = 0
    sector_hits: dict[str, int] = {}
    sector_total: dict[str, int] = {}
    compression_vals = []

    for ticker, df in ticker_dfs.items():
        if ticker.startswith("__") or ticker == CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"):
            continue
        if df is None or df.empty:
            continue
        didx = df.index  # FIX BUG 2: renamed from idx to didx to avoid shadowing ts above
        # FIX BUG 2: always assign ts_use; add else branch for the matching-tz case
        if didx.tz is not None and ts.tz is None:
            ts_use = ts.tz_localize("UTC")
        elif didx.tz is None and ts.tz is not None:
            ts_use = ts.tz_localize(None)
        else:
            ts_use = ts  # both tz-aware or both tz-naive — use ts directly
        past = df[didx <= ts_use]
        if past.empty:
            continue
        row = past.iloc[-1]
        breadth_total += 1
        if "ABOVE_EMA50" in row.index and not pd.isna(row["ABOVE_EMA50"]):
            breadth_hits += int(float(row["ABOVE_EMA50"]) >= 1.0)
        elif "Close" in row.index and "EMA50" in row.index and not pd.isna(row.get("EMA50", np.nan)):
            breadth_hits += int(float(row["Close"]) > float(row["EMA50"]))

        leadership_total += 1
        mkt_rs = row.get("MKT_RS_SCORE", np.nan)
        if not pd.isna(mkt_rs) and float(mkt_rs) >= 75:
            leadership_hits += 1

        sector = SECTOR_OF.get(ticker)
        if sector:
            sector_total[sector] = sector_total.get(sector, 0) + 1
            if "ABOVE_EMA50" in row.index and not pd.isna(row["ABOVE_EMA50"]) and float(row["ABOVE_EMA50"]) >= 1.0:
                sector_hits[sector] = sector_hits.get(sector, 0) + 1

        if "ATR_PCT_MA20" in row.index and "ATR_PCT_MA60" in row.index:
            short = row.get("ATR_PCT_MA20", np.nan)
            long = row.get("ATR_PCT_MA60", np.nan)
            if not pd.isna(short) and not pd.isna(long) and float(long) > 0:
                compression_vals.append(float(np.clip(1.0 - float(short) / float(long), -1.0, 1.0)))

    sector_participating = 0
    for sector, total in sector_total.items():
        if total <= 0:
            continue
        if sector_hits.get(sector, 0) / total >= 0.5:
            sector_participating += 1

    nifty_state = _nifty_trend_state(nifty_df, as_of=as_of)
    breadth_pct = breadth_hits / max(breadth_total, 1)
    leadership_pct = leadership_hits / max(leadership_total, 1)
    sector_participation_pct = sector_participating / max(len(sector_total), 1)
    volatility_compression = float(np.nanmean(compression_vals)) if compression_vals else 0.0
    volatility_compression = float(np.clip(volatility_compression, -1.0, 1.0))

    return {
        **nifty_state,
        "breadth_pct": float(breadth_pct),
        "sector_participation_pct": float(sector_participation_pct),
        "leadership_pct": float(leadership_pct),
        "volatility_compression": float(volatility_compression),
        "breadth_hits": int(breadth_hits),
        "breadth_total": int(breadth_total),
        "leadership_hits": int(leadership_hits),
        "leadership_total": int(leadership_total),
    }


# ══════════════════════════════════════════════════════════════════════
# DATA LAYER
# ══════════════════════════════════════════════════════════════════════

# v5.8.1: Single sanitisation point — called once per ticker right after
# yfinance returns data.  Any NaN in OHLCV (holiday gaps, halts, IPO
# edge-dates, thin trading) is forward-filled then back-filled so the
# rest of the pipeline never sees a NaN price.  Volume NaN → 0.
# This is the only place we need to worry about it; every downstream
# consumer — indicators, scoring, backtest, walk-forward, diagnostics —
# is then guaranteed clean OHLCV regardless of universe size.
_OHLCV = ["Open", "High", "Low", "Close", "Volume"]

def _sanitise_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Forward-fill then back-fill OHLCV NaNs, zero-fill Volume NaNs,
    and drop any row where Close is still NaN after filling.
    Safe to call on an empty DataFrame.
    """
    if df.empty:
        return df
    df = df.copy()
    price_cols = [c for c in ["Open", "High", "Low", "Close"] if c in df.columns]
    vol_cols   = [c for c in ["Volume"] if c in df.columns]
    if price_cols:
        df[price_cols] = df[price_cols].ffill().bfill()
    if vol_cols:
        df[vol_cols] = df[vol_cols].fillna(0)
    # Drop any rows still missing Close (e.g. very first row with no prior data)
    if "Close" in df.columns:
        df = df[df["Close"].notna()]
    return df


def download(ticker: str, start, end, retries: int = 3) -> pd.DataFrame:
    for i in range(retries):
        try:
            df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
            cols = [c for c in ["Open","High","Low","Close","Volume"] if c in df.columns]
            if not df.empty:
                return _sanitise_df(df[cols])   # v5.8.1: sanitise at source
        except Exception:
            if i == retries - 1:
                pass
            time.sleep(2 ** i)
    return pd.DataFrame()


def latest_price(ticker: str) -> Optional[float]:
    try:
        d = yf.Ticker(ticker).history(period="2d", auto_adjust=True)
        d = _sanitise_df(d)   # v5.8.1
        return float(d["Close"].iloc[-1]) if not d.empty else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
# INDICATORS
# ══════════════════════════════════════════════════════════════════════

def rsi(series: pd.Series, n: int) -> pd.Series:
    d    = series.diff()
    gain = d.clip(lower=0).ewm(com=n-1, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(com=n-1, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    h, l, pc = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([h-l, (h-pc).abs(), (l-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(com=n-1, adjust=False).mean()


def compute_pvd(df: pd.DataFrame, window: int = 10) -> pd.DataFrame:
    df = df.copy()
    x  = np.arange(window)
    price_slopes = []; vol_slopes = []

    closes  = df["Close"].values
    volumes = df["Volume"].values

    for i in range(len(df)):
        if i < window - 1:
            price_slopes.append(np.nan); vol_slopes.append(np.nan); continue
        c_win = closes[i - window + 1 : i + 1]
        v_win = volumes[i - window + 1 : i + 1]
        c_mean = np.mean(c_win); v_mean = np.mean(v_win)
        if c_mean == 0 or v_mean == 0:
            price_slopes.append(0.0); vol_slopes.append(0.0); continue
        p_slope = np.polyfit(x, c_win / c_mean, 1)[0]
        v_slope = np.polyfit(x, v_win / v_mean, 1)[0]
        price_slopes.append(p_slope); vol_slopes.append(v_slope)

    df["PVD_PRICE_SLOPE"] = price_slopes
    df["PVD_VOL_SLOPE"]   = vol_slopes

    signals = []
    for ps, vs in zip(price_slopes, vol_slopes):
        if np.isnan(ps) or np.isnan(vs):
            signals.append("unknown")
        elif ps < 0 and vs < 0: signals.append("exhaustion")
        elif ps < 0 and vs > 0: signals.append("distribution")
        elif ps > 0 and vs > 0: signals.append("confirmed_up")
        else:                   signals.append("weak_rally")

    df["PVD_SIGNAL"]   = signals
    df["PVD_STRENGTH"] = np.abs(vol_slopes)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    n  = CFG["rsi_window"]
    a  = CFG["atr_window"]
    v  = CFG["volume_window"]

    df["RSI"]       = rsi(df["Close"], n)
    df["ATR"]       = atr(df, a)
    df["ATR_PCT"]   = df["ATR"] / df["Close"] * 100
    df["VOL_MA"]    = df["Volume"].rolling(v).mean()
    df["VOL_RATIO"] = df["Volume"] / df["VOL_MA"].replace(0, np.nan)

    df["DAILY_VALUE_CR"] = (df["Close"] * df["Volume"]) / 1e7

    df = compute_pvd(df, CFG["pvd_window"])

    df["REVERSAL"]  = (df["Close"] > df["Close"].shift(1)).astype(float)

    vw = CFG["vol_decline_window"]
    df["VOL_TREND"] = df["Volume"].rolling(vw).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True
    )
    df["VOL_EXHAUST"] = df["VOL_TREND"] / df["VOL_MA"].replace(0, np.nan)
    df["ATR_PCT_MA20"] = df["ATR_PCT"].rolling(20, min_periods=5).mean()
    df["ATR_PCT_MA60"] = df["ATR_PCT"].rolling(60, min_periods=20).mean()

    df["RET_3D"] = df["Close"].pct_change(3) * 100

    df["HIGH_52W"]     = df["Close"].rolling(252, min_periods=50).max()
    df["DISC_52W_PCT"] = (df["HIGH_52W"] - df["Close"]) / df["HIGH_52W"] * 100
    df["LOW_52W"]      = df["Close"].rolling(252, min_periods=50).min()
    df["LOW_52W_PCT"]  = (df["Close"] - df["LOW_52W"]) / df["Close"] * 100

    # PATH 2: Trend Resumption indicators
    ep = CFG["tr_ema_period"]
    df["EMA50"]       = df["Close"].ewm(span=ep, adjust=False).mean()
    df["ABOVE_EMA50"] = (df["Close"] > df["EMA50"]).astype(float)

    # Recent high over lookback for pullback measurement
    pb_window = 30
    df["RECENT_HIGH"] = df["High"].rolling(pb_window, min_periods=10).max()
    df["PULLBACK_PCT"] = (df["RECENT_HIGH"] - df["Close"]) / df["RECENT_HIGH"] * 100

    # Volume trend on down days (last N days)
    decay_bars = CFG["tr_vol_decline_window"]
    down_days_vol = []
    for i in range(len(df)):
        if i < decay_bars:
            down_days_vol.append(np.nan)
            continue
        window_df = df.iloc[i - decay_bars + 1 : i + 1]
        down_vols = window_df.loc[
            window_df["Close"] < window_df["Close"].shift(1), "Volume"
        ].dropna()
        if len(down_vols) >= 2:
            # slope of volume on down days — negative = decaying selling pressure
            slope = np.polyfit(range(len(down_vols)), down_vols.values, 1)[0]
            down_days_vol.append(slope)
        else:
            down_days_vol.append(np.nan)
    df["DOWN_VOL_SLOPE"] = down_days_vol

    # Intrabar close position: 1 = top of bar, 0 = bottom
    bar_range = (df["High"] - df["Low"]).replace(0, np.nan)
    df["CLOSE_POSITION"] = (df["Close"] - df["Low"]) / bar_range

    # PATH 3: Bear Market Compounder indicators
    # Nifty relative drawdown will be computed at scoring time
    df["RS_TREND_20"] = np.nan  # filled after RS is added

    return df


def add_rs_trend_20(df: pd.DataFrame) -> pd.DataFrame:
    """Compute 20-day RS trend acceleration for bear compounder path."""
    if "MKT_RS_SCORE" not in df.columns:
        return df
    df = df.copy()
    df["RS_TREND_20"] = df["MKT_RS_SCORE"].diff(20)
    return df


# ══════════════════════════════════════════════════════════════════════
# RELATIVE STRENGTH — LOOKAHEAD-FREE
# ══════════════════════════════════════════════════════════════════════

def compute_rs_at_date(ticker_dfs: dict, as_of_date, rs_window: int) -> dict[str, tuple]:
    ts = pd.Timestamp(as_of_date)
    ticker_ret: dict[str, float] = {}
    for ticker, df in ticker_dfs.items():
        idx = df.index
        if idx.tz is not None:
            ts_use = ts.tz_localize("UTC") if ts.tz is None else ts
        else:
            ts_use = ts.tz_localize(None) if ts.tz is not None else ts
        past = df[idx <= ts_use]
        if len(past) < max(rs_window // 4, 30):
            continue
        actual_window = min(rs_window, len(past) - 1)
        if actual_window < 20:
            continue
        ret = (past["Close"].iloc[-1] / past["Close"].iloc[-actual_window] - 1) * 100
        ticker_ret[ticker] = ret

    if not ticker_ret:
        return {}

    results: dict[str, tuple] = {}
    all_rets = list(ticker_ret.values())
    n_all    = len(all_rets)

    for ticker, ret in ticker_ret.items():
        mkt_rs = float((sum(1 for r in all_rets if r < ret)) / max(n_all - 1, 1) * 100)
        sector = SECTOR_OF.get(ticker)
        if sector:
            peer_rets = [ticker_ret[t] for t in ticker_ret if SECTOR_OF.get(t) == sector]
            n_peers   = len(peer_rets)
            sec_rs    = float((sum(1 for r in peer_rets if r < ret)) / max(n_peers - 1, 1) * 100) if n_peers >= 2 else 50.0
        else:
            sec_rs = 50.0
        results[ticker] = (sec_rs, mkt_rs)

    return results


def add_all_rs_lookahead_free(ticker_dfs: dict, rs_window: int) -> dict:
    print("  Computing lookahead-free RS scores...")
    all_dates     = sorted(set().union(*[set(df.index) for df in ticker_dfs.values()]))
    # FIX-R1: sample every 3 bars instead of 5 for more responsive rotation
    sampled_dates = all_dates[::3] + ([all_dates[-1]] if all_dates[-1] not in all_dates[::3] else [])

    rs_lookup: dict = {}
    for d in sampled_dates:
        rs_lookup[d] = compute_rs_at_date(ticker_dfs, d, rs_window)

    updated = {}
    for ticker, df in ticker_dfs.items():
        df = df.copy()
        sec_rs_vals, mkt_rs_vals = [], []
        for date in df.index:
            past_samples = [s for s in sampled_dates if s <= date]
            if not past_samples:
                sec_rs_vals.append(np.nan); mkt_rs_vals.append(np.nan)
                continue
            nearest = past_samples[-1]
            rs_data = rs_lookup.get(nearest, {})
            vals    = rs_data.get(ticker, (np.nan, np.nan))
            sec_rs_vals.append(vals[0]); mkt_rs_vals.append(vals[1])
        df["RS_SCORE"]     = sec_rs_vals
        df["MKT_RS_SCORE"] = mkt_rs_vals
        df = add_rs_trend_20(df)
        updated[ticker]    = df

    updated["__rs_lookup__"]     = rs_lookup
    updated["__sampled_dates__"] = sampled_dates
    return updated


def add_rs_live(ticker_dfs: dict, rs_window: int) -> dict:
    """Compute live RS snapshots without any backtest date slicing."""
    clean = {t: df for t, df in ticker_dfs.items() if isinstance(df, pd.DataFrame) and not df.empty}
    if not clean:
        return {t: df.copy() for t, df in ticker_dfs.items()}

    latest_date = max(df.index[-1] for df in clean.values())
    rs_data     = compute_rs_at_date(clean, latest_date, rs_window)

    tw = CFG["rs_trend_window"]
    all_dates = sorted(set().union(*[set(df.index) for df in clean.values()]))

    if len(all_dates) >= tw + 1:
        trend_date  = all_dates[-(tw + 1)]
        rs_data_old = compute_rs_at_date(clean, trend_date, rs_window)
    else:
        rs_data_old = {}

    # For 20-day trend (BMC path)
    if len(all_dates) >= 21:
        trend_20_date = all_dates[-21]
        rs_data_20    = compute_rs_at_date(clean, trend_20_date, rs_window)
    else:
        rs_data_20 = {}

    updated = {}
    for ticker, df in ticker_dfs.items():
        df = df.copy()
        vals = rs_data.get(ticker, (np.nan, np.nan))
        df["RS_SCORE"]         = vals[0]
        df["MKT_RS_SCORE"]     = vals[1]
        old_vals = rs_data_old.get(ticker, (np.nan, np.nan))
        df["MKT_RS_SCORE_OLD"] = old_vals[1]
        old_20   = rs_data_20.get(ticker, (np.nan, np.nan))
        df["MKT_RS_SCORE_20AGO"] = old_20[1]
        df["RS_TREND_20"] = vals[1] - old_20[1] if not np.isnan(vals[1]) and not np.isnan(old_20[1]) else np.nan
        updated[ticker] = df
    return updated


# ══════════════════════════════════════════════════════════════════════
# FIX-R1: DYNAMIC SECTOR RANKING
# ══════════════════════════════════════════════════════════════════════

def rank_sectors_by_rs(ticker_dfs: dict, as_of_date, lookback_days: int = 20) -> list[str]:
    ts = pd.Timestamp(as_of_date)
    sector_rets: dict[str, list[float]] = {s: [] for s in UNIVERSE}

    for ticker, df in ticker_dfs.items():
        sector = SECTOR_OF.get(ticker)
        if sector is None: continue
        idx = df.index
        if idx.tz is not None:
            ts_use = ts.tz_localize("UTC") if ts.tz is None else ts
        else:
            ts_use = ts.tz_localize(None) if ts.tz is not None else ts

        past = df[idx <= ts_use]
        if len(past) < lookback_days + 2: continue
        ret = (past["Close"].iloc[-1] / past["Close"].iloc[-lookback_days] - 1) * 100
        sector_rets[sector].append(ret)

    scored = [(float(np.mean(rets)), sector) for sector, rets in sector_rets.items() if rets]
    scored.sort(reverse=True)
    return [s for _, s in scored]


def build_sector_rank_series(ticker_dfs: dict, all_dates: list,
                              lookback_days: int = 20) -> dict:
    print("  Computing sector RS rankings (dynamic, every 3 bars)...")
    # FIX-R1: every 3 trading days
    sampled = all_dates[::3] + ([all_dates[-1]] if all_dates[-1] not in all_dates[::3] else [])
    rank_lookup: dict = {}
    for d in sampled:
        rank_lookup[d] = rank_sectors_by_rs(ticker_dfs, d, lookback_days)
    return rank_lookup


def get_allowed_sectors(rank_lookup: dict, date, regime: str = "neutral") -> set[str]:
    top_n    = _regime_cfg(regime).get("sector_rs_top_n", CFG["sector_rs_top_n"])
    sampled  = sorted(rank_lookup.keys())
    past     = [s for s in sampled if s <= date]
    if not past: return set(UNIVERSE.keys())
    nearest  = past[-1]
    ranking  = rank_lookup.get(nearest, [])
    if not ranking: return set(UNIVERSE.keys())
    return set(ranking[:top_n])


# ══════════════════════════════════════════════════════════════════════
# LIQUIDITY FILTER
# ══════════════════════════════════════════════════════════════════════

def passes_liquidity(df: pd.DataFrame, lookback: int = 20, as_of=None) -> bool:
    if "DAILY_VALUE_CR" not in df.columns: return True
    if as_of is not None:
        ts = pd.Timestamp(as_of)
        idx = df.index
        if idx.tz is not None and ts.tz is None: ts = ts.tz_localize("UTC")
        elif idx.tz is None and ts.tz is not None: ts = ts.tz_localize(None)
        slice_ = df.loc[idx <= ts, "DAILY_VALUE_CR"]
    else:
        slice_ = df["DAILY_VALUE_CR"]
    if len(slice_) < lookback: return True
    return slice_.tail(lookback).mean() >= CFG["min_avg_daily_value_cr"]


# ══════════════════════════════════════════════════════════════════════
# KELLY SIZING
# ══════════════════════════════════════════════════════════════════════

def _new_kelly_state() -> dict:
    return {"wins": 0, "losses": 0, "avg_win": 1.0, "avg_loss": -1.0}


def kelly_risk_fraction(state: dict) -> float:
    w = state["wins"]; l = state["losses"]
    if w + l < 20: return CFG["risk_per_trade"]
    p      = w / (w + l)
    avg_w  = abs(state["avg_win"])
    avg_l  = abs(state["avg_loss"])
    if avg_l == 0: return CFG["risk_per_trade"]
    wl     = avg_w / avg_l
    f_star = (wl * p - (1 - p)) / wl
    f_star = max(0.005, min(f_star, 0.04))
    return round(f_star * CFG["kelly_fraction"], 4)


def _update_kelly(state: dict, pnl: float):
    if pnl > 0:
        state["wins"] += 1
        n = state["wins"]
        state["avg_win"] = (state["avg_win"] * (n - 1) + pnl) / n
    else:
        state["losses"] += 1
        n = state["losses"]
        state["avg_loss"] = (state["avg_loss"] * (n - 1) + pnl) / n


def size_position(entry: float, atr_val: float, equity: float,
                  kelly_state: dict,
                  max_pos_override: Optional[int] = None,
                  kelly_scale: float = 1.0,
                  stop_mult_override: Optional[float] = None,
                  target_mult_override: Optional[float] = None) -> Optional[dict]:
    max_pos    = max_pos_override if max_pos_override is not None else CFG["max_position"]
    stop_mult  = stop_mult_override  if stop_mult_override  is not None else CFG["atr_stop_mult"]
    tgt_mult   = target_mult_override if target_mult_override is not None else CFG["profit_target_mult"]
    # BUG-FIX v5.8.1: guard NaN inputs — atr_val/entry NaN would propagate to int() crash
    if np.isnan(atr_val) or np.isnan(entry) or entry <= 0: return None
    stop_dist  = atr_val * stop_mult
    if stop_dist <= 0: return None
    risk_frac  = kelly_risk_fraction(kelly_state) * max(0.1, min(kelly_scale, 1.0))
    risk_amt   = equity * risk_frac
    shares     = max(1, int(risk_amt / stop_dist))
    invested   = shares * entry
    if invested < CFG["min_position"]:
        shares = max(1, int(CFG["min_position"] / entry)); invested = shares * entry
    if invested > max_pos:
        shares = int(max_pos / entry)
        if shares < 1: return None
        invested = shares * entry
    if invested > equity * 0.35:
        shares = int(equity * 0.35 / entry)
        if shares < 1: return None
        invested = shares * entry
    if invested > equity: return None
    return {
        "shares":    shares,
        "entry":     round(entry, 2),
        "stop":      round(entry - stop_dist, 2),
        "target":    round(entry + atr_val * tgt_mult, 2),
        "invested":  round(invested, 2),
        "stop_dist": round(stop_dist, 2),
    }


# ══════════════════════════════════════════════════════════════════════
# EXCEPTIONAL TIER GATE  (v5.8.4)
# ══════════════════════════════════════════════════════════════════════

def is_exceptional_tier(row: pd.Series, score: float, regime_label) -> bool:
    """
    Returns True only in bear regime when a BMC-path stock clears all 5
    independent exceptional gates.

    Gates use only pre-computed row columns — no new data fetches.
    Called AFTER score_bear_flush() has already returned a passing score,
    so this purely unlocks upgraded sizing on an already-valid signal.

    regime_label may be a str label ("bear") OR a float intensity — both
    are handled.  The backtest passes intensity as a float; the live scan
    passes a string label.  v5.9.0: accept both to avoid always-False bug.

    Gates:
      1. Regime is bear (intensity < -0.10 or label == "bear")
      2. Elite RS level OR strong RS acceleration
         (mkt_rs ≥ exc_mkt_rs_min OR rs_trend ≥ exc_rs_trend_min)
      3. VOL_RATIO     ≥ exc_vol_ratio_min (genuine buying pressure)
      4. score         ≥ exc_score_min     (high-conviction composite)
      5. ATR_PCT       ≤ exc_atr_pct_max   (controlled vol — not a wildcard)
    """
    # v5.9.0: accept float intensity OR string label — the backtest passes
    # intensity as a float; live scan passes a string.
    if isinstance(regime_label, str):
        label = regime_label
    else:
        label = _intensity_label(float(regime_label))
    if label != "bear":
        return False

    mkt_rs    = float(row.get("MKT_RS_SCORE", 0) or 0)
    rs_trend  = float(row.get("RS_TREND_20",  0) or 0)
    vol_ratio = float(row.get("VOL_RATIO",    0) or 0)
    atr_pct   = float(row.get("ATR_PCT",      0) or 0)

    if pd.isna(mkt_rs):   mkt_rs   = 0.0
    if pd.isna(rs_trend): rs_trend = 0.0
    if pd.isna(vol_ratio):vol_ratio= 0.0
    if pd.isna(atr_pct):  atr_pct  = 99.0  # treat missing as too volatile

    # v5.9.0: gate 2 — elite RS level OR meaningful RS acceleration.
    # Previous code required mkt_rs >= exc_mkt_rs_min on line 1 AND then
    # re-checked _rs_ok on line 2; since line 1 already enforced it, the OR
    # branch (rs_trend fallback) was never reachable. Fix: gate 2 stands on
    # its own — a stock at mkt_rs=85 for months (low rs_trend) OR a stock
    # with strong 20d acceleration both qualify.
    _rs_gate = (
        (mkt_rs >= CFG["exc_mkt_rs_min"]) or
        (rs_trend >= CFG["exc_rs_trend_min"])
    )

    return (
        _rs_gate                              and  # elite RS level OR accelerating RS
        vol_ratio >= CFG["exc_vol_ratio_min"] and
        score     >= CFG["exc_score_min"]     and
        atr_pct   <= CFG["exc_atr_pct_max"]
    )


# ══════════════════════════════════════════════════════════════════════
# SIGNAL SCORING — ALL 3 PATHS
# ══════════════════════════════════════════════════════════════════════

_SCORE_MAX_RAW = 105



def _is_weak_market(regime, nifty_drawdown_pct: float, market_context: Optional[dict] = None) -> tuple[bool, list[str], float, bool]:
    """Shared weak-market definition used by the live scorer and diagnostics.

    v6.1.0 fix: regime=bear alone no longer triggers weak_market when the
    actual drawdown is shallow (< 5%).  The bear label can lag the real market
    by several sessions because the EMA-slope engine is slow to flip.  A 3-4%
    pullback with the index bouncing is NOT a distressed tape; applying the
    full bear kill-switch in that window wiped all PATH-1 signals despite the
    universe containing valid setups.  Require at least ONE price-based
    confirmation (dd≥5%, below key EMAs, or 60d dd≥8%) before flagging weak.
    """
    market_context = market_context or {}
    reg_label = regime if isinstance(regime, str) else _intensity_label(float(regime))
    wm_flags = []

    # v6.1.0: regime=bear alone only counts as a weak-market flag when at
    # least one price-based condition also fires.  On its own it is too slow
    # (EMA lag) and was wiping valid signals during shallow recoveries.
    _price_stress = (
        nifty_drawdown_pct >= 5.0
        or market_context.get("nifty_below_50",  False)
        or market_context.get("nifty_below_200", False)
        or float(market_context.get("nifty_drawdown_60", 0.0) or 0.0) >= 8.0
    )
    if reg_label == "bear" and _price_stress:
        wm_flags.append("regime=bear")

    if nifty_drawdown_pct >= 5.0:
        wm_flags.append(f"nifty_dd={nifty_drawdown_pct:.1f}%")
    if market_context.get("nifty_below_50", False):
        wm_flags.append("nifty below EMA50")
    if market_context.get("nifty_below_200", False):
        wm_flags.append("nifty below EMA200")
    if market_context.get("nifty_drawdown_60", 0.0) >= 8.0:
        wm_flags.append(f"nifty_60d_dd={float(market_context.get('nifty_drawdown_60', 0.0)):.1f}%")
    weak_market = bool(wm_flags)
    breadth_pct = float(market_context.get("breadth_pct", 0.0) or 0.0)
    reclaim_ok = bool(
        market_context.get("nifty_reclaimed_50", False)
        or market_context.get("nifty_reclaimed_200", False)
        or breadth_pct >= 0.55
    )
    return weak_market, wm_flags, breadth_pct, reclaim_ok

def score_oversold_pullback(row: pd.Series, regime: str = "neutral",
                            nifty_drawdown_pct: float = 0.0,
                            market_context: Optional[dict] = None) -> tuple[int, list[str]]:
    """PATH 1: Oversold Pullback — the flagship edge, protected by regime filters."""
    rcfg = _regime_cfg(regime)
    market_context = market_context or {}

    required = ["Close","RSI","ATR","ATR_PCT","VOL_RATIO","PVD_SIGNAL",
                "PVD_STRENGTH","VOL_EXHAUST","RET_3D","RS_SCORE",
                "MKT_RS_SCORE","REVERSAL","DISC_52W_PCT","LOW_52W_PCT"]
    for col in required:
        if col not in row.index:
            return 0, []
        if col != "PVD_SIGNAL" and pd.isna(row[col]):
            return 0, []

    rsi_v      = float(row["RSI"])
    atr_pct    = float(row["ATR_PCT"])
    vol_ratio  = float(row["VOL_RATIO"])
    pvd_signal = str(row["PVD_SIGNAL"])
    pvd_str    = float(row["PVD_STRENGTH"]) if not pd.isna(row["PVD_STRENGTH"]) else 0.0
    ret_3d     = float(row["RET_3D"])
    rs_score   = float(row["RS_SCORE"])
    mkt_rs     = float(row["MKT_RS_SCORE"])
    reversal   = float(row["REVERSAL"])
    disc_52w   = float(row["DISC_52W_PCT"])
    low_52w    = float(row["LOW_52W_PCT"])

    if not (rcfg["rsi_entry_min"] <= rsi_v <= rcfg["rsi_entry_max"]):
        return 0, []
    if atr_pct < rcfg["min_atr_pct"] or atr_pct > CFG["max_atr_pct"]:
        return 0, []
    if vol_ratio < rcfg["min_volume_ratio"]:
        return 0, []
    if mkt_rs < rcfg["mkt_rs_min_entry"]:
        return 0, []
    if rcfg.get("require_reversal", False) and reversal < 1:
        return 0, []
    if pvd_signal == "distribution":
        return 0, []

    weak_market, _, breadth_pct, reclaim_ok = _is_weak_market(
        regime, nifty_drawdown_pct, market_context
    )

    # v6.1.0: relaxed from AND (all-5-required) to OR (any-one-sufficient).
    # The old AND gate required reclaim_ok + reversal + pvd + rs + mkt_rs all
    # simultaneously — practically zero stocks passed, wiping PATH-1 entirely
    # in bear regime even during shallow recoveries.  Any single condition
    # now grants passage; the 40% penalty below still reduces the score.
    if weak_market and not (
        reclaim_ok
        or reversal == 1
        or pvd_signal in ("exhaustion", "confirmed_up")
        or rs_score >= 60
        or mkt_rs >= 55
    ):
        return 0, []

    mom_mult    = rcfg.get("momentum_weight_mult",  1.0)
    rs_mult     = rcfg.get("rs_weight_mult",        1.0)
    pvd_mult    = rcfg.get("pvd_weight_mult",       1.0)
    mkt_rs_mult = rcfg.get("mkt_rs_weight_mult",    1.0)
    value_mult  = rcfg.get("value_weight_mult",     1.0)
    pvd_strong  = rcfg.get("pvd_strong_thresh",     0.25)
    pvd_mild    = rcfg.get("pvd_mild_thresh",       0.08)
    rs_strong   = rcfg.get("rs_min_strong",         65)
    rs_moderate = rcfg.get("rs_min_moderate",       50)

    score = 0
    reasons = []

    if 25 <= rsi_v <= 35:
        score += 20; reasons.append(f"RSI deep oversold ({rsi_v:.1f})")
    elif 35 < rsi_v <= 43:
        score += 14; reasons.append(f"RSI oversold ({rsi_v:.1f})")
    elif 43 < rsi_v <= rcfg["rsi_entry_max"]:
        score += 7;  reasons.append(f"RSI mild pullback ({rsi_v:.1f})")

    if reversal == 1:
        score += 10; reasons.append("Price reversal bar")

    if disc_52w >= 30 and low_52w >= 10:
        score += int(15 * value_mult); reasons.append(f"52w discount {disc_52w:.0f}%")
    elif disc_52w >= 20 and low_52w >= 5:
        score += int(9 * value_mult);  reasons.append(f"52w discount {disc_52w:.0f}%")
    elif disc_52w >= 10:
        score += int(4 * value_mult)

    if pvd_signal == "exhaustion":
        if pvd_str >= pvd_strong:
            score += int(15 * pvd_mult); reasons.append("PVD: strong volume exhaustion")
        elif pvd_str >= pvd_mild:
            score += int(9 * pvd_mult);  reasons.append("PVD: mild volume exhaustion")
        else:
            score += int(5 * pvd_mult)
    elif pvd_signal == "confirmed_up":
        if pvd_str >= pvd_strong:
            score += int(12 * pvd_mult); reasons.append("PVD: confirmed advance")
        else:
            score += int(6 * pvd_mult)

    if rs_score >= rs_strong:
        score += int(15 * rs_mult);    reasons.append(f"Sector RS top ({rs_score:.0f})")
    elif rs_score >= rs_moderate:
        score += int(9 * rs_mult);     reasons.append(f"Sector RS above median ({rs_score:.0f})")
    elif rs_score < 30:
        score -= int(6 * rs_mult)

    if mkt_rs >= 75:
        score += int(15 * mkt_rs_mult); reasons.append(f"MktRS top quartile ({mkt_rs:.0f})")
    elif mkt_rs >= 50:
        score += int(9 * mkt_rs_mult);  reasons.append(f"MktRS above median ({mkt_rs:.0f})")
    elif mkt_rs < 30:
        score -= int(8 * mkt_rs_mult)

    if 0.3 <= ret_3d <= 5.0:
        score += int(5 * mom_mult); reasons.append(f"Constructive 3d return ({ret_3d:.1f}%)")
    elif ret_3d > 8.0:
        score -= 8

    if atr_pct >= 3.0:
        score += 3; reasons.append(f"ATR {atr_pct:.1f}%")
    elif atr_pct >= 2.0:
        score += 1
    if vol_ratio >= 1.4:
        score += 2; reasons.append(f"Vol surge {vol_ratio:.1f}x")
    elif vol_ratio >= 1.1:
        score += 1

    if weak_market:
        # v6.1.0: penalty eased from 40/60% → 55/75%.
        # 40% was double-punishing: stocks already passed the kill gate above,
        # then got halved again, making the effective score ceiling ~40 in bear.
        # At 55/75% a genuinely strong setup in a shallow bear can still clear
        # the entry_score_min threshold.
        penalty = 0.55 if market_context.get("breadth_pct", 0.0) < 0.45 else 0.75
        score = int(score * penalty)
        reasons.append("Market protection engaged")

    normalised = int(round(max(0, score) / _SCORE_MAX_RAW * 100))
    return min(normalised, 100), reasons


def score_trend_resumption(row: pd.Series, regime: str = "neutral") -> tuple[int, list[str]]:
    """
    PATH 2: Bull Trend Resumption — bull/recovering only.
    Stock above 50 EMA, pulled back 6-16%, RSI 40-60, volume confirming buyers.
    """
    required = ["Close","RSI","ATR_PCT","VOL_RATIO","ABOVE_EMA50",
                "PULLBACK_PCT","CLOSE_POSITION","DOWN_VOL_SLOPE",
                "RS_SCORE","MKT_RS_SCORE"]
    for col in required:
        if col not in row.index or pd.isna(row[col]): return 0, []

    above_ema  = float(row["ABOVE_EMA50"])
    pullback   = float(row["PULLBACK_PCT"])
    rsi_v      = float(row["RSI"])
    vol_ratio  = float(row["VOL_RATIO"])
    close_pos  = float(row["CLOSE_POSITION"])
    down_slope = float(row["DOWN_VOL_SLOPE"]) if not pd.isna(row["DOWN_VOL_SLOPE"]) else 0.0
    rs_score   = float(row["RS_SCORE"])
    mkt_rs     = float(row["MKT_RS_SCORE"])
    atr_pct    = float(row["ATR_PCT"])

    # Hard gates
    if above_ema < 1.0:   return 0, []  # must be above 50 EMA (uptrend)
    if not (CFG["tr_pullback_min_pct"] <= pullback <= CFG["tr_pullback_max_pct"]):
        return 0, []
    if not (CFG["tr_rsi_min"] <= rsi_v <= CFG["tr_rsi_max"]):
        return 0, []
    if vol_ratio < CFG["tr_vol_spike_min"]: return 0, []
    if close_pos < 0.5:   return 0, []  # close in upper half of bar
    if mkt_rs < CFG["tr_mkt_rs_min"]: return 0, []

    score = 0; reasons = []

    # RSI zone bonus
    if CFG["tr_rsi_min"] <= rsi_v <= 48:
        score += 18; reasons.append(f"TR: RSI reset to buy zone ({rsi_v:.1f})")
    elif rsi_v <= CFG["tr_rsi_max"]:
        score += 10; reasons.append(f"TR: RSI moderate reset ({rsi_v:.1f})")

    # Pullback depth
    if 10 <= pullback <= 15:
        score += 18; reasons.append(f"TR: ideal pullback depth {pullback:.1f}%")
    elif pullback < 10:
        score += 10; reasons.append(f"TR: shallow pullback {pullback:.1f}%")
    else:
        score += 5; reasons.append(f"TR: deep pullback {pullback:.1f}%")

    # Volume confirmation
    if vol_ratio >= 1.8:
        score += 18; reasons.append(f"TR: strong vol surge {vol_ratio:.1f}x")
    elif vol_ratio >= 1.25:
        score += 12; reasons.append(f"TR: moderate vol surge {vol_ratio:.1f}x")
    else:
        score += 6

    # Selling pressure decaying
    if down_slope < 0:
        score += 12; reasons.append("TR: selling pressure decaying")
    elif down_slope < 0.1:
        score += 5

    # Close position in bar
    if close_pos >= 0.70:
        score += 10; reasons.append(f"TR: bullish close position {close_pos:.2f}")
    elif close_pos >= 0.5:
        score += 5

    # RS bonus
    if rs_score >= 65:
        score += 12; reasons.append(f"TR: strong sector RS {rs_score:.0f}")
    elif rs_score >= 45:
        score += 6

    if mkt_rs >= 70:
        score += 10; reasons.append(f"TR: strong MktRS {mkt_rs:.0f}")
    elif mkt_rs >= 55:
        score += 5

    normalised = int(round(max(0, score) / 104.0 * 100))
    return min(normalised, 100), reasons



def score_bear_flush(row: pd.Series, nifty_drawdown_pct: float = 0.0,
                     market_context: Optional[dict] = None) -> tuple[int, list[str]]:
    """PATH 3: Bear Survivor + Capitulation Reversal."""
    market_context = market_context or {}
    required = ["Close","Open","RSI","ATR_PCT","VOL_RATIO","MKT_RS_SCORE",
                "DISC_52W_PCT","RS_TREND_20","RS_SCORE",
                "PVD_SIGNAL","REVERSAL","CLOSE_POSITION"]
    for col in required:
        if col not in row.index or pd.isna(row[col]):
            return 0, []

    mkt_rs     = float(row["MKT_RS_SCORE"])
    disc_52w   = float(row["DISC_52W_PCT"])
    rs_trend20 = float(row["RS_TREND_20"]) if not pd.isna(row["RS_TREND_20"]) else 0.0
    rs_score   = float(row["RS_SCORE"])
    atr_pct    = float(row["ATR_PCT"])
    vol_ratio  = float(row["VOL_RATIO"])
    rsi_v      = float(row["RSI"])
    pvd_signal = str(row["PVD_SIGNAL"])
    reversal   = float(row["REVERSAL"])
    close_pos  = float(row["CLOSE_POSITION"])
    open_v     = float(row["Open"])
    close_v    = float(row["Close"])
    prev_high  = float(row.get("RECENT_HIGH", close_v))
    above_ema50 = float(row.get("ABOVE_EMA50", np.nan))

    if nifty_drawdown_pct < 5.0 and market_context.get("nifty_drawdown_60", nifty_drawdown_pct) < 5.0:
        return 0, []

    # v5.8.5: Removed the second early-gate that blocked survivor_mode whenever
    # nifty was below EMA50 but dd < 10%. That combination (moderate bear, index
    # weak) is precisely when survivor_mode should be active — strong stocks hold
    # above their own EMA50 even when the index is below its EMA50.
    # bmc_nifty_down_pct now only gates capitulation_mode (correct semantics).

    # Score threshold: flat at bmc_score_min — the tiered 65 at 8-10% was
    # adding a second layer of restriction on an already restrictive path.
    _bmc_score_threshold = CFG["bmc_score_min"]

    # v5.8.5: survivor_mode rs_trend20 fix.
    # Root cause: rs_trend20 >= 6 required RECENT ACCELERATION, not sustained
    # leadership. A stock at mkt_rs=85 for 3 months (rs_trend20 ~2) was
    # blocked while a stock at mkt_rs=65 that just jumped 8pts passed.
    # That is backwards. Fix: mkt_rs >= 78 OR rs_trend20 >= 4 — either
    # sustained elite-tier RS OR recent meaningful acceleration is enough.
    _survivor_rs_ok = (mkt_rs >= 78) or (rs_trend20 >= 4)

    survivor_mode = (
        (mkt_rs >= 75 or rs_score >= 60)
        and _survivor_rs_ok
        and disc_52w <= 22
        and rsi_v >= 40
        and vol_ratio >= 0.85
        and atr_pct <= 4.5
        and (pd.isna(above_ema50) or above_ema50 >= 1.0 or close_v > open_v)
    )
    # capitulation_mode: bmc_nifty_down_pct gate kept here — it is correct
    # semantics for a full market-flush reversal play.
    capitulation_mode = (
        nifty_drawdown_pct >= CFG["bmc_nifty_down_pct"]
        and disc_52w >= 12
        and vol_ratio >= 1.15
        and close_pos >= 0.58
        and pvd_signal in ("exhaustion", "confirmed_up", "weak_rally")
        and (reversal == 1 or close_v > open_v)
        and close_v >= prev_high * 0.97
        and rs_score >= 45
        and mkt_rs >= 55
    )

    if not (survivor_mode or capitulation_mode):
        return 0, []

    score = 0
    reasons = []

    if survivor_mode:
        reasons.append("Bear Survivor")
        if mkt_rs >= 85:
            score += 22; reasons.append(f"elite MktRS in bear {mkt_rs:.0f}")
        elif mkt_rs >= 75:
            score += 16; reasons.append(f"strong MktRS in bear {mkt_rs:.0f}")
        else:
            score += 10; reasons.append(f"MktRS holding up {mkt_rs:.0f}")

        relative_strength = nifty_drawdown_pct - disc_52w
        if relative_strength >= 10:
            score += 12; reasons.append(f"holding vs Nifty {relative_strength:.0f}%")
        elif relative_strength >= 5:
            score += 8; reasons.append(f"modest outperformance {relative_strength:.0f}%")

        if rs_trend20 >= 15:
            score += 14; reasons.append(f"RS acceleration +{rs_trend20:.0f}pt")
        elif rs_trend20 >= 6:
            score += 8; reasons.append(f"RS rising +{rs_trend20:.0f}pt")

        if rs_score >= 70:
            score += 10; reasons.append(f"top sector RS {rs_score:.0f}")
        elif rs_score >= 55:
            score += 6

        if atr_pct <= 2.8:
            score += 6; reasons.append(f"low volatility ATR {atr_pct:.1f}%")

    else:
        reasons.append("Capitulation Reversal")
        if pvd_signal == "exhaustion":
            score += 18; reasons.append("volume exhaustion")
        elif pvd_signal == "confirmed_up":
            score += 12; reasons.append("reclaim confirmation")

        if reversal == 1:
            score += 14; reasons.append("reversal candle")

        if rsi_v <= 28:
            score += 16; reasons.append(f"RSI washout {rsi_v:.1f}")
        elif rsi_v <= 35:
            score += 10; reasons.append(f"RSI reset {rsi_v:.1f}")

        if close_pos >= 0.72:
            score += 10; reasons.append(f"strong close {close_pos:.2f}")
        elif close_pos >= 0.60:
            score += 6

        if vol_ratio >= 1.8:
            score += 10; reasons.append(f"volume surge {vol_ratio:.1f}x")
        elif vol_ratio >= 1.25:
            score += 6

        if rs_score >= 65:
            score += 8; reasons.append(f"sector RS {rs_score:.0f}")
        elif rs_score >= 50:
            score += 4

        if disc_52w <= 8:
            score += 6
        elif disc_52w <= 15:
            score += 3

        if mkt_rs >= 75:
            score += 8; reasons.append(f"strong MktRS {mkt_rs:.0f}")

    normalised = int(round(max(0, score) / 100.0 * 100))
    normalised = min(normalised, 100)
    # v5.8.3: tiered gate — early DD tier (8-10%) requires score ≥ 65 to pass
    if normalised < _bmc_score_threshold:
        return 0, []
    return normalised, reasons


def score_nifty_momentum(row: pd.Series, regime: str = "neutral",
                          nifty_drawdown_pct: float = 0.0,
                          market_context: Optional[dict] = None) -> tuple[int, list[str]]:
    """PATH 4: NIFTYBEES is an active asset, scored on breadth and leadership."""
    market_context = market_context or {}
    required = ["Close","RSI","ATR_PCT","VOL_RATIO","RS_SCORE","MKT_RS_SCORE","REVERSAL","CLOSE_POSITION"]
    for col in required:
        if col not in row.index or pd.isna(row[col]):
            return 0, []

    rsi_v     = float(row["RSI"])
    atr_pct   = float(row["ATR_PCT"])
    vol_ratio = float(row["VOL_RATIO"])
    rs_score  = float(row["RS_SCORE"])
    mkt_rs    = float(row["MKT_RS_SCORE"])
    reversal  = float(row["REVERSAL"])
    close_pos = float(row["CLOSE_POSITION"])

    breadth_pct = float(market_context.get("breadth_pct", 0.0) or 0.0)
    sector_part = float(market_context.get("sector_participation_pct", 0.0) or 0.0)
    leadership  = float(market_context.get("leadership_pct", 0.0) or 0.0)
    compression = float(market_context.get("volatility_compression", 0.0) or 0.0)
    reclaim_50  = bool(market_context.get("nifty_reclaimed_50", False))
    reclaim_200 = bool(market_context.get("nifty_reclaimed_200", False))
    below_50    = bool(market_context.get("nifty_below_50", False))
    below_200   = bool(market_context.get("nifty_below_200", False))
    drawdown_60 = float(market_context.get("nifty_drawdown_60", nifty_drawdown_pct) or nifty_drawdown_pct)

    if regime == "bear" or nifty_drawdown_pct > 4.0:
        return 0, []
    if below_50 or below_200:
        return 0, []
    if not (CFG["nifty_rsi_min"] <= rsi_v <= CFG["nifty_rsi_max"]):
        return 0, []
    if atr_pct > 3.5:
        return 0, []
    if vol_ratio < 0.80:
        return 0, []
    if mkt_rs < 55 or rs_score < 50:
        return 0, []
    if close_pos < 0.5:
        return 0, []
    if breadth_pct < 0.48 and sector_part < 0.45 and leadership < 0.10:
        return 0, []

    score = 0
    reasons = []
    if breadth_pct >= 0.60:
        score += 16; reasons.append(f"broad breadth {breadth_pct*100:.0f}%")
    elif breadth_pct >= 0.52:
        score += 10; reasons.append(f"healthy breadth {breadth_pct*100:.0f}%")

    if sector_part >= 0.60:
        score += 12; reasons.append(f"sector participation {sector_part*100:.0f}%")
    elif sector_part >= 0.45:
        score += 8

    if leadership >= 0.18:
        score += 14; reasons.append(f"leadership expansion {leadership*100:.0f}%")
    elif leadership >= 0.10:
        score += 8

    if compression >= 0.08:
        score += 10; reasons.append(f"volatility compression {compression*100:.0f}%")
    elif compression >= 0.03:
        score += 6

    if rsi_v >= 60:
        score += 14; reasons.append(f"strong momentum RSI {rsi_v:.1f}")
    elif rsi_v >= 50:
        score += 10; reasons.append(f"healthy RSI {rsi_v:.1f}")

    if mkt_rs >= 75:
        score += 14; reasons.append(f"elite MktRS {mkt_rs:.0f}")
    elif mkt_rs >= 60:
        score += 10; reasons.append(f"strong MktRS {mkt_rs:.0f}")

    if rs_score >= 65:
        score += 10; reasons.append(f"top sector RS {rs_score:.0f}")
    elif rs_score >= 50:
        score += 7

    if atr_pct <= 1.8:
        score += 8; reasons.append(f"low vol ATR {atr_pct:.1f}%")
    elif atr_pct <= 2.5:
        score += 4

    if vol_ratio >= 1.2:
        score += 8; reasons.append(f"volume support {vol_ratio:.1f}x")
    elif vol_ratio >= 0.9:
        score += 4

    if reversal == 1:
        score += 4
    if close_pos >= 0.75:
        score += 5
    elif close_pos >= 0.60:
        score += 3

    if drawdown_60 <= 2.0:
        score += 6; reasons.append(f"stable tape DD {drawdown_60:.1f}%")
    elif drawdown_60 <= 4.0:
        score += 3

    if reclaim_50 or reclaim_200:
        score += 6; reasons.append("trend reclaim")

    normalised = int(round(max(0, score) / 110.0 * 100))
    return min(normalised, 100), reasons


def score_bar(row: pd.Series, regime: str = "neutral",
              nifty_drawdown_pct: float = 0.0,
              ticker: Optional[str] = None,
              market_context: Optional[dict] = None) -> tuple[int, list[str], str]:
    """
    Master scoring function. Returns (score, reasons, path_name).
    Tries all eligible paths for current regime, returns best.
    """
    label    = _intensity_label(float(regime)) if isinstance(regime, float) else regime
    best_score = 0; best_reasons = []; best_path = "blocked"

    # PATH 4: NIFTYBEES treated as its own scored index ticker
    if ticker == CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"):
        s0, r0 = score_nifty_momentum(row, regime=label, nifty_drawdown_pct=nifty_drawdown_pct, market_context=market_context)
        if s0 > 0:
            return s0, r0, "nifty_momentum"
        return 0, [], "blocked"

    # PATH 1: Oversold pullback — all regimes, but guarded in weak tapes
    s1, r1 = score_oversold_pullback(row, regime, nifty_drawdown_pct=nifty_drawdown_pct, market_context=market_context)
    if label == "neutral" and s1 > 0:
        s1 += 2
    if s1 > best_score:
        best_score = s1; best_reasons = r1; best_path = "oversold_pullback"

    # PATH 2: Bull trend resumption — bull/recovering only
    if label in PATH_REGIME_MAP["trend_resumption"]:
        s2, r2 = score_trend_resumption(row, regime)
        if s2 > 0:
            s2 += 3
        if s2 > best_score:
            best_score = s2; best_reasons = r2; best_path = "trend_resumption"

    # PATH 3: Bear Flush / reclaim — bear only
    if label in PATH_REGIME_MAP["bear_flush"]:
        s3, r3 = score_bear_flush(row, nifty_drawdown_pct, market_context=market_context)
        if s3 > 0:
            s3 += 5
        if s3 > best_score:
            best_score = s3; best_reasons = r3; best_path = "bear_flush"

    if best_score == 0:
        return 0, [], "blocked"
    return best_score, best_reasons, best_path


# ══════════════════════════════════════════════════════════════════════
# SECTOR ELIGIBILITY
# ══════════════════════════════════════════════════════════════════════

def _mkt_rs_trend(ticker: str, date, rs_lookup: dict, sampled_dates: list) -> float:
    tw   = CFG["rs_trend_window"]
    past = [s for s in sampled_dates if s <= date]
    if len(past) < 2: return 0.0
    nearest_now = past[-1]
    target_idx  = max(0, len(past) - 1 - tw)
    nearest_old = past[target_idx]
    now_val = rs_lookup.get(nearest_now, {}).get(ticker, (np.nan, np.nan))[1]
    old_val = rs_lookup.get(nearest_old, {}).get(ticker, (np.nan, np.nan))[1]
    if np.isnan(now_val) or np.isnan(old_val): return 0.0
    return float(now_val - old_val)


def is_sector_eligible(
    ticker: str, sector: str,
    allowed_sectors: set, sector_ranking: list,
    row: pd.Series, regime: str = "neutral",
    rs_lookup: Optional[dict] = None,
    sampled_dates: Optional[list] = None,
    date=None,
    entry_path: str = "oversold_pullback",
) -> tuple[bool, str, int]:

    # Special paths bypass normal sector filter
    if entry_path in ("bear_flush", "nifty_momentum"):
        return True, entry_path, 0

    if sector in allowed_sectors:
        return True, "sector_rs", 0

    mkt_rs = float(row.get("MKT_RS_SCORE", 0) or 0)
    if pd.isna(mkt_rs): mkt_rs = 0.0

    n_sectors       = len(sector_ranking)
    sector_rank_pos = sector_ranking.index(sector) if sector in sector_ranking else n_sectors
    top_n           = len(allowed_sectors)
    bottom_cutoff   = int(n_sectors * 0.75)

    if sector_rank_pos >= bottom_cutoff:
        return False, "filtered_bottom_quartile", 0

    eligible_band  = max(bottom_cutoff - top_n, 1)
    rank_frac      = float(np.clip((sector_rank_pos - top_n) / eligible_band, 0.0, 1.0))
    base_thresholds = {
        "bull": CFG["override_mkt_rs_bull"], "recovering": CFG["override_mkt_rs_neutral"],
        "neutral": CFG["override_mkt_rs_neutral"], "bear": CFG["override_mkt_rs_bear"],
    }
    base_threshold  = base_thresholds.get(regime, CFG["override_mkt_rs_neutral"])
    penalty_key     = "override_rank_mkt_rs_penalty_bull" if regime == "bull" else "override_rank_mkt_rs_penalty"
    rank_penalty    = CFG.get(penalty_key, 12)
    threshold       = base_threshold + rank_frac * rank_penalty
    if mkt_rs < threshold: return False, "filtered_mkt_rs_level", 0

    override_trend_min = CFG.get("override_rs_trend_min", 12)
    if rs_lookup is not None and sampled_dates is not None and date is not None:
        trend = _mkt_rs_trend(ticker, date, rs_lookup, sampled_dates)
        if trend < override_trend_min: return False, "filtered_rs_trend", 0
    else:
        old_mkt_rs = float(row.get("MKT_RS_SCORE_OLD", np.nan) or np.nan)
        if not np.isnan(old_mkt_rs):
            if mkt_rs - float(old_mkt_rs) < override_trend_min:
                return False, "filtered_rs_trend", 0

    score_base    = CFG.get("override_rank_score_base",  10)
    score_extra   = CFG.get("override_rank_score_extra", 15)
    score_premium = int(round(score_base + rank_frac * score_extra))
    return True, "stock_rs_override", score_premium


# ══════════════════════════════════════════════════════════════════════
# PVD DETERIORATION EXIT
# ══════════════════════════════════════════════════════════════════════

def pvd_deteriorating(df: pd.DataFrame, lookback: int = None, min_bars: int = None) -> bool:
    if lookback is None: lookback = CFG["pvd_exit_window"]
    if min_bars is None: min_bars = CFG["pvd_exit_bars"]
    if "PVD_SIGNAL" not in df.columns or len(df) < lookback: return False
    recent = df["PVD_SIGNAL"].tail(lookback)
    return int((recent == "distribution").sum()) >= min_bars


# ══════════════════════════════════════════════════════════════════════
# NIFTY DRAWDOWN HELPER
# ══════════════════════════════════════════════════════════════════════

def _nifty_drawdown_pct(nifty_df: Optional[pd.DataFrame], as_of=None,
                         lookback_days: int = 60) -> float:
    """Returns how much Nifty is down from its recent high (positive = down)."""
    if nifty_df is None or nifty_df.empty: return 0.0
    try:
        if as_of is not None:
            ts  = pd.Timestamp(as_of)
            idx = nifty_df.index
            if idx.tz is not None and ts.tz is None: ts = ts.tz_localize("UTC")
            elif idx.tz is None and ts.tz is not None: ts = ts.tz_localize(None)
            df = nifty_df[idx <= ts]
        else:
            df = nifty_df
        if len(df) < 10: return 0.0
        recent = df["Close"].tail(lookback_days)
        peak   = float(recent.max())
        curr   = float(recent.iloc[-1])
        return max(0.0, (peak - curr) / peak * 100)
    except Exception:
        return 0.0


# ══════════════════════════════════════════════════════════════════════
# BENCHMARK HELPERS
# ══════════════════════════════════════════════════════════════════════

def blended_benchmark_ann(nifty_ann: float, avg_invested_pct: float,
                           avg_nifty_sleeve_pct: float = 0.0) -> float:
    """
    v5.6.0: three-way blended benchmark.
    avg_invested_pct   = fraction in active stocks
    avg_nifty_sleeve_pct = fraction in NIFTYBEES
    remainder          = liquid fund
    """
    lf   = CFG["liquid_fund_annual"] * 100
    idle = max(0.0, 1.0 - avg_invested_pct - avg_nifty_sleeve_pct)
    return (avg_invested_pct * nifty_ann
            + avg_nifty_sleeve_pct * nifty_ann
            + idle * lf)


def nifty_return(start, end) -> dict:
    df = download(NIFTY, start, end)
    if df.empty or len(df) < 5:
        return {"ann_ret":0.0,"sharpe":0.0,"max_dd":0.0,"total_ret":0.0}
    price   = df["Close"]
    years   = max((price.index[-1]-price.index[0]).days/365.25, 0.1)
    div_adj = (1.015 ** years)
    log_r   = np.log(price/price.shift(1)).dropna()
    total   = (price.iloc[-1]/price.iloc[0]*div_adj-1)*100
    ann     = ((1+total/100)**(1/years)-1)*100 if years > 0 else 0.0
    sharpe  = (log_r.mean()/log_r.std()*np.sqrt(252)) if log_r.std()>0 else 0.0
    peak = price.iloc[0]; dd = 0.0
    for v in price:
        peak = max(peak,v); dd = min(dd,(v-peak)/peak)
    return {"ann_ret":round(ann,2),"sharpe":round(float(sharpe),2),
            "max_dd":round(dd*100,2),"total_ret":round(total,2)}


# ══════════════════════════════════════════════════════════════════════
# ETF SLEEVE
# ══════════════════════════════════════════════════════════════════════


def _nifty_etf_target_alloc(regime_or_intensity) -> float:
    """
    v5.7.0 — Continuous NIFTY allocation.
    0% below neutral (intensity ≤ -0.10) — full bear means all liquid.
    Ramps smoothly from 0% at neutral threshold to nifty_alloc_max at full bull.
    Capped at cash_floor so it never exceeds the idle-capital budget.
    """
    if isinstance(regime_or_intensity, str):
        _map = {"bull": 1.0, "recovering": 0.35, "neutral": 0.0, "bear": -1.0}
        intensity = _map.get(regime_or_intensity, 0.0)
    else:
        intensity = float(regime_or_intensity)

    if intensity <= -0.10:
        return 0.0

    # ramp from 0 at intensity=-0.10 up to nifty_alloc_max at intensity=+1.0
    t           = float(np.clip((intensity + 0.10) / 1.10, 0.0, 1.0))
    nifty_alloc = t * float(CFG.get("nifty_alloc_max", 0.55))
    cash_floor  = _cash_floor(intensity=intensity)
    return float(min(nifty_alloc, cash_floor))


def _etf_rebalance(etf_pos: dict, target_alloc: float, total_nav: float,
                   etf_price: float, cash: float, date=None) -> tuple[dict, float, float, list[dict]]:
    try:
        if etf_price is None or etf_price <= 0 or np.isnan(etf_price):
            return etf_pos, cash, 0.0, []
    except Exception:
        return etf_pos, cash, 0.0, []

    # BUG-FIX v5.8.1: total_nav can be NaN if any open-position Close was NaN;
    # guard here so delta_val/current_val arithmetic stays finite.
    try:
        if total_nav is None or np.isnan(total_nav) or np.isinf(total_nav) or total_nav <= 0:
            return etf_pos, cash, 0.0, []
    except Exception:
        return etf_pos, cash, 0.0, []

    tolerance = CFG.get("nifty_alloc_tolerance", 0.05)
    cost_rate = CFG.get("nifty_etf_cost_per_leg", 0.0010)
    slip      = CFG.get("slippage_per_leg", 0.0030)

    target_val  = target_alloc * total_nav
    current_val = etf_pos.get("shares", 0) * etf_price
    drift       = abs(target_val - current_val) / max(total_nav, 1)
    etf_pnl     = 0.0
    txs: list[dict] = []

    if drift < tolerance:
        return etf_pos, cash, etf_pnl, txs

    delta_val = target_val - current_val
    tx_date = _json_safe(date) if date is not None else None

    if delta_val > 0:
        buy_val    = min(delta_val, cash * 0.98)
        if buy_val < 1000:
            return etf_pos, cash, etf_pnl, txs
        exec_price = etf_price * (1 + slip)
        if np.isnan(exec_price) or exec_price <= 0:
            return etf_pos, cash, 0.0, txs
        # BUG-FIX v5.8.1: buy_val can be NaN if delta_val/total_nav was NaN
        if np.isnan(buy_val):
            return etf_pos, cash, etf_pnl, txs
        new_shares = int(buy_val / exec_price)
        if new_shares < 1:
            return etf_pos, cash, etf_pnl, txs
        cost  = exec_price * new_shares * cost_rate
        spent = exec_price * new_shares + cost
        if spent > cash:
            new_shares = max(0, int((cash * 0.98) / exec_price))
            if new_shares < 1:
                return etf_pos, cash, etf_pnl, txs
            spent = exec_price * new_shares + new_shares * exec_price * cost_rate
            cost  = new_shares * exec_price * cost_rate

        prev_shares   = etf_pos.get("shares", 0)
        prev_invested = etf_pos.get("invested", 0.0)
        prev_avg      = etf_pos.get("avg_price", exec_price)
        etf_pos = {
            "shares":    prev_shares + new_shares,
            "invested":  prev_invested + spent,
            "avg_price": (prev_invested + spent) / max(prev_shares + new_shares, 1),
        }
        cash -= spent
        txs.append({
            "date": tx_date,
            "ticker": CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"),
            "side": "buy",
            "shares": new_shares,
            "price": round(exec_price, 2),
            "gross_value": round(exec_price * new_shares, 2),
            "cost": round(cost, 2),
            "pnl": 0.0,
            "target_alloc": round(target_alloc, 4),
            "reason": "rebalance_buy",
            "avg_price_before": round(prev_avg, 2),
        })
    else:
        sell_val   = abs(delta_val)
        exec_price = etf_price * (1 - slip)
        if np.isnan(exec_price) or exec_price <= 0:
            return etf_pos, cash, 0.0, txs
        # BUG-FIX v5.8.1: sell_val (from delta_val/current_val) can be NaN when
        # total_nav contained a NaN Close price; guard here before int() conversion.
        if np.isnan(sell_val) or sell_val <= 0:
            return etf_pos, cash, etf_pnl, txs
        sell_shares = min(int(sell_val / exec_price), etf_pos.get("shares", 0))
        if sell_shares < 1:
            return etf_pos, cash, etf_pnl, txs
        cost     = exec_price * sell_shares * cost_rate
        proceeds = exec_price * sell_shares - cost
        avg_p    = etf_pos.get("avg_price", exec_price)
        etf_pnl  = (exec_price - avg_p) * sell_shares - cost
        remaining = etf_pos.get("shares", 0) - sell_shares
        etf_pos   = {
            "shares":    remaining,
            "invested":  remaining * etf_pos.get("avg_price", exec_price),
            "avg_price": etf_pos.get("avg_price", exec_price),
        }
        cash += proceeds
        txs.append({
            "date": tx_date,
            "ticker": CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"),
            "side": "sell",
            "shares": sell_shares,
            "price": round(exec_price, 2),
            "gross_value": round(exec_price * sell_shares, 2),
            "cost": round(cost, 2),
            "pnl": round(etf_pnl, 2),
            "target_alloc": round(target_alloc, 4),
            "reason": "rebalance_sell",
            "avg_price_before": round(avg_p, 2),
        })

    return etf_pos, cash, etf_pnl, txs


def get_nifty_etf_recommendation(regime_or_intensity, equity: float) -> dict:
    """
    v5.6.0 — Continuous regime-aware capital allocation.
    Three buckets, nothing sits idle:
      1. Active stocks  — whatever the algo deploys (= 1 - cash_floor)
      2. NIFTYBEES ETF  — attack sleeve, scales up smoothly with bullishness
      3. Liquid fund    — stability sleeve, fills remaining cash floor

    Both NIFTYBEES and Liquid fund change smoothly with intensity in [-1, +1].
    At bear extreme: 0% NIFTY, 80% liquid, 20% stocks.
    At bull extreme: 55% NIFTY, 0% liquid, 45% stocks.
    """
    ticker = CFG.get("nifty_etf_ticker", "NIFTYBEES.NS")

    if isinstance(regime_or_intensity, str):
        label     = regime_or_intensity
        _map      = {"bull": 1.0, "recovering": 0.35, "neutral": 0.0, "bear": -1.0}
        intensity = _map.get(label, 0.0)
    else:
        intensity = float(regime_or_intensity)
        label     = _intensity_label(intensity)

    # ── Continuous interpolation ──────────────────────────────────────
    t           = float(np.clip((intensity + 1.0) / 2.0, 0.0, 1.0))
    nifty_min   = float(CFG.get("nifty_alloc_min",   0.00))
    nifty_max   = float(CFG.get("nifty_alloc_max",   0.55))
    liquid_max  = float(CFG.get("liquid_alloc_max",  0.80))  # at bear
    liquid_min  = float(CFG.get("liquid_alloc_min",  0.00))  # at bull

    cash_floor  = _cash_floor(label)
    nifty_alloc = float(np.clip(nifty_min + t * (nifty_max - nifty_min), 0.0, cash_floor))
    liquid_alloc = max(0.0, cash_floor - nifty_alloc)

    # Sanity-check: liquid_alloc should also respect the liquid curve
    # (cash_floor already drives the total idle budget correctly)
    stock_alloc = max(0.0, 1.0 - cash_floor)

    etf_value    = round(equity * nifty_alloc,  2)
    liquid_value = round(equity * liquid_alloc, 2)
    stock_budget = round(equity * stock_alloc,  2)

    # Fetch live NIFTYBEES price
    price = None
    try:
        df = yf.Ticker(ticker).history(period="2d", auto_adjust=True)
        df = _sanitise_df(df)   # v5.8.1
        if not df.empty:
            raw = df["Close"].iloc[-1]
            if raw is not None and not (isinstance(raw, float) and np.isnan(raw)):
                price = float(raw)
            if price is not None and price <= 0:
                price = None
    except Exception:
        pass

    shares = 0
    if price and etf_value > 0:
        shares = int(etf_value / price)

    liquid_rate = CFG.get("liquid_fund_annual", 0.06)

    return {
        "regime":            label,
        "intensity":         round(float(intensity), 4),
        "equity":            equity,
        # Bucket 1 — active stocks
        "stock_alloc_pct":   round(stock_alloc  * 100, 1),
        "stock_budget":      stock_budget,
        # Bucket 2 — NIFTYBEES attack sleeve
        "ticker":            ticker,
        "etf_alloc_pct":     round(nifty_alloc  * 100, 1),
        "etf_value":         etf_value,
        "price":             round(price, 2) if price else None,
        "shares":            shares,
        # Bucket 3 — liquid fund stability sleeve
        "liquid_alloc_pct":  round(liquid_alloc * 100, 1),
        "liquid_value":      liquid_value,
        "liquid_rate":       liquid_rate,
        "liquid_annual_earn": round(liquid_value * liquid_rate, 2),
        # Summary
        "cash_floor_pct":    round(cash_floor * 100, 1),
        "note": (
            f"Regime: {label.upper()}  intensity={intensity:+.3f}  |  "
            f"Stocks {stock_alloc*100:.0f}%  "
            f"NIFTYBEES {nifty_alloc*100:.0f}%  "
            f"Liquid {liquid_alloc*100:.0f}%"
        ),
    }


def print_capital_allocation(regime: str, intensity: float, equity: float) -> None:
    """
    v5.6.0 — Prints a dynamic capital allocation panel to the terminal.
    Shows the continuous NIFTY ↔ Liquid split driven by regime intensity.
    """
    rec  = get_nifty_etf_recommendation(intensity, equity)
    bar  = "─" * 70
    lf_r = rec["liquid_rate"] * 100

    # ── Regime intensity gauge ──────────────────────────────────────────
    t         = (intensity + 1.0) / 2.0          # 0 = full bear, 1 = full bull
    bar_width = 40
    filled    = int(round(t * bar_width))
    empty     = bar_width - filled

    # Colour codes (work in most terminals; stripped in logs)
    RESET = "\033[0m"; BOLD  = "\033[1m"
    RED   = "\033[91m"; YEL  = "\033[93m"; GRN  = "\033[92m"; CYN  = "\033[96m"

    if   intensity >=  0.40: gauge_col = GRN;  regime_icon = "▲ BULL"
    elif intensity >=  0.10: gauge_col = CYN;  regime_icon = "↗ RECOVERING"
    elif intensity >= -0.10: gauge_col = YEL;  regime_icon = "◆ NEUTRAL"
    else:                    gauge_col = RED;   regime_icon = "▼ BEAR"

    gauge_bar = gauge_col + "█" * filled + RESET + "░" * empty

    print(f"\n{bar}")
    print(f"  {BOLD}CAPITAL ALLOCATION  v5.7.0{RESET}  [{gauge_col}{BOLD}{regime_icon}{RESET}]"
          f"  intensity = {intensity:+.3f}")
    print(f"  Bear ◄ [{gauge_bar}] ► Bull")
    print(f"{bar}")
    print(f"  Total equity          Rs{equity:>12,.0f}")
    print(f"{bar}")

    # ── Stock budget bar ────────────────────────────────────────────────
    stk_pct = rec["stock_alloc_pct"]
    stk_bar = GRN + "█" * int(stk_pct / 5) + RESET
    print(f"  [1] Active stocks     Rs{rec['stock_budget']:>12,.0f}   "
          f"{GRN}{stk_pct:>4.0f}%{RESET}  {stk_bar}")

    # ── NIFTYBEES ETF bar ───────────────────────────────────────────────
    etf_pct = rec["etf_alloc_pct"]
    etf_bar = CYN + "█" * max(int(etf_pct / 5), 0) + RESET
    etf_line = (f"  [2] NIFTYBEES (attack) Rs{rec['etf_value']:>12,.0f}   "
                f"{CYN}{etf_pct:>4.0f}%{RESET}  {etf_bar}")
    if etf_pct > 0 and rec["price"]:
        etf_line += f"   ~{rec['shares']} units @ Rs{rec['price']:,.0f}"
    elif etf_pct == 0:
        etf_line += f"   {RED}(0% — full bear, no NIFTYBEES){RESET}"
    print(etf_line)

    # ── Liquid fund bar ─────────────────────────────────────────────────
    liq_pct = rec["liquid_alloc_pct"]
    liq_bar = YEL + "█" * max(int(liq_pct / 5), 0) + RESET
    print(f"  [3] Liquid (stability) Rs{rec['liquid_value']:>12,.0f}   "
          f"{YEL}{liq_pct:>4.0f}%{RESET}  {liq_bar}"
          f"   ~{lf_r:.1f}% p.a. → Rs{rec['liquid_annual_earn']:,.0f}/yr")

    print(f"{bar}")

    # ── Continuous param snapshot at current intensity ──────────────────
    rcfg = _regime_cfg(intensity)
    print(f"  {BOLD}Regime parameters at intensity {intensity:+.3f}:{RESET}")
    print(f"  Max positions: {rcfg['max_positions']}  │  "
          f"Max per sector: {rcfg['max_per_sector']}  │  "
          f"Sector top-N: {rcfg['sector_rs_top_n']}  │  "
          f"Max hold: {rcfg['max_hold_days']}d")
    print(f"  Max pos size: Rs{rcfg['max_position']:,}  │  "
          f"Stop mult: {rcfg['atr_stop_mult']:.2f}×ATR  │  "
          f"Target mult: {rcfg['profit_target_mult']:.2f}×ATR  │  "
          f"Entry score ≥ {rcfg['entry_score_min']}")
    print(f"  RSI entry: {rcfg['rsi_entry_min']}–{rcfg['rsi_entry_max']}  │  "
          f"Min vol ratio: {rcfg['min_volume_ratio']:.2f}  │  "
          f"Require reversal: {rcfg['require_reversal']}")

    nifty_min  = CFG.get("nifty_alloc_min",  0.00)
    nifty_max  = CFG.get("nifty_alloc_max",  0.55)
    liquid_max = CFG.get("liquid_alloc_max", 0.80)
    print(f"{bar}")
    print(f"  Curve:  NIFTY {nifty_min*100:.0f}% (bear) → {nifty_max*100:.0f}% (bull)  │  "
          f"Liquid {liquid_max*100:.0f}% (bear) → 0% (bull)  [continuous]")

    # Action note driven by actual continuous values — no discrete snapping
    etf_pct_val  = rec["etf_alloc_pct"]
    liq_pct_val  = rec["liquid_alloc_pct"]
    stk_pct_val  = rec["stock_alloc_pct"]

    if etf_pct_val == 0 and liq_pct_val >= 50:
        action_col  = RED
        action_verb = (f"Park Rs{rec['liquid_value']:,.0f} in liquid fund (Nippon/HDFC/SBI, T+1). "
                       f"Hold zero NIFTYBEES until intensity > -0.10.")
    elif etf_pct_val > 0 and liq_pct_val > 0:
        action_col  = YEL
        action_verb = (f"Split idle cash: Rs{rec['etf_value']:,.0f} → NIFTYBEES (~{rec['shares']} units), "
                       f"Rs{rec['liquid_value']:,.0f} → liquid fund. "
                       f"Stocks get Rs{rec['stock_budget']:,.0f}.")
    elif etf_pct_val > 0 and liq_pct_val == 0:
        action_col  = CYN
        action_verb = (f"All idle cash → NIFTYBEES (~{rec['shares']} units @ Rs{rec['price']:,.0f}). "
                       f"Zero liquid fund. Full stock budget Rs{rec['stock_budget']:,.0f}.")
    else:
        action_col  = GRN
        action_verb = f"Max NIFTYBEES. Zero liquid. Full stock budget Rs{rec['stock_budget']:,.0f}."

    print(f"  {action_col}Action: {action_verb}{RESET}")
    print(f"{bar}\n")


# ══════════════════════════════════════════════════════════════════════
# PORTFOLIO HEAT CHECK
# ══════════════════════════════════════════════════════════════════════

def _portfolio_heat(open_pos: dict, ticker_dfs: dict, date) -> float:
    if not open_pos: return 0.0
    returns = []
    for ticker, pos in open_pos.items():
        df = ticker_dfs.get(ticker)
        if df is None or date not in df.index: continue
        current = float(df.loc[date, "Close"])
        entry   = float(pos["entry"])
        if entry > 0: returns.append((current - entry) / entry)
    return float(np.mean(returns)) if returns else 0.0


# ══════════════════════════════════════════════════════════════════════
# PORTFOLIO BACKTEST
# ══════════════════════════════════════════════════════════════════════

def run_backtest(ticker_dfs: dict,
                 nifty_df: Optional[pd.DataFrame] = None,
                 sector_rank_lookup: Optional[dict] = None,
                 start_date=None,
                 end_date=None) -> dict:

    rs_lookup_bt     = ticker_dfs.pop("__rs_lookup__",    None)
    sampled_dates_bt = ticker_dfs.pop("__sampled_dates__", None)

    all_dates = sorted(set().union(*[set(df.index) for df in ticker_dfs.values()]))
    if start_date is not None:
        sdt = pd.Timestamp(start_date)
        if all_dates:
            ref = all_dates[0]
            ref_tz = getattr(ref, "tz", None)
            if ref_tz is not None and sdt.tz is None:
                sdt = sdt.tz_localize(ref_tz)
            elif ref_tz is None and sdt.tz is not None:
                sdt = sdt.tz_convert(None)
        all_dates = [d for d in all_dates if d >= sdt]
    if end_date is not None:
        edt = pd.Timestamp(end_date)
        if all_dates:
            ref = all_dates[0]
            ref_tz = getattr(ref, "tz", None)
            if ref_tz is not None and edt.tz is None:
                edt = edt.tz_localize(ref_tz)
            elif ref_tz is None and edt.tz is not None:
                edt = edt.tz_convert(None)
        all_dates = [d for d in all_dates if d <= edt]
    if not all_dates:
        if rs_lookup_bt:     ticker_dfs["__rs_lookup__"]     = rs_lookup_bt
        if sampled_dates_bt: ticker_dfs["__sampled_dates__"] = sampled_dates_bt
        return _empty_bt()

    intensity_series: Optional[pd.Series] = None
    label_series:     Optional[pd.Series] = None
    if nifty_df is not None and not nifty_df.empty:
        intensity_series, label_series = build_regime_series(nifty_df)

    def _get_intensity(date) -> float:
        if intensity_series is None: return 0.0
        try:
            idx = intensity_series.index; d = pd.Timestamp(date)
            if idx.tz is not None and d.tz is None: d = d.tz_localize("UTC")
            elif idx.tz is None and d.tz is not None: d = d.tz_localize(None)
            mask = idx <= d
            if mask.any(): return float(intensity_series[mask].iloc[-1])
        except Exception: pass
        return 0.0

    def _get_label(date) -> str:
        if label_series is None: return "neutral"
        try:
            idx = label_series.index; d = pd.Timestamp(date)
            if idx.tz is not None and d.tz is None: d = d.tz_localize("UTC")
            elif idx.tz is None and d.tz is not None: d = d.tz_localize(None)
            mask = idx <= d
            if mask.any(): return str(label_series[mask].iloc[-1])
        except Exception: pass
        return "neutral"

    # FIX BUG 3: Pre-build the fundamental exclusion set ONCE instead of calling
    # passes_fundamental_gate() (which loads+saves pickle) inside the hot loop.
    def _safe_fund_gate(t: str) -> bool:
        try:
            return passes_fundamental_gate(t)
        except Exception:
            return True  # permissive on error

    _fund_excluded: frozenset = frozenset(
        t for t in ticker_dfs
        if not t.startswith("__") and not _safe_fund_gate(t)
    )

    kelly_state  = _new_kelly_state()
    cash         = float(CFG["capital"])
    open_pos     = {}; trades = []; daily_nav = []; regime_log = []
    daily_invested_frac = []
    daily_nifty_sleeve_frac = []   # v5.6.0: track NIFTYBEES sleeve separately
    path_counts  = {"oversold_pullback": 0, "trend_resumption": 0,
                    "bear_flush": 0, "nifty_momentum": 0, "stock_rs_override": 0,
                    "exceptional_tier": 0}  # v5.8.4
    etf_pos: dict = {"shares": 0, "invested": 0.0, "avg_price": 0.0}
    etf_total_pnl = 0.0
    nifty_trades: list[dict] = []

    for date in all_dates:
        intensity = _get_intensity(date)
        regime    = _get_label(date)
        regime_log.append(regime)
        rcfg      = _regime_cfg(intensity)
        nifty_dd  = _nifty_drawdown_pct(nifty_df, as_of=date)

        if sector_rank_lookup is not None:
            allowed_sectors    = get_allowed_sectors(sector_rank_lookup, date, regime=regime)
            sampled_sorted     = sorted(sector_rank_lookup.keys())
            past_s             = [s for s in sampled_sorted if s <= date]
            sector_ranking_now = sector_rank_lookup.get(past_s[-1], []) if past_s else []
        else:
            allowed_sectors    = set(UNIVERSE.keys())
            sector_ranking_now = []

        if nifty_df is not None and not nifty_df.empty:
            try:
                nidx = nifty_df.index; nd = pd.Timestamp(date)
                if nidx.tz is not None and nd.tz is None: nd = nd.tz_localize("UTC")
                elif nidx.tz is None and nd.tz is not None: nd = nd.tz_localize(None)
                nmask     = nidx <= nd
                etf_price = float(nifty_df["Close"][nmask].iloc[-1]) / 100.0 if nmask.any() else None
            except Exception:
                etf_price = None
        else:
            etf_price = None

        market_context = compute_market_context(ticker_dfs, date, nifty_df=nifty_df)

        if etf_price and etf_price > 0 and not np.isnan(etf_price):
            etf_mtm_now   = etf_pos["shares"] * etf_price
            total_nav_now = cash + etf_mtm_now + sum(
                float(ticker_dfs[t].loc[date,"Close"]) * p["shares"]
                for t, p in open_pos.items()
                if t in ticker_dfs and date in ticker_dfs[t].index
                and not pd.isna(ticker_dfs[t].loc[date,"Close"])
            )
            # v5.6.0: continuous NIFTY target from intensity (not discrete regime)
            etf_target = _nifty_etf_target_alloc(intensity)
            etf_pos, cash, _etf_bar_pnl, _etf_txs = _etf_rebalance(
                etf_pos, etf_target, total_nav_now, etf_price, cash, date=date
            )
            etf_total_pnl += _etf_bar_pnl
            nifty_trades.extend(_etf_txs)

        # v5.6.0: Liquid fund accrual — idle cash (above what's in ETF/stocks)
        # earns liquid_fund_annual / 252 per bar. This ensures no cash sits idle.
        _liquid_daily_rate = (1.0 + CFG.get("liquid_fund_annual", 0.06)) ** (1.0 / 252) - 1.0
        _etf_val_accrual   = etf_pos["shares"] * (etf_price if etf_price and etf_price > 0 and not np.isnan(etf_price) else etf_pos.get("avg_price", 0))
        _stock_val_accrual = sum(
            float(ticker_dfs[t].loc[date,"Close"]) * p["shares"]
            for t, p in open_pos.items()
            if t in ticker_dfs and date in ticker_dfs[t].index
            and not pd.isna(ticker_dfs[t].loc[date,"Close"])
        )
        # idle cash = all uninvested cash (cash already excludes open stock & ETF positions)
        _idle_cash = cash
        cash += _idle_cash * _liquid_daily_rate

        # ── EXIT LOOP ──────────────────────────────────────────────────
        to_close = []
        for ticker, pos in open_pos.items():
            df = ticker_dfs.get(ticker)
            if df is None or date not in df.index: continue
            row   = df.loc[date]
            # BUG-FIX v5.8.1: skip bar if Close/High are NaN — can't exit at a price we don't have
            _close_raw = row["Close"]; _high_raw = row["High"]
            if pd.isna(_close_raw) or pd.isna(_high_raw): continue
            price = float(_close_raw); high = float(_high_raw)
            rsi_v = float(row["RSI"]) if not pd.isna(row["RSI"]) else 50.0
            atr_v = float(row["ATR"]) if not pd.isna(row["ATR"]) else pos["atr"]

            pos["trail_high"] = max(pos.get("trail_high", pos["entry"]), high)
            trail_stop        = pos["trail_high"] - atr_v * CFG["atr_trail_mult"]
            pos["stop"]       = max(pos["stop"], trail_stop)
            pos["days"]      += 1

            pos_max_hold = _max_hold(intensity=pos.get("entry_intensity", intensity))
            # v5.8.4: exceptional tier gets extra breathing room
            if pos.get("exceptional_tier", False):
                pos_max_hold = max(pos_max_hold, CFG["exc_max_hold_days"])

            pvd_exit = False
            try:
                idx = df.index; ts_use = pd.Timestamp(date)
                if idx.tz is not None and ts_use.tz is None: ts_use = ts_use.tz_localize("UTC")
                elif idx.tz is None and ts_use.tz is not None: ts_use = ts_use.tz_localize(None)
                # else ts_use remains as-is (both same tz)
                past_df  = df[idx <= ts_use].tail(CFG["pvd_exit_window"])
                pvd_exit = pvd_deteriorating(past_df)
            except Exception: pass

            # Bear compounder: tighter targets
            is_bmc     = pos.get("entry_path") == "bear_flush"
            is_exc     = pos.get("exceptional_tier", False)  # v5.8.4

            exit_p = None; reason = None
            if price <= pos["stop"]:
                exit_p = pos["stop"];   reason = "stop"
            elif price >= pos["target"]:
                exit_p = pos["target"]; reason = "target"
            elif rsi_v >= rcfg.get("rsi_exit", CFG["rsi_exit"]) and not is_bmc and not is_exc:
                exit_p = price;         reason = "rsi_exit"
            elif pos["days"] >= pos_max_hold:
                exit_p = price;         reason = "timeout"
            elif pvd_exit and pos["days"] >= 3 and price > pos["entry"]:
                exit_p = price;         reason = "pvd_exit"
            # Bear compounder: exit if regime turns recovering/bull
            elif is_bmc and regime in ("recovering", "bull"):
                exit_p = price;         reason = "regime_change"

            if exit_p is not None:
                to_close.append((ticker, exit_p, reason, dict(pos)))

        for ticker, exit_p, reason, pos in to_close:
            ep   = exit_p * (1 - CFG["slippage_per_leg"])
            cost = (pos["entry"] + ep) * pos["shares"] * CFG["cost_per_leg"]
            pnl  = (ep - pos["entry"]) * pos["shares"] - cost
            cash += pos["invested"] + pnl
            _update_kelly(kelly_state, pnl)
            trades.append({
                "ticker": ticker,
                "sector": pos.get("sector", SECTOR_OF.get(ticker, "OTHER")),
                "entry_date": pos.get("entry_date"),
                "exit_date": str(date),
                "entry": pos["entry"],
                "exit": round(ep,2),
                "shares": pos["shares"],
                "pnl": round(pnl,2),
                "reason": reason,
                "days": pos["days"],
                "regime": pos.get("regime","?"),
                "entry_regime_label": pos.get("entry_regime_label", pos.get("regime","?")),
                "entry_path": pos.get("entry_path","oversold_pullback"),
                "elig_path": pos.get("elig_path", "sector_rs"),
                "score": pos.get("score", 0),
                "nifty_dd_entry": pos.get("nifty_dd_entry", None),
                "invested": pos.get("invested", 0.0),
                "stop": pos.get("stop", None),
                "target": pos.get("target", None),
            })
            del open_pos[ticker]

        # ── ENTRY LOOP ─────────────────────────────────────────────────
        max_pos_regime = rcfg.get("max_positions", CFG["max_positions"])

        etf_val_now   = etf_pos["shares"] * (etf_price if etf_price and not np.isnan(etf_price) else etf_pos.get("avg_price", 0))
        stock_val_now = sum(
            float(ticker_dfs[t].loc[date,"Close"]) * p["shares"]
            for t, p in open_pos.items()
            if t in ticker_dfs and date in ticker_dfs[t].index
            and not pd.isna(ticker_dfs[t].loc[date,"Close"])
        )
        total_nav_entry = cash + etf_val_now + stock_val_now
        deployed_frac   = (etf_val_now + stock_val_now) / max(total_nav_entry, 1)
        floor           = _cash_floor(intensity=intensity)
        cash_floor_ok   = deployed_frac < (1.0 - floor)

        heat    = _portfolio_heat(open_pos, ticker_dfs, date)
        heat_ok = (heat >= CFG["portfolio_heat_threshold"]
                   or len(open_pos) < CFG["portfolio_heat_min_pos"])

        if len(open_pos) < max_pos_regime and cash_floor_ok and heat_ok:
            sector_invested: dict[str, float] = {}
            for t, p in open_pos.items():
                s = SECTOR_OF.get(t, "OTHER")
                sector_invested[s] = sector_invested.get(s, 0) + p["invested"]
            total_book = sum(p["invested"] for p in open_pos.values()) + cash
            if total_book == 0: total_book = float(CFG["capital"])

            candidates = []
            for ticker, df in ticker_dfs.items():
                if ticker in open_pos or date not in df.index: continue
                # FIX BUG 3: use the pre-built set instead of calling passes_fundamental_gate()
                if ticker in _fund_excluded: continue
                if not passes_liquidity(df, as_of=date): continue

                row = df.loc[date]
                if rs_lookup_bt and sampled_dates_bt:
                    past_s2 = [s for s in sampled_dates_bt if s <= date]
                    tw = CFG["rs_trend_window"]
                    if len(past_s2) >= tw + 1:
                        old_s   = past_s2[-(tw + 1)]
                        old_val = rs_lookup_bt.get(old_s, {}).get(ticker, (np.nan, np.nan))[1]
                    else:
                        old_val = np.nan
                    row = row.copy()
                    row["MKT_RS_SCORE_OLD"] = old_val

                score, reasons, entry_path = score_bar(row, regime=intensity,
                                                        nifty_drawdown_pct=nifty_dd,
                                                        ticker=ticker,
                                                        market_context=market_context)
                if score == 0: continue

                # Path-specific score minimums
                path_min = {
                    "oversold_pullback": rcfg["entry_score_min"],
                    "trend_resumption":  CFG["tr_score_min"],
                    "bear_flush":         CFG["bmc_score_min"],
                    "nifty_momentum":     CFG["nifty_score_min"],
                }.get(entry_path, rcfg["entry_score_min"])
                if score < path_min: continue

                sector = SECTOR_OF.get(ticker, "OTHER")
                eligible, elig_path, rank_score_premium = is_sector_eligible(
                    ticker, sector, allowed_sectors, sector_ranking_now,
                    row, regime=regime,
                    rs_lookup=rs_lookup_bt,
                    sampled_dates=sampled_dates_bt,
                    date=date,
                    entry_path=entry_path,
                )
                if not eligible: continue

                if elig_path == "stock_rs_override":
                    override_min = min(rcfg["entry_score_min"] + rank_score_premium, 90)
                    if score < override_min: continue

                candidates.append((score, ticker, row, reasons, entry_path, elig_path))

            candidates.sort(reverse=True)
            max_per_sec = rcfg.get("max_per_sector", CFG["max_per_sector"])

            for score, ticker, row, reasons, entry_path, elig_path in candidates:
                if len(open_pos) >= max_pos_regime: break

                etf_v_check = etf_pos["shares"] * (etf_price if etf_price and not np.isnan(etf_price) else etf_pos.get("avg_price", 0))
                stk_v_check = sum(
                    float(ticker_dfs[t].loc[date,"Close"]) * p["shares"]
                    for t, p in open_pos.items()
                    if t in ticker_dfs and date in ticker_dfs[t].index
                    and not pd.isna(ticker_dfs[t].loc[date,"Close"])
                )
                nav_check = cash + etf_v_check + stk_v_check
                dep_check = (etf_v_check + stk_v_check) / max(nav_check, 1)
                if dep_check >= (1.0 - floor): break

                sector    = SECTOR_OF.get(ticker, "OTHER")
                sec_count = sum(1 for t in open_pos if SECTOR_OF.get(t) == sector)
                if sec_count >= max_per_sec: continue
                sec_pct = sector_invested.get(sector, 0) / total_book
                if sec_pct >= 0.25: continue

                price = float(row["Close"]); atr_v = float(row["ATR"])
                if pd.isna(price) or pd.isna(atr_v): continue
                entry = price * (1 + CFG["slippage_per_leg"])

                # v5.7.0: all mults from continuous rcfg; BMC overrides stay tighter
                # v5.8.4: exceptional tier unlocks bull-mode sizing for bear-defying stocks
                _exceptional = (
                    entry_path == "bear_flush"
                    and is_exceptional_tier(row, score, regime)
                )
                if _exceptional:
                    max_pos_sz = CFG["exc_max_position"]
                    stop_mult  = CFG["exc_stop_mult"]
                    tgt_mult   = CFG["exc_target_mult"]
                elif entry_path == "bear_flush":
                    max_pos_sz = CFG["bmc_max_position"]
                    stop_mult  = CFG["bmc_atr_stop_mult"]
                    tgt_mult   = CFG["bmc_profit_target_mult"]
                else:
                    max_pos_sz = rcfg.get("max_position", CFG["max_position"])
                    stop_mult  = rcfg.get("atr_stop_mult",      CFG["atr_stop_mult"])
                    tgt_mult   = rcfg.get("profit_target_mult", CFG["profit_target_mult"])
                if entry_path == "nifty_momentum":
                    max_pos_sz = min(max_pos_sz, 15_000)
                _int_ks    = float(np.clip(1.0 + float(intensity) * CFG.get("intensity_kelly_scale", 0.35), 0.5, 1.5))

                sz = size_position(entry, atr_v, cash, kelly_state,
                                   max_pos_override=max_pos_sz,
                                   kelly_scale=_int_ks,
                                   stop_mult_override=stop_mult,
                                   target_mult_override=tgt_mult)
                if sz is None or sz["invested"] > cash: continue

                cash -= sz["invested"]
                sector_invested[sector] = sector_invested.get(sector, 0) + sz["invested"]
                if entry_path in path_counts: path_counts[entry_path] += 1
                if elig_path == "stock_rs_override": path_counts["stock_rs_override"] += 1
                if _exceptional:
                    path_counts["exceptional_tier"] = path_counts.get("exceptional_tier", 0) + 1

                open_pos[ticker] = {
                    "shares": sz["shares"], "entry": sz["entry"],
                    "invested": sz["invested"], "stop": sz["stop"],
                    "target": sz["target"], "trail_high": price,
                    "atr": atr_v, "days": 0, "regime": regime,
                    "entry_regime_label": regime,
                    "entry_date": str(date),
                    "exit_date": None,
                    "sector": sector,
                    "score": score,
                    "elig_path": elig_path,
                    "intensity": round(float(intensity), 3),
                    "entry_intensity": round(float(intensity), 3),  # v5.7.0: for continuous max_hold
                    "entry_path": entry_path,
                    "nifty_dd_entry": round(float(nifty_dd), 2),
                    "exceptional_tier": _exceptional,              # v5.8.4: tag for exit + reporting
                }

        stock_mtm = sum(
            float(ticker_dfs[t].loc[date,"Close"]) * p["shares"]
            for t, p in open_pos.items()
            if t in ticker_dfs and date in ticker_dfs[t].index
            and not pd.isna(ticker_dfs[t].loc[date,"Close"])
        )
        etf_mtm   = etf_pos["shares"] * (etf_price if etf_price and not np.isnan(etf_price) else etf_pos.get("avg_price", 0))
        total_nav = cash + stock_mtm + etf_mtm
        daily_nav.append({"date": date, "nav": total_nav})
        invested_amt = sum(p["invested"] for p in open_pos.values()) + etf_pos.get("invested", 0)
        daily_invested_frac.append(invested_amt / total_nav if total_nav > 0 else 0.0)
        # v5.6.0: track NIFTYBEES sleeve separately for blended benchmark
        daily_nifty_sleeve_frac.append(etf_mtm / total_nav if total_nav > 0 else 0.0)

    # Close remaining positions
    for ticker, pos in list(open_pos.items()):
        _last_raw = ticker_dfs[ticker]["Close"].dropna()
        if _last_raw.empty: continue  # BUG-FIX v5.8.1: no valid close price, skip
        last = float(_last_raw.iloc[-1])
        ep   = last * (1 - CFG["slippage_per_leg"])
        cost = (pos["entry"] + ep) * pos["shares"] * CFG["cost_per_leg"]
        pnl  = (ep - pos["entry"]) * pos["shares"] - cost
        cash += pos["invested"] + pnl
        trades.append({
            "ticker": ticker,
            "sector": pos.get("sector", SECTOR_OF.get(ticker, "OTHER")),
            "entry_date": pos.get("entry_date"),
            "exit_date": str(all_dates[-1]),
            "entry": pos["entry"], "exit": round(ep,2),
            "shares": pos["shares"], "pnl": round(pnl,2),
            "reason": "period_end", "days": pos["days"],
            "regime": pos.get("regime","?"),
            "entry_regime_label": pos.get("entry_regime_label", pos.get("regime","?")),
            "entry_path": pos.get("entry_path","oversold_pullback"),
            "elig_path": pos.get("elig_path", "sector_rs"),
            "score": pos.get("score", 0),
            "nifty_dd_entry": pos.get("nifty_dd_entry", None),
            "invested": pos.get("invested", 0.0),
            "stop": pos.get("stop", None),
            "target": pos.get("target", None),
        })

    if etf_pos["shares"] > 0 and nifty_df is not None and not nifty_df.empty:
        try:
            _etf_last_raw = nifty_df["Close"].dropna()
            last_etf_price = float(_etf_last_raw.iloc[-1]) / 100.0 if not _etf_last_raw.empty else etf_pos.get("avg_price", 0)
        except Exception: last_etf_price = etf_pos.get("avg_price", 0)
        ep_etf   = last_etf_price * (1 - CFG["slippage_per_leg"])
        cost_etf = (etf_pos["avg_price"] + ep_etf) * etf_pos["shares"] * CFG["nifty_etf_cost_per_leg"]
        pnl_etf  = (ep_etf - etf_pos["avg_price"]) * etf_pos["shares"] - cost_etf
        cash    += ep_etf * etf_pos["shares"] - cost_etf
        etf_total_pnl += pnl_etf
        nifty_trades.append({
            "date": _json_safe(all_dates[-1]),
            "ticker": CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"),
            "side": "sell",
            "shares": etf_pos["shares"],
            "price": round(ep_etf, 2),
            "gross_value": round(ep_etf * etf_pos["shares"], 2),
            "cost": round(cost_etf, 2),
            "pnl": round(pnl_etf, 2),
            "target_alloc": 0.0,
            "reason": "period_end",
            "avg_price_before": round(etf_pos.get("avg_price", ep_etf), 2),
        })

    if rs_lookup_bt:     ticker_dfs["__rs_lookup__"]     = rs_lookup_bt
    if sampled_dates_bt: ticker_dfs["__sampled_dates__"] = sampled_dates_bt

    res = _metrics(daily_nav, trades, start_capital=CFG["capital"])
    nifty_total_pnl = round(float(sum(float(t.get("pnl", 0.0)) for t in nifty_trades)), 2)
    res["avg_invested_pct"] = float(np.mean(daily_invested_frac)) if daily_invested_frac else 0.0
    res["avg_nifty_sleeve_pct"] = float(np.mean(daily_nifty_sleeve_frac)) if daily_nifty_sleeve_frac else 0.0
    res["path_counts"]      = path_counts
    res["etf_total_pnl"]    = nifty_total_pnl
    res["nifty_total_pnl"]  = nifty_total_pnl
    res["nifty_trade_count"] = len(nifty_trades)
    res["nifty_trades"]     = nifty_trades
    res["etf_category"]     = _etf_category_breakdown(nifty_trades)

    if regime_log:
        counts = {r: regime_log.count(r) for r in ["bull","recovering","neutral","bear"]}
        total  = len(regime_log)
        res["regime_pct"] = {k: round(v/total*100,1) for k, v in counts.items()}

    res["regime_trades"]    = _regime_trade_breakdown(trades)
    res["path_trades"]      = _path_trade_breakdown(trades)
    res["trades_list"]      = trades
    return res


def _regime_trade_breakdown(trades: list) -> dict:
    buckets: dict[str, list] = {"bull":[],"recovering":[],"neutral":[],"bear":[]}
    for t in trades:
        r = t.get("regime","neutral")
        if r not in buckets: r = "neutral"
        buckets[r].append(t["pnl"])
    result = {}
    for regime, pnls in buckets.items():
        if not pnls:
            result[regime] = {"trades":0,"win_rate":0.0,"avg_pnl":0.0,"total_pnl":0.0}
            continue
        wins = [p for p in pnls if p > 0]
        result[regime] = {
            "trades":    len(pnls),
            "win_rate":  round(len(wins)/len(pnls)*100,1),
            "avg_pnl":   round(float(np.mean(pnls)),2),
            "total_pnl": round(float(sum(pnls)),2),
        }
    return result


def _path_trade_breakdown(trades: list) -> dict:
    buckets: dict[str, list] = {"oversold_pullback":[],
                                "trend_resumption":[],
                                "bear_flush":[],
                                "nifty_momentum":[]}
    for t in trades:
        p = t.get("entry_path","oversold_pullback")
        if p not in buckets: p = "oversold_pullback"
        buckets[p].append(t["pnl"])
    result = {}
    for path, pnls in buckets.items():
        if not pnls:
            result[path] = {"trades":0,"win_rate":0.0,"avg_pnl":0.0,"total_pnl":0.0}
            continue
        wins = [p for p in pnls if p > 0]
        result[path] = {
            "trades":    len(pnls),
            "win_rate":  round(len(wins)/len(pnls)*100,1),
            "avg_pnl":   round(float(np.mean(pnls)),2),
            "total_pnl": round(float(sum(pnls)),2),
        }
    return result


def _etf_category_breakdown(nifty_trades: list[dict]) -> dict:
    """Summarize the NIFTY ticker as a separate category."""
    if not nifty_trades:
        return {"trades": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}

    pnls = [float(t.get("pnl", 0.0)) for t in nifty_trades if isinstance(t, dict)]
    if not pnls:
        return {"trades": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0}

    wins = [p for p in pnls if p > 0]
    return {
        "trades": len(pnls),
        "win_rate": round(len(wins) / len(pnls) * 100, 1),
        "avg_pnl": round(float(np.mean(pnls)), 2),
        "total_pnl": round(float(sum(pnls)), 2),
    }

# NOTE: _nifty_trade_breakdown() removed — it was dead code that always returned
# hardcoded zeros for buy_trades/sell_trades. _etf_category_breakdown() is the
# live equivalent used everywhere.


def _json_safe(obj):
    """Recursively convert pandas/numpy/date objects into JSON-safe structures."""
    if obj is None:
        return None
    if isinstance(obj, (str, bool, int, float)):
        if isinstance(obj, float) and (np.isnan(obj) or np.isinf(obj)):
            return None
        return obj
    if isinstance(obj, (np.generic,)):
        try:
            return obj.item()
        except Exception:
            return float(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    if isinstance(obj, pd.Series):
        return [_json_safe(v) for v in obj.tolist()]
    if isinstance(obj, pd.DataFrame):
        return [_json_safe(r) for r in obj.to_dict("records")]
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_json_safe(v) for v in obj]
    try:
        if pd.isna(obj):
            return None
    except Exception:
        pass
    return str(obj)


def _journal_read() -> list:
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
    return []


def _journal_write(entries: list) -> None:
    try:
        with open(JOURNAL_PATH, "w", encoding="utf-8") as f:
            json.dump(_json_safe(entries), f, indent=2)
    except Exception:
        pass


def _journal_append(entry: dict) -> None:
    entries = _journal_read()
    entries.append(_json_safe(entry))
    # keep journal reasonably small
    if len(entries) > 100:
        entries = entries[-100:]
    _journal_write(entries)


def _empty_bt() -> dict:
    empty_nav = pd.Series(dtype=float, name="nav")
    return {
        "trades": 0,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "rr": 0.0,
        "sharpe": 0.0,
        "max_dd_pct": 0.0,
        "final_cap": float(CFG["capital"]),
        "total_pnl": 0.0,
        "nav_series": empty_nav,
        "avg_invested_pct": 0.0,
        "avg_nifty_sleeve_pct": 0.0,
        "path_counts": {"oversold_pullback": 0, "trend_resumption": 0, "bear_flush": 0, "nifty_momentum": 0, "stock_rs_override": 0},
        "etf_total_pnl": 0.0,
        "nifty_total_pnl": 0.0,
        "nifty_trade_count": 0,
        "nifty_trades": [],
        "etf_category": {"trades": 0, "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0},
        "regime_pct": {"bull": 0.0, "recovering": 0.0, "neutral": 0.0, "bear": 0.0},
        "regime_trades": _regime_trade_breakdown([]),
        "path_trades": _path_trade_breakdown([]),
        "trades_list": [],
    }


def _metrics(daily_nav: list, trades: list, start_capital: Optional[float] = None) -> dict:
    if start_capital is None:
        start_capital = float(CFG["capital"])
    if not daily_nav:
        return _empty_bt()

    nav_df = pd.DataFrame(daily_nav).copy()
    if nav_df.empty or "nav" not in nav_df.columns:
        return _empty_bt()

    nav_df["date"] = pd.to_datetime(nav_df["date"])
    nav_df = nav_df.sort_values("date")
    nav_series = nav_df.set_index("date")["nav"].astype(float)
    rets = nav_series.pct_change().replace([np.inf, -np.inf], np.nan).dropna()

    final_cap = float(nav_series.iloc[-1]) if len(nav_series) else float(start_capital)
    total_pnl = final_cap - float(start_capital)

    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    peak = -np.inf
    max_dd = 0.0
    for v in nav_series:
        peak = max(peak, float(v))
        if peak > 0:
            max_dd = min(max_dd, (float(v) - peak) / peak)
    max_dd_pct = round(float(max_dd) * 100, 2)

    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    rr = abs(avg_win / avg_loss) if avg_win > 0 and avg_loss < 0 else 0.0
    win_rate = (len(wins) / len(pnls) * 100) if pnls else 0.0

    return {
        "trades": len(pnls),
        "win_rate": round(win_rate, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "rr": round(rr, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "final_cap": round(final_cap, 2),
        "total_pnl": round(total_pnl, 2),
        "nav_series": nav_series,
    }


def _print_sig(sig: dict, label: str = "SIGNAL") -> None:
    ticker = sig.get("ticker", "?")
    sector = sig.get("sector", "OTHER")
    score = sig.get("score", 0)
    path = sig.get("entry_path", sig.get("path", "blocked"))
    elig = sig.get("elig_path", "")
    rsi_v = sig.get("rsi", np.nan)
    mkt_rs = sig.get("mkt_rs", np.nan)
    sec_rs = sig.get("sec_rs", np.nan)
    price = sig.get("price", np.nan)
    data_date = sig.get("data_date", "?")
    days_stale = int(sig.get("days_stale", 0) or 0)
    reasons = sig.get("reasons", [])
    reason_str = "; ".join(reasons[:3]) if isinstance(reasons, list) else str(reasons)
    # v5.8.4: exceptional tier badge
    exc_badge = "  [*** EXCEPTIONAL TIER — BULL SIZING ***]" if sig.get("exceptional_tier") else ""
    print(f"  [{label:<8}] {ticker:<16} Score {score:>3}  RSI {rsi_v:>5.1f}  SecRS {sec_rs:>5.1f}  MktRS {mkt_rs:>5.1f}  Price Rs{price:>8.2f}{exc_badge}")
    print(f"             Path [{path:<18}|elig:{elig:<14}]")
    print(f"             Data {data_date}  |  stale {days_stale} NSE session(s)")
    if reason_str:
        print(f"             · {reason_str}")


def _print_path_breakdown(path_breakdown: dict) -> None:
    print(f"\n  ┌──────────────────────────────────────────────────────────────┐")
    print(f"  │  ENTRY PATH BREAKDOWN                                        │")
    print(f"  ├──────────────────────┬────────┬──────────┬────────────┬──────────┤")
    print(f"  │  Path                │ Trades │  Win %   │  Avg PnL   │ Total PnL│")
    print(f"  ├──────────────────────┼────────┼──────────┼────────────┼──────────┤")
    order = ["oversold_pullback", "trend_resumption", "bear_flush", "nifty_momentum"]
    labels = {
        "oversold_pullback": "Oversold Pullback",
        "trend_resumption": "Bull Trend",
        "bear_flush": "Bear Survivor / Capitulation",
        "nifty_momentum": "NIFTYBEES Active",
    }
    for path in order:
        d = path_breakdown.get(path, {"trades":0,"win_rate":0.0,"avg_pnl":0.0,"total_pnl":0.0})
        print(f"  │  {labels[path]:<20} │ {d['trades']:<6} │ {d['win_rate']:<8.1f} │ Rs{d['avg_pnl']:<10,.0f} │ Rs{d['total_pnl']:<8,.0f}│")
    print(f"  └──────────────────────┴────────┴──────────┴────────────┴──────────┘")


def _print_etf_breakdown(etf_breakdown: dict) -> None:
    print(f"\n  ┌──────────────────────────────────────────────────────────────┐")
    print(f"  │  ETF CATEGORY — NIFTY SLEEVE                                 │")
    print(f"  ├──────────────┬────────┬──────────┬────────────┬───────────────┤")
    print(f"  │  Sleeve      │ Trades │  Win %   │  Avg PnL   │  Total PnL    │")
    print(f"  ├──────────────┼────────┼──────────┼────────────┼───────────────┤")
    print(f"  │  NIFTYBEES   │ {etf_breakdown.get('trades',0):<6} │ {etf_breakdown.get('win_rate',0.0):<8.1f} │ Rs{etf_breakdown.get('avg_pnl',0.0):<10,.0f} │ Rs{etf_breakdown.get('total_pnl',0.0):<13,.0f}│")
    print(f"  └──────────────┴────────┴──────────┴────────────┴───────────────┘")


def _print_regime_breakdown(regime_breakdown: dict) -> None:
    print(f"\n  ┌──────────────────────────────────────────────────────────────┐")
    print(f"  │  REGIME BREAKDOWN                                            │")
    print(f"  ├─────────────┬────────┬──────────┬────────────┬───────────────┤")
    print(f"  │  Regime     │ Trades │  Win %   │  Avg PnL   │  Total PnL    │")
    print(f"  ├─────────────┼────────┼──────────┼────────────┼───────────────┤")
    for regime in ["bull", "recovering", "neutral", "bear"]:
        d = regime_breakdown.get(regime, {"trades":0,"win_rate":0.0,"avg_pnl":0.0,"total_pnl":0.0})
        name = {"bull":"▲ Bull","recovering":"↗ Recovering","neutral":"◆ Neutral","bear":"▼ Bear"}[regime]
        print(f"  │  {name:<11} │ {d['trades']:<6} │ {d['win_rate']:<8.1f} │ Rs{d['avg_pnl']:<10,.0f} │ Rs{d['total_pnl']:<13,.0f}│")
    print(f"  └─────────────┴────────┴──────────┴────────────┴───────────────┘")


def _plot_nav(nav_series: pd.Series, title: str, filename: str) -> None:
    if nav_series is None or len(nav_series) == 0:
        return
    plt.figure(figsize=(12, 5))
    plt.plot(nav_series.index, nav_series.values)
    plt.title(title)
    plt.xlabel("Date")
    plt.ylabel("NAV")
    plt.tight_layout()
    try:
        plt.savefig(filename, dpi=150)
    finally:
        plt.close()


def print_journal() -> None:
    entries = _journal_read()
    if not entries:
        print("  Journal is empty.")
        return
    print("\n─── Journal ───────────────────────────────────────────────────")
    for e in entries[-5:]:
        if not isinstance(e, dict):
            print(f"  {e}")
            continue
        ts = e.get("timestamp", e.get("ts", "?"))
        typ = e.get("type", "run")
        if typ == "scan":
            print(f"  {ts} | scan | regime={e.get('regime','?')} | strong={e.get('strong',0)} moderate={e.get('moderate',0)} watch={e.get('watch',0)} sells={e.get('sells',0)}")
        elif typ == "walk_forward":
            print(f"  {ts} | walk_forward | folds={len(e.get('folds', []))} | avg_alpha={e.get('avg_alpha', 0):+.2f}% | avg_sharpe={e.get('avg_sharpe', 0):.2f}")
        else:
            print(f"  {ts} | {typ}")
    print("───────────────────────────────────────────────────────────────")


def _data_freshness(data_date, latest_market_date, nifty_df: pd.DataFrame) -> tuple[str, int]:
    """Return YYYY-MM-DD data date and number of later NSE sessions."""
    try:
        stock_ts = pd.Timestamp(data_date)
        market_ts = pd.Timestamp(latest_market_date)
        if stock_ts.tz is not None and market_ts.tz is None:
            market_ts = market_ts.tz_localize(stock_ts.tz)
        elif stock_ts.tz is None and market_ts.tz is not None:
            stock_ts = stock_ts.tz_localize(market_ts.tz)

        stale = 0
        if nifty_df is not None and not nifty_df.empty:
            nidx = nifty_df.index
            compare_ts = stock_ts
            compare_market_ts = market_ts
            if nidx.tz is not None and compare_ts.tz is None:
                compare_ts = compare_ts.tz_localize(nidx.tz)
            elif nidx.tz is None and compare_ts.tz is not None:
                compare_ts = compare_ts.tz_localize(None)
            if nidx.tz is not None and compare_market_ts.tz is None:
                compare_market_ts = compare_market_ts.tz_localize(nidx.tz)
            elif nidx.tz is None and compare_market_ts.tz is not None:
                compare_market_ts = compare_market_ts.tz_localize(None)
            stale = int(((nidx > compare_ts) & (nidx <= compare_market_ts)).sum())
        elif market_ts.normalize() > stock_ts.normalize():
            stale = int(np.busday_count(
                stock_ts.date().isoformat(), market_ts.date().isoformat()
            ))
        return stock_ts.date().isoformat(), max(0, stale)
    except Exception:
        try:
            return pd.Timestamp(data_date).date().isoformat(), 0
        except Exception:
            return str(data_date), 0


def scan(sectors=None, log: bool = True, regime: str = "neutral", intensity: float = 0.0) -> dict:
    """Live scan over the selected universe sectors."""
    sectors = sectors or list(UNIVERSE.keys())
    tickers = [t for s in sectors for t in UNIVERSE[s]]
    live_end = datetime.now()
    live_start = live_end - timedelta(days=420)

    nifty_df = download(NIFTY, live_start - timedelta(days=30), live_end)
    if not nifty_df.empty:
        latest_date = nifty_df.index[-1]
        live_dd = _nifty_drawdown_pct(nifty_df, as_of=latest_date)
    else:
        latest_date = live_end
        live_dd = 0.0

    earnings_tickers = [
        t for t in tickers
        if t != CFG.get("nifty_etf_ticker", "NIFTYBEES.NS")
    ]
    prefetch_earnings(earnings_tickers)

    ticker_dfs = {}
    for ticker in tickers:
        df = download(ticker, live_start, live_end)
        if df.empty or len(df) <= MIN_WARMUP_BARS:
            continue
        df = add_indicators(df)
        if not passes_liquidity(df, as_of=latest_date) and ticker != CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"):
            continue
        df = df[df.index <= latest_date]
        if len(df) > 0:
            ticker_dfs[ticker] = df

    ticker_dfs = add_rs_live(ticker_dfs, CFG["rs_window"])
    clean_dfs = {k: v for k, v in ticker_dfs.items() if not k.startswith("__")}
    rcfg = _regime_cfg(intensity if isinstance(intensity, (int, float)) else 0.0)
    if UNIVERSE_FILE_MODE:
        sector_ranking_now = []
        allowed_sectors = set(UNIVERSE.keys())
    else:
        sector_ranking_now = rank_sectors_by_rs(
            clean_dfs, latest_date, CFG["sector_rs_days"]
        ) if clean_dfs else []
        allowed_sectors = (
            set(sector_ranking_now[: rcfg.get("sector_rs_top_n", CFG["sector_rs_top_n"])])
            if sector_ranking_now else set(UNIVERSE.keys())
        )
    market_context = compute_market_context(clean_dfs, latest_date, nifty_df)

    strong = []
    moderate = []
    watch = []
    sells = []
    earnings_blocked = []

    for ticker, df in clean_dfs.items():
        if df.empty:
            continue
        if latest_date not in df.index:
            row = df.iloc[-1]
            row_date = df.index[-1]
        else:
            row = df.loc[latest_date]
            row_date = latest_date
        data_date, days_stale = _data_freshness(row_date, latest_date, nifty_df)

        score, reasons, entry_path = score_bar(row, regime=intensity, nifty_drawdown_pct=live_dd, ticker=ticker, market_context=market_context)
        if score <= 0:
            # overbought sell idea
            rsi_v = float(row["RSI"]) if "RSI" in row and not pd.isna(row["RSI"]) else 50.0
            if rsi_v >= rcfg.get("rsi_exit", CFG["rsi_exit"]):
                sells.append({
                    "ticker": ticker,
                    "price": float(row["Close"]),
                    "rsi": rsi_v,
                    "reason": "rsi_overbought",
                    "data_date": data_date,
                    "days_stale": days_stale,
                })
            continue

        sector = SECTOR_OF.get(ticker, "OTHER")
        if UNIVERSE_FILE_MODE:
            eligible, elig_path = True, "universe_file"
        else:
            eligible, elig_path, _ = is_sector_eligible(
                ticker, sector, allowed_sectors, sector_ranking_now,
                row, regime=regime, rs_lookup=None, sampled_dates=None, date=latest_date,
                entry_path=entry_path,
            )
            if not eligible:
                continue

        if ticker != CFG.get("nifty_etf_ticker", "NIFTYBEES.NS") and in_earnings_blackout(
            ticker, check_date=pd.Timestamp(latest_date).date()
        ):
            earnings_blocked.append({
                "ticker": ticker,
                "data_date": data_date,
                "days_stale": days_stale,
                "score": int(score),
                "entry_path": entry_path,
            })
            continue

        rsi_v = float(row["RSI"]) if "RSI" in row and not pd.isna(row["RSI"]) else np.nan
        sec_rs = float(row["RS_SCORE"]) if "RS_SCORE" in row and not pd.isna(row["RS_SCORE"]) else np.nan
        mkt_rs = float(row["MKT_RS_SCORE"]) if "MKT_RS_SCORE" in row and not pd.isna(row["MKT_RS_SCORE"]) else np.nan

        # v5.8.4: check exceptional tier for bear_flush signals
        _exc_live = (
            entry_path == "bear_flush"
            and is_exceptional_tier(row, score, regime)
        )

        sig = {
            "ticker": ticker,
            "sector": sector,
            "score": int(score),
            "reasons": reasons,
            "entry_path": entry_path,
            "elig_path": elig_path,
            "rsi": rsi_v,
            "sec_rs": sec_rs,
            "mkt_rs": mkt_rs,
            "price": float(row["Close"]),
            "data_date": data_date,
            "days_stale": days_stale,
            "regime": regime,
            "intensity": round(float(intensity), 3),
            "exceptional_tier": _exc_live,             # v5.8.4
        }

        path_min = {
            "oversold_pullback": rcfg["entry_score_min"],
            "trend_resumption": CFG["tr_score_min"],
            "bear_flush": CFG["bmc_score_min"],
            "nifty_momentum": CFG["nifty_score_min"],
        }.get(entry_path, rcfg["entry_score_min"])

        if score >= max(path_min + 10, 65):
            strong.append(sig)
        elif score >= path_min:
            moderate.append(sig)
        elif score >= max(35, path_min - 10):
            watch.append(sig)

    results = {
        "strong": strong,
        "moderate": moderate,
        "watch": watch,
        "sells": sells,
        "earnings_blocked": earnings_blocked,
    }
    if log:
        _journal_append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "type": "scan",
            "regime": regime,
            "intensity": float(intensity),
            "strong": len(strong),
            "moderate": len(moderate),
            "watch": len(watch),
            "sells": len(sells),
            "earnings_blocked": len(earnings_blocked),
            "top_strong": [s["ticker"] for s in strong[:5]],
        })
    return results


def run_walk_forward(ticker_dfs: dict, nifty_df: Optional[pd.DataFrame] = None,
                     plot: bool = False, compound_wf: bool = True) -> list[dict]:
    """Three-fold walk-forward with per-fold JSON outputs."""
    clean_dfs = {k: v for k, v in ticker_dfs.items() if not k.startswith("__")}
    if not clean_dfs:
        return []

    all_dates = sorted(set().union(*[set(df.index) for df in clean_dfs.values()]))
    if not all_dates:
        return []

    end_date = pd.Timestamp(all_dates[-1])
    folds = []
    fold_summaries = []
    current_capital = float(CFG["capital"])
    original_capital = float(CFG["capital"])

    # Oldest to newest: fold1, fold2, fold3
    fold_ends = [
        end_date - pd.DateOffset(months=CFG["wf_test_months"] * (CFG["wf_folds"] - 1 - i))
        for i in range(CFG["wf_folds"])
    ]

    # FIX BUG 1: renamed outer loop variable from `idx` to `fold_num` to prevent
    # shadowing by `didx = df.index` inside the inner ticker loop.
    for fold_num, test_end in enumerate(fold_ends, start=1):
        test_start = test_end - pd.DateOffset(months=CFG["wf_test_months"])
        train_start = test_start - pd.DateOffset(years=CFG["wf_train_years"])

        fold_dfs = {}
        for ticker, df in clean_dfs.items():
            s = pd.Timestamp(train_start)
            e = pd.Timestamp(test_end)
            didx = df.index  # FIX BUG 1: was `idx`, renamed to `didx`
            if didx.tz is not None and s.tz is None:
                s = s.tz_localize(didx.tz)
            elif didx.tz is None and s.tz is not None:
                s = s.tz_convert(None)
            if didx.tz is not None and e.tz is None:
                e = e.tz_localize(didx.tz)
            elif didx.tz is None and e.tz is not None:
                e = e.tz_convert(None)
            sl = df[(didx >= s) & (didx <= e)].copy()
            if len(sl) >= 20:
                fold_dfs[ticker] = sl

        if not fold_dfs:
            continue

        fold_dates = sorted(set().union(*[set(df.index) for df in fold_dfs.values()]))
        sector_rank_lookup = build_sector_rank_series(fold_dfs, fold_dates, CFG["sector_rs_days"])
        nifty_slice = None
        if nifty_df is not None and not nifty_df.empty:
            nifty_idx = nifty_df.index
            _ts = pd.Timestamp(train_start)
            _te = pd.Timestamp(test_end)
            if nifty_idx.tz is not None and _ts.tz is None:
                _ts = _ts.tz_localize(nifty_idx.tz)
                _te = _te.tz_localize(nifty_idx.tz)
            elif nifty_idx.tz is None and _ts.tz is not None:
                _ts = _ts.tz_localize(None)
                _te = _te.tz_localize(None)
            nifty_slice = nifty_df[(nifty_idx >= _ts) & (nifty_idx <= _te)].copy()

        prev_cap = CFG["capital"]
        if compound_wf:
            CFG["capital"] = float(current_capital)
        try:
            res = run_backtest(
                fold_dfs,
                nifty_df=nifty_slice,
                sector_rank_lookup=sector_rank_lookup,
                start_date=test_start,
                end_date=test_end,
            )
        finally:
            CFG["capital"] = prev_cap

        years = max((pd.Timestamp(test_end) - pd.Timestamp(test_start)).days / 365.25, 0.1)
        port_ann = ((res["final_cap"] / current_capital) ** (1 / years) - 1) * 100
        nifty_bmk = nifty_return(test_start, test_end)
        alpha = port_ann - nifty_bmk["ann_ret"]

        fold_obj = {
            "version": "v6.0.0",
            "fold": fold_num,  # FIX BUG 1: was `idx`, now correctly an int
            "test_start": _json_safe(test_start),
            "test_end": _json_safe(test_end),
            "start_capital": round(float(current_capital), 2),
            "end_capital": round(float(res["final_cap"]), 2),
            "summary": {
                "trades": int(res.get("trades", 0)),
                "win_rate": float(res.get("win_rate", 0.0)),
                "total_pnl": float(res.get("total_pnl", 0.0)),
                "avg_win": float(res.get("avg_win", 0.0)),
                "avg_loss": float(res.get("avg_loss", 0.0)),
                "rr": float(res.get("rr", 0.0)),
                "sharpe": float(res.get("sharpe", 0.0)),
                "max_dd_pct": float(res.get("max_dd_pct", 0.0)),
                "final_cap": round(float(res.get("final_cap", 0.0)), 2),
                "avg_invested_pct": float(res.get("avg_invested_pct", 0.0)),
                "avg_nifty_sleeve_pct": float(res.get("avg_nifty_sleeve_pct", 0.0)),
                "path_counts": _json_safe(res.get("path_counts", {})),
                "etf_total_pnl": float(res.get("etf_total_pnl", 0.0)),
                "nifty_total_pnl": float(res.get("nifty_total_pnl", 0.0)),
                "nifty_trade_count": int(res.get("nifty_trade_count", 0)),
                "etf_category": _json_safe(res.get("etf_category", {})),
                "regime_pct": _json_safe(res.get("regime_pct", {})),
                "regime_trades": _json_safe(res.get("regime_trades", {})),
                "path_trades": _json_safe(res.get("path_trades", {})),
                "alpha": float(alpha),
                "nifty_ann": float(nifty_bmk["ann_ret"]),
                "start_capital": round(float(current_capital), 2),
            },
            "nifty": _json_safe(nifty_bmk),
            "trades": _json_safe(res.get("trades_list", [])),
        }
        fold_path = os.path.join(BASE_DIR, f"fold{fold_num}.json")  # FIX BUG 1: was `idx`
        with open(fold_path, "w", encoding="utf-8") as f:
            json.dump(fold_obj, f, indent=2, ensure_ascii=False)
        folds.append(fold_obj)

        fold_summaries.append({
            "fold": fold_num,
            "alpha": alpha,
            "sharpe": float(res.get("sharpe", 0.0)),
            "max_dd": float(res.get("max_dd_pct", 0.0)),
        })

        print(f"  Fold {fold_num}/{CFG['wf_folds']}: {pd.Timestamp(test_start).date()} → {pd.Timestamp(test_end).date()}  (capital: Rs{current_capital:,.0f})")
        print(f"    Trades: {res.get('trades',0)}  Win%: {res.get('win_rate',0.0):.1f}  Sharpe: {res.get('sharpe',0.0):.2f}  MaxDD: {res.get('max_dd_pct',0.0):.1f}%")
        print(f"    Paths → oversold: {res.get('path_counts',{}).get('oversold_pullback',0)}  trend_res: {res.get('path_counts',{}).get('trend_resumption',0)}  bear_flush: {res.get('path_counts',{}).get('bear_flush',0)}  nifty: {res.get('path_counts',{}).get('nifty_momentum',0)}  exceptional: {res.get('path_counts',{}).get('exceptional_tier',0)}")
        print(f"    Port Ann: {port_ann:.1f}%  Nifty Ann TR: {nifty_bmk['ann_ret']:.1f}%  Alpha: {alpha:+.1f}%  End NAV: Rs{res.get('final_cap',0.0):,.0f}")

        current_capital = float(res["final_cap"]) if compound_wf else float(CFG["capital"])

    if folds:
        avg_alpha = float(np.mean([f["summary"]["alpha"] for f in folds]))
        avg_sharpe = float(np.mean([f["summary"]["sharpe"] for f in folds]))
        avg_dd = float(np.mean([f["summary"]["max_dd_pct"] for f in folds]))
        print("\n" + "─"*78)
        print(f"  WALK-FORWARD AGGREGATE ({len(folds)} valid OOS folds)")
        print(f"  Avg Alpha: {avg_alpha:+.2f}%")
        print(f"  Avg Sharpe: {avg_sharpe:.2f}")
        print(f"  Avg Max DD: {avg_dd:.1f}%")
        print("─"*78)

        _journal_append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "type": "walk_forward",
            "folds": [{"fold": f["fold"], "alpha": f["summary"]["alpha"], "sharpe": f["summary"]["sharpe"], "max_dd": f["summary"]["max_dd_pct"]} for f in folds],
            "avg_alpha": avg_alpha,
            "avg_sharpe": avg_sharpe,
            "avg_max_dd": avg_dd,
        })
    CFG["capital"] = original_capital
    return folds


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

def run(
    sectors        = None,
    do_backtest    = True,
    do_walkforward = True,
    plot_charts    = False,
    log_signals    = True,
    compound_wf    = True,
):
    _clear_non_journal_json_files()
    sectors = sectors or list(UNIVERSE.keys())
    bar     = "═" * 70

    print(f"\n{bar}")
    print(f"  NIFTY ALPHA SWING SCANNER v6.0.0  [fully continuous regime engine]")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Capital Rs{CFG['capital']:,}")
    rr_cfg = CFG["profit_target_mult"] / CFG["atr_stop_mult"]
    print(f"  R:R: {rr_cfg:.1f}:1  Break-even WR: {1/(1+rr_cfg)*100:.0f}%  "
          f"Slippage: {CFG['slippage_per_leg']*100:.2f}%/leg  "
          f"Cost: {CFG['cost_per_leg']*100:.2f}%/leg")
    print(f"  Universe: {len(ALL_TICKERS)} tickers  {len(UNIVERSE)} sectors")
    if UNIVERSE_FILE_MODE:
        print("  Universe-file mode: sector eligibility OFF | stock and market RS remain ON")
    print(f"  Paths: [1] Choppy MeanRev  [2] Bull Trend  "
          f"[3] Bear Survivor / Capitulation  [4] NIFTYBEES Active")
    print(f"  Fixes: slippage 0.30% | dynamic fundamentals (90d TTL) | "
          f"earnings blackout ±5/2d | RS refresh every 3 bars")
    print(f"{bar}\n")

    print("  Detecting market regime...")
    _nifty_live     = download(NIFTY, datetime.now()-timedelta(days=200), datetime.now())
    _regime_live    = "neutral"
    _intensity_live = 0.0
    if not _nifty_live.empty:
        _int_s, _lbl_s = build_regime_series(_nifty_live)
        if len(_int_s) > 0:
            _intensity_live = float(_int_s.iloc[-1])
            _regime_live    = str(_lbl_s.iloc[-1])

    bar_filled    = int((_intensity_live + 1.0) / 2.0 * 40)
    intensity_bar = "█" * bar_filled + "░" * (40 - bar_filled)
    print(f"  Intensity: {_intensity_live:+.3f}  [{intensity_bar}]  {_regime_live.upper()}\n")

    nifty_dd = _nifty_drawdown_pct(_nifty_live)
    print(f"  Nifty drawdown from recent high: {nifty_dd:.1f}%  "
          f"(Bear Survivor / Capitulation activates if ≥{CFG['bmc_nifty_down_pct']:.0f}%)\n")

    # ── Capital allocation breakdown (v5.6.0 — continuous NIFTY/Liquid) ─
    print_capital_allocation(_regime_live, _intensity_live, float(CFG["capital"]))

    print(f"  Stock budget: Rs{CFG['capital']:,.0f}  (NIFTYBEES managed as passive sleeve — see allocation above)\n")

    print("  Scanning universe...\n")
    results = scan(sectors=sectors, log=log_signals,
                   regime=_regime_live, intensity=_intensity_live)
    rcfg = _regime_cfg(_intensity_live)

    print(f"{'─'*70}")
    print(f"  SIGNALS  [Regime: {_regime_live.upper()}  intensity={_intensity_live:+.3f}]")
    print(f"  Strong: {len(results['strong'])}   Moderate: {len(results['moderate'])}   "
          f"Watch: {len(results['watch'])}   Sells: {len(results['sells'])}   "
          f"Earnings-blocked: {len(results.get('earnings_blocked', []))}")
    path_ct = {
        p: sum(1 for s in results["strong"]+results["moderate"] if s.get("entry_path")==p)
        for p in ["oversold_pullback","trend_resumption","bear_flush","nifty_momentum"]
    }
    path_ct["exceptional_tier"] = sum(
        1 for s in results["strong"]+results["moderate"]
        if s.get("exceptional_tier", False)
    )
    print(f"  Path breakdown → oversold: {path_ct['oversold_pullback']}  "
          f"trend_res: {path_ct['trend_resumption']}  "
          f"bear_flush: {path_ct['bear_flush']}  "
          f"nifty: {path_ct['nifty_momentum']}  "
          f"exceptional: {path_ct.get('exceptional_tier', 0)}")
    print(f"{'─'*70}\n")

    if results["strong"]:
        print("  ═══ STRONG BUY ═══\n")
        for sig in results["strong"]: _print_sig(sig, "STRONG")

    if results["moderate"]:
        print("  ═══ MODERATE BUY ═══\n")
        for sig in results["moderate"]: _print_sig(sig, "MODERATE")

    if results["watch"]:
        print("  ═══ WATCH LIST ═══\n")
        for sig in results["watch"][:8]: _print_sig(sig, "WATCH")

    if results["sells"]:
        print("  ═══ SELL ALERTS ═══\n")
        for s in results["sells"]:
            print(f"  [SELL    ] {s['ticker']:<16}  {s.get('reason','signal'):<12}  "
                  f"RSI {s['rsi']:.1f}  Price Rs{s['price']:.2f}  "
                  f"Data {s.get('data_date','?')}  stale {s.get('days_stale',0)}")
        print()

    if results.get("earnings_blocked"):
        print("  ═══ EARNINGS BLACKOUT — ENTRIES BLOCKED ═══\n")
        for item in results["earnings_blocked"]:
            print(f"  [BLOCKED ] {item['ticker']:<16} Score {item['score']:>3}  "
                  f"Path {item['entry_path']:<18}  Data {item['data_date']}  "
                  f"stale {item['days_stale']}")
        print()

    ticker_dfs  = {}
    nifty_bt_df = pd.DataFrame()

    if do_backtest:
        tickers  = [t for s in sectors for t in UNIVERSE[s]]
        bt_end   = datetime.now()
        bt_start = bt_end - timedelta(days=365*(CFG["backtest_years"]+1)+60)

        print(f"\n{bar}")
        print(f"  {CFG['backtest_years']}-YEAR PORTFOLIO BACKTEST  (v5.7.0)")
        print(f"  Fully continuous regime engine | liquid fund accrual | 0.30% slippage | dynamic fundamentals")
        print(f"{bar}\n")
        print("  Downloading data...")

        nifty_bt_df = download(NIFTY, bt_start-timedelta(days=90), bt_end)

        for ticker in tickers:
            df = download(ticker, bt_start, bt_end)
            if not df.empty and len(df) > MIN_WARMUP_BARS:
                df = add_indicators(df)
                if passes_liquidity(df):
                    ticker_dfs[ticker] = df
                else:
                    print(f"  {ticker}: skipped (liquidity)")
            elif df.empty:
                print(f"  {ticker}: no data")
            else:
                print(f"  {ticker}: insufficient ({len(df)} bars)")

        ticker_dfs = add_all_rs_lookahead_free(ticker_dfs, CFG["rs_window"])

        clean_dfs    = {k: v for k, v in ticker_dfs.items() if not k.startswith("__")}
        bt_all_dates = sorted(set().union(*[set(df.index) for df in clean_dfs.values()]))
        sector_rank_lookup = build_sector_rank_series(
            clean_dfs, bt_all_dates, CFG["sector_rs_days"]
        )

        bt_actual_start = bt_end - timedelta(days=365*CFG["backtest_years"])
        bt_dfs = {}
        for ticker, df in clean_dfs.items():
            didx = df.index
            ts   = pd.Timestamp(bt_actual_start, tz="UTC") if didx.tz else pd.Timestamp(bt_actual_start)
            sl   = df[didx >= ts]
            if len(sl) >= 20: bt_dfs[ticker] = sl

        if "__rs_lookup__"     in ticker_dfs: bt_dfs["__rs_lookup__"]     = ticker_dfs["__rs_lookup__"]
        if "__sampled_dates__" in ticker_dfs: bt_dfs["__sampled_dates__"] = ticker_dfs["__sampled_dates__"]

        n_bt = len(bt_dfs) - sum(1 for k in bt_dfs if k.startswith("__"))
        print(f"  Running backtest on {n_bt} tickers...\n")
        res = run_backtest(bt_dfs, nifty_df=nifty_bt_df,
                           sector_rank_lookup=sector_rank_lookup)

        nifty    = nifty_return(bt_actual_start, bt_end)
        years    = CFG["backtest_years"]
        port_ann = ((res["final_cap"]/CFG["capital"])**(1/years)-1)*100
        alpha    = port_ann - nifty["ann_ret"]
        avg_inv  = res.get("avg_invested_pct", 0.0)
        avg_nifty_sleeve = res.get("avg_nifty_sleeve_pct", 0.0)
        blended  = blended_benchmark_ann(nifty["ann_ret"], avg_inv, avg_nifty_sleeve)
        alpha_b  = port_ann - blended

        rp = res.get("regime_pct", {})
        print(f"  Regime breakdown: " +
              "  ".join(f"{k}: {v:.0f}%" for k, v in rp.items() if v > 0))
        print(f"  Avg deployed: stocks {avg_inv*100:.1f}%  "
              f"NIFTYBEES {avg_nifty_sleeve*100:.1f}%  "
              f"liquid {max(0,(1-avg_inv-avg_nifty_sleeve))*100:.1f}%")

        rr_real = res.get("rr",0.0); rr_gap = rr_real - rr_cfg
        rr_note = (
            "✓ On-par"                              if abs(rr_gap) < 0.2 else
            f"△ Winners cut short ({rr_gap:+.2f})" if rr_gap < 0       else
            f"△ Outperforming R:R ({rr_gap:+.2f})"
        )

        print()
        print(f"  ┌──────────────────────────────────────────────────────────────┐")
        print(f"  │  Portfolio             │ Nifty TR    │ Blended bench          │")
        print(f"  ├──────────────────────────────────────────────────────────────┤")
        print(f"  │  Trades:    {res['trades']:<49}│")
        print(f"  │  Win %:     {res['win_rate']:<49.1f}│")
        print(f"  │  Avg Win:   Rs{res['avg_win']:<47,.0f}│")
        print(f"  │  Avg Loss:  Rs{res['avg_loss']:<47,.0f}│")
        print(f"  │  R:R real:  {rr_real:<12.2f}│ cfg: {rr_cfg:<6.2f} │ {rr_note:<22}│")
        print(f"  │  Sharpe:    {res['sharpe']:<12.2f}│ {nifty['sharpe']:<12.2f}│                        │")
        print(f"  │  Max DD:    {res['max_dd_pct']:<12.1f}│ {nifty['max_dd']:<12.1f}│                        │")
        print(f"  │  Ann Ret:   {port_ann:<12.1f}│ {nifty['ann_ret']:<12.1f}│ {blended:<22.1f}│")
        print(f"  │  Alpha (vs Nifty TR):   {alpha:>+8.2f}%                              │")
        print(f"  │  Alpha (vs Blended):    {alpha_b:>+8.2f}%  ← fair comparison       │")
        print(f"  │  Final NAV: Rs{res['final_cap']:,.0f}                                │")
        print(f"  └──────────────────────────────────────────────────────────────┘")

        etf_pnl = res.get("etf_total_pnl", 0.0)
        print(f"  Return decomp: Stock Rs{res.get('total_pnl',0):+,.0f}  "
              f"ETF/Index Rs{etf_pnl:+,.0f}  "
              f"Total Rs{res.get('total_pnl',0)+etf_pnl:+,.0f}")

        _print_path_breakdown(res.get("path_trades", {}))
        _print_etf_breakdown(res.get("etf_category", {}))
        _print_regime_breakdown(res.get("regime_trades", {}))
        print()

        if plot_charts and not res["nav_series"].empty:
            _plot_nav(res["nav_series"], f"Portfolio NAV — {years}yr", "portfolio_nav.png")

    if do_walkforward and do_backtest and ticker_dfs:
        run_walk_forward(ticker_dfs, nifty_df=nifty_bt_df, plot=plot_charts,
                         compound_wf=compound_wf)

    print_journal()

    # ── Near-miss leaderboard (v5.8.0 — written on every normal scan) ─
    # Uses the scan data already computed above. Calls only existing
    # functions — build_near_miss_leaderboard() reuses score_bar()
    # identically to the live scan.  Zero strategy-side effects.
    try:
        os.makedirs(_DIAG_DIR, exist_ok=True)
        nm_rows   = build_near_miss_leaderboard(top_n=20)
        nm_report = render_near_miss_report(nm_rows, _regime_live, _intensity_live)
        nm_path   = os.path.join(_DIAG_DIR, "near_misses.md")
        with open(nm_path, "w", encoding="utf-8") as _nm_f:
            _nm_f.write(nm_report)
        if nm_rows:
            print(f"\n  Near-miss leaderboard ({len(nm_rows)} tickers) → {nm_path}")
            print(f"  Top 3: " + "  |  ".join(
                f"{r['ticker']} {r['score']}/{r['threshold']} [{r['path'][:12]}]"
                for r in nm_rows[:3]
            ))
        else:
            print(f"\n  Near-miss leaderboard → {nm_path} (no near-misses found)")
    except Exception as _nm_err:
        print(f"\n  Near-miss leaderboard: skipped ({_nm_err})")




# ══════════════════════════════════════════════════════════════════════
# DIAGNOSTIC LAYER  v5.8.0  —  observability only, zero strategy changes
# ══════════════════════════════════════════════════════════════════════
# Usage:   python scanner.py diagnose MARICO.NS
#          python scanner.py diagnose MARICO.NS --no-near-misses
# Normal run unchanged: python scanner.py
#   (near_misses.md is also written automatically at end of normal run)
# ══════════════════════════════════════════════════════════════════════

import textwrap as _tw

_DIAG_DIR = os.path.join(BASE_DIR, "diagnostics")


def _diag_fmt_pass(label: str, actual, threshold=None, note: str = "") -> str:
    val_str = f"{actual}" if threshold is None else f"{actual}  (threshold: {threshold})"
    return f"  ✓  {label:<38}  {val_str}  {note}".rstrip()


def _diag_fmt_fail(label: str, actual, threshold=None, note: str = "") -> str:
    val_str = f"{actual}" if threshold is None else f"{actual}  (threshold: {threshold})"
    return f"  ✗  {label:<38}  {val_str}  {note}".rstrip()


def _diag_fmt_skip(label: str, reason: str = "") -> str:
    return f"  —  {label:<38}  {reason}"


def _v(val, decimals: int = 1) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return "n/a"
    if isinstance(val, float):
        return f"{val:.{decimals}f}"
    return str(val)


# ─────────────────────────────────────────────────────────────────────
# Core diagnostic engine: returns a rich dict, writes nothing
# ─────────────────────────────────────────────────────────────────────

def diagnose_ticker(ticker: str) -> dict:
    """
    Full diagnostic pass for one ticker.
    Returns a structured dict — all values are native Python types.
    Does NOT print anything.  Does NOT modify any strategy state.
    Reuses every existing scanner function directly.
    """
    result: dict = {
        "ticker":    ticker,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "error":     None,
    }

    # ── 1. Market regime (live) ────────────────────────────────────────
    live_end   = datetime.now()
    live_start = live_end - timedelta(days=200)
    nifty_df   = download(NIFTY, live_start, live_end)

    intensity_live = 0.0
    regime_live    = "neutral"
    nifty_dd       = 0.0
    market_context: dict = {}

    if not nifty_df.empty:
        int_s, lbl_s   = build_regime_series(nifty_df)
        intensity_live  = float(int_s.iloc[-1]) if len(int_s) > 0 else 0.0
        regime_live     = str(lbl_s.iloc[-1])   if len(lbl_s) > 0 else "neutral"
        latest_date     = nifty_df.index[-1]
        nifty_dd        = _nifty_drawdown_pct(nifty_df, as_of=latest_date)

    rcfg = _regime_cfg(intensity_live)
    result["regime"] = {
        "label":        regime_live,
        "intensity":    round(intensity_live, 4),
        "nifty_dd_pct": round(nifty_dd, 2),
        "params":       {k: v for k, v in rcfg.items() if not k.startswith("_")},
    }

    # ── 2. Download + prepare ticker data ─────────────────────────────
    data_start = live_end - timedelta(days=420)
    raw_df = download(ticker, data_start, live_end)

    if raw_df.empty or len(raw_df) <= MIN_WARMUP_BARS:
        result["error"] = f"Insufficient data ({len(raw_df)} bars, need >{MIN_WARMUP_BARS})"
        return result

    df = add_indicators(raw_df)
    latest_date = df.index[-1]
    row = df.iloc[-1]

    # ── 3. RS (live, single ticker in universe context) ────────────────
    all_tickers_dfs: dict = {}
    scan_start = live_end - timedelta(days=420)
    for t in ALL_TICKERS:
        tmp = download(t, scan_start, live_end)
        if not tmp.empty and len(tmp) > MIN_WARMUP_BARS:
            all_tickers_dfs[t] = add_indicators(tmp)

    all_tickers_dfs = add_rs_live(all_tickers_dfs, CFG["rs_window"])
    market_context  = compute_market_context(
        {k: v for k, v in all_tickers_dfs.items() if not k.startswith("__")},
        latest_date, nifty_df
    )

    if ticker in all_tickers_dfs:
        df  = all_tickers_dfs[ticker]
        row = df.iloc[-1]

    sector           = SECTOR_OF.get(ticker, "UNKNOWN")
    sector_ranking   = rank_sectors_by_rs(
        {k: v for k, v in all_tickers_dfs.items() if not k.startswith("__")},
        latest_date, CFG["sector_rs_days"]
    )
    allowed_sectors  = set(sector_ranking[:rcfg.get("sector_rs_top_n", 4)])
    sector_rank_pos  = sector_ranking.index(sector) + 1 if sector in sector_ranking else None
    rs_score         = float(row.get("RS_SCORE",     np.nan))
    mkt_rs           = float(row.get("MKT_RS_SCORE", np.nan))
    mkt_rs_old       = float(row.get("MKT_RS_SCORE_OLD",  np.nan))
    rs_trend_20      = float(row.get("RS_TREND_20",   np.nan))
    rsi_v            = float(row.get("RSI",           np.nan))
    atr_pct          = float(row.get("ATR_PCT",       np.nan))
    atr_v            = float(row.get("ATR",           np.nan))
    vol_ratio        = float(row.get("VOL_RATIO",     np.nan))
    pvd_signal       = str(row.get("PVD_SIGNAL",     "unknown"))
    pvd_str          = float(row.get("PVD_STRENGTH",  np.nan))
    reversal         = float(row.get("REVERSAL",      np.nan))
    disc_52w         = float(row.get("DISC_52W_PCT",  np.nan))
    low_52w          = float(row.get("LOW_52W_PCT",   np.nan))
    ret_3d           = float(row.get("RET_3D",        np.nan))
    close_v          = float(row.get("Close",         np.nan))
    above_ema50      = float(row.get("ABOVE_EMA50",   np.nan))
    pullback         = float(row.get("PULLBACK_PCT",  np.nan))
    close_pos        = float(row.get("CLOSE_POSITION",np.nan))
    down_slope       = float(row.get("DOWN_VOL_SLOPE",np.nan))

    # ── 4. Pre-trade filters ───────────────────────────────────────────
    liquidity_ok   = passes_liquidity(df, as_of=latest_date)
    fundamental_ok = passes_fundamental_gate(ticker)
    earnings_ok    = not in_earnings_blackout(ticker)

    result["ticker_info"] = {
        "sector":           sector,
        "price":            round(close_v, 2) if not np.isnan(close_v) else None,
        "sector_rank":      sector_rank_pos,
        "total_sectors":    len(sector_ranking),
        "sector_in_top_n":  sector in allowed_sectors,
        "allowed_sectors":  sorted(allowed_sectors),
        "sector_ranking":   sector_ranking[:8],
        "rs_score":         round(rs_score, 1)  if not np.isnan(rs_score)  else None,
        "mkt_rs":           round(mkt_rs, 1)    if not np.isnan(mkt_rs)    else None,
        "mkt_rs_old":       round(mkt_rs_old,1) if not np.isnan(mkt_rs_old) else None,
        "rs_trend_20":      round(rs_trend_20,1)if not np.isnan(rs_trend_20) else None,
        "rsi":              round(rsi_v, 1)     if not np.isnan(rsi_v)     else None,
        "atr_pct":          round(atr_pct, 2)   if not np.isnan(atr_pct)   else None,
        "atr_abs":          round(atr_v, 2)     if not np.isnan(atr_v)     else None,
        "vol_ratio":        round(vol_ratio, 2) if not np.isnan(vol_ratio) else None,
        "pvd_signal":       pvd_signal,
        "pvd_strength":     round(pvd_str, 3)   if not np.isnan(pvd_str)   else None,
        "reversal":         int(reversal)        if not np.isnan(reversal)  else None,
        "disc_52w_pct":     round(disc_52w, 1)  if not np.isnan(disc_52w)  else None,
        "above_ema50":      bool(above_ema50 >= 1) if not np.isnan(above_ema50) else None,
        "pullback_pct":     round(pullback, 1)  if not np.isnan(pullback)  else None,
        "ret_3d":           round(ret_3d, 2)    if not np.isnan(ret_3d)    else None,
    }
    result["pre_filters"] = {
        "liquidity":    liquidity_ok,
        "fundamental":  fundamental_ok,
        "earnings_ok":  earnings_ok,
    }

    # ── 5. Path-by-path evaluation ─────────────────────────────────────
    paths: dict = {}

    # Helper: score each path separately with full reasons
    s1, r1 = score_oversold_pullback(
        row, regime=intensity_live,
        nifty_drawdown_pct=nifty_dd,
        market_context=market_context,
    )
    paths["oversold_pullback"] = {
        "score":       s1,
        "reasons":     r1,
        "threshold":   rcfg["entry_score_min"],
        "eligible":    True,   # all regimes
        "passes":      s1 >= rcfg["entry_score_min"],
        "gate_checks": _oversold_gate_checks(row, rcfg, nifty_dd, market_context),
    }

    label = _intensity_label(intensity_live)
    tr_eligible = label in PATH_REGIME_MAP["trend_resumption"]
    if tr_eligible:
        s2, r2 = score_trend_resumption(row, regime=intensity_live)
    else:
        s2, r2 = 0, []
    paths["trend_resumption"] = {
        "score":       s2,
        "reasons":     r2,
        "threshold":   CFG["tr_score_min"],
        "eligible":    tr_eligible,
        "passes":      tr_eligible and s2 >= CFG["tr_score_min"],
        "gate_checks": _tr_gate_checks(row) if tr_eligible else [],
        "ineligible_reason": None if tr_eligible else f"requires bull/recovering; current: {label}",
    }

    bf_eligible = label in PATH_REGIME_MAP["bear_flush"]
    if bf_eligible:
        s3, r3 = score_bear_flush(row, nifty_dd, market_context=market_context)
    else:
        s3, r3 = 0, []
    paths["bear_flush"] = {
        "score":       s3,
        "reasons":     r3,
        "threshold":   CFG["bmc_score_min"],
        "eligible":    bf_eligible,
        "passes":      bf_eligible and s3 >= CFG["bmc_score_min"],
        "gate_checks": _bf_gate_checks(row, nifty_dd, market_context) if bf_eligible else [],
        "ineligible_reason": None if bf_eligible else f"requires bear regime; current: {label}",
    }

    nifty_is_etf = (ticker == CFG.get("nifty_etf_ticker", "NIFTYBEES.NS"))
    if nifty_is_etf:
        s4, r4 = score_nifty_momentum(row, regime=label,
                                       nifty_drawdown_pct=nifty_dd,
                                       market_context=market_context)
    else:
        s4, r4 = 0, []
    paths["nifty_momentum"] = {
        "score":           s4,
        "reasons":         r4,
        "threshold":       CFG["nifty_score_min"],
        "eligible":        nifty_is_etf,
        "passes":          nifty_is_etf and s4 >= CFG["nifty_score_min"],
        "gate_checks":     [],
        "ineligible_reason": None if nifty_is_etf else "only for NIFTYBEES.NS ticker",
    }

    result["paths"] = paths

    # ── 6. Best path from score_bar (the actual live decision) ────────
    best_score, best_reasons, best_path = score_bar(
        row, regime=intensity_live,
        nifty_drawdown_pct=nifty_dd,
        ticker=ticker,
        market_context=market_context,
    )

    # ── 7. Sector eligibility check on the best path ──────────────────
    sector_eligible, sector_path, sector_premium = is_sector_eligible(
        ticker, sector, allowed_sectors, sector_ranking, row,
        regime=regime_live, rs_lookup=None, sampled_dates=None,
        date=latest_date, entry_path=best_path,
    )

    result["best_path"] = {
        "path":              best_path,
        "score":             best_score,
        "reasons":           best_reasons,
        "sector_eligible":   sector_eligible,
        "sector_reason":     sector_path,
        "sector_premium":    sector_premium,
    }

    # ── 8. Closest path (highest score, even if it didn't pass) ───────
    all_scored = [
        ("oversold_pullback", s1, rcfg["entry_score_min"]),
        ("trend_resumption",  s2, CFG["tr_score_min"]),
        ("bear_flush",        s3, CFG["bmc_score_min"]),
    ]
    best_near  = max(all_scored, key=lambda x: x[1])
    result["closest_path"] = {
        "path":      best_near[0],
        "score":     best_near[1],
        "threshold": best_near[2],
        "distance":  best_near[2] - best_near[1],
    }

    # ── 9. Position sizing (hypothetical) ─────────────────────────────
    sizing = None
    if not np.isnan(close_v) and not np.isnan(atr_v) and atr_v > 0:
        dummy_kelly = _new_kelly_state()
        path_max_pos = {
            "bear_flush":   CFG.get("bmc_max_position", 10_000),
        }.get(best_path, rcfg.get("max_position", CFG["max_position"]))
        path_stop = {
            "bear_flush": CFG.get("bmc_atr_stop_mult", 1.3),
        }.get(best_path, rcfg.get("atr_stop_mult", CFG["atr_stop_mult"]))
        path_tgt = {
            "bear_flush": CFG.get("bmc_profit_target_mult", 2.0),
        }.get(best_path, rcfg.get("profit_target_mult", CFG["profit_target_mult"]))
        s = size_position(
            close_v, atr_v, float(CFG["capital"]),
            dummy_kelly,
            max_pos_override=path_max_pos,
            kelly_scale=float(np.clip(1.0 + intensity_live * CFG.get("intensity_kelly_scale", 0.35), 0.5, 1.5)),
            stop_mult_override=path_stop,
            target_mult_override=path_tgt,
        )
        if s:
            sizing = {k: round(v, 2) if isinstance(v, float) else v for k, v in s.items()}
    result["sizing"] = sizing

    # ── 10. Final verdict ─────────────────────────────────────────────
    path_min = {
        "oversold_pullback": rcfg["entry_score_min"],
        "trend_resumption":  CFG["tr_score_min"],
        "bear_flush":        CFG["bmc_score_min"],
        "nifty_momentum":    CFG["nifty_score_min"],
    }.get(best_path, rcfg["entry_score_min"])

    if not liquidity_ok:
        verdict = "NO TRADE"
        verdict_reason = "Fails liquidity filter — insufficient average daily turnover."
    elif not fundamental_ok:
        verdict = "NO TRADE"
        verdict_reason = "Fails fundamental gate — declining revenue detected."
    elif not earnings_ok:
        verdict = "NO TRADE"
        verdict_reason = "Inside earnings blackout window."
    elif best_score <= 0 or best_path == "blocked":
        verdict = "NO TRADE"
        verdict_reason = "No path generated a positive score after all gates."
    elif not sector_eligible:
        verdict = "NO TRADE"
        verdict_reason = f"Sector {sector} not eligible: {sector_path}."
    elif best_score >= max(path_min + 10, 65):
        verdict = "STRONG BUY"
        verdict_reason = f"Score {best_score} ≥ strong threshold {max(path_min+10,65)} via {best_path}."
    elif best_score >= path_min:
        verdict = "BUY"
        verdict_reason = f"Score {best_score} ≥ path minimum {path_min} via {best_path}."
    elif best_score >= max(35, path_min - 10):
        verdict = "WATCHLIST"
        verdict_reason = f"Score {best_score} in watch band [{max(35,path_min-10)}, {path_min-1}]."
    else:
        cp = result["closest_path"]
        if cp["distance"] <= 12:
            verdict = "NEAR MISS"
            verdict_reason = (
                f"Best path {cp['path']}: score {cp['score']}, "
                f"threshold {cp['threshold']}, {cp['distance']} points short."
            )
        else:
            verdict = "NO TRADE"
            verdict_reason = (
                f"Best path {cp['path']}: score {cp['score']}, "
                f"{cp['distance']} points below threshold {cp['threshold']}."
            )

    result["verdict"] = verdict
    result["verdict_reason"] = verdict_reason
    return result


# ─────────────────────────────────────────────────────────────────────
# Gate check helpers — expose exactly which condition passed/failed
# ─────────────────────────────────────────────────────────────────────

def _oversold_gate_checks(row: "pd.Series", rcfg: dict,
                           nifty_dd: float, mc: dict) -> list[dict]:
    """Returns list of {name, pass, actual, threshold, note} for Path 1."""
    checks = []
    def ck(name, passed, actual, threshold=None, note=""):
        checks.append({"name": name, "pass": passed,
                        "actual": actual, "threshold": threshold, "note": note})

    rsi_v     = float(row.get("RSI",        np.nan))
    atr_pct   = float(row.get("ATR_PCT",    np.nan))
    vol_ratio = float(row.get("VOL_RATIO",  np.nan))
    mkt_rs    = float(row.get("MKT_RS_SCORE", np.nan))
    pvd_sig   = str(row.get("PVD_SIGNAL",  "unknown"))
    reversal  = float(row.get("REVERSAL",   np.nan))
    rs_score  = float(row.get("RS_SCORE",   np.nan))

    rsi_ok = rcfg["rsi_entry_min"] <= rsi_v <= rcfg["rsi_entry_max"]
    ck("RSI in oversold range", rsi_ok,
       f"{_v(rsi_v)}", f"{rcfg['rsi_entry_min']}–{rcfg['rsi_entry_max']}")

    atr_ok = rcfg["min_atr_pct"] <= atr_pct <= CFG["max_atr_pct"]
    ck("ATR% in range", atr_ok,
       f"{_v(atr_pct)}%", f"{rcfg['min_atr_pct']}–{CFG['max_atr_pct']}%")

    vol_ok = vol_ratio >= rcfg["min_volume_ratio"]
    ck("Volume ratio ≥ min", vol_ok,
       f"{_v(vol_ratio)}x", f"≥{_v(rcfg['min_volume_ratio'])}x")

    mkt_ok = mkt_rs >= rcfg["mkt_rs_min_entry"]
    ck("Market RS ≥ entry min", mkt_ok,
       f"{_v(mkt_rs)}", f"≥{_v(rcfg['mkt_rs_min_entry'], 0)}")

    if rcfg.get("require_reversal", False):
        rev_ok = reversal >= 1
        ck("Reversal bar required", rev_ok,
           "yes" if reversal >= 1 else "no", "required at this intensity")
    else:
        ck("Reversal bar", True, "not required", note="intensity above -0.35 threshold")

    pvd_ok = pvd_sig != "distribution"
    ck("PVD not distribution", pvd_ok,
       pvd_sig, "not distribution")

    # Weak market composite check — shared with the live scorer
    weak_market, wm_flags, bp, reclaim_ok = _is_weak_market(
        rcfg.get("_label", "neutral"), nifty_dd, mc
    )
    if weak_market:
        combo = (reclaim_ok and reversal == 1
                 and pvd_sig in ("exhaustion","confirmed_up")
                 and rs_score >= 60 and mkt_rs >= 55)
        ck("Weak-market combo gate",  combo,
           f"breadth={_v(bp*100,0)}%  RS={_v(rs_score,0)}  MktRS={_v(mkt_rs,0)}",
           "all required in bear/weak tape",
           note=f"triggers: {', '.join(wm_flags)}")
    return checks


def _tr_gate_checks(row: "pd.Series") -> list[dict]:
    """Returns gate checks for Path 2 — Trend Resumption."""
    checks = []
    def ck(name, passed, actual, threshold=None, note=""):
        checks.append({"name": name, "pass": passed,
                        "actual": actual, "threshold": threshold, "note": note})

    ema = float(row.get("ABOVE_EMA50",   np.nan))
    pb  = float(row.get("PULLBACK_PCT",  np.nan))
    rsi = float(row.get("RSI",           np.nan))
    vr  = float(row.get("VOL_RATIO",     np.nan))
    cp  = float(row.get("CLOSE_POSITION",np.nan))
    mr  = float(row.get("MKT_RS_SCORE",  np.nan))

    ck("Above 50 EMA (uptrend)",   not np.isnan(ema) and ema >= 1,
       "yes" if not np.isnan(ema) and ema >= 1 else "no", "required")
    ck("Pullback depth",
       not np.isnan(pb) and CFG["tr_pullback_min_pct"] <= pb <= CFG["tr_pullback_max_pct"],
       f"{_v(pb)}%", f"{CFG['tr_pullback_min_pct']}–{CFG['tr_pullback_max_pct']}%")
    ck("RSI in range",
       not np.isnan(rsi) and CFG["tr_rsi_min"] <= rsi <= CFG["tr_rsi_max"],
       _v(rsi), f"{CFG['tr_rsi_min']}–{CFG['tr_rsi_max']}")
    ck("Vol spike ≥ min",
       not np.isnan(vr) and vr >= CFG["tr_vol_spike_min"],
       f"{_v(vr)}x", f"≥{CFG['tr_vol_spike_min']}x")
    ck("Close in upper half of bar",
       not np.isnan(cp) and cp >= 0.5,
       f"{_v(cp, 2)}", "≥0.50")
    ck("Market RS ≥ min",
       not np.isnan(mr) and mr >= CFG["tr_mkt_rs_min"],
       _v(mr, 0), f"≥{CFG['tr_mkt_rs_min']}")
    return checks


def _bf_gate_checks(row: "pd.Series", nifty_dd: float, mc: dict) -> list[dict]:
    """Returns gate checks for Path 3 — Bear Flush (survivor/capitulation)."""
    checks = []
    def ck(name, passed, actual, threshold=None, note=""):
        checks.append({"name": name, "pass": passed,
                        "actual": actual, "threshold": threshold, "note": note})

    dd60 = float(mc.get("nifty_drawdown_60", nifty_dd) or nifty_dd)
    ck("Nifty drawdown ≥ 5%",
       nifty_dd >= 5.0 or dd60 >= 5.0,
       f"{_v(nifty_dd)}% (60d: {_v(dd60)}%)", "≥5%")

    mkt_rs  = float(row.get("MKT_RS_SCORE",np.nan))
    rs_s    = float(row.get("RS_SCORE",    np.nan))
    rs20    = float(row.get("RS_TREND_20", np.nan))
    disc    = float(row.get("DISC_52W_PCT",np.nan))
    rsi_v   = float(row.get("RSI",        np.nan))
    vr      = float(row.get("VOL_RATIO",   np.nan))
    atp     = float(row.get("ATR_PCT",     np.nan))
    ema50   = float(row.get("ABOVE_EMA50", np.nan))

    survivor = (
        (not np.isnan(mkt_rs) and mkt_rs >= 75 or not np.isnan(rs_s) and rs_s >= 60)
        and not np.isnan(rs20) and rs20 >= 4   # v5.9.0: synced to live scorer (was 6)
        and not np.isnan(disc) and disc <= 22
        and not np.isnan(rsi_v) and rsi_v >= 40
        and not np.isnan(vr) and vr >= 0.85
        and not np.isnan(atp) and atp <= 4.5
    )
    ck("Survivor: MktRS≥75 or RS≥60",
       not np.isnan(mkt_rs) and mkt_rs >= 75 or not np.isnan(rs_s) and rs_s >= 60,
       f"MktRS={_v(mkt_rs,0)}  RS={_v(rs_s,0)}", "MktRS≥75 or RS≥60")
    ck("Survivor: RS trend accelerating",
       (not np.isnan(rs20) and rs20 >= 4) or (not np.isnan(mkt_rs) and mkt_rs >= 78),       # v5.9.1: matches live scorer (mkt_rs>=78 OR rs20>=4)
       f"+{_v(rs20,1)}pt over 20d  (MktRS={_v(mkt_rs,0)})", "mkt_rs≥78 OR rs20≥4pt")
    ck("Survivor: disc from 52w high ≤22%",
       not np.isnan(disc) and disc <= 22,
       f"{_v(disc)}%", "≤22%")
    ck("Survivor: RSI ≥ 40",
       not np.isnan(rsi_v) and rsi_v >= 40,
       _v(rsi_v), "≥40")
    ck("Capitulation: Nifty DD ≥ threshold",
       nifty_dd >= CFG["bmc_nifty_down_pct"],
       f"{_v(nifty_dd)}%", f"≥{CFG['bmc_nifty_down_pct']}%",
       note="capitulation sub-mode only")
    return checks



# ─────────────────────────────────────────────────────────────────────
# Diagnostic helpers — root cause, counterfactual, distance
# All pure functions: read data from existing diagnostic dicts, return
# plain Python structures.  Zero interaction with strategy logic.
# ─────────────────────────────────────────────────────────────────────

def _build_root_cause(paths: dict, rcfg: dict) -> dict:
    """
    Inspect failed gates across all eligible paths.
    Returns:
        primary   — first gate to fail in the path closest to scoring
        secondary — second failed gate in same path (or None)
        minor     — remaining failed gates in same/other paths
        summary   — human-readable one-liner per blocker
    """
    # Find the path with the highest score (closest to threshold)
    eligible_paths = [(p, d) for p, d in paths.items() if d["eligible"]]
    if not eligible_paths:
        return {"primary": None, "secondary": None, "minor": [], "summary": "No eligible paths."}

    # Sort by score descending, then by how close they are to threshold
    eligible_paths.sort(key=lambda x: x[1]["score"], reverse=True)
    closest_name, closest_data = eligible_paths[0]

    failed_gates = [ck for ck in closest_data.get("gate_checks", []) if not ck["pass"]]
    # Also collect failed gates from other eligible paths (minor context)
    other_failed: list[dict] = []
    for pname, pdata in eligible_paths[1:]:
        for ck in pdata.get("gate_checks", []):
            if not ck["pass"]:
                other_failed.append({"path": pname, **ck})

    primary   = failed_gates[0] if len(failed_gates) > 0 else None
    secondary = failed_gates[1] if len(failed_gates) > 1 else None
    minor     = failed_gates[2:] + other_failed

    def _blocker_summary(ck: dict) -> str:
        if ck is None:
            return ""
        thr = f" (needs {ck['threshold']})" if ck.get("threshold") else ""
        return f"{ck['name']}: current {ck['actual']}{thr}"

    return {
        "closest_path": closest_name,
        "primary":   primary,
        "secondary": secondary,
        "minor":     minor,
        "primary_summary":   _blocker_summary(primary),
        "secondary_summary": _blocker_summary(secondary),
        "minor_summaries":   [_blocker_summary(m) for m in minor],
    }


def _build_counterfactual(paths: dict, rcfg: dict) -> list[dict]:
    """
    For each eligible path that did NOT pass, estimate the score if its
    failed hard-gates hypothetically passed.

    Strategy: call the path's scoring function is NOT repeated here
    (that would duplicate logic).  Instead we use the score already
    computed by the real scorer, add the contribution that the failed
    gates are blocking (estimated from the known gate penalty — i.e. the
    scorer returned 0 because a gate failed, so the counterfactual is:
    "if this gate passed, scoring would proceed; estimated score = the
    score the scorer DID produce on non-blocked paths, scaled up").

    Because the scorers hard-return 0 when a gate fails before scoring
    begins, the true counterfactual is: remove that gate block and
    re-score with remaining gates.  We cannot call the scorer without
    re-introducing logic, so instead we report:
      - Which gates failed
      - Whether fixing them would unlock scoring at all
      - The score that WAS produced (0 if blocked by hard gate)
      - An estimated score range using the path's max possible score
        and the fraction of scoring components that did fire

    This is purely observational — no strategy code is called or modified.
    """
    results = []
    path_max_scores = {
        "oversold_pullback": _SCORE_MAX_RAW,   # 105 raw → 100 normalised
        "trend_resumption":  104,
        "bear_flush":        100,
        "nifty_momentum":    110,
    }
    for pname, pdata in paths.items():
        if not pdata["eligible"]:
            continue
        score   = pdata["score"]
        thresh  = pdata["threshold"]
        if score >= thresh:
            continue   # already passed — no counterfactual needed

        failed_gates = [ck for ck in pdata.get("gate_checks", []) if not ck["pass"]]
        if not failed_gates:
            # Score is positive but below threshold — just needs more scoring components
            results.append({
                "path":         pname,
                "score":        score,
                "threshold":    thresh,
                "distance":     thresh - score,
                "hard_blocked": False,
                "failed_gates": [],
                "scenario":     (
                    f"No hard-gate failures — path reached scoring. "
                    f"Score {score} is {thresh - score} pts below threshold {thresh}. "
                    f"Improvement needed in scoring components (RSI zone, volume surge, RS, PVD)."
                ),
                "estimated_score_if_fixed": score,
            })
            continue

        # Hard gate(s) prevented scoring → score is 0
        # Estimate: if all hard gates passed, scoring components would fire.
        # We use the reasons list from the path (populated only if scoring ran)
        # combined with the path max to form a rough estimate.
        n_hard_failed = len(failed_gates)
        max_s = path_max_scores.get(pname, 100)

        # Conservative estimate: assume 40–60% of scoring components fire
        # when the stock is near-threshold on the failed gates
        conservative_est = int(max_s * 0.35)
        optimistic_est   = int(max_s * 0.65)

        gate_fix_strs = []
        for ck in failed_gates:
            gate_fix_strs.append(
                f"{ck['name']} → currently {ck['actual']}, "
                f"needs {ck.get('threshold','requirement met')}"
            )

        would_trigger = optimistic_est >= thresh

        results.append({
            "path":         pname,
            "score":        score,
            "threshold":    thresh,
            "distance":     thresh - score,
            "hard_blocked": True,
            "failed_gates": failed_gates,
            "gate_fix_strs": gate_fix_strs,
            "estimated_score_if_fixed": f"{conservative_est}–{optimistic_est}",
            "would_likely_trigger": would_trigger,
            "scenario": (
                f"{'Signal would likely trigger' if would_trigger else 'Signal unlikely even if gates pass'} "
                f"— estimated score {conservative_est}–{optimistic_est} vs threshold {thresh}."
            ),
        })

    return results


def _build_distance_table(paths: dict, rcfg: dict) -> list[dict]:
    """
    Per-gate distance table for the closest eligible path.
    Returns list of {gate, current, required, delta, pass}.
    Passed gates show delta=0; failed gates show the shortfall.
    """
    eligible_scored = [(p, d) for p, d in paths.items()
                       if d["eligible"] and d.get("gate_checks")]
    if not eligible_scored:
        return []

    # Use the path with the highest score
    eligible_scored.sort(key=lambda x: x[1]["score"], reverse=True)
    _, best = eligible_scored[0]

    rows = []
    for ck in best.get("gate_checks", []):
        rows.append({
            "gate":     ck["name"],
            "current":  ck["actual"],
            "required": ck.get("threshold", "—"),
            "pass":     ck["pass"],
            "note":     ck.get("note", ""),
        })
    return rows




def build_near_miss_leaderboard(top_n: int = 20) -> list[dict]:
    """
    Runs score_bar over all tickers with the live regime.
    Returns list of dicts sorted by score descending, for stocks that
    scored > 0 but are below the signal threshold (near-misses + watch).
    Zero strategy changes — reuses scan() data exactly.
    """
    live_end   = datetime.now()
    live_start = live_end - timedelta(days=420)

    nifty_df       = download(NIFTY, live_start - timedelta(days=30), live_end)
    intensity_live = 0.0
    regime_live    = "neutral"
    nifty_dd       = 0.0
    if not nifty_df.empty:
        int_s, lbl_s   = build_regime_series(nifty_df)
        intensity_live  = float(int_s.iloc[-1]) if len(int_s) > 0 else 0.0
        regime_live     = str(lbl_s.iloc[-1])   if len(lbl_s) > 0 else "neutral"
        latest_date     = nifty_df.index[-1]
        nifty_dd        = _nifty_drawdown_pct(nifty_df, as_of=latest_date)
    else:
        latest_date = live_end

    rcfg = _regime_cfg(intensity_live)
    tickers = [t for s in list(UNIVERSE.keys()) for t in UNIVERSE[s]]
    ticker_dfs: dict = {}
    for t in tickers:
        tmp = download(t, live_start, live_end)
        if not tmp.empty and len(tmp) > MIN_WARMUP_BARS:
            tmp2 = add_indicators(tmp)
            if passes_liquidity(tmp2, as_of=latest_date):
                ticker_dfs[t] = tmp2

    ticker_dfs   = add_rs_live(ticker_dfs, CFG["rs_window"])
    clean        = {k: v for k, v in ticker_dfs.items() if not k.startswith("__")}
    market_ctx   = compute_market_context(clean, latest_date, nifty_df)
    sector_rank  = rank_sectors_by_rs(clean, latest_date, CFG["sector_rs_days"])
    allowed      = set(sector_rank[:rcfg.get("sector_rs_top_n", 4)])

    rows = []
    for ticker, df in clean.items():
        row = df.iloc[-1]
        score, reasons, path = score_bar(
            row, regime=intensity_live,
            nifty_drawdown_pct=nifty_dd,
            ticker=ticker,
            market_context=market_ctx,
        )
        if score <= 0:
            continue
        path_min = {
            "oversold_pullback": rcfg["entry_score_min"],
            "trend_resumption":  CFG["tr_score_min"],
            "bear_flush":        CFG["bmc_score_min"],
            "nifty_momentum":    CFG["nifty_score_min"],
        }.get(path, rcfg["entry_score_min"])

        if score >= path_min:
            continue  # actual signal, not a near-miss

        sector = SECTOR_OF.get(ticker, "OTHER")
        sec_eligible, _, _ = is_sector_eligible(
            ticker, sector, allowed, sector_rank, row,
            regime=regime_live, rs_lookup=None, sampled_dates=None,
            date=latest_date, entry_path=path,
        )
        def _safe_round(val, decimals):
            try:
                v = float(val)
                return round(v, decimals) if not np.isnan(v) else np.nan
            except (TypeError, ValueError):
                return np.nan

        rows.append({
            "ticker":      ticker,
            "sector":      sector,
            "score":       score,
            "threshold":   path_min,
            "distance":    path_min - score,
            "path":        path,
            "reasons":     reasons,
            "rsi":         _safe_round(row.get("RSI", np.nan), 1),
            "mkt_rs":      _safe_round(row.get("MKT_RS_SCORE", np.nan), 1),
            "sec_rs":      _safe_round(row.get("RS_SCORE", np.nan), 1),
            "price":       _safe_round(row.get("Close", np.nan), 2),
            "sec_eligible": sec_eligible,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows[:top_n]


# ─────────────────────────────────────────────────────────────────────
# Report renderer — converts diagnostic dict → markdown text
# ─────────────────────────────────────────────────────────────────────

def render_diagnostic_report(d: dict) -> str:
    lines = []
    L = lines.append

    ticker = d["ticker"]
    ts     = d.get("timestamp", "")

    L(f"# Diagnostic Report: {ticker}")
    L(f"")
    L(f"**Generated:** {ts}  ")

    if d.get("error"):
        L(f"\n> **Error:** {d['error']}")
        return "\n".join(lines)

    reg   = d["regime"]
    info  = d["ticker_info"]
    pf    = d["pre_filters"]
    paths = d["paths"]
    best  = d["best_path"]
    near  = d["closest_path"]
    siz   = d["sizing"]
    # rcfg: regime params dict already computed by diagnose_ticker and stored in d["regime"]["params"]
    rcfg  = reg["params"]

    # ── Header ────────────────────────────────────────────────────────
    L(f"**Sector:** {info['sector']}  ")
    L(f"**Price:** ₹{info['price']}  ")
    L(f"**Regime:** {reg['label'].upper()}  |  **Intensity:** {reg['intensity']:+.4f}  |  **Nifty DD:** {reg['nifty_dd_pct']}%")
    L(f"")

    # ── Rankings ──────────────────────────────────────────────────────
    L(f"## Rankings")
    L(f"")
    L(f"| Metric | Value |")
    L(f"|--------|-------|")
    L(f"| Sector rank | #{info['sector_rank']} of {info['total_sectors']} sectors |")
    L(f"| Sector in top-N (top {reg['params'].get('sector_rs_top_n','?')}) | {'✓ YES' if info['sector_in_top_n'] else '✗ NO'} |")
    L(f"| Sector RS score | {_v(info['rs_score'])} (percentile vs peers) |")
    L(f"| Market RS score | {_v(info['mkt_rs'])} (percentile vs universe) |")
    L(f"| Market RS 20d trend | {'+' if (info['rs_trend_20'] or 0) >= 0 else ''}{_v(info['rs_trend_20'])}pt |")
    L(f"")

    # ── Sector Eligibility Panel ──────────────────────────────────────
    top_n         = rcfg.get("sector_rs_top_n", CFG["sector_rs_top_n"])
    sector_ranking_full = info.get("sector_ranking", [])          # already top-8 slice from diagnose_ticker
    allowed_set   = set(info.get("allowed_sectors", []))
    this_sector   = info["sector"]

    L(f"## Sector Eligibility Panel")
    L(f"")
    L(f"**Regime:** {reg['label'].upper()}  |  "
      f"**Intensity:** {reg['intensity']:+.4f}  |  "
      f"**Top-{top_n}** sectors eligible this scan")
    L(f"")
    L(f"| Rank | Sector | Eligible | Note |")
    L(f"|------|--------|----------|------|")

    for i, sec in enumerate(sector_ranking_full, start=1):
        is_allowed  = sec in allowed_set
        is_this     = sec == this_sector
        elig_icon   = "✅ YES" if is_allowed else "❌ NO"
        rank_badge  = f"**#{i}**" if is_this else f"#{i}"
        note_parts  = []
        if is_this:
            note_parts.append("← this ticker")
        if i <= top_n and is_allowed:
            note_parts.append(f"top-{top_n} cutoff")
        note = "  ".join(note_parts) if note_parts else ""
        L(f"| {rank_badge} | {'**' + sec + '**' if is_this else sec} | {elig_icon} | {note} |")

    # If this ticker's sector wasn't in the top-8 slice, add it explicitly
    if this_sector not in sector_ranking_full:
        rank_pos    = info.get("sector_rank")
        is_allowed  = this_sector in allowed_set
        elig_icon   = "✅ YES" if is_allowed else "❌ NO"
        rank_str    = f"**#{rank_pos}**" if rank_pos else "**?**"
        L(f"| {rank_str} | **{this_sector}** | {elig_icon} | ← this ticker (outside top-8 display) |")

    L(f"")
    L(f"*Eligibility = sector rank ≤ top-{top_n} OR within soft-band (is_sector_eligible logic). "
      f"Showing top-{min(len(sector_ranking_full), 8)} of {info['total_sectors']} sectors.*")
    L(f"")


    # ── Technical snapshot ────────────────────────────────────────────
    L(f"## Technical Snapshot")
    L(f"")
    L(f"| Indicator | Value |")
    L(f"|-----------|-------|")
    L(f"| RSI (14) | {_v(info['rsi'])} |")
    L(f"| ATR% | {_v(info['atr_pct'])}% |")
    L(f"| Volume ratio | {_v(info['vol_ratio'])}× 20d avg |")
    L(f"| PVD signal | {info['pvd_signal']} (strength {_v(info['pvd_strength'],3)}) |")
    L(f"| Reversal bar | {'yes' if info['reversal'] else 'no'} |")
    L(f"| 52w discount | {_v(info['disc_52w_pct'])}% from high |")
    L(f"| Above EMA50 | {'yes' if info['above_ema50'] else 'no'} |")
    L(f"| Pullback from recent high | {_v(info['pullback_pct'])}% |")
    L(f"| 3d return | {_v(info['ret_3d'])}% |")
    L(f"")

    # ── Pre-trade filters ─────────────────────────────────────────────
    L(f"## Pre-Trade Filters")
    L(f"")
    def pf_line(name, ok, note=""):
        icon = "✓" if ok else "✗"
        return f"- {icon} **{name}**{('  — '+note) if note else ''}"
    L(pf_line("Liquidity", pf["liquidity"],
              f"avg daily turnover ≥ ₹{CFG['min_avg_daily_value_cr']}Cr"))
    L(pf_line("Fundamentals", pf["fundamental"],
              "revenue growth check (90d TTL cache)"))
    L(pf_line("Earnings blackout", pf["earnings_ok"],
              f"±{CFG['earnings_blackout_before']}/{CFG['earnings_blackout_after']}d window"))
    L(f"")

    # ── Regime params at current intensity ───────────────────────────
    L(f"## Regime Parameters at Intensity {reg['intensity']:+.4f}")
    L(f"")
    p = reg["params"]
    L(f"| Parameter | Value |")
    L(f"|-----------|-------|")
    for key in ["entry_score_min","rsi_entry_max","rsi_entry_min","min_volume_ratio",
                "min_atr_pct","mkt_rs_min_entry","rs_min_strong","rs_min_moderate",
                "max_position","max_positions","max_per_sector","sector_rs_top_n",
                "max_hold_days","cash_floor","atr_stop_mult","atr_trail_mult",
                "profit_target_mult","require_reversal"]:
        if key in p:
            L(f"| {key} | {p[key]} |")
    L(f"")

    # ── Path evaluation ───────────────────────────────────────────────
    L(f"## Path Evaluation")
    L(f"")

    path_labels = {
        "oversold_pullback": "Path 1 — Oversold Pullback",
        "trend_resumption":  "Path 2 — Trend Resumption",
        "bear_flush":        "Path 3 — Bear Survivor / Capitulation",
        "nifty_momentum":    "Path 4 — NIFTYBEES Momentum",
    }
    for pkey, plabel in path_labels.items():
        pd_ = paths[pkey]
        eligible = pd_["eligible"]
        score    = pd_["score"]
        thresh   = pd_["threshold"]
        passes   = pd_["passes"]

        if not eligible:
            L(f"### {plabel}")
            L(f"")
            L(f"**Status:** INELIGIBLE — {pd_.get('ineligible_reason','')}")
            L(f"")
            continue

        status = "PASS" if passes else ("NEAR MISS" if score >= thresh - 12 else "FAIL")
        L(f"### {plabel}")
        L(f"")
        L(f"**Score:** {score}  |  **Threshold:** {thresh}  |  **Status:** {status}")
        if pd_["reasons"]:
            L(f"")
            L(f"**Score contributors:**")
            for r in pd_["reasons"]:
                L(f"- {r}")
        L(f"")

        checks = pd_.get("gate_checks", [])
        if checks:
            L(f"**Gate-by-gate filter analysis:**")
            L(f"")
            for ck in checks:
                icon = "✓" if ck["pass"] else "✗"
                thr  = f"  *(threshold: {ck['threshold']})*" if ck.get("threshold") else ""
                note = f"  `{ck['note']}`" if ck.get("note") else ""
                L(f"- {icon} **{ck['name']}**: {ck['actual']}{thr}{note}")
            L(f"")

    # ── Best path summary ─────────────────────────────────────────────
    L(f"## Live Decision")
    L(f"")
    L(f"**Winning path:** `{best['path']}`  ")
    L(f"**Score:** {best['score']}  ")
    L(f"**Sector eligible:** {'✓ YES' if best['sector_eligible'] else '✗ NO'}  ({best['sector_reason']})")
    if best["reasons"]:
        L(f"")
        L(f"**Drivers:**")
        for r in best["reasons"]:
            L(f"- {r}")
    L(f"")

    # ── Closest path / near miss ──────────────────────────────────────
    L(f"## Closest Path to Trigger")
    L(f"")
    L(f"| | |")
    L(f"|--|--|")
    L(f"| Path | `{near['path']}` |")
    L(f"| Score | {near['score']} |")
    L(f"| Threshold | {near['threshold']} |")
    L(f"| Distance | **{near['distance']} points short** |")
    L(f"")

    # ── Near-miss — what would need to change ─────────────────────────
    if d["verdict"] in ("NO TRADE", "NEAR MISS", "WATCHLIST"):
        L(f"## Near-Miss Analysis")
        L(f"")
        L(f"Conditions that would move this ticker closer to a trigger:")
        L(f"*(derived from existing failed gates only — no invented criteria)*")
        L(f"")
        pdata = paths.get(near["path"], {})
        for ck in pdata.get("gate_checks", []):
            if not ck["pass"]:
                L(f"- **{ck['name']}**: currently `{ck['actual']}`"
                  f"{', needs ' + str(ck['threshold']) if ck.get('threshold') else ''}")
        if near["distance"] > 0:
            L(f"- Overall score needs **+{near['distance']} points** to reach threshold {near['threshold']}")
        L(f"")

    # ── Root Cause Analysis ───────────────────────────────────────────
    L(f"## Root Cause Analysis")
    L(f"")
    rc = _build_root_cause(paths, rcfg)
    if rc["primary"] is None:
        if d["verdict"] in ("STRONG BUY", "BUY"):
            L(f"No blockers — all gates passed. Signal is active.")
        else:
            L(f"No eligible paths found for current regime conditions.")
    else:
        L(f"**Closest path evaluated:** `{rc['closest_path']}`")
        L(f"")
        if rc["primary_summary"]:
            L(f"**Primary Blocker:** {rc['primary_summary']}")
        if rc["secondary_summary"]:
            L(f"")
            L(f"**Secondary Blocker:** {rc['secondary_summary']}")
        if rc["minor_summaries"]:
            L(f"")
            L(f"**Minor Blockers:**")
            for ms in rc["minor_summaries"][:5]:   # cap at 5 to avoid noise
                L(f"- {ms}")
    L(f"")

    # ── Component Breakdown ───────────────────────────────────────────
    L(f"## Component Breakdown")
    L(f"")
    L(f"Raw indicator values used by the scanner — no synthetic metrics.")
    L(f"")
    L(f"| Component | Value | Context |")
    L(f"|-----------|-------|---------|")
    L(f"| RSI (14) | {_v(info['rsi'])} | entry band {rcfg.get('rsi_entry_min',22)}–{rcfg.get('rsi_entry_max',50)} |")
    L(f"| ATR% | {_v(info['atr_pct'])}% | range {rcfg.get('min_atr_pct',1.5):.1f}–{CFG['max_atr_pct']:.1f}% |")
    L(f"| Volume ratio | {_v(info['vol_ratio'])}× | min {rcfg.get('min_volume_ratio',0.85):.2f}× |")
    L(f"| Market RS | {_v(info['mkt_rs'])} | entry min {_v(rcfg.get('mkt_rs_min_entry'),0)} |")
    L(f"| Sector RS | {_v(info['rs_score'])} | strong ≥{rcfg.get('rs_min_strong',65)} / moderate ≥{rcfg.get('rs_min_moderate',50)} |")
    L(f"| RS trend (20d) | {'+' if (info.get('rs_trend_20') or 0) >= 0 else ''}{_v(info.get('rs_trend_20'))}pt | acceleration signal |")
    L(f"| PVD signal | {info['pvd_signal']} | exhaustion / confirmed_up are positive |")
    L(f"| PVD strength | {_v(info.get('pvd_strength'), 3)} | strong ≥{rcfg.get('pvd_strong_thresh',0.25):.2f} / mild ≥{rcfg.get('pvd_mild_thresh',0.08):.2f} |")
    L(f"| Reversal bar | {'yes' if info['reversal'] else 'no'} | required if intensity < −0.35 |")
    L(f"| Above EMA50 | {'yes' if info['above_ema50'] else 'no'} | required for trend resumption |")
    L(f"| Pullback depth | {_v(info['pullback_pct'])}% | TR path: {CFG['tr_pullback_min_pct']}–{CFG['tr_pullback_max_pct']}% |")
    L(f"| 52w discount | {_v(info['disc_52w_pct'])}% | value signal; BMC ≤22% |")
    L(f"| 3d return | {_v(info['ret_3d'])}% | constructive: 0.3–5.0% |")
    L(f"| Sector rank | #{info['sector_rank']} of {info['total_sectors']} | top-{rcfg.get('sector_rs_top_n',4)} allowed |")
    L(f"| Liquidity | {'✓ pass' if d['pre_filters']['liquidity'] else '✗ fail'} | avg daily ≥₹{CFG['min_avg_daily_value_cr']}Cr |")
    L(f"| Fundamentals | {'✓ pass' if d['pre_filters']['fundamental'] else '✗ fail'} | revenue trend check |")
    L(f"| Earnings window | {'✓ clear' if d['pre_filters']['earnings_ok'] else '✗ blackout'} | ±{CFG['earnings_blackout_before']}/{CFG['earnings_blackout_after']}d |")
    L(f"")

    # ── Why Triggered / Not Triggered ────────────────────────────────
    if d["verdict"] in ("STRONG BUY", "BUY"):
        L(f"## Why Triggered")
        L(f"")
        L(f"This stock generated a **{d['verdict']}** signal via path `{best['path']}`.")
        L(f"")
        if best["reasons"]:
            L(f"**Signal drivers (from live scorer):**")
            for r in best["reasons"]:
                L(f"- {r}")
            L(f"")
        # Summarise what is strong
        strengths = []
        if info.get("mkt_rs") and info["mkt_rs"] >= rcfg.get("rs_min_strong", 65):
            strengths.append(f"Market RS {info['mkt_rs']:.0f} — universe leader")
        if info.get("rs_score") and info["rs_score"] >= rcfg.get("rs_min_strong", 65):
            strengths.append(f"Sector RS {info['rs_score']:.0f} — sector leader")
        if info.get("rsi") and info["rsi"] <= 40:
            strengths.append(f"RSI {info['rsi']:.1f} — deep oversold reset")
        if info.get("pvd_signal") in ("exhaustion", "confirmed_up"):
            strengths.append(f"PVD {info['pvd_signal']} — volume confirms")
        if info.get("vol_ratio") and info["vol_ratio"] >= 1.4:
            strengths.append(f"Vol ratio {info['vol_ratio']:.1f}× — strong participation")
        if info.get("above_ema50"):
            strengths.append("Above 50 EMA — uptrend intact")
        if info.get("pullback_pct") and 10 <= info["pullback_pct"] <= 15:
            strengths.append(f"Pullback {info['pullback_pct']:.1f}% — ideal depth for trend resumption")
        if strengths:
            L(f"**Key strengths:**")
            for s in strengths:
                L(f"- {s}")
            L(f"")
        if best["sector_eligible"]:
            L(f"**Sector eligibility:** ✓ Passed — {best['sector_reason']}")
        L(f"")
    else:
        L(f"## Why Not Triggered")
        L(f"")
        L(f"This stock did **not** generate a signal. Exact conditions that prevented it:")
        L(f"")
        # Pre-filter failures first
        pf_fails = []
        if not d["pre_filters"]["liquidity"]:
            pf_fails.append(f"**Liquidity:** insufficient average daily turnover (< ₹{CFG['min_avg_daily_value_cr']}Cr)")
        if not d["pre_filters"]["fundamental"]:
            pf_fails.append("**Fundamentals:** revenue declining year-over-year")
        if not d["pre_filters"]["earnings_ok"]:
            pf_fails.append(f"**Earnings blackout:** within ±{CFG['earnings_blackout_before']}/{CFG['earnings_blackout_after']}d of earnings date")
        if pf_fails:
            L(f"**Pre-trade filter failures:**")
            for f_ in pf_fails:
                L(f"- {f_}")
            L(f"")

        # Regime/path eligibility
        inelig = [f"`{p}` — {d_['ineligible_reason']}"
                  for p, d_ in paths.items() if not d_["eligible"]]
        if inelig:
            L(f"**Paths unavailable in current regime ({reg['label'].upper()}, intensity {reg['intensity']:+.4f}):**")
            for i_ in inelig:
                L(f"- {i_}")
            L(f"")

        # Hard gate failures on eligible paths
        has_hard_fail = False
        for pname, pdata_ in paths.items():
            if not pdata_["eligible"]:
                continue
            failed = [ck for ck in pdata_.get("gate_checks", []) if not ck["pass"]]
            if failed:
                if not has_hard_fail:
                    L(f"**Hard gate failures (these prevented scoring from running):**")
                    has_hard_fail = True
                path_display = {
                    "oversold_pullback": "Path 1 — Oversold Pullback",
                    "trend_resumption":  "Path 2 — Trend Resumption",
                    "bear_flush":        "Path 3 — Bear Survivor",
                }.get(pname, pname)
                for ck in failed:
                    thr = f", needs {ck['threshold']}" if ck.get("threshold") else ""
                    L(f"- [{path_display}] **{ck['name']}**: current `{ck['actual']}`{thr}")
        if has_hard_fail:
            L(f"")

        # Score shortfall
        if near["score"] > 0 and near["distance"] > 0:
            L(f"**Score shortfall:** `{near['path']}` scored **{near['score']}**, needs **{near['threshold']}** "
              f"(gap: {near['distance']} points)")
            L(f"")

        if not pf_fails and not inelig and not has_hard_fail and near["score"] == 0:
            L(f"No scoring conditions met in the current regime. "
              f"All eligible paths returned score = 0 after gate evaluation.")
            L(f"")

    # ── Counterfactual Analysis ───────────────────────────────────────
    L(f"## Counterfactual Analysis")
    L(f"")
    L(f"*What would need to change before this stock becomes tradable?*")
    L(f"*(Estimated only — based on failed hard gates and path max scores)*")
    L(f"")
    cf_list = _build_counterfactual(paths, rcfg)
    if not cf_list:
        if d["verdict"] in ("STRONG BUY", "BUY"):
            L(f"Stock already triggered — no counterfactual analysis needed.")
        else:
            L(f"No eligible paths found for counterfactual analysis.")
    else:
        for cf in cf_list:
            path_display = {
                "oversold_pullback": "Path 1 — Oversold Pullback",
                "trend_resumption":  "Path 2 — Trend Resumption",
                "bear_flush":        "Path 3 — Bear Survivor / Capitulation",
                "nifty_momentum":    "Path 4 — NIFTYBEES Momentum",
            }.get(cf["path"], cf["path"])
            L(f"### {path_display}")
            L(f"")
            L(f"**Current score:** {cf['score']}  |  **Threshold:** {cf['threshold']}  "
              f"|  **Gap:** {cf['distance']} points")
            L(f"")
            if cf["hard_blocked"]:
                L(f"**Hard gates currently blocking entry:**")
                for gs in cf.get("gate_fix_strs", []):
                    L(f"- {gs}")
                L(f"")
                est = cf["estimated_score_if_fixed"]
                wt  = cf.get("would_likely_trigger", False)
                L(f"**If all failed gates resolved:**  "
                  f"Estimated score range: **{est}**  "
                  f"vs threshold {cf['threshold']}  →  "
                  f"{'✅ Signal would likely trigger' if wt else '⚠️ Signal still unlikely — scoring components also need improvement'}")
            else:
                L(f"No hard gate failures — path reached scoring but score {cf['score']} is "
                  f"{cf['distance']} points short of threshold {cf['threshold']}.")
                L(f"")
                L(f"**Improvement needed:** Stronger RSI reset, higher volume ratio, "
                  f"better RS scores, or more positive PVD signal to accumulate scoring points.")
            L(f"")

    # ── Distance to Signal ────────────────────────────────────────────
    L(f"## Distance to Signal")
    L(f"")
    L(f"*Gate-by-gate gap table for the closest eligible path.*")
    L(f"")
    dist_table = _build_distance_table(paths, rcfg)
    if dist_table:
        L(f"| Gate | Current | Required | Status |")
        L(f"|------|---------|----------|--------|")
        for row_ in dist_table:
            icon = "✓ PASS" if row_["pass"] else "✗ FAIL"
            note_str = f"  `{row_['note']}`" if row_.get("note") else ""
            L(f"| {row_['gate']} | {row_['current']} | {row_['required']} | {icon}{note_str} |")
        L(f"")
        fail_count = sum(1 for r in dist_table if not r["pass"])
        pass_count = sum(1 for r in dist_table if r["pass"])
        L(f"**Summary:** {pass_count} gates passing, {fail_count} gates failing for `{max(paths.items(), key=lambda x: x[1]['score'])[0]}`")
    else:
        L(f"*No gate data available for distance table.*")
    L(f"")

    # ── Position sizing ───────────────────────────────────────────────
    L(f"## Hypothetical Position Sizing")
    L(f"")
    if siz:
        L(f"*(if this stock were selected today)*")
        L(f"")
        L(f"| | |")
        L(f"|--|--|")
        L(f"| Shares | {siz['shares']} |")
        L(f"| Entry price | ₹{siz['entry']} |")
        L(f"| Stop loss | ₹{siz['stop']} (−₹{siz['stop_dist']}/share) |")
        L(f"| Target | ₹{siz['target']} |")
        L(f"| Total invested | ₹{siz['invested']:,.0f} |")
    else:
        L(f"*Position sizing unavailable (missing price/ATR data).*")
    L(f"")

    # ── Final verdict ─────────────────────────────────────────────────
    verdict_icon = {
        "STRONG BUY": "🟢", "BUY": "🟢", "WATCHLIST": "🟡",
        "NEAR MISS": "🟡", "NO TRADE": "🔴",
    }.get(d["verdict"], "⚪")
    L(f"## Final Verdict: {verdict_icon} {d['verdict']}")
    L(f"")
    L(f"{d['verdict_reason']}")
    L(f"")

    return "\n".join(lines)


def render_near_miss_report(rows: list[dict], regime: str, intensity: float) -> str:
    lines = []
    L = lines.append
    ts = datetime.now().isoformat(timespec="seconds")
    L(f"# Near-Miss Leaderboard")
    L(f"")
    L(f"**Generated:** {ts}  ")
    L(f"**Regime:** {regime.upper()}  |  **Intensity:** {intensity:+.4f}")
    L(f"")
    if not rows:
        L("*No near-misses found — either no tickers scored above 0 or all signals cleared threshold.*")
        return "\n".join(lines)

    L(f"Stocks that scored above 0 but below their path threshold — closest to triggering a signal.")
    L(f"")
    L(f"| Rank | Ticker | Sector | Score | Threshold | Distance | Path | RSI | MktRS | Sec Eligible |")
    L(f"|------|--------|--------|-------|-----------|----------|------|-----|-------|--------------|")
    for i, r in enumerate(rows, 1):
        sec_flag = "✓" if r["sec_eligible"] else "✗"
        L(f"| {i} | **{r['ticker']}** | {r['sector']} | {r['score']} | {r['threshold']} "
          f"| {r['distance']} | {r['path']} | {_v(r['rsi'])} | {_v(r['mkt_rs'],0)} | {sec_flag} |")
    L(f"")
    L(f"*Scores are from the live scan engine. No synthetic ranking applied.*")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# CLI entry point for diagnostic mode
# ─────────────────────────────────────────────────────────────────────

def run_diagnostic(ticker: str, write_near_misses: bool = True) -> None:
    """
    Full diagnostic pass for one ticker + optional near-miss leaderboard.
    Writes markdown to diagnostics/<TICKER>.md
    Normal scan() is NOT called — no journal entries, no backtest.
    """
    os.makedirs(_DIAG_DIR, exist_ok=True)

    bar = "═" * 70
    print(f"\n{bar}")
    print(f"  DIAGNOSTIC MODE  —  {ticker}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{bar}\n")
    print(f"  Running full diagnostic pass (downloads live data)...")

    d      = diagnose_ticker(ticker)
    report = render_diagnostic_report(d)

    # Write ticker report
    safe_name = ticker.replace(".", "_").replace("/", "_")
    out_path  = os.path.join(_DIAG_DIR, f"{safe_name}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  Report written → {out_path}")

    # Print summary to terminal
    print(f"\n  ── Summary ──")
    if d.get("error"):
        print(f"  Error: {d['error']}")
    else:
        info = d["ticker_info"]
        reg  = d["regime"]
        best = d["best_path"]
        near = d["closest_path"]
        print(f"  Ticker:      {ticker}  ({info['sector']})")
        print(f"  Price:       ₹{info['price']}")
        print(f"  Regime:      {reg['label'].upper()}  intensity={reg['intensity']:+.4f}")
        print(f"  MktRS:       {_v(info['mkt_rs'])}  SecRS: {_v(info['rs_score'])}")
        print(f"  RSI:         {_v(info['rsi'])}  ATR%: {_v(info['atr_pct'])}%  Vol: {_v(info['vol_ratio'])}×")
        print(f"  Best path:   {best['path']}  score={best['score']}")
        print(f"  Closest:     {near['path']}  score={near['score']}  threshold={near['threshold']}  distance={near['distance']}")
        print(f"  Verdict:     {d['verdict']}  —  {d['verdict_reason']}")

        if d["sizing"]:
            s = d["sizing"]
            print(f"  Sizing:      {s['shares']} shares  entry ₹{s['entry']}"
                  f"  stop ₹{s['stop']}  target ₹{s['target']}  invested ₹{s['invested']:,.0f}")

    # Near-miss leaderboard
    if write_near_misses:
        print(f"\n  Building near-miss leaderboard (scans full universe)...")
        nifty_df       = download(NIFTY, datetime.now()-timedelta(days=200), datetime.now())
        intensity_live = 0.0
        regime_live    = "neutral"
        if not nifty_df.empty:
            int_s, lbl_s   = build_regime_series(nifty_df)
            intensity_live  = float(int_s.iloc[-1]) if len(int_s) > 0 else 0.0
            regime_live     = str(lbl_s.iloc[-1])   if len(lbl_s) > 0 else "neutral"

        nm_rows   = build_near_miss_leaderboard(top_n=20)
        nm_report = render_near_miss_report(nm_rows, regime_live, intensity_live)
        nm_path   = os.path.join(_DIAG_DIR, "near_misses.md")
        with open(nm_path, "w", encoding="utf-8") as f:
            f.write(nm_report)
        print(f"  Near-miss leaderboard → {nm_path}")
        if nm_rows:
            print(f"\n  Top 5 near-misses:")
            for r in nm_rows[:5]:
                sec_flag = "✓" if r["sec_eligible"] else "✗"
                print(f"    {r['ticker']:<16} score={r['score']:<4}  "
                      f"threshold={r['threshold']:<4}  Δ={r['distance']:<4}  "
                      f"path={r['path']}  sec={sec_flag}")

    print(f"\n  Full report: {out_path}\n")


# ══════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys as _sys
    _args = _sys.argv[1:]

    # ── Diagnostic mode ───────────────────────────────────────────────
    # Usage: python scanner.py diagnose MARICO.NS
    #        python scanner.py diagnose MARICO.NS --no-near-misses
    if len(_args) >= 2 and _args[0].lower() in ("diagnose", "--diagnose"):
        _diag_ticker       = _args[1].upper()
        _write_near_misses = "--no-near-misses" not in _args
        run_diagnostic(_diag_ticker, write_near_misses=_write_near_misses)
        _sys.exit(0)

    # ── Universe path: hardcoded (default) OR variable (V200 auto-screen) ─
    #   python scanner.py                          -> hardcoded
    #   python scanner.py variable                 -> yfinance V200 auto-screen
    #   python scanner.py variable --screen-limit 300   (quick test, fewer names)
    #   python scanner.py variable --force-screen        (ignore 90d cache)
    #   python scanner.py variable --universe-file quality.txt  (manual list)
    def _argval(flag, default=None):
        return _args[_args.index(flag) + 1] if (flag in _args
                and _args.index(flag) + 1 < len(_args)) else default

    # TWO universe modes ONLY:
    #   python scanner.py                -> HARDCODED list (use for backtest/WF)
    #   python scanner.py universe.txt   -> the scraped V200 list (live scan)
    _ufile = _argval("--universe-file", None)
    for _a in _args:
        if _a.lstrip("-").lower().endswith(".txt"):
            _ufile = _a.lstrip("-"); break
    if _ufile:
        set_universe("variable", universe_file=_ufile)
    else:
        set_universe("hardcoded")

    # ── Normal run ─────────────────────────────────────────────────────
    run(
        sectors        = list(UNIVERSE.keys()),
        do_backtest    = False,
        do_walkforward = False,
        plot_charts    = False,
        log_signals    = True,
        compound_wf    = True,
    )