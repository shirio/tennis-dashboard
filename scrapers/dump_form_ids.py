"""
scrapers/dump_form_ids.py
Quick diagnostic: dumps all form element IDs and visibility for SearchType=2 and =3.
Run: python3 scrapers/dump_form_ids.py
"""
from __future__ import annotations
import os, sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"

def dump_forms(page, label):
    print(f"\n{'='*60}")
    print(f"PAGE: {label}  URL: {page.url}")
    print("="*60)

    # All select elements + visibility
    selects = page.query_selector_all("select")
    print(f"\n--- SELECTS ({len(selects)}) ---")
    for sel in selects:
        sel_id = sel.get_attribute("id") or "?"
        sel_name = sel.get_attribute("name") or ""
        is_vis = sel.is_visible()
        opts = [(o.get_attribute("value") or "", (o.inner_text() or "").strip())
                for o in sel.query_selector_all("option")]
        print(f"  SELECT id={sel_id!r} name={sel_name!r} visible={is_vis}")
        for v, t in opts[:10]:
            print(f"    [{v}] {t}")
        if len(opts) > 10:
            print(f"    ... ({len(opts)-10} more)")

    # Inputs
    inputs = page.query_selector_all("input:not([type='hidden'])")
    print(f"\n--- INPUTS ({len(inputs)}) ---")
    for inp in inputs:
        itype = inp.get_attribute("type") or "text"
        iid = inp.get_attribute("id") or "?"
        iname = inp.get_attribute("name") or ""
        is_vis = inp.is_visible()
        ival = inp.get_attribute("value") or ""
        print(f"  INPUT type={itype} id={iid!r} name={iname!r} visible={is_vis} value={ival!r}")

    # Buttons
    btns = page.query_selector_all("button, input[type='submit'], input[type='button']")
    print(f"\n--- BUTTONS ({len(btns)}) ---")
    for btn in btns:
        bid = btn.get_attribute("id") or "?"
        bval = btn.get_attribute("value") or ""
        btxt = (btn.inner_text() or "").strip()
        is_vis = btn.is_visible()
        print(f"  BUTTON id={bid!r} value={bval!r} text={btxt!r} visible={is_vis}")


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            # Login
            print("[login] navigating ...")
            page.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx",
                      wait_until="domcontentloaded", timeout=30_000)
            time.sleep(1)
            page.fill("input[name='username']", username)
            page.keyboard.press("Enter")
            time.sleep(1)
            page.fill("input[type='password']", password)
            page.keyboard.press("Enter")
            page.wait_for_url("**/tennislink.usta.com/**", timeout=20_000)
            print(f"[login] success: {page.url}")
            time.sleep(1)

            for st in [2, 3]:
                url = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType={st}"
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)
                dump_forms(page, f"SearchType={st}")
                # Save screenshot
                page.screenshot(path=f"data/diag_st{st}.png", full_page=False)
                print(f"  [screenshot saved to data/diag_st{st}.png]")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
