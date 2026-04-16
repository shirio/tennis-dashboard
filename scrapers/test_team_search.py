"""
Quick test: search for one team and see what flight links come back.
Run: python3 scrapers/test_team_search.py
"""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
STANDINGS_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=2"
PLAYER_SEARCH_URL    = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"


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
            # Login
            print("[login] ...")
            page.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(1)
            page.fill("input[name='username']", username)
            page.keyboard.press("Enter")
            time.sleep(1)
            page.fill("input[type='password']", password)
            page.keyboard.press("Enter")
            page.wait_for_url("**/tennislink.usta.com/**", timeout=20_000)
            print(f"[login] ok: {page.url}")
            time.sleep(1)

            # Test 1: Team search
            print("\n=== TEST: Team search for 'SPANISH TRAIL' ===")
            page.goto(STANDINGS_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            page.fill("#ctl00_mainContent_txtTeamName", "SPANISH TRAIL", timeout=3_000)
            page.click("#ctl00_mainContent_btnSearchTeamByName", timeout=3_000)
            try:
                page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            time.sleep(1)
            page.screenshot(path="data/test_team_search.png", full_page=False)

            links = []
            for a in page.query_selector_all("a[href]"):
                href = (a.get_attribute("href") or "").strip()
                text = (a.inner_text() or "").strip()
                if re.search(r"TeamID=|FlightID=|LocalLeagueID=", href) and "/FlexLeagues/" not in href:
                    full = abs_url(href)
                    links.append(f"  [{text[:60]}] -> {full[:100]}")
            print(f"Flight/team links found: {len(links)}")
            for l in links[:20]:
                print(l)

            # If links found, visit first one
            if links:
                first_url = links[0].split("-> ")[1].strip()
                print(f"\n=== Visiting first link: {first_url[:100]} ===")
                page.goto(first_url, wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)
                page.screenshot(path="data/test_team_page.png", full_page=False)
                # Check for flight links
                sub = []
                for a in page.query_selector_all("a[href]"):
                    href = (a.get_attribute("href") or "").strip()
                    text = (a.inner_text() or "").strip()
                    if re.search(r"FlightID=|LocalLeagueID=", href) and "/FlexLeagues/" not in href:
                        sub.append(f"  [{text[:60]}] -> {href[:100]}")
                print(f"Sub-links: {len(sub)}")
                for l in sub[:10]:
                    print(l)

            # Test 2: Player search
            print("\n=== TEST: Player search for 'Anna Clark' ===")
            page.goto(PLAYER_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            print(f"  txtFirstName visible: {page.locator('#ctl00_mainContent_txtFirstName').is_visible()}")
            print(f"  txtLastName visible: {page.locator('#ctl00_mainContent_txtLastName').is_visible()}")
            page.fill("#ctl00_mainContent_txtFirstName", "Anna", timeout=3_000)
            page.fill("#ctl00_mainContent_txtLastName", "Clark", timeout=3_000)
            page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            time.sleep(1)
            page.screenshot(path="data/test_player_search.png", full_page=False)
            player_links = []
            for a in page.query_selector_all("a[href]"):
                href = (a.get_attribute("href") or "").strip()
                text = (a.inner_text() or "").strip()
                if re.search(r"personId|playerId|PlayerID|search=", href, re.I) and "logout" not in href:
                    player_links.append(f"  [{text[:50]}] -> {href[:100]}")
            print(f"Player links: {len(player_links)}")
            for l in player_links[:10]:
                print(l)

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
