#!/usr/bin/env python3
"""
scrapers/scrape_ntrp_list.py
Save every TennisLink "Find NTRP Rating Info" results page for a district.

One login, in a visible (headed) browser window. Fills the Advanced Search at
  https://tennislink.usta.com/leagues/reports/NTRP/AdvancedSearch.aspx
with Championship Year, Section USTA/INTERMOUNTAIN, District NEVADA,
Gender All, Level All, then saves each results page's raw HTML to
data/ntrp_raw/<district>_<year>_pNN.html. Parse them afterwards with
scrapers/ntrp_list.py.

The results page is forward-only: never go "back" to the form; start a fresh
Advanced Search instead. On any failure the window stays open for 60 s and a
screenshot is saved next to the HTML, so you can see what went wrong.

Usage:
    python3 scrapers/scrape_ntrp_list.py --year 2026
    python3 scrapers/scrape_ntrp_list.py --year 2022,2023,2024,2025   # one login
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from scrapers.scrape_tennislink import login  # noqa: E402

SEARCH_URL = "https://tennislink.usta.com/leagues/reports/NTRP/AdvancedSearch.aspx"
MAX_PAGES = 300


def _select_by_option_text(page, wanted: str, label_hint: str) -> bool:
    """Pick `wanted` in whichever <select> offers it (ASP.NET ids are unstable)."""
    for sel in page.query_selector_all("select"):
        opts = sel.evaluate("s => Array.from(s.options).map(o => [o.value, o.text.trim()])")
        for value, text in opts:
            if text.upper() == wanted.upper():
                print(f"    {label_hint}: {text}")
                sel.select_option(value=value)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                time.sleep(1.5)  # dropdowns cascade via postback
                return True
    print(f"    [warn] no dropdown offers {wanted!r} ({label_hint})")
    return False


def _dismiss_cookie_banner(page) -> None:
    """Click "Reject All" on the cookie banner if it's showing."""
    for name in ("Reject All", "Reject All Cookies"):
        try:
            b = page.get_by_role("button", name=name)
            if b.count() and b.first.is_visible():
                b.first.click(timeout=3_000)
                print("    dismissed cookie banner")
                time.sleep(1)
                return
        except Exception:
            pass


def _next_page_link(page, current: int):
    """Find a pager link for page current+1, or a 'Next' link."""
    nxt = str(current + 1)
    for a in page.query_selector_all("a"):
        t = (a.inner_text() or "").strip()
        href = a.get_attribute("href") or ""
        if t == nxt and ("Page$" in href or "__doPostBack" in href or "page" in href.lower()):
            return a
    for a in page.query_selector_all("a"):
        t = (a.inner_text() or "").strip().lower()
        if t in ("next", "next >", ">", "next page", "»"):
            return a
    # ASP.NET pagers show "..." to reach the next block of page numbers
    for a in page.query_selector_all("a"):
        if (a.inner_text() or "").strip() == "..." and current % 10 == 0:
            return a
    return None


def _search_year(page, year, args, out, tag) -> None:
    """Fresh Advanced Search for one year; save every results page."""
    print(f"  opening Advanced Search for {year} …")
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=45_000)
    time.sleep(2)
    _dismiss_cookie_banner(page)
    if not _select_by_option_text(page, str(year), "year"):
        raise RuntimeError(f"year {year} not offered")
    _select_by_option_text(page, args.section, "section")
    _select_by_option_text(page, args.district, "district")
    # Gender and Level default to "All"; leave League blank.
    # The page also has hidden header "Search" buttons (e.g. #btnSearchNTRP),
    # so target the form's own button by id.
    btn = page.locator("#ctl00_mainContent_btnSearch")
    if btn.count() == 0:
        btn = page.locator("#ctl00_mainContent input[type=submit][value='Search']:visible").first
    btn.click(timeout=15_000)
    page.wait_for_load_state("networkidle", timeout=60_000)
    time.sleep(2)

    n = 1
    while True:
        html = page.content()
        path = out / f"{tag}_p{n:02d}.html"
        path.write_text(html, encoding="utf-8")
        rows = len(re.findall(r"<tr", html, re.I))
        print(f"  saved {path.name} ({rows} table rows)")
        if n >= MAX_PAGES:
            break
        link = _next_page_link(page, n)
        if not link:
            break
        link.click()
        page.wait_for_load_state("networkidle", timeout=60_000)
        time.sleep(2)
        n += 1
    print(f"  {year}: {n} page(s) saved to {out}")
    time.sleep(5)  # be gentle between searches


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="2026", help="One year or a comma list, all in one login")
    ap.add_argument("--section", default="USTA/INTERMOUNTAIN")
    ap.add_argument("--district", default="NEVADA")
    ap.add_argument("--out", default=str(ROOT / "data" / "ntrp_raw"))
    ap.add_argument("--tag", default="nv", help="File prefix for saved pages")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    user, pw_ = os.getenv("TENNISLINK_USER"), os.getenv("TENNISLINK_PASS")
    if not user or not pw_:
        print("TENNISLINK_USER / TENNISLINK_PASS not set")
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    years = [y.strip() for y in str(args.year).split(",") if y.strip()]
    tag = f"{args.tag}_{years[0]}"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, slow_mo=150)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            login(page, user, pw_)
            for year in years:
                tag = f"{args.tag}_{year}"
                _search_year(page, year, args, out, tag)
        except Exception as e:
            shot = out / f"{tag}_error.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                (out / f"{tag}_error.html").write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            print(f"  [error] {e}\n  screenshot: {shot}\n  window stays open 60 s …")
            time.sleep(60)
            return 1
        finally:
            time.sleep(3)
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
