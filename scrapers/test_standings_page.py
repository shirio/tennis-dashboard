"""
Examine the subflight standings page structure.
Navigate there via player search → team link, then dump the content.
"""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
PLAYER_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"


def search_player(page, first, last, nv_team=None):
    """Search for a player and click team link for NV row matching nv_team."""
    page.goto(PLAYER_URL, wait_until="domcontentloaded", timeout=30_000)
    time.sleep(2)
    page.fill("#ctl00_mainContent_txtFirstName", first, timeout=3_000)
    page.fill("#ctl00_mainContent_txtLastName", last, timeout=3_000)
    page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    time.sleep(1)

    for tbl in page.query_selector_all("table.CommonTable.Segmented"):
        for tr in tbl.query_selector_all("tbody tr"):
            tds = tr.query_selector_all("td")
            if len(tds) < 3:
                continue
            city_state = (tds[1].inner_text() or "").strip()
            if ",NV" not in city_state:
                continue
            team_a = tds[2].query_selector("a")
            if not team_a:
                continue
            team_txt = (team_a.inner_text() or "").strip()
            if nv_team and nv_team.lower() not in team_txt.lower():
                continue
            return team_a
    return None


def main():
    username = os.getenv("TENNISLINK_USER","")
    password = os.getenv("TENNISLINK_PASS","")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width":1280,"height":900})
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

            # Navigate to 3.0 Women A via Anna Clark → DTC #3
            print("=== 3.0 Women A: Anna Clark → DTC #3 ===")
            team_el = search_player(page, "Anna", "Clark", "DTC #3")
            if team_el:
                team_el.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                time.sleep(2)
                print(f"URL: {page.url}")
                page.screenshot(path="data/test_30A_standings.png", full_page=False)

                # Dump the main content div
                main_div = page.query_selector("#ctl00_mainContent_UpdatePanel1")
                if main_div:
                    html = main_div.inner_html()
                    print(f"Main div HTML ({len(html)} chars):")
                    print(html[:5000])
                else:
                    # Try to find any table with team standings
                    tables = page.query_selector_all("table")
                    for t in tables[:3]:
                        print(f"Table: {(t.inner_text() or '')[:300]!r}")

                # Extract "Link to this Page" URL
                link_el = page.query_selector("#ctl00_mainContent_pnlSetupLink a, a:has-text('Link to this Page')")
                if link_el:
                    href = link_el.get_attribute("href") or ""
                    print(f"\n'Link to this Page' href: {href}")
                    from urllib.parse import urljoin
                    full = href if href.startswith("http") else urljoin(BASE_URL + "/Leagues/Main/", href)
                    print(f"Full URL: {full}")

                # All visible links on the page
                print("\n--- All relevant links on standings page ---")
                for a in page.query_selector_all("a[href]"):
                    href = a.get_attribute("href") or ""
                    txt = (a.inner_text() or "").strip()
                    if any(kw in href or kw in txt for kw in ["Flight", "Scorecard", "Match", "Team", "StatsAnd", "t=3", "t=4"]):
                        print(f"  [{txt[:60]!r}] -> {href[:120]}")

            else:
                print("Could not find NV row for Anna Clark / DTC #3")

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
