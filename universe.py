#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_v200_universe.py
======================

Scrape TODAY's V200 list from a screener.in screen and write `universe.txt`
for scanner.py ( `python scanner.py universe.txt` ).

Screen (default): "debt equity 0.25, RoCE >20%, NP > 200 Cr"
    https://www.screener.in/screens/1021245/debt-equity-025-roce-20-np-200-cr/
    Query: Debt to equity < 0.25 AND Return on capital employed > 20% AND
           Net profit > 200

WHY LOGIN IS NEEDED
-------------------
screener.in serves only PAGE 1 (25 names) to anonymous requests — pagination
and sorting redirect to the login wall. To pull all ~197 names you must log in
with a FREE screener.in account. This script does a normal Django form login
with your credentials, then walks every page.

This scrapes the screen as it stands RIGHT NOW (present-day data), which is
exactly what you want for a live universe.

USAGE
-----
    # credentials via env (recommended):
    export SCREENER_EMAIL="you@example.com"
    export SCREENER_PASSWORD="yourpassword"
    python fetch_v200_universe.py

    # or pass them in:
    python fetch_v200_universe.py --email you@example.com --password 'pw'

    # custom screen / output / add sector tags (slower, one fetch per name):
    python fetch_v200_universe.py --screen-url https://www.screener.in/screens/1021245/x/
    python fetch_v200_universe.py --out universe.txt --with-sectors

    # no creds? grabs only page 1 (25 names), like an anonymous fetch:
    python fetch_v200_universe.py --anonymous

Requires:  requests  beautifulsoup4   (pip install requests beautifulsoup4)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Optional

import requests

try:
    from bs4 import BeautifulSoup
except Exception:
    print("Please: pip install beautifulsoup4")
    raise


# ══════════════════════════════════════════════════════════════════════
#  
#
#  ENV VARS ENABLED
# ══════════════════════════════════════════════════════════════════════
MY_EMAIL    = ""        # e.g. "you@example.com"
MY_PASSWORD = ""        # e.g. "your-password"


DEFAULT_SCREEN = ("https://www.screener.in/screens/1021245/"
                  "debt-equity-025-roce-20-np-200-cr/")
BASE = "https://www.screener.in"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# screener.in "Industry"/sector text -> scanner.py sector buckets
SECTOR_MAP = {
    "bank": "BANKING", "finance": "BANKING", "nbfc": "BANKING",
    "insurance": "BANKING", "amc": "BANKING", "capital market": "BANKING",
    "exchange": "BANKING", "broking": "BANKING",
    "it ": "TECH", "software": "TECH", "technolog": "TECH", "computers": "TECH",
    "pharma": "PHARMA", "healthcare": "PHARMA", "hospital": "PHARMA",
    "drugs": "PHARMA", "diagnostic": "PHARMA",
    "fmcg": "FMCG", "personal product": "FMCG", "household": "FMCG",
    "food": "FMCG", "consumer food": "FMCG", "tea": "FMCG", "cigarette": "FMCG",
    "consumer": "CONSUMER", "retail": "CONSUMER", "textile": "TEXTILES",
    "apparel": "TEXTILES", "footwear": "CONSUMER", "jewell": "CONSUMER",
    "paint": "CONSUMER", "auto": "AUTO", "tyre": "AUTO",
    "chemical": "CHEMICALS", "fertiliz": "CHEMICALS",
    "power": "POWER", "energy": "POWER", "renewable": "POWER",
    "electric equipment": "CAPITAL_GOODS", "capital good": "CAPITAL_GOODS",
    "engineering": "CAPITAL_GOODS", "defence": "DEFENCE", "defense": "DEFENCE",
    "infrastructure": "INFRA", "construction": "INFRA", "cement": "INFRA",
    "cable": "INFRA", "logistic": "INFRA",
}


