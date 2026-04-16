"""
Test: navigate from my team page → Flight link → find Subflight B → show its teams.
"""
import os, sys, json, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://tennislink.usta.com"
PLAYER_SEARCH_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"
MY_USTA_NUMBER = "2019825517"

def sleep(s=1.2): time.sleep(s)
def wait_idle(page, t=10_000):
    try: page.wait_for_load_state("networkidle", timeout=t)
    except PWTimeoutError: pass

def login(page, u, p):
    page.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx", wait_until="domcontentloaded", timeout=45_000)
    sleep(2)
    for sel in ["input[name='username']", "input[name='email']"]:
        try: page.wait_for_selector(sel, timeout=3_000); page.fill(sel, u); break
        except: pass
    pw_vis = any(page.is_visible(s) for s in ["input[type='password']"] if True)
    try: pw_vis = page.is_visible("input[type='password']", timeout=500)
    except: pw_vis = False
    if not pw_vis:
        page.keyboard.press("Enter"); sleep(1.5)
    for sel in ["input[type='password']", "input[name='password']"]:
        try: page.wait_for_selector(sel, timeout=5_000); page.fill(sel, p); break
        except: pass
    page.keyboard.press("Enter")
    try: page.wait_for_url("**/tennislink.usta.com/**", timeout=25_000)
    except: pass
    wait_idle(page); sleep(2)
    print(f"  [login] {page.url}")

