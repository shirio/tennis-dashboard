"""
Dump the standings and match tables HTML to understand data structure.
"""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin

BASE_URL = "https://tennislink.usta.com"
PLAYER_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"


def nav_to_team(page, first, last, nv_team):
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
            if ",NV" not in (tds[1].inner_text() or ""):
                continue
            team_a = tds[2].query_selector("a")
            if team_a and nv_team.lower() in (team_a.inner_text() or "").lower():
                team_a.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                time.sleep(2)
                return True
    return False


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

            if nav_to_team(page, "Anna", "Clark", "DTC #3"):
                print(f"On team page: {page.url}")

                # 1. Dump standings table
                standings_tbl = page.query_selector("#ctl00_mainContent_tblTeamStandings, table[id*='Standings']")
                if not standings_tbl:
                    # find by class or nearby heading
                    for tbl in page.query_selector_all("table"):
                        txt = (tbl.inner_text() or "")
                        if "DTC #3" in txt and "ANTHEM" in txt and "W" in txt:
                            standings_tbl = tbl
                            break
                if standings_tbl:
                    print("\n=== STANDINGS TABLE ===")
                    print(standings_tbl.inner_html()[:4000])
                else:
                    print("[no standings table found by id/class]")
                    # dump ALL tables
                    for i, t in enumerate(page.query_selector_all("table")):
                        txt = (t.inner_text() or "").strip()
                        if "DTC" in txt or "ANTHEM" in txt or "W" in txt[:100]:
                            print(f"\n=== TABLE {i} ===")
                            print(t.inner_html()[:2000])

                # 2. Dump match table
                print("\n=== MATCH SCHEDULE TABLE ===")
                main_div = page.query_selector("#ctl00_mainContent_UpdatePanel1")
                if main_div:
                    # Look for match-related table
                    html = main_div.inner_html()
                    # Find rptTeamMatches section
                    idx = html.find("rptTeamMatches")
                    if idx >= 0:
                        print(html[max(0,idx-200):idx+2000])

                # 3. Click SubFlight to get full subflight page
                print("\n\n=== Clicking SubFlight link / A ===")
                sub_link = page.query_selector("#ctl00_mainContent_lnkSubFlightForTeams")
                if sub_link:
                    sub_link.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass
                    time.sleep(2)
                    print(f"URL: {page.url}")
                    page.screenshot(path="data/test_subflight.png", full_page=False)

                    # Get Link to this Page URL
                    link_el = page.query_selector("a.share-link, a:has-text('Link to this Page')")
                    if link_el:
                        href = link_el.get_attribute("href") or ""
                        full = urljoin(BASE_URL + "/Leagues/Main/", href)
                        print(f"Subflight 'Link to this Page': {full}")

                    # Dump content
                    main_div = page.query_selector("#ctl00_mainContent_UpdatePanel1")
                    if main_div:
                        print("\nSubflight page content:")
                        print(main_div.inner_text()[:3000])

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
