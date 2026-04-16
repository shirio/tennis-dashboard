"""
Test: dump actual page content (tables, inner HTML) after player/team search.
Run: python3 scrapers/test_page_content.py
"""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
STANDINGS_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=2"
PLAYER_URL    = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(1)
            page.fill("input[name='username']", username)
            page.keyboard.press("Enter")
            time.sleep(1)
            page.fill("input[type='password']", password)
            page.keyboard.press("Enter")
            page.wait_for_url("**/tennislink.usta.com/**", timeout=20_000)
            time.sleep(1)

            # ── Player search: dump table HTML ────────────────────────────────
            print("\n=== PLAYER SEARCH RESULT TABLE (Anna Clark) ===")
            page.goto(PLAYER_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            page.fill("#ctl00_mainContent_txtFirstName", "Anna", timeout=3_000)
            page.fill("#ctl00_mainContent_txtLastName", "Clark", timeout=3_000)
            page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            time.sleep(1)

            # Dump the main content inner HTML (first 8000 chars)
            main_el = page.query_selector("#ctl00_mainContent")
            if main_el:
                html = main_el.inner_html()
                print(html[:8000])
                print("..." if len(html) > 8000 else "")
            else:
                print("[no #ctl00_mainContent found]")
                print(page.content()[:4000])

            # ── Team search: dump result HTML ──────────────────────────────────
            print("\n\n=== TEAM SEARCH RESULT (empty name) ===")
            page.goto(STANDINGS_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            page.fill("#ctl00_mainContent_txtTeamName", "", timeout=3_000)
            page.click("#ctl00_mainContent_btnSearchTeamByName", timeout=3_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            time.sleep(2)
            page.screenshot(path="data/test_team_empty_result.png", full_page=False)

            main_el = page.query_selector("#ctl00_mainContent")
            if main_el:
                html = main_el.inner_html()
                print(html[:6000])
            else:
                print("[no #ctl00_mainContent]")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
