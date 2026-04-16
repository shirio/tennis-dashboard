import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv; load_dotenv()
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://tennislink.usta.com"
def sleep(s=1.2): time.sleep(s)
def wait_idle(page, t=10_000):
    try: page.wait_for_load_state("networkidle", timeout=t)
    except: pass

u = os.getenv("TENNISLINK_USER",""); p = os.getenv("TENNISLINK_PASS","")
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width":1280,"height":900})
    pg = ctx.new_page()
    try:
        pg.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx", wait_until="domcontentloaded", timeout=45_000)
        sleep(2)
        pg.fill("input[name='username']", u)
        pg.keyboard.press("Enter")
        sleep(1.5)
        pg.fill("input[type='password']", p)
        pg.keyboard.press("Enter")
        pg.wait_for_url("**/tennislink.usta.com/**", timeout=25_000)
        wait_idle(pg); sleep(2)

        pg.goto(f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3", wait_until="domcontentloaded", timeout=30_000)
        sleep(2)
        pg.fill("#ctl00_mainContent_txtUSTANum", "2019825517")
        pg.click("#ctl00_mainContent_btnSearchStatsAndStandings")
        wait_idle(pg, 12_000); sleep(1.5)

        for tbl in pg.query_selector_all("table.CommonTable.Segmented"):
            for tr in tbl.query_selector_all("tbody tr"):
                tds = tr.query_selector_all("td")
                if len(tds) < 4: continue
                a = tds[0].query_selector("a")
                if not a: continue
                league = tds[3].inner_text().strip()
                flight = tds[4].inner_text().strip() if len(tds) > 4 else ""
                if "2026" in league and "WOMEN" in flight.upper() and "ADULT" in league.upper():
                    print(f"Clicking: {a.inner_text().strip()} | {flight}")
                    a.click()
                    wait_idle(pg, 12_000); sleep(2)
                    break
            else:
                continue
            break

        print(f"\nURL: {pg.url}")
        # Save HTML
        Path("data/debug_team_full.html").write_text(pg.content())
        pg.screenshot(path="data/debug_team_page2.png")
        print("Saved HTML and screenshot")
        # Count all tables
        all_tbls = pg.query_selector_all("table")
        print(f"Total tables: {len(all_tbls)}")
        for i, tbl in enumerate(pg.query_selector_all("table")):
            try:
                vis = tbl.is_visible()
                txt = tbl.inner_text()
                has_wins = "Wins" in txt
                has_matches = "Matches" in txt
                if not (has_wins and has_matches): continue
                print(f"\nTable {i} visible={vis} — len(txt)={len(txt)}")
                print(f"  txt snippet: {txt[:200]!r}")
                for j, tr in enumerate(tbl.query_selector_all("tr")):
                    cells_td = [td.inner_text().strip()[:25] for td in tr.query_selector_all(":scope > td")]
                    cells_th = [th.inner_text().strip()[:25] for th in tr.query_selector_all(":scope > th")]
                    cells = cells_td if cells_td else cells_th
                    if cells: print(f"  row{j}({len(cells)}): {cells[:6]}")
            except Exception as e:
                print(f"  Table {i} error: {e}")
    finally:
        ctx.close(); b.close()
