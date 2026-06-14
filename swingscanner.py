#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
swingscanner.py   —   v3.0.0   (universe.txt-only V20 scanner)
==============================================================

A clean, focused V20 swing scanner. It reads its universe from ONE place —
`universe.txt` (the live V200 list scraped from screener.in by the companion
`fetch_v200_universe.py`) — and runs the V20 strategy on those names.

Everything that used to build/classify the universe (NSE list download,
screener.in fundamentals, V40/V40_NEXT/V200 classification, quarterly refresh)
has been REMOVED. The names in universe.txt are already the screened list, so
swingscanner just: loads them -> pulls prices -> finds today's V20 setups ->
(optionally) backtests / walk-forward tests.

V20 strategy (faithful):
  * Daily candles, NO stop-loss.
  * A "zone" is a continuous green-candle rally whose low->high gain is
    >= 20% (and <= an upper cap to reject split/merge artifacts).
  * ENTRY: limit at the lower line (zone_low) when the day's LOW touches it.
  * EXIT : limit at the upper line (zone_high) when the day's HIGH touches it.
  * Each leg owns its own zone; one averaging leg allowed -> max 2 legs / 6%.
  * V200 rule: a zone is only valid if zone_low < 200-DMA at the zone's end
    (these are V200 names, so the gate is applied to all by default).

Run
---
    python swingscanner.py                 # live TODAY-scan on universe.txt
    python swingscanner.py --backtest      # also run the historical backtest
    python swingscanner.py --backtest --walkforward --start 2018-01-01
    python swingscanner.py --universe-file myV200.txt
    python swingscanner.py --offline-demo  # synthetic self-test (no network)