def main():
    u = os.getenv("TENNISLINK_USER", "")
    p = os.getenv("TENNISLINK_PASS", "")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            login(page, u, p)

            # ── 1. USTA# search → click DTC #3 (3.0 Women) ──────────────────
            page.goto(PLAYER_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
            sleep(2)
            page.fill("#ctl00_mainContent_txtUSTANum", MY_USTA_NUMBER)
            page.click("#ctl00_mainContent_btnSearchStatsAndStandings")
            wait_idle(page, 12_000); sleep(1.5)

            for tbl in page.query_selector_all("table.CommonTable.Segmented"):
                for tr in tbl.query_selector_all("tbody tr"):
                    tds = tr.query_selector_all("td")
                    if len(tds) < 4: continue
                    a = tds[0].query_selector("a")
                    if not a: continue
                    league = tds[3].inner_text().strip()
                    flight = tds[4].inner_text().strip() if len(tds) > 4 else ""
                    if "2026" in league and "3.0" in flight and "WOMEN" in flight.upper() and "ADULT" in league.upper():
                        print(f"\n[1] Clicking team: {a.inner_text().strip()} | {flight}")
                        a.click(); wait_idle(page, 12_000); sleep(2)
                        break
                else: continue
                break

            print(f"    Team page URL: {page.url}")

            # ── 2. Click the Flight link ("3.0 WOMEN- 18 & OVER") ───────────
            print("\n[2] Looking for Flight link ...")
            flight_link = page.query_selector("#ctl00_mainContent_lnkFlightForTeams")
            if flight_link:
                txt = flight_link.inner_text().strip()
                print(f"    Clicking Flight link: {txt!r}")
                flight_link.click()
                wait_idle(page, 12_000); sleep(2)
            else:
                # Try by text content
                for a in page.query_selector_all("a"):
                    try:
                        txt = a.inner_text().strip()
                        href = a.get_attribute("href") or ""
                        if "WOMEN" in txt.upper() and "lnkFlight" in href:
                            print(f"    Clicking: {txt!r}")
                            a.click(); wait_idle(page, 12_000); sleep(2)
                            break
                    except: pass
                else:
                    print("    Flight link not found by ID. Trying SubFlight link ...")
                    sf_link = page.query_selector("#ctl00_mainContent_lnkSubFlightForTeams")
                    if sf_link:
                        print(f"    Clicking SubFlight link: {sf_link.inner_text().strip()!r}")
                        sf_link.click()
                        wait_idle(page, 12_000); sleep(2)

            print(f"    After click URL: {page.url}")
            page.screenshot(path="data/debug_flight_page.png")
            print(f"    Page title: {page.title()}")

            # ── 3. Show all links on the flight page ──────────────────────────
            print("\n[3] Links on flight/subflight page:")
            for a in page.query_selector_all("a"):
                try:
                    txt = a.inner_text().strip()
                    href = a.get_attribute("href") or ""
                    if txt and len(txt) < 80 and "usta.com/en" not in href and "ustafoundation" not in href:
                        print(f"    {txt!r}: {href[:80]!r}")
                except: pass

            # ── 4. Show page text to understand structure ─────────────────────
            print("\n[4] Page text (first 1000 chars):")
            body = page.query_selector("body")
            if body: print(body.inner_text()[:1000])

            # ── 5. Look for Subflight B link ──────────────────────────────────
            print("\n[5] Looking for Subflight B link ...")
            subflight_b_el = None
            for a in page.query_selector_all("a"):
                try:
                    txt = a.inner_text().strip()
                    href = a.get_attribute("href") or ""
                    if "/ B" in txt or txt == "B" or (href and "par3=1" in href) or "Subflight B" in txt:
                        print(f"    Found B link: {txt!r}: {href[:80]!r}")
                        subflight_b_el = a
                        break
                except: pass

            if not subflight_b_el:
                print("    Could not find / B link by text. Trying tables ...")
                for tbl in page.query_selector_all("table"):
                    try:
                        if not tbl.is_visible(): continue
                        rows = []
                        for tr in tbl.query_selector_all("tr"):
                            cells = [td.inner_text().strip()[:40] for td in tr.query_selector_all(":scope > td, :scope > th")]
                            if cells: rows.append(cells)
                        if rows:
                            print(f"    Table ({len(rows)} rows): {rows[:4]}")
                    except: pass
                return

            # ── 6. Click Subflight B ──────────────────────────────────────────
            print(f"\n[6] Clicking Subflight B ...")
            subflight_b_el.click()
            wait_idle(page, 12_000); sleep(2)
            print(f"    Subflight B URL: {page.url}")
            page.screenshot(path="data/debug_subflight_b.png")

            # ── 7. Extract team standings from B ─────────────────────────────
            print("\n[7] Teams in Subflight B (from #TeamSummary standings table):")
            tbl = page.query_selector("#TeamSummary")
            if tbl:
                for tr in tbl.query_selector_all("tr"):
                    cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
                    if len(cells) >= 3 and cells[0] and re.match(r'\d+', cells[1]):
                        print(f"    {cells[0]}: {cells[1]}W-{cells[5]}L")
            else:
                print("    #TeamSummary not found")
                # Show first few tables
                for i, t in enumerate(page.query_selector_all("table")):
                    if not t.is_visible(): continue
                    rows = []
                    for tr in t.query_selector_all("tr"):
                        c = [td.inner_text().strip()[:30] for td in tr.query_selector_all(":scope > td")]
                        if c: rows.append(c)
                    if len(rows) > 1:
                        print(f"    Table {i}: {rows[:3]}")

            # Also show header info and look for team links to click into
            print("\n[8] Finding team links on Subflight B page ...")
            team_links_b = []
            # Team links are inside the teams table (not in navigation)
            # Find all tables and look for links within them
            NAV_SKIP = {"A","B","Summary","Team Standings","Match Summary","Match Schedule",
                        "Player Roster","Player Counts","Send To Excel","Print Report",
                        "Link to this Page","Send to Excel","> Stats & Standings",
                        "2026 USTA ADULT LEAGUE 18 & OVER","3.0 WOMEN- 18 & OVER",
                        "3.5 WOMEN- 18 & OVER"}
            for tbl in page.query_selector_all("table"):
                if not tbl.is_visible(): continue
                for a in tbl.query_selector_all("a"):
                    try:
                        txt = a.inner_text().strip()
                        href = a.get_attribute("href") or ""
                        if "javascript:__doPostBack" in href and txt and txt not in NAV_SKIP:
                            team_links_b.append((txt, a))
                            print(f"    Team link: {txt!r}")
                    except: pass
                if team_links_b:
                    break

            if team_links_b:
                first_team_b, first_el_b = team_links_b[0]
                print(f"\n[9] Clicking first B team: {first_team_b!r} ...")
                first_el_b.click()
                wait_idle(page, 12_000); sleep(2)
                print(f"    URL: {page.url}")

                # Check #TeamSummary
                tbl_b = page.query_selector("#TeamSummary")
                if tbl_b:
                    print("    #TeamSummary found! Standings:")
                    for tr in tbl_b.query_selector_all("tr"):
                        cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
                        if len(cells) >= 6 and cells[0] and re.match(r'\d+', cells[1]):
                            print(f"      {cells[0]}: {cells[1]}W-{cells[5]}L  indiv:{cells[6]}-{cells[7]}")
                else:
                    print("    #TeamSummary NOT found on B team page")
                    print("    Page title:", page.title())
                    for i, tbl in enumerate(page.query_selector_all("table")):
                        if not tbl.is_visible(): continue
                        rows = []
                        for tr in tbl.query_selector_all("tr"):
                            c = [td.inner_text().strip()[:30] for td in tr.query_selector_all(":scope > td")]
                            if c: rows.append(c)
                        if len(rows) > 1:
                            print(f"    Table {i} ({len(rows)} rows): {rows[:3]}")
                            if i > 5: break

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
