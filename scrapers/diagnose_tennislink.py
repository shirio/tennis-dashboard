"""
scrapers/diagnose_tennislink.py
Logs into TennisLink, navigates key pages, and dumps their structure
so we can fix the scraper selectors.

Run:  python3 scrapers/diagnose_tennislink.py
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://tennislink.usta.com"

def sleep(s=1.5): time.sleep(s)

def dump_page(page, label: str):
    print(f"\n{'='*60}")
    print(f"PAGE: {label}")
    print(f"URL:  {page.url}")
    print("="*60)

    # All select elements + their options
    selects = page.query_selector_all("select")
    print(f"\n--- SELECTS ({len(selects)}) ---")
    for sel in selects:
        sel_id   = sel.get_attribute("id") or ""
        sel_name = sel.get_attribute("name") or ""
        opts = [(o.get_attribute("value") or "", (o.inner_text() or "").strip())
                for o in sel.query_selector_all("option")]
        # Find label for this select
        lbl_text = ""
        if sel_id:
            lbl = page.query_selector(f"label[for='{sel_id}']")
            if lbl:
                lbl_text = lbl.inner_text().strip()
        print(f"  SELECT id={sel_id!r} name={sel_name!r} label={lbl_text!r}")
        for v, t in opts[:20]:
            print(f"    [{v}] {t}")
        if len(opts) > 20:
            print(f"    ... ({len(opts)-20} more)")

    # All input elements (non-hidden)
    inputs = page.query_selector_all("input:not([type='hidden'])")
    print(f"\n--- INPUTS ({len(inputs)}) ---")
    for inp in inputs:
        itype = inp.get_attribute("type") or "text"
        iname = inp.get_attribute("name") or ""
        iid   = inp.get_attribute("id") or ""
        ival  = inp.get_attribute("value") or ""
        print(f"  INPUT type={itype} id={iid!r} name={iname!r} value={ival!r}")

    # All buttons
    buttons = page.query_selector_all("button, input[type='submit'], input[type='button']")
    print(f"\n--- BUTTONS ({len(buttons)}) ---")
    for btn in buttons:
        btype = btn.get_attribute("type") or ""
        bname = btn.get_attribute("name") or ""
        bval  = btn.get_attribute("value") or ""
        btxt  = (btn.inner_text() or "").strip()
        print(f"  BUTTON type={btype} name={bname!r} value={bval!r} text={btxt!r}")

    # All links
    links = page.query_selector_all("a[href]")
    print(f"\n--- LINKS (first 40 of {len(links)}) ---")
    seen = set()
    count = 0
    for a in links:
        href = a.get_attribute("href") or ""
        text = (a.inner_text() or "").strip()
        if href in seen or href.startswith("javascript"):
            continue
        seen.add(href)
        print(f"  [{text[:50]}] → {href[:120]}")
        count += 1
        if count >= 40:
            break

    # Page title and h1/h2
    title = page.title()
    h1s   = [h.inner_text().strip() for h in page.query_selector_all("h1,h2,h3")]
    print(f"\n--- HEADINGS ---")
    print(f"  title: {title}")
    for h in h1s[:10]:
        print(f"  {h}")

    # Save screenshot
    out = Path("data") / f"diag_{label.replace(' ','_').replace('/','_')}.png"
    try:
        page.screenshot(path=str(out), full_page=False)
        print(f"\n  [screenshot saved to {out}]")
    except Exception as e:
        print(f"\n  [screenshot failed: {e}]")


def login(page):
    from scrapers.scrape_tennislink import login as do_login
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    do_login(page, username, password)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            login(page)
            sleep(1)

            # ── 1. Dashboard ─────────────────────────────────────────────────
            dump_page(page, "dashboard")

            # ── 2. Standings search page ─────────────────────────────────────
            page.goto(f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3",
                      wait_until="domcontentloaded", timeout=30_000)
            sleep(2)
            dump_page(page, "standings_search")

            # ── 3. Try to fill Section dropdown and see what changes ──────────
            selects = page.query_selector_all("select")
            if selects:
                print("\n\n--- ATTEMPTING TO FILL FIRST SELECT ---")
                first_sel = selects[0]
                sel_id = first_sel.get_attribute("id") or ""
                opts = [(o.get_attribute("value") or "", (o.inner_text() or "").strip())
                        for o in first_sel.query_selector_all("option")]
                print(f"  First select id={sel_id!r}, options:")
                for v, t in opts:
                    print(f"    [{v}] {t}")

                # Find the Intermountain option
                for v, t in opts:
                    if "intermountain" in t.lower():
                        print(f"\n  Selecting Intermountain: value={v!r}")
                        try:
                            first_sel.select_option(value=v)
                            sleep(2)
                            dump_page(page, "after_section_select")
                        except Exception as e:
                            print(f"  Error: {e}")
                        break

            # ── 4. Player search page ─────────────────────────────────────────
            page.goto(f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=2",
                      wait_until="domcontentloaded", timeout=30_000)
            sleep(2)
            dump_page(page, "player_search")

            # Try searching for one player
            print("\n\n--- TRYING PLAYER SEARCH FOR 'Anna Clark' ---")
            for inp in page.query_selector_all("input[type='text'], input[type='search']"):
                try:
                    inp.fill("Anna Clark", timeout=2000)
                    print(f"  Filled search input id={inp.get_attribute('id')!r}")
                    page.keyboard.press("Enter")
                    sleep(2)
                    dump_page(page, "player_search_results")
                    break
                except Exception:
                    pass

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
