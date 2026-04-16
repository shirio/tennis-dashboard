"""
Test: dump all links from player search results, and try team names.
Run: python3 scrapers/test_player_links.py
"""
import os, sys, time, re, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
STANDINGS_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=2"
PLAYER_URL    = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"


def abs_url(href):
    from urllib.parse import urljoin
    return href if href.startswith("http") else urljoin(BASE_URL, href)


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
            print(f"[login] ok")
            time.sleep(1)

            # ── Player search: dump ALL links ──────────────────────────────────
            print("\n=== PLAYER SEARCH: all links after searching 'Anna Clark' ===")
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

            seen = set()
            for a in page.query_selector_all("a[href]"):
                href = (a.get_attribute("href") or "").strip()
                text = (a.inner_text() or "").strip()
                if not href or href in seen or href.startswith("javascript"):
                    continue
                seen.add(href)
                print(f"  [{text[:50]!r}] -> {href[:120]}")

            # ── Team search: try different team names ──────────────────────────
            print("\n=== TEAM SEARCH: try a few team names ===")
            teams_to_try = ["SPANISH", "ANTHEM", "DESERT", "TPC", ""]
            for team in teams_to_try:
                page.goto(STANDINGS_URL, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)
                page.fill("#ctl00_mainContent_txtTeamName", team, timeout=3_000)
                page.click("#ctl00_mainContent_btnSearchTeamByName", timeout=3_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                time.sleep(1)
                page.screenshot(path=f"data/test_team_{team or 'empty'}.png", full_page=False)

                count = 0
                for a in page.query_selector_all("a[href]"):
                    href = (a.get_attribute("href") or "").strip()
                    text = (a.inner_text() or "").strip()
                    if re.search(r"TeamID=|FlightID=|LocalLeagueID=|TeamStandings", href, re.I) \
                       and "/FlexLeagues/" not in href and href not in seen:
                        seen.add(href)
                        print(f"  team={team!r}: [{text[:60]!r}] -> {href[:100]}")
                        count += 1
                if count == 0:
                    print(f"  team={team!r}: 0 results")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