Outputs (in ./output): active_setups.csv, trades.csv (with --backtest),
backtest_summary.csv, walk_forward_summary.csv, charts/*.png.

Deps: pandas numpy yfinance matplotlib   (sqlite3 is stdlib)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import sqlite3
import sys
import warnings
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except Exception:
    plt = None

try:
    import yfinance as yf
except Exception:
    yf = None


# ===========================================================================
# CONFIG
# ===========================================================================
VERSION = "3.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "output")
CHART_DIR = os.path.join(OUTPUT_DIR, "charts")
DB_PATH = os.path.join(HERE, "swingscanner.db")
DEFAULT_UNIVERSE_FILE = os.path.join(HERE, "universe.txt")

NIFTY_TICKER = "^NSEI"
TRADING_DAYS = 252
RISK_FREE_RATE = 0.06

# V20 strategy
V20_MIN_GAIN_PCT = 20.0
V20_MAX_GAIN_PCT = 80.0     # reject merged-candle / unadjusted-split "zones"
DMA_PERIOD = 200
V20_REQUIRE_DMA_GATE = True  # these are V200 names: zone_low must be < 200-DMA

# Sizing / costs
POSITION_SIZE = 0.03         # 3% per leg
MAX_STOCK_EXPOSURE = 0.06    # 6% per stock (one averaging leg)
BROKERAGE_PCT = 0.0003
SLIPPAGE_PCT = 0.0010
STT_PCT = 0.001
INITIAL_CAPITAL = 1_000_000.0

# Tradeability gate (applied to universe.txt names)
# NOTE: this is the TECHNICAL price-history floor V20 needs to form a 200-DMA
# and detect a rally — NOT a company-age/quality filter. Company *age* is an
# INCORPORATION question (a 1995-incorporated firm that IPO'd in 2023 is
# established but has short price history), and that's handled upstream by the
# V200 screen that builds universe.txt, not here. Lower this only if you want
# very recently-listed names (the 200-DMA gets shaky below ~1 year of data).
MIN_HISTORY_YEARS = 1.5      # ~378 trading bars: enough for a meaningful 200-DMA
MIN_ADV_CR = 10.0            # avg daily traded value, Rs crore
MIN_PRICE = 30.0

# Walk-forward
WF_TRAIN_DAYS = 504
WF_TEST_DAYS = 252
WF_STEP_DAYS = 252


# ===========================================================================
# LOGGING
# ===========================================================================
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("swingscanner")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s",
                                         "%H:%M:%S"))
        logger.addHandler(h)
    return logger


LOG = setup_logging()


# ===========================================================================
# SMALL UTILITIES
# ===========================================================================
def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else v
    except Exception:
        return None


# ===========================================================================
# UNIVERSE: read universe.txt  (the ONLY input source)
# ===========================================================================
def parse_universe_file(path: str) -> List[Tuple[str, Optional[str]]]:
    """Read universe.txt: one `TICKER[.NS][,SECTOR]` per line. '#' comments and
    blank lines ignored. Returns [(yf_ticker, sector|None), ...]."""
    out: List[Tuple[str, Optional[str]]] = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p.strip() for p in line.replace("\t", ",").split(",")]
            sym = parts[0].upper()
            if not sym:
                continue
            yf_t = sym if sym.endswith(".NS") else f"{sym}.NS"
            sec = (parts[1].upper() if len(parts) > 1 and parts[1] else None)
            if yf_t not in seen:
                seen.add(yf_t)
                out.append((yf_t, sec))
    return out


# ===========================================================================
# DATABASE  (price cache + run records only)
# ===========================================================================
SCHEMA = {
    "prices": """CREATE TABLE IF NOT EXISTS prices(
        symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
        adj_close REAL, volume REAL, PRIMARY KEY(symbol,date))""",
    "signals": """CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, sector TEXT,
        zone_low REAL, zone_high REAL, zone_start TEXT, zone_end TEXT,
        gain_pct REAL, current_close REAL, entry_ready INTEGER,
        dma_200 REAL, rs_rank REAL, updated_at TEXT)""",
    "trades": """CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, symbol TEXT,
        entry_date TEXT, entry_price REAL, exit_date TEXT, exit_price REAL,
        shares REAL, pnl REAL, return_pct REAL, holding_days INTEGER,
        costs REAL)""",
    "backtests": """CREATE TABLE IF NOT EXISTS backtests(
        run_id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT,
        metrics_json TEXT, created_at TEXT)""",
    "walkforward": """CREATE TABLE IF NOT EXISTS walkforward(
        run_id TEXT, fold INTEGER, train_start TEXT, train_end TEXT,
        test_start TEXT, test_end TEXT, metrics_json TEXT)""",
}


# Data columns expected per table (PK/autoincrement cols omitted — they
# always exist). Used to auto-add columns to OLD databases so a schema change
# (e.g. signals.classification -> signals.sector) doesn't crash on insert.
EXPECTED_COLS = {
    "signals": [("symbol", "TEXT"), ("sector", "TEXT"), ("zone_low", "REAL"),
                ("zone_high", "REAL"), ("zone_start", "TEXT"), ("zone_end", "TEXT"),
                ("gain_pct", "REAL"), ("current_close", "REAL"),
                ("entry_ready", "INTEGER"), ("dma_200", "REAL"),
                ("rs_rank", "REAL"), ("updated_at", "TEXT")],
    "trades": [("run_id", "TEXT"), ("symbol", "TEXT"), ("entry_date", "TEXT"),
               ("entry_price", "REAL"), ("exit_date", "TEXT"), ("exit_price", "REAL"),
               ("shares", "REAL"), ("pnl", "REAL"), ("return_pct", "REAL"),
               ("holding_days", "INTEGER"), ("costs", "REAL")],
    "backtests": [("start_date", "TEXT"), ("end_date", "TEXT"),
                  ("metrics_json", "TEXT"), ("created_at", "TEXT")],
    "walkforward": [("run_id", "TEXT"), ("fold", "INTEGER"), ("train_start", "TEXT"),
                    ("train_end", "TEXT"), ("test_start", "TEXT"),
                    ("test_end", "TEXT"), ("metrics_json", "TEXT")],
    "prices": [("open", "REAL"), ("high", "REAL"), ("low", "REAL"),
               ("close", "REAL"), ("adj_close", "REAL"), ("volume", "REAL")],
}


class Database:
    def __init__(self, path: str = DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.cursor()
        for ddl in SCHEMA.values():
            cur.execute(ddl)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """Add any missing columns to pre-existing (older-schema) tables."""
        cur = self.conn.cursor()
        for table, cols in EXPECTED_COLS.items():
            try:
                existing = {r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()}
            except Exception:
                continue
            for name, typ in cols:
                if name not in existing:
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                    except Exception:
                        pass
        self.conn.commit()

    def execute(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        self.conn.commit()
        return cur

    def store_prices(self, symbol: str, df: pd.DataFrame):
        if df is None or df.empty:
            return
        rows = []
        for idx, r in df.iterrows():
            d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
            rows.append((symbol, d, _f(r.get("Open")), _f(r.get("High")),
                         _f(r.get("Low")), _f(r.get("Close")),
                         _f(r.get("Adj Close", r.get("Close"))), _f(r.get("Volume"))))
        self.conn.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", rows)
        self.conn.commit()

    def has_prices(self, symbol: str, min_rows: int = 200) -> bool:
        row = self.execute("SELECT COUNT(*) c FROM prices WHERE symbol=?", (symbol,)).fetchone()
        return bool(row) and row["c"] >= min_rows

    def latest_price_date(self, symbol: str) -> Optional[pd.Timestamp]:
        row = self.execute("SELECT MAX(date) d FROM prices WHERE symbol=?", (symbol,)).fetchone()
        if not row or not row["d"]:
            return None
        try:
            return pd.Timestamp(row["d"])
        except Exception:
            return None

    def earliest_price_date(self, symbol: str) -> Optional[pd.Timestamp]:
        row = self.execute("SELECT MIN(date) d FROM prices WHERE symbol=?", (symbol,)).fetchone()
        if not row or not row["d"]:
            return None
        try:
            return pd.Timestamp(row["d"])
        except Exception:
            return None

    def get_prices(self, symbol: str) -> pd.DataFrame:
        df = pd.read_sql_query(
            "SELECT date,open,high,low,close,adj_close,volume FROM prices "
            "WHERE symbol=? ORDER BY date", self.conn, params=(symbol,))
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df.columns = ["Open", "High", "Low", "Close", "Adj Close", "Volume"]
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[df["Close"] > 0]
        return df

    def clear_prices(self):
        self.execute("DELETE FROM prices")

    def insert_signal(self, s: Dict[str, Any]):
        self.execute(
            """INSERT INTO signals(symbol,sector,zone_low,zone_high,zone_start,
               zone_end,gain_pct,current_close,entry_ready,dma_200,rs_rank,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s["symbol"], s.get("sector"), s["zone_low"], s["zone_high"],
             s["zone_start"], s["zone_end"], s["gain_pct"], s["current_close"],
             int(s["entry_ready"]), s["dma_200"], s.get("rs_rank"), _now()))

    def insert_trades(self, run_id: str, trades: List["Trade"]):
        rows = [(run_id, t.symbol, t.entry_date, t.entry_price, t.exit_date,
                 t.exit_price, t.shares, t.pnl, t.return_pct, t.holding_days,
                 t.costs) for t in trades]
        self.conn.executemany(
            """INSERT INTO trades(run_id,symbol,entry_date,entry_price,exit_date,
               exit_price,shares,pnl,return_pct,holding_days,costs)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""", rows)
        self.conn.commit()

    def save_backtest(self, run_id, start, end, metrics):
        self.execute("INSERT OR REPLACE INTO backtests VALUES(?,?,?,?,?)",
                     (run_id, start, end, json.dumps(metrics, default=str), _now()))

    def save_walkforward(self, run_id, fold, ts, te, vs, ve, metrics):
        self.execute("INSERT INTO walkforward VALUES(?,?,?,?,?,?,?)",
                     (run_id, fold, ts, te, vs, ve, json.dumps(metrics, default=str)))

    def close(self):
        self.conn.close()


# ===========================================================================
# PRICE FETCHER  (yfinance, split-adjusted, cached, refresh-to-today)
# ===========================================================================
class PriceFetcher:
    def __init__(self, db: Database):
        self.db = db

    def fetch(self, symbol: str, start: str, end: str, allow_network: bool = True,
              refresh: bool = True, fresh_tol_days: int = 4) -> pd.DataFrame:
        if self.db.has_prices(symbol):
            use_cache = True
            last = self.db.latest_price_date(symbol)
            first = self.db.earliest_price_date(symbol)
            # re-download if the cache isn't recent enough...
            if refresh and (last is None or (pd.Timestamp(end) - last).days > fresh_tol_days):
                use_cache = False
            # ...OR if it doesn't cover the requested history start (the window
            # may have grown since the cache was first built). 90d tolerance.
            if first is None or (first - pd.Timestamp(start)).days > 90:
                use_cache = False
            if use_cache:
                cached = self.db.get_prices(symbol)
                if not cached.empty:
                    return cached
        if not allow_network or yf is None:
            return self.db.get_prices(symbol)
        try:
            df = yf.download(symbol, start=start, end=end, progress=False,
                             auto_adjust=True, threads=False)
            if df is None or df.empty:
                return self.db.get_prices(symbol)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            self.db.store_prices(symbol, df)
            return self.db.get_prices(symbol)
        except Exception as e:
            LOG.debug("price fetch failed %s: %s", symbol, e)
            return self.db.get_prices(symbol)


def tradeability_reason(df: pd.DataFrame) -> str:
    """Return 'OK' if tradeable, else a short reason code explaining the drop."""
    if df is None or df.empty:
        return "NO_DATA"
    need = int(MIN_HISTORY_YEARS * TRADING_DAYS * 0.9)
    if len(df) < need:
        return f"SHORT_HISTORY({len(df)}/{need} bars)"
    last = df.tail(120)
    price = float(last["Close"].iloc[-1])
    if not np.isfinite(price) or price < MIN_PRICE:
        return f"LOW_PRICE(Rs{price:.0f})"
    adv = float((last["Close"] * last["Volume"]).mean()) / 1e7
    if not np.isfinite(adv) or adv < MIN_ADV_CR:
        return f"LOW_LIQUIDITY(Rs{adv:.1f}cr/day)"
    return "OK"


def passes_tradeability(df: pd.DataFrame) -> bool:
    return tradeability_reason(df) == "OK"


# ===========================================================================
# V20 ENGINE
# ===========================================================================
@dataclass
class V20Zone:
    zone_low: float
    zone_high: float
    start_idx: int
    end_idx: int
    start_date: str
    end_date: str
    gain_pct: float


def find_v20_zones(prices: pd.DataFrame, min_gain: float = V20_MIN_GAIN_PCT,
                   max_gain: float = V20_MAX_GAIN_PCT) -> List[V20Zone]:
    """Continuous green-candle rallies with low->high gain in [min,max] %."""
    if prices is None or prices.empty or len(prices) < 2:
        return []
    prices = prices.dropna(subset=["Open", "High", "Low", "Close"])
    if len(prices) < 2:
        return []
    o, h, l, c = (prices["Open"].values, prices["High"].values,
                  prices["Low"].values, prices["Close"].values)
    dates = [d.strftime("%Y-%m-%d") for d in prices.index]
    n = len(c)
    zones: List[V20Zone] = []
    i = 0
    while i < n:
        if c[i] > o[i]:
            j = i
            while j + 1 < n and c[j + 1] > o[j + 1]:
                j += 1
            zlow = float(np.min(l[i:j + 1]))
            zhigh = float(np.max(h[i:j + 1]))
            if zlow > 0:
                gain = (zhigh - zlow) / zlow * 100.0
                if min_gain <= gain <= max_gain:
                    zones.append(V20Zone(zlow, zhigh, i, j, dates[i], dates[j],
                                         round(gain, 2)))
            i = j + 1
        else:
            i += 1
    return zones


def dma(prices: pd.DataFrame, period: int = DMA_PERIOD) -> pd.Series:
    return prices["Close"].rolling(period, min_periods=1).mean()


# ===========================================================================
# RELATIVE STRENGTH vs NIFTY
# ===========================================================================
def relative_strength(stock: pd.DataFrame, nifty: pd.DataFrame,
                      lookback: int = 252) -> Optional[float]:
    if stock is None or stock.empty or nifty is None or nifty.empty:
        return None
    df = pd.concat([stock["Close"], nifty["Close"]], axis=1, keys=["s", "n"]).dropna()
    if len(df) < 20:
        return None
    df = df.tail(lookback)
    rel = (df["s"] / df["s"].iloc[0]) / (df["n"] / df["n"].iloc[0])
    return float(rel.iloc[-1])


def rank_relative_strength(rs_map: Dict[str, Optional[float]]) -> Dict[str, float]:
    items = [(s, v) for s, v in rs_map.items() if v is not None]
    if not items:
        return {}
    items.sort(key=lambda x: x[1], reverse=True)
    total = len(items)
    return {s: round(100.0 * (total - i) / total, 2) for i, (s, _) in enumerate(items)}


# ===========================================================================
# PORTFOLIO  (lot-based: each leg owns its zone & exit)
# ===========================================================================
@dataclass
class Lot:
    symbol: str
    shares: float
    entry_price: float
    cost_basis: float
    open_date: str
    zone_low: float
    zone_high: float
    zone_id: str


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    shares: float
    pnl: float
    return_pct: float
    holding_days: int
    costs: float


class Portfolio:
    def __init__(self, capital: float = INITIAL_CAPITAL):
        self.initial_capital = capital
        self.cash = capital
        self.lots: Dict[str, List[Lot]] = {}
        self.closed_trades: List[Trade] = []
        self.equity_curve: List[Tuple[str, float]] = []

    def _sym_value(self, sym, price_map):
        return sum(lot.shares * price_map.get(sym, lot.entry_price)
                   for lot in self.lots.get(sym, []))

    def total_equity(self, price_map):
        return self.cash + sum(self._sym_value(s, price_map) for s in self.lots)

    def exposure(self, price_map):
        eq = self.total_equity(price_map)
        if eq <= 0:
            return 0.0
        return sum(self._sym_value(s, price_map) for s in self.lots) / eq

    def stock_exposure(self, sym, price_map):
        eq = self.total_equity(price_map)
        return 0.0 if (eq <= 0 or sym not in self.lots) else self._sym_value(sym, price_map) / eq

    def num_lots(self, sym):
        return len(self.lots.get(sym, []))

    def holds_zone(self, sym, zone_id):
        return any(lot.zone_id == zone_id for lot in self.lots.get(sym, []))

    def can_open_leg(self, sym, price_map):
        return self.num_lots(sym) < 2 and self.stock_exposure(sym, price_map) < MAX_STOCK_EXPOSURE

    def buy_leg(self, sym, zone_low, zone_high, zone_id, date, price_map,
                target_weight=POSITION_SIZE) -> bool:
        if zone_low <= 0 or not self.can_open_leg(sym, price_map):
            return False
        eq = self.total_equity(price_map)
        room = MAX_STOCK_EXPOSURE - self.stock_exposure(sym, price_map)
        weight = min(target_weight, room)
        if weight <= 0:
            return False
        budget = eq * weight
        fill = zone_low * (1 + SLIPPAGE_PCT)
        fees = budget * BROKERAGE_PCT
        shares = (budget - fees) / fill
        if shares <= 0:
            return False
        cost = shares * fill + fees
        if cost > self.cash:
            shares = max(0.0, (self.cash - fees) / fill)
            if shares <= 0:
                return False
            cost = shares * fill + fees
        self.cash -= cost
        self.lots.setdefault(sym, []).append(Lot(
            sym, shares, fill, shares * fill, date, zone_low, zone_high, zone_id))
        return True

    def sell_leg(self, sym, lot, price, date) -> Optional[Trade]:
        lots = self.lots.get(sym, [])
        if lot not in lots or price <= 0:
            return None
        lots.remove(lot)
        if not lots:
            self.lots.pop(sym, None)
        gross = lot.shares * price * (1 - SLIPPAGE_PCT)
        fees = gross * (BROKERAGE_PCT + STT_PCT)
        proceeds = gross - fees
        self.cash += proceeds
        pnl = proceeds - lot.cost_basis
        ret = (pnl / lot.cost_basis * 100.0) if lot.cost_basis > 0 else 0.0
        try:
            hold = (dt.datetime.strptime(date, "%Y-%m-%d") -
                    dt.datetime.strptime(lot.open_date, "%Y-%m-%d")).days
        except Exception:
            hold = 0
        t = Trade(sym, lot.open_date, round(lot.entry_price, 4), date,
                  round(price, 4), round(lot.shares, 4), round(pnl, 2),
                  round(ret, 2), hold, round(fees, 2))
        self.closed_trades.append(t)
        return t

    def open_lots(self):
        return [(s, lot) for s, lots in self.lots.items() for lot in lots]

    def mark(self, date, price_map):
        self.equity_curve.append((date, self.total_equity(price_map)))


# ===========================================================================
# BACKTEST ENGINE
# ===========================================================================
class Backtester:
    def __init__(self, price_data: Dict[str, pd.DataFrame], nifty: pd.DataFrame,
                 dma_gate: bool = V20_REQUIRE_DMA_GATE):
        self.price_data = price_data
        self.nifty = nifty
        self.dma_gate = dma_gate

    def _zones(self) -> Dict[str, List[V20Zone]]:
        zmap = {}
        for sym, df in self.price_data.items():
            zones = find_v20_zones(df)
            if self.dma_gate and zones:
                ds = dma(df).values
                zones = [z for z in zones
                         if z.end_idx < len(ds) and z.zone_low < ds[z.end_idx]]
            zmap[sym] = zones
        return zmap

    @staticmethod
    def _entry_zone(sym, zones, d, low_today, high_df, portfolio):
        best = None
        for z in zones:
            zend = pd.to_datetime(z.end_date)
            if zend >= d or portfolio.holds_zone(sym, z.end_date):
                continue
            seg = high_df[(high_df.index > zend) & (high_df.index <= d)]
            if len(seg) and float(seg.max()) >= z.zone_high:
                continue
            if low_today <= z.zone_low:
                if best is None or zend > pd.to_datetime(best.end_date):
                    best = z
        return best

    def run(self, start=None, end=None, capital=INITIAL_CAPITAL):
        portfolio = Portfolio(capital)
        zmap = self._zones()
        all_dates = sorted({d for df in self.price_data.values() for d in df.index})
        if start:
            sd = pd.to_datetime(start); all_dates = [d for d in all_dates if d >= sd]
        if end:
            ed = pd.to_datetime(end); all_dates = [d for d in all_dates if d <= ed]
        if not all_dates:
            return portfolio, pd.DataFrame()

        closes = {s: df["Close"] for s, df in self.price_data.items()}
        highs = {s: df["High"] for s, df in self.price_data.items()}
        lows = {s: df["Low"] for s, df in self.price_data.items()}

        for d in all_dates:
            dstr = d.strftime("%Y-%m-%d")
            price_map = {s: float(ser.loc[d]) for s, ser in closes.items() if d in ser.index}

            # exits: each leg at its own upper line (intraday high touch)
            for sym, lot in portfolio.open_lots():
                hs = highs.get(sym)
                if hs is not None and d in hs.index and float(hs.loc[d]) >= lot.zone_high:
                    portfolio.sell_leg(sym, lot, lot.zone_high, dstr)

            # entries: lower-line touch; bigger 20%+ move first
            candidates = []
            for sym, zones in zmap.items():
                ls = lows.get(sym)
                if ls is None or d not in ls.index or not portfolio.can_open_leg(sym, price_map):
                    continue
                z = self._entry_zone(sym, zones, d, float(ls.loc[d]), highs[sym], portfolio)
                if z is not None:
                    candidates.append((-z.gain_pct, sym, z))
            candidates.sort(key=lambda x: x[0])
            for _, sym, z in candidates:
                if portfolio.exposure(price_map) >= 0.98:
                    break
                portfolio.buy_leg(sym, z.zone_low, z.zone_high, z.end_date, dstr, price_map)

            portfolio.mark(dstr, price_map)

        eq = pd.DataFrame(portfolio.equity_curve, columns=["date", "equity"])
        if not eq.empty:
            eq["date"] = pd.to_datetime(eq["date"]); eq = eq.set_index("date")
        return portfolio, eq


# ===========================================================================
# METRICS  (always benchmarked to NIFTY50)
# ===========================================================================
def _alpha_beta(eq: pd.Series, nifty: pd.DataFrame):
    if nifty is None or nifty.empty:
        return None, None
    n = nifty["Close"].reindex(eq.index).ffill()
    df = pd.concat([eq.pct_change(), n.pct_change()], axis=1, keys=["p", "m"]).dropna()
    if len(df) < 10 or df["m"].var() == 0:
        return None, None
    beta, intercept = np.polyfit(df["m"].values, df["p"].values, 1)
    return float(intercept) * TRADING_DAYS * 100, float(beta)


def _nifty_benchmark(nifty: pd.DataFrame, eq_index) -> Dict[str, Any]:
    out = {"cagr": None, "total_return": None, "max_dd": None}
    if nifty is None or nifty.empty or eq_index is None or len(eq_index) < 2:
        return out
    n = nifty["Close"].reindex(eq_index).ffill().bfill().dropna()
    if len(n) < 2 or float(n.iloc[0]) <= 0:
        return out
    years = max((n.index[-1] - n.index[0]).days / 365.25, 1e-9)
    out["total_return"] = round(float(n.iloc[-1] / n.iloc[0] - 1) * 100, 2)
    out["cagr"] = round((float(n.iloc[-1] / n.iloc[0]) ** (1 / years) - 1) * 100, 2)
    out["max_dd"] = round(float((n / n.cummax() - 1).min()) * 100, 2)
    return out


def compute_metrics(equity: pd.DataFrame, trades: List[Trade], nifty: pd.DataFrame,
                    capital: float = INITIAL_CAPITAL) -> Dict[str, Any]:
    if equity is None or equity.empty:
        return {"error": "no equity curve"}
    eq = equity["equity"]
    rets = eq.pct_change().dropna()
    years = max(len(eq) / TRADING_DAYS, 1e-9)
    m: Dict[str, Any] = {}
    m["_start_capital"] = round(float(capital), 2)
    m["final_equity"] = round(float(eq.iloc[-1]), 2)
    m["total_return_pct"] = round(float(eq.iloc[-1] / eq.iloc[0] - 1) * 100, 2)
    m["CAGR_pct"] = round((float(eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1) * 100, 2)
    rf = RISK_FREE_RATE / TRADING_DAYS
    excess = rets - rf
    m["Sharpe"] = round(float((excess.mean() / rets.std()) * math.sqrt(TRADING_DAYS))
                        if rets.std() > 0 else 0.0, 3)
    dd_std = rets[rets < 0].std()
    m["Sortino"] = round(float((excess.mean() / dd_std) * math.sqrt(TRADING_DAYS))
                         if dd_std and dd_std > 0 else 0.0, 3)
    m["max_drawdown_pct"] = round(float((eq / eq.cummax() - 1).min()) * 100, 2)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    m["num_trades"] = len(trades)
    m["win_rate_pct"] = round(100 * len(wins) / len(trades), 2) if trades else 0.0
    gp, gl = sum(t.pnl for t in wins), abs(sum(t.pnl for t in losses))
    m["profit_factor"] = round(gp / gl, 3) if gl > 0 else (float("inf") if gp > 0 else 0.0)
    m["avg_win"] = round(np.mean([t.pnl for t in wins]), 2) if wins else 0.0
    m["avg_loss"] = round(np.mean([t.pnl for t in losses]), 2) if losses else 0.0
    m["avg_holding_days"] = round(np.mean([t.holding_days for t in trades]), 1) if trades else 0.0
    a, b = _alpha_beta(eq, nifty)
    m["alpha_annual_pct"] = round(a, 3) if a is not None else None
    m["beta"] = round(b, 3) if b is not None else None
    nb = _nifty_benchmark(nifty, eq.index)
    m["nifty_CAGR_pct"] = nb["cagr"]
    m["nifty_total_return_pct"] = nb["total_return"]
    m["nifty_max_drawdown_pct"] = nb["max_dd"]
    m["alpha_vs_nifty_cagr_pct"] = (round(m["CAGR_pct"] - nb["cagr"], 2)
                                    if nb["cagr"] is not None else None)
    return m


def print_benchmark_table(m: Dict[str, Any]):
    if not m or m.get("error"):
        return
    LOG.info("┌──────────── STRATEGY vs NIFTY50 ────────────┐")
    LOG.info("│ CAGR   strat %7s%%  NIFTY %7s%%  alpha %7s%% │",
             m.get("CAGR_pct"), m.get("nifty_CAGR_pct"), m.get("alpha_vs_nifty_cagr_pct"))
    LOG.info("│ MaxDD  strat %7s%%  NIFTY %7s%%             │",
             m.get("max_drawdown_pct"), m.get("nifty_max_drawdown_pct"))
    LOG.info("│ Sharpe %5s  Sortino %5s  Beta %5s  PF %5s │",
             m.get("Sharpe"), m.get("Sortino"), m.get("beta"), m.get("profit_factor"))
    LOG.info("│ Win%% %5s  Trades %4s  Final NAV Rs%-12s │",
             m.get("win_rate_pct"), m.get("num_trades"), f"{m.get('final_equity', 0):,.0f}")
    LOG.info("└─────────────────────────────────────────────┘")


# ===========================================================================
# WALK-FORWARD  (lookahead-free, compounding, alpha vs NIFTY)
# ===========================================================================
class WalkForward:
    def __init__(self, backtester: Backtester, nifty: pd.DataFrame):
        self.bt = backtester
        self.nifty = nifty

    def _slice(self, train_start, test_end):
        out = {}
        for sym, df in self.bt.price_data.items():
            sub = df.loc[(df.index >= train_start) & (df.index <= test_end)]
            if len(sub) >= 20:
                out[sym] = sub
        return out

    def _fold(self, train_start, test_start, test_end, capital):
        sliced = self._slice(train_start, test_end)
        bt = Backtester(sliced, self.nifty, dma_gate=self.bt.dma_gate)
        return bt.run(start=test_start.strftime("%Y-%m-%d"),
                      end=test_end.strftime("%Y-%m-%d"), capital=capital)

    def _row(self, rows, run_id, db, fold, ts0, te0, vs, ve, m):
        m["fold"] = fold
        db.save_walkforward(run_id, fold, ts0.strftime("%Y-%m-%d"), te0.strftime("%Y-%m-%d"),
                            vs.strftime("%Y-%m-%d"), ve.strftime("%Y-%m-%d"), m)
        rows.append({"fold": fold, "test_start": vs.strftime("%Y-%m-%d"),
                     "test_end": ve.strftime("%Y-%m-%d"),
                     "start_capital": m.get("_start_capital"),
                     "end_equity": m.get("final_equity"), "CAGR_pct": m.get("CAGR_pct"),
                     "nifty_CAGR_pct": m.get("nifty_CAGR_pct"),
                     "alpha_vs_nifty_pct": m.get("alpha_vs_nifty_cagr_pct"),
                     "Sharpe": m.get("Sharpe"), "max_drawdown_pct": m.get("max_drawdown_pct"),
                     "win_rate_pct": m.get("win_rate_pct"), "num_trades": m.get("num_trades")})

    def run(self, run_id, db, compound=True, capital=INITIAL_CAPITAL) -> pd.DataFrame:
        all_dates = sorted({d for df in self.bt.price_data.values() for d in df.index})
        rows = []
        if len(all_dates) < (WF_TRAIN_DAYS + WF_TEST_DAYS):
            LOG.warning("Not enough history for full walk-forward; single fold.")
            if not all_dates:
                return pd.DataFrame()
            mid = len(all_dates) // 2
            p, eq = self._fold(all_dates[0], all_dates[mid], all_dates[-1], capital)
            m = compute_metrics(eq, p.closed_trades, self.nifty, capital)
            self._row(rows, run_id, db, 1, all_dates[0], all_dates[mid - 1],
                      all_dates[mid], all_dates[-1], m)
            return pd.DataFrame(rows)
        fold, start, cur = 0, 0, float(capital)
        while start + WF_TRAIN_DAYS + WF_TEST_DAYS <= len(all_dates):
            fold += 1
            ts0 = all_dates[start]; te0 = all_dates[start + WF_TRAIN_DAYS - 1]
            vs = all_dates[start + WF_TRAIN_DAYS]
            ve = all_dates[min(start + WF_TRAIN_DAYS + WF_TEST_DAYS - 1, len(all_dates) - 1)]
            p, eq = self._fold(ts0, vs, ve, cur)
            m = compute_metrics(eq, p.closed_trades, self.nifty, cur)
            self._row(rows, run_id, db, fold, ts0, te0, vs, ve, m)
            LOG.info("Fold %d: %s->%s | cap Rs%s | CAGR %.1f%% vs NIFTY %s%% | alpha %s%% | trades %d",
                     fold, vs.date(), ve.date(), f"{cur:,.0f}", m.get("CAGR_pct", 0.0),
                     m.get("nifty_CAGR_pct"), m.get("alpha_vs_nifty_cagr_pct"),
                     m.get("num_trades", 0))
            if compound and m.get("final_equity"):
                cur = float(m["final_equity"])
            start += WF_STEP_DAYS
        df = pd.DataFrame(rows)
        if not df.empty:
            LOG.info("Walk-forward AGGREGATE (%d folds): mean alpha vs NIFTY=%.2f%% | mean CAGR=%.2f%%",
                     len(df), pd.to_numeric(df["alpha_vs_nifty_pct"], errors="coerce").mean(),
                     df["CAGR_pct"].mean())
        return df


# ===========================================================================
# CHARTS
# ===========================================================================
def make_charts(equity: pd.DataFrame, nifty: pd.DataFrame, outdir: str):
    if plt is None or equity is None or equity.empty:
        LOG.warning("matplotlib unavailable or empty equity; skipping charts.")
        return
    os.makedirs(outdir, exist_ok=True)
    eq = equity["equity"]
    plt.figure(figsize=(11, 5))
    plt.plot(eq.index, eq.values, color="#1f77b4", label="Strategy NAV")
    plt.title("Portfolio NAV Curve"); plt.xlabel("Date"); plt.ylabel("Equity (INR)")
    plt.grid(alpha=0.3); plt.legend(); plt.tight_layout()
    plt.savefig(os.path.join(outdir, "nav_curve.png"), dpi=120)
    plt.savefig(os.path.join(outdir, "portfolio_nav.png"), dpi=120); plt.close()

    dd = (eq / eq.cummax() - 1) * 100
    plt.figure(figsize=(11, 4))
    plt.fill_between(dd.index, dd.values, 0, color="#d62728", alpha=0.5)
    plt.title("Drawdown Curve"); plt.xlabel("Date"); plt.ylabel("Drawdown %")
    plt.grid(alpha=0.3); plt.tight_layout()
    plt.savefig(os.path.join(outdir, "drawdown_curve.png"), dpi=120); plt.close()

    if nifty is not None and not nifty.empty:
        n = nifty["Close"].reindex(eq.index).ffill().bfill()
        if n.notna().any() and float(n.iloc[0]) > 0:
            ns, nn = eq / float(eq.iloc[0]), n / float(n.iloc[0])
            plt.figure(figsize=(11, 5))
            plt.plot(ns.index, ns.values, label="Strategy")
            plt.plot(nn.index, nn.values, label="NIFTY 50", color="#ff7f0e")
            plt.title("Equity vs NIFTY (normalised)"); plt.xlabel("Date")
            plt.ylabel("Growth of 1"); plt.grid(alpha=0.3); plt.legend(); plt.tight_layout()
            plt.savefig(os.path.join(outdir, "equity_vs_nifty.png"), dpi=120); plt.close()
            rel = ns / nn
            plt.figure(figsize=(11, 4))
            plt.plot(rel.index, rel.values, color="#2ca02c")
            plt.axhline(1.0, color="grey", ls="--", lw=0.8)
            plt.title("Relative Performance vs NIFTY (>1 = outperforming)")
            plt.xlabel("Date"); plt.ylabel("Strategy / NIFTY"); plt.grid(alpha=0.3)
            plt.tight_layout()
            plt.savefig(os.path.join(outdir, "relative_performance.png"), dpi=120); plt.close()
    LOG.info("Charts -> %s", outdir)


# ===========================================================================
# OFFLINE DEMO  (synthetic, for --offline-demo bug-checking; no network/file)
# ===========================================================================
def _synth(start, end, seed, drift=0.0006, vol=0.018, start_price=100.0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, end=end)
    n = len(idx)
    close = start_price * np.cumprod(1 + rng.normal(drift, vol, n))
    op = close / (1 + rng.normal(0, 0.004, n))
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    df = pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": close,
                       "Adj Close": close, "Volume": rng.integers(2e6, 9e6, n).astype(float)},
                      index=idx)
    df.index.name = "date"
    return df


def _inject_rallies(df, seed, n_rallies=5):
    rng = np.random.default_rng(seed + 9999)
    df = df.copy(); n = len(df)
    if n < 200:
        return df
    for s in sorted(rng.choice(range(60, n - 80), size=min(n_rallies, 5), replace=False)):
        rlen = int(rng.integers(8, 14)); step = 0.30 / rlen
        base = float(df["Open"].iloc[s]); price = base
        for k in range(rlen):
            if s + k >= n: break
            o = price; price = o * (1 + step); c = price
            df.iloc[s + k, df.columns.get_loc("Open")] = o
            df.iloc[s + k, df.columns.get_loc("Close")] = c
            df.iloc[s + k, df.columns.get_loc("Low")] = o * 0.999
            df.iloc[s + k, df.columns.get_loc("High")] = c * 1.001
        rlow, rhigh = base * 0.999, price * 1.001
        e = s + rlen
        for k in range(6):
            if e + k >= n: break
            px = price - (price - rlow) * ((k + 1) / 6)
            df.iloc[e + k, df.columns.get_loc("Open")] = px * 1.005
            df.iloc[e + k, df.columns.get_loc("Close")] = px
            df.iloc[e + k, df.columns.get_loc("Low")] = px * 0.998
            df.iloc[e + k, df.columns.get_loc("High")] = px * 1.006
        r = e + 6
        for k in range(8):
            if r + k >= n: break
            px = rlow + (rhigh - rlow) * ((k + 1) / 8)
            df.iloc[r + k, df.columns.get_loc("Open")] = px * 0.997
            df.iloc[r + k, df.columns.get_loc("Close")] = px
            df.iloc[r + k, df.columns.get_loc("Low")] = px * 0.996
            df.iloc[r + k, df.columns.get_loc("High")] = px * 1.002
    df["Adj Close"] = df["Close"]
    return df


def build_offline_demo(db: Database, start, end):
    demo = [("NESTLEIND.NS", "FMCG"), ("TCS.NS", "TECH"), ("PIDILITIND.NS", "CONSUMER"),
            ("COLPAL.NS", "FMCG"), ("PAGEIND.NS", "CONSUMER"), ("DIVISLAB.NS", "PHARMA"),
            ("MCX.NS", "BANKING"), ("HBLENGINE.NS", "DEFENCE")]
    price_data, sectors = {}, {}
    for i, (sym, sec) in enumerate(demo):
        df = _inject_rallies(_synth(start, end, seed=100 + i, start_price=100 + 10 * i),
                             seed=100 + i)
        db.store_prices(sym, df)
        price_data[sym] = df
        sectors[sym] = sec
    nifty = _synth(start, end, seed=7, drift=0.0003, vol=0.012, start_price=18000)
    db.store_prices("NIFTY50", nifty)
    return price_data, sectors, nifty


# ===========================================================================
# RUN
# ===========================================================================
def write_csv(df: pd.DataFrame, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    LOG.info("Wrote %s (%d rows)", os.path.basename(path), len(df))


def _write_drop_report(drops: List[Tuple[str, str]], path: str, kept: int, total: int):
    """Per-name breakdown of WHY each universe.txt name was dropped, plus a
    category tally. Logged to console and written to dropped.txt."""
    from collections import Counter
    cats = Counter(r.split("(")[0] for _, r in drops)
    tally = ", ".join(f"{k}={v}" for k, v in sorted(cats.items(), key=lambda x: -x[1]))
    LOG.info("Tradeable: %d/%d  |  dropped %d  ->  %s",
             kept, total, len(drops), tally or "none")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"DROP REPORT  (swingscanner v{VERSION})   {_now()}\n")
        f.write(f"universe.txt: {total} names  ->  {kept} tradeable, {len(drops)} dropped\n")
        f.write("=" * 70 + "\n")
        f.write("REASON CODES:\n")
        f.write("  BSE_CODE        numeric scrip code, not an NSE symbol (can't trade)\n")
        f.write("  NO_DATA         yfinance returned nothing (delisted/wrong ticker)\n")
        f.write("  SHORT_HISTORY   < %.1fy of PRICE bars (need %d for the 200-DMA;\n"
                "                  NOT a company-age filter — listing, not incorporation)\n"
                % (MIN_HISTORY_YEARS, int(MIN_HISTORY_YEARS * TRADING_DAYS * 0.9)))
        f.write("  LOW_PRICE       last close < Rs%.0f\n" % MIN_PRICE)
        f.write("  LOW_LIQUIDITY   avg daily traded value < Rs%.0fcr\n" % MIN_ADV_CR)
        f.write("-" * 70 + "\n")
        for k, v in sorted(cats.items(), key=lambda x: -x[1]):
            f.write(f"  {k:<16} {v}\n")
        f.write("-" * 70 + "\n")
        for sym, reason in sorted(drops, key=lambda x: x[1]):
            f.write(f"{sym:<18} {reason}\n")
    LOG.info("Drop breakdown -> %s", os.path.basename(path))


def write_dmafail(df: pd.DataFrame, path: str):
    """Readable text doc: names with a valid 20%+ V20 zone that were REJECTED
    only because the rally's low was NOT below the 200-DMA (zone_low >= DMA)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("DMA-REJECTED SETUPS  (swingscanner v%s)\n" % VERSION)
        f.write("Generated: %s\n" % _now())
        f.write("These names have a valid 20%+ V20 zone and otherwise qualify,\n")
        f.write("but were REJECTED because the rally's low was NOT below the\n")
        f.write("200-DMA (V200 rule needs zone_low < 200-DMA; here zone_low is\n")
        f.write("ABOVE it). 'above_dma%' = how far the zone_low sits over the DMA.\n")
        f.write("=" * 78 + "\n")
        if df is None or df.empty:
            f.write("None today.\n")
            LOG.info("Wrote %s (0 names)", os.path.basename(path))
            return
        f.write(f"{'SYMBOL':<16}{'SECTOR':<11}{'ZONE_LOW':>10}{'ZONE_HIGH':>11}"
                f"{'CLOSE':>10}{'200DMA':>10}{'above_dma%':>11}{'  READY':>8}\n")
        f.write("-" * 78 + "\n")
        for _, r in df.iterrows():
            f.write(f"{str(r['symbol']):<16}{str(r.get('sector') or '-'):<11}"
                    f"{r['zone_low']:>10}{r['zone_high']:>11}{r['current_close']:>10}"
                    f"{r['dma_200_at_zone_end']:>10}{str(r['zone_low_above_dma_pct']):>11}"
                    f"{('  YES' if r['entry_ready'] else '   no'):>8}\n")
    LOG.info("Wrote %s (%d names)", os.path.basename(path), len(df))


def run(universe_file: Optional[str] = None, do_backtest: bool = False,
        do_walkforward: bool = False, plot_charts: bool = False,
        start: Optional[str] = None, end: Optional[str] = None,
        capital: float = INITIAL_CAPITAL, allow_network: bool = True,
        refresh_prices: bool = True, reset_prices: bool = False,
        offline_demo: bool = False, dma_gate: bool = V20_REQUIRE_DMA_GATE,
        log_signals: bool = True) -> Dict[str, Any]:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(CHART_DIR, exist_ok=True)
    db = Database(DB_PATH)
    run_id = dt.datetime.now().strftime("run_%Y%m%d_%H%M%S")
    today = dt.date.today()
    # default window MUST cover the tradeability history requirement, else every
    # name "fails history" and gets dropped. Pull MIN_HISTORY_YEARS + 1y buffer.
    start = start or (today - dt.timedelta(
        days=int((MIN_HISTORY_YEARS + 1) * 365))).isoformat()
    end = end or today.isoformat()
    if reset_prices and not offline_demo:
        db.clear_prices(); LOG.info("Price cache cleared (--reset-prices).")

    LOG.info("=== swingscanner v%s | run_id=%s ===", VERSION, run_id)
    LOG.info("Mode: %s | backtest=%s walkforward=%s | dma_gate=%s",
             "OFFLINE-DEMO" if offline_demo else ("BACKTEST" if do_backtest else "LIVE TODAY-SCAN"),
             do_backtest, do_walkforward, dma_gate)

    # ----- universe + prices -----
    if offline_demo:
        LOG.info("OFFLINE DEMO (synthetic data).")
        price_data, sectors, nifty = build_offline_demo(db, start, end)
    else:
        ufile = universe_file or DEFAULT_UNIVERSE_FILE
        if not os.path.exists(ufile):
            LOG.error("universe file not found: %s", ufile)
            LOG.error("Generate it first:  python fetch_v200_universe.py")
            db.close()
            return {"error": "no universe file"}
        names = parse_universe_file(ufile)
        LOG.info("Loaded %d names from %s", len(names), os.path.basename(ufile))
        sectors = {sym: sec for sym, sec in names}
        pf = PriceFetcher(db)
        nifty = pf.fetch("NIFTY50" if False else NIFTY_TICKER, start, end,
                         allow_network, refresh=refresh_prices)
        price_data = {}
        drops: List[Tuple[str, str]] = []   # (symbol, reason)
        for i, (sym, _sec) in enumerate(names, 1):
            # screener sometimes gives BSE numeric scrip codes (e.g. 544467) for
            # names not on NSE — they 404 on yfinance, so skip them.
            if sym.split(".")[0].isdigit():
                drops.append((sym, "BSE_CODE(not on NSE)"))
                continue
            df = pf.fetch(sym, start, end, allow_network, refresh=refresh_prices)
            reason = tradeability_reason(df)
            if reason == "OK":
                price_data[sym] = df
            else:
                drops.append((sym, reason))
            if i % 50 == 0:
                LOG.info("  ...screened %d/%d (%d tradeable so far)",
                         i, len(names), len(price_data))
        _write_drop_report(drops, os.path.join(OUTPUT_DIR, "dropped.txt"),
                           kept=len(price_data), total=len(names))
        if not price_data:
            LOG.warning("No tradeable names with data — check network / universe.txt.")
            db.close()
            return {"error": "no price data"}

    # ----- relative strength -----
    rs_map = {sym: relative_strength(df, nifty) for sym, df in price_data.items()}
    rs_rank = rank_relative_strength(rs_map)

    # ----- active V20 setups (today) + DMA-rejected list -----
    # V200 rule: a zone is only valid if zone_low < 200-DMA (at the zone's end).
    # If the rally's low sits ABOVE the 200-DMA, the setup is REJECTED — those
    # "all-conditions-met-except-DMA" names are collected into DMAfail.txt.
    setups = []
    dma_fails = []
    for sym, df in price_data.items():
        zones_all = find_v20_zones(df)
        if not zones_all:
            continue
        ds = dma(df)
        last_close = float(df["Close"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        d200_now = float(ds.iloc[-1])

        def _passes_dma(z):
            return z.end_idx < len(ds) and z.zone_low < float(ds.iloc[z.end_idx])

        zones_pass = [z for z in zones_all if _passes_dma(z)] if dma_gate else zones_all

        # DMA-FAIL: the most recent zone exists but is blocked ONLY by the DMA gate
        if dma_gate:
            latest = zones_all[-1]
            if not _passes_dma(latest):
                dma_at_end = float(ds.iloc[latest.end_idx]) if latest.end_idx < len(ds) else d200_now
                gap_pct = round((latest.zone_low - dma_at_end) / dma_at_end * 100, 2) if dma_at_end else None
                entry_ready_f = (last_low <= latest.zone_low) or (last_close <= latest.zone_low)
                dma_fails.append({
                    "symbol": sym, "sector": sectors.get(sym),
                    "zone_low": round(latest.zone_low, 2),
                    "zone_high": round(latest.zone_high, 2),
                    "gain_pct": latest.gain_pct,
                    "current_close": round(last_close, 2),
                    "dma_200_at_zone_end": round(dma_at_end, 2),
                    "zone_low_above_dma_pct": gap_pct,
                    "entry_ready": entry_ready_f,
                    "rs_rank": rs_rank.get(sym),
                })

        if not zones_pass:
            continue
        z = zones_pass[-1]
        entry_ready = (last_low <= z.zone_low) or (last_close <= z.zone_low)
        sig = {"symbol": sym, "sector": sectors.get(sym), "zone_low": round(z.zone_low, 2),
               "zone_high": round(z.zone_high, 2), "zone_start": z.start_date,
               "zone_end": z.end_date, "gain_pct": z.gain_pct,
               "current_close": round(last_close, 2), "entry_ready": entry_ready,
               "dma_200": round(d200_now, 2), "rs_rank": rs_rank.get(sym)}
        db.insert_signal(sig)
        setups.append(sig)
    setups_df = pd.DataFrame(setups)
    if not setups_df.empty:
        setups_df = setups_df.sort_values(["entry_ready", "rs_rank"],
                                          ascending=[False, False])
    if log_signals and not setups_df.empty:
        ready = setups_df[setups_df["entry_ready"]]
        LOG.info("Active V20 setups: %d (entry-ready now: %d)", len(setups_df), len(ready))
        for _, s in setups_df.head(20).iterrows():
            LOG.info("  [%-9s] %-14s %-10s zone %.2f-%.2f  close %.2f  rs %s",
                     "BUY-READY" if s["entry_ready"] else "watch", s["symbol"],
                     s.get("sector") or "-", s["zone_low"], s["zone_high"],
                     s["current_close"], s["rs_rank"])

    dma_fail_df = pd.DataFrame(dma_fails)
    if not dma_fail_df.empty:
        dma_fail_df = dma_fail_df.sort_values(["entry_ready", "zone_low_above_dma_pct"],
                                              ascending=[False, True])
    write_dmafail(dma_fail_df, os.path.join(OUTPUT_DIR, "DMAfail.txt"))
    LOG.info("DMA-rejected (valid 20%% zone but zone_low ABOVE 200-DMA): %d "
             "-> DMAfail.txt", len(dma_fail_df))

    # ----- backtest (optional) -----
    metrics, portfolio, equity = {}, None, pd.DataFrame()
    bt = Backtester(price_data, nifty, dma_gate=dma_gate)
    if do_backtest:
        LOG.info("Running backtest %s -> %s ...", start, end)
        portfolio, equity = bt.run(start=start, end=end, capital=capital)
        metrics = compute_metrics(equity, portfolio.closed_trades, nifty, capital)
        db.insert_trades(run_id, portfolio.closed_trades)
        db.save_backtest(run_id, start, end, metrics)
        print_benchmark_table(metrics)

    # ----- walk-forward (optional) -----
    wf_df = pd.DataFrame()
    if do_walkforward:
        LOG.info("Running lookahead-free walk-forward ...")
        wf_df = WalkForward(bt, nifty).run(run_id, db, compound=True, capital=capital)

    # ----- charts (optional) -----
    if plot_charts and do_backtest and not equity.empty:
        make_charts(equity, nifty, CHART_DIR)

    # ----- outputs -----
    write_csv(setups_df if not setups_df.empty else pd.DataFrame(
        columns=["symbol", "sector", "zone_low", "zone_high", "current_close", "entry_ready"]),
        os.path.join(OUTPUT_DIR, "active_setups.csv"))
    if do_backtest:
        write_csv(pd.DataFrame([{"run_id": run_id, **metrics}]),
                  os.path.join(OUTPUT_DIR, "backtest_summary.csv"))
        tdf = pd.DataFrame([asdict(t) for t in portfolio.closed_trades])
        write_csv(tdf if not tdf.empty else pd.DataFrame(
            columns=["symbol", "entry_date", "exit_date", "pnl", "return_pct"]),
            os.path.join(OUTPUT_DIR, "trades.csv"))
    if do_walkforward:
        write_csv(wf_df if not wf_df.empty else pd.DataFrame(
            columns=["fold", "CAGR_pct", "nifty_CAGR_pct", "alpha_vs_nifty_pct"]),
            os.path.join(OUTPUT_DIR, "walk_forward_summary.csv"))

    LOG.info("=== SUMMARY (v%s) ===", VERSION)
    LOG.info("Universe: %d tradeable | active V20 setups: %d",
             len(price_data), len(setups_df))
    if do_backtest:
        LOG.info("Backtest CAGR: %s%% | NIFTY: %s%% | ALPHA: %s%% | Trades: %s",
                 metrics.get("CAGR_pct"), metrics.get("nifty_CAGR_pct"),
                 metrics.get("alpha_vs_nifty_cagr_pct"), metrics.get("num_trades"))
    LOG.info("Outputs in: %s", OUTPUT_DIR)
    db.close()
    return {"metrics": metrics, "walkforward": wf_df, "setups": setups_df}


# ===========================================================================
# CLI
# ===========================================================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="swingscanner v3.0.0 — V20 scanner on universe.txt")
    today = dt.date.today()
    p.add_argument("universe_file", nargs="?", default=None,
                   help="path to universe.txt (default: ./universe.txt)")
    p.add_argument("--universe-file", dest="ufile_opt", default=None,
                   help="alternative way to pass the universe file")
    p.add_argument("--start",
                   default=(today - dt.timedelta(days=int((MIN_HISTORY_YEARS + 1) * 365))).isoformat(),
                   help="history start (default covers the listing-history filter; "
                        "pass earlier for backtests)")
    p.add_argument("--end", default=today.isoformat())
    p.add_argument("--capital", type=float, default=INITIAL_CAPITAL)
    p.add_argument("--backtest", action="store_true", help="also run the backtest")
    p.add_argument("--walkforward", action="store_true", help="also run walk-forward")
    p.add_argument("--charts", action="store_true", help="write charts (implied by --backtest)")
    p.add_argument("--no-dma-gate", action="store_true",
                   help="do NOT require zone_low < 200-DMA (off = treat as plain V20)")
    p.add_argument("--no-refresh-prices", action="store_true",
                   help="use cached bars as-is (don't top up to today)")
    p.add_argument("--reset-prices", action="store_true",
                   help="wipe price cache and re-pull clean split-adjusted bars")
    p.add_argument("--no-fetch", action="store_true", help="cache only, no network")
    p.add_argument("--offline-demo", action="store_true",
                   help="synthetic self-test (no network, no universe.txt)")
    p.add_argument("--quiet", action="store_true")
    return p.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    ufile = a.ufile_opt or a.universe_file
    try:
        run(universe_file=ufile, do_backtest=a.backtest, do_walkforward=a.walkforward,
            plot_charts=a.charts or a.backtest, start=a.start, end=a.end,
            capital=a.capital, allow_network=not a.no_fetch and not a.offline_demo,
            refresh_prices=not a.no_refresh_prices, reset_prices=a.reset_prices,
            offline_demo=a.offline_demo, dma_gate=not a.no_dma_gate,
            log_signals=not a.quiet)
    except KeyboardInterrupt:
        LOG.warning("Interrupted.")


if __name__ == "__main__":
    main()