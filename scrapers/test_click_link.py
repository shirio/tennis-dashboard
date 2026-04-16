"""
Test: click player/flight links to see where they lead.
Run: python3 scrapers/test_click_link.py
"""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
PLAYER_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"


def get_result_rows(page):
    """Return list of (player_name, city_state, team_text, row_index, year_index) from result tables."""
    rows = []
    year_idx = 0
    for tbl in page.query_selector_all("table.CommonTable.Segmented"):
        heading = tbl.query_selector("thead th")
        year_txt = (heading.inner_text() if heading else "").strip()
        tr_els = tbl.query_selector_all("tbody tr")
        for i, tr in enumerate(tr_els):
            tds = tr.query_selector_all("td")
            if len(tds) < 3:
                continue
            name_a = tds[0].query_selector("a")
            team_a = tds[2].query_selector("a")
            name = (name_a.inner_text() if name_a else "").strip()
            city_state = (tds[1].inner_text() or "").strip()
            team = (team_a.inner_text() if team_a else "").strip()
            if name:
                rows.append({
                    "name": name, "city_state": city_state, "team": team,
                    "name_el": name_a, "team_el": team_a,
                    "year": year_txt, "year_idx": year_idx, "row_idx": i
                })
        year_idx += 1
    return rows


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

            # Search for Anna Clark (in 3.0 Women A, DTC #3, Las Vegas NV)
            print("=== Searching for Anna Clark ===")
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

            rows = get_result_rows(page)
            print(f"Total rows: {len(rows)}")
            for r in rows:
                print(f"  {r['year'][:20]}: {r['name']} | {r['city_state']} | {r['team']}")

            # Find NV (Las Vegas) player
            nv_rows = [r for r in rows if ",NV" in r["city_state"] or "Las Vegas" in r["city_state"] or "Nevada" in r["city_state"]]
            print(f"\nNV rows: {len(nv_rows)}")
            if nv_rows:
                r = nv_rows[0]
                print(f"Using: {r['name']} | {r['city_state']} | {r['team']} | {r['year']}")

                # Click the PLAYER NAME link
                print("\n--- Clicking PLAYER NAME link ---")
                r["name_el"].click()
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                time.sleep(2)
                print(f"URL after click: {page.url}")
                print(f"Title: {page.title()}")
                page.screenshot(path="data/test_player_clicked.png", full_page=False)
                # Check for scorecard/match data
                content = page.content()
                for kw in ["Scorecard", "Match", "Sets", "W/L", "Date"]:
                    idx = content.find(kw)
                    if idx >= 0:
                        print(f"  Found {kw!r} at {idx}: {content[idx:idx+100]!r}")

                # Go back and click TEAM/FLIGHT link
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
                rows2 = get_result_rows(page)
                nv_rows2 = [r for r in rows2 if ",NV" in r["city_state"]]
                if nv_rows2:
                    r2 = nv_rows2[0]
                    print(f"\n--- Clicking TEAM/FLIGHT link: {r2['team']} ---")
                    r2["team_el"].click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=12_000)
                    except Exception:
                        pass
                    time.sleep(2)
                    print(f"URL after team click: {page.url}")
                    print(f"Title: {page.title()}")
                    page.screenshot(path="data/test_flight_clicked.png", full_page=False)
                    # Look for links on this page
                    for a in page.query_selector_all("a[href]"):
                        href = a.get_attribute("href") or ""
                        txt = (a.inner_text() or "").strip()
                        if re.search(r"Flight|Standings|Scorecard|LocalLeague|TeamID|FlightID", href, re.I) \
                           or re.search(r"Flight|Standings|Scorecard", txt, re.I):
                            print(f"  [{txt[:60]!r}] -> {href[:100]}")

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