# ---------------------------------------------------------------------------
def login(session: requests.Session, email: str, password: str) -> bool:
    """Django form login to screener.in. Returns True on success."""
    url = f"{BASE}/login/"
    r = session.get(url, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    token_el = soup.find("input", attrs={"name": "csrfmiddlewaretoken"})
    csrf = token_el["value"] if token_el else session.cookies.get("csrftoken", "")
    # screener's login form uses "username" for the email field
    payload = {
        "csrfmiddlewaretoken": csrf,
        "username": email,
        "password": password,
        "next": "/",
    }
    headers = {**HEADERS, "Referer": url}
    resp = session.post(url, data=payload, headers=headers, timeout=20,
                        allow_redirects=True)
    ok = ("logout" in resp.text.lower()) or ("/login" not in resp.url)
    # secondary check: hitting a gated page should not bounce to /login
    chk = session.get(f"{BASE}/screens/1021245/x/?page=2", timeout=20,
                      allow_redirects=True)
    ok = ok and ("register" not in chk.url and "login" not in chk.url)
    return ok


# ---------------------------------------------------------------------------
def parse_codes(html: str) -> list[str]:
    """Extract NSE company codes from a screen results page.

    Robust to: absolute (https://www.screener.in/company/X/) OR relative
    (/company/X/) hrefs, missing trailing slash, '/consolidated/' suffix, and
    query/fragment tails. Scoped to the results <table> when possible so we
    don't pick up nav/peer links; ignores the /company/compare/ link."""
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.select("table a[href*='/company/']")
    if not anchors:                       # fall back to every company link
        anchors = soup.find_all("a", href=re.compile(r"/company/"))
    codes: list[str] = []
    seen = set()
    for a in anchors:
        m = re.search(r"/company/([^/?#]+)", a.get("href", ""))
        if not m:
            continue
        code = m.group(1).strip()
        if code and code.lower() != "compare" and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def n_pages(html: str) -> int:
    m = re.search(r"page\s+\d+\s+of\s+(\d+)", html, re.I)
    return int(m.group(1)) if m else 1


def fetch_screen(session: requests.Session, screen_url: str,
                 anonymous: bool = False) -> list[str]:
    """Walk every page of the screen, return de-duplicated NSE codes."""
    base = screen_url.rstrip("/") + "/"
    r = session.get(base, headers=HEADERS, timeout=20)
    if "register" in r.url or "login" in r.url:
        print("  Screen requires login (could not read page 1).")
        return []
    total = n_pages(r.text)
    codes = parse_codes(r.text)
    print(f"  page 1/{total}: {len(codes)} names")
    if anonymous:
        if total > 1:
            print("  (anonymous: only page 1 of "
                  f"{total} is available — log in for the rest)")
        return codes
    for p in range(2, total + 1):
        rp = session.get(base, params={"page": p}, headers=HEADERS, timeout=20)
        if "register" in rp.url or "login" in rp.url:
            print(f"  page {p}: blocked (login expired?) — stopping")
            break
        pc = parse_codes(rp.text)
        new = [c for c in pc if c not in codes]
        codes.extend(new)
        print(f"  page {p}/{total}: +{len(new)} names ({len(codes)} total)")
        time.sleep(0.5)
    return codes


# ---------------------------------------------------------------------------
def fetch_sector(session: requests.Session, code: str) -> Optional[str]:
    """Best-effort: read a company's industry and map to a scanner bucket."""
    try:
        r = session.get(f"{BASE}/company/{code}/", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        text = ""
        peer = soup.find("a", href=re.compile(r"/company/compare/"))
        if peer:
            text = peer.get_text(" ", strip=True)
        if not text:
            text = soup.title.get_text(" ", strip=True) if soup.title else ""
        low = text.lower()
        for key, bucket in SECTOR_MAP.items():
            if key in low:
                return bucket
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
def write_universe(path: str, codes: list[str],
                   sectors: Optional[dict] = None) -> None:
    sectors = sectors or {}
    with open(path, "w", encoding="utf-8") as f:
        f.write("# universe.txt — V200 screen scraped live from screener.in\n")
        f.write("# Query: Debt to equity < 0.25 AND ROCE > 20% AND Net profit > 200\n")
        f.write(f"# {len(codes)} names, scraped {time.strftime('%Y-%m-%d %H:%M')}\n")
        f.write("# Load with:  python scanner.py universe.txt\n#\n")
        for c in codes:
            tkr = c if c.endswith(".NS") else f"{c}.NS"   # NSE yfinance ticker
            sec = sectors.get(c)
            f.write(f"{tkr},{sec}\n" if sec else f"{tkr}\n")
    full = os.path.abspath(path)
    if codes:
        print(f"  wrote {len(codes)} names -> {full}")
        print(f"  first few: {', '.join(codes[:8])} ...")
    else:
        print(f"  WARNING: 0 names parsed — wrote only a header to {full}")
        print("  (login may have failed, or the page layout changed)")


def main(argv=None):
    p = argparse.ArgumentParser(description="Scrape the V200 screen -> universe.txt")
    p.add_argument("--screen-url", default=DEFAULT_SCREEN)
    p.add_argument("--out", default="universe.txt")
    # priority: hardcoded MY_EMAIL/MY_PASSWORD -> env var -> --email/--password
    p.add_argument("--email",
                   default=(MY_EMAIL or os.environ.get("SCREENER_EMAIL")))
    p.add_argument("--password",
                   default=(MY_PASSWORD or os.environ.get("SCREENER_PASSWORD")))
    p.add_argument("--anonymous", action="store_true",
                   help="don't log in (page 1 / 25 names only)")
    p.add_argument("--with-sectors", action="store_true",
                   help="also fetch each name's sector (slower)")
    args = p.parse_args(argv)

    s = requests.Session()
    s.headers.update(HEADERS)

    if not args.anonymous:
        if not args.email or not args.password:
            print("No credentials. Set SCREENER_EMAIL / SCREENER_PASSWORD or "
                  "pass --email/--password, or use --anonymous for page 1 only.")
            sys.exit(1)
        print("Logging in to screener.in ...")
        if not login(s, args.email, args.password):
            print("  Login failed — check credentials. Falling back to anonymous.")
            args.anonymous = True

    print("Scraping screen ...")
    codes = fetch_screen(s, args.screen_url, anonymous=args.anonymous)
    if not codes:
        print("No names scraped.")
        sys.exit(2)

    sectors = {}
    if args.with_sectors:
        print(f"Fetching sectors for {len(codes)} names (slow)...")
        for i, c in enumerate(codes, 1):
            sectors[c] = fetch_sector(s, c)
            if i % 25 == 0:
                print(f"  sectors {i}/{len(codes)}")
            time.sleep(0.3)

    write_universe(args.out, codes, sectors)
    print("Done. Now run:  python scanner.py", args.out)


if __name__ == "__main__":
    main()
