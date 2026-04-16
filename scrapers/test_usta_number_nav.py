"""
Test: navigate via USTA number → first 2026 Women's team → extract roster+matches as JSON.
Shows output JSON for that one team only. User must confirm before expanding.
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

def sleep(s=1.2):
    time.sleep(s)

def wait_idle(page, timeout=10_000):
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeoutError:
        pass

def login(page, username, password):
    page.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx", wait_until="domcontentloaded", timeout=45_000)
    sleep(2)
    for sel in ["input[name='username']", "input[name='email']", "input[type='email']"]:
        try:
            page.wait_for_selector(sel, timeout=3_000)
            page.fill(sel, username)
            break
        except: pass
    pw_visible = False
    for sel in ["input[type='password']", "#password"]:
        try:
            if page.is_visible(sel, timeout=500):
                pw_visible = True; break
        except: pass
    if not pw_visible:
        page.keyboard.press("Enter")
        sleep(1.5)
    for sel in ["input[type='password']", "input[name='password']"]:
        try:
            page.wait_for_selector(sel, timeout=5_000)
            page.fill(sel, password)
            break
        except: pass
    page.keyboard.press("Enter")
    try: page.wait_for_url("**/tennislink.usta.com/**", timeout=25_000)
    except: pass
    wait_idle(page)
    sleep(2)


def extract_team_page(page) -> dict:
    """
    Extract standings, match schedule, and roster from a team summary page.
    Returns dict with keys: team_name, flight, league, standings, matches, roster.
    """
    result = {}

    # ── Header info ───────────────────────────────────────────────────────────
    for tbl in page.query_selector_all("table"):
        rows = []
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip().replace('\n', ' ')
                     for td in tr.query_selector_all(":scope > td, :scope > th")]
            if cells: rows.append(cells)
        if not rows: continue
        # Team header table: Section / District / League / Flight/SubFlight
        if rows and len(rows) >= 2 and "Section" in rows[0]:
            for row in rows[1:]:
                if len(row) >= 4 and row[0] and "USTA" in row[0].upper():
                    result["section"]  = row[0]
                    result["district"] = row[1]
                    result["league"]   = row[2]
                    result["flight"]   = row[3]
                    break
        # Team name row: Team Name | Team Number | Season Start | No. Players
        if rows and "Team Name" in (rows[0][0] if rows[0] else ""):
            for row in rows[1:]:
                if len(row) >= 4:
                    result["team_name"]   = row[0]
                    result["num_players"] = row[3]
                    break

    # ── Standings ─────────────────────────────────────────────────────────────
    # The standings table has id="TeamSummary". 13 total columns:
    # [0]=Team Name  [1]=Wins  [2]=Matches Played  [3]=Games Won  [4]=Points
    # [5]=Losses  [6]=Indiv.Wins  [7]=Indiv.Losses  [8]=Sets Won  [9]=Sets Lost
    # [10]=Games Won  [11]=Games Lost  [12]=* Games Won %
    # (Some are CSS display:none but inner_text() returns their content anyway)
    standings = []
    tbl = page.query_selector("#TeamSummary")
    if tbl:
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td")]
            if len(cells) < 6: continue
            # Data rows: cells[0]=Team Name, cells[1]=Wins (digit)
            if cells[0] and re.match(r'\d+', cells[1]):
                standings.append({
                    "team":          cells[0],
                    "wins":          cells[1],
                    "matches":       cells[2] if len(cells) > 2 else "",
                    "losses":        cells[5] if len(cells) > 5 else "",
                    "indiv_wins":    cells[6] if len(cells) > 6 else "",
                    "indiv_losses":  cells[7] if len(cells) > 7 else "",
                    "games_pct":     cells[12] if len(cells) > 12 else cells[-1],
                })
    result["standings"] = standings

    # ── Match schedule (from the 2-column layout table) ───────────────────────
    # Each row has: [Date, '', Opponent, Result, Date, '', Opponent, Result]
    # Then tooltip sub-rows follow (Date:, Team:, Opponent:, Action:)
    matches = []
    for tbl in page.query_selector_all("table"):
        txt = tbl.inner_text()
        if "Opponent" not in txt or "Date" not in txt:
            continue
        # Check header row has double Date/Opponent pattern
        header_rows = tbl.query_selector_all("tr")
        if not header_rows: continue
        first_cells = [td.inner_text().strip() for td in header_rows[0].query_selector_all(":scope > td, :scope > th")]
        if first_cells != ["Date", "", "Opponent", "Result", "Date", "", "Opponent", "Result"]:
            continue
        # Parse data rows
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip().replace('\n', ' ')
                     for td in tr.query_selector_all(":scope > td")]
            if len(cells) < 4: continue
            if cells[0] in ("Date", "Date:") or not cells[0]: continue
            if re.match(r'\d+/\d+/\d+', cells[0]):
                # Left match
                if cells[0] and cells[2]:
                    matches.append({"date": cells[0], "opponent": cells[2], "result": cells[3]})
                # Right match (columns 4-7)
                if len(cells) >= 8 and cells[4] and cells[6]:
                    matches.append({"date": cells[4], "opponent": cells[6], "result": cells[7]})
        if matches:
            break
    result["matches"] = sorted(matches, key=lambda m: m["date"])

    # ── Roster (from the 3-per-row table at bottom of summary page) ──────────
    # Columns: Player Name | NTRP | Player Name | NTRP | Player Name | NTRP
    roster = []
    for tbl in page.query_selector_all("table"):
        txt = tbl.inner_text()
        if "Player Name" not in txt and "NTRP" not in txt:
            continue
        rows_all = []
        for tr in tbl.query_selector_all("tr"):
            cells = [td.inner_text().strip() for td in tr.query_selector_all(":scope > td, :scope > th")]
            if cells: rows_all.append(cells)
        if not rows_all: continue
        # Header: Player Name | NTRP | Player Name | NTRP | ...
        if "Player Name" in rows_all[0]:
            for row in rows_all[1:]:
                # 3 players per row: [name, ntrp, name, ntrp, name, ntrp]
                for i in range(0, len(row) - 1, 2):
                    name = row[i].strip()
                    ntrp = row[i+1].strip() if i+1 < len(row) else ""
                    if name and name not in ("Player Name", "NTRP", ""):
                        roster.append({"name": name, "ntrp": ntrp})
            if roster:
                break
    result["roster"] = roster

    return result


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            login(page, username, password)

            # ── Search USTA# ─────────────────────────────────────────────────
            page.goto(PLAYER_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
            sleep(2)
            page.fill("#ctl00_mainContent_txtUSTANum", MY_USTA_NUMBER)
            page.click("#ctl00_mainContent_btnSearchStatsAndStandings")
            wait_idle(page, 12_000)
            sleep(1.5)

            # ── Collect team rows ─────────────────────────────────────────────
            team_rows = []
            for tbl in page.query_selector_all("table.CommonTable.Segmented"):
                for tr in tbl.query_selector_all("tbody tr"):
                    tds = tr.query_selector_all("td")
                    if len(tds) < 4: continue
                    team_a = tds[0].query_selector("a")
                    if not team_a: continue
                    league = tds[3].inner_text().strip() if len(tds) > 3 else ""
                    flight = tds[4].inner_text().strip() if len(tds) > 4 else ""
                    team_rows.append({
                        "team_name": team_a.inner_text().strip(),
                        "league": league,
                        "flight": flight,
                        "_el": team_a,
                    })

            # Pick first 2026 Adult Women's team
            target = None
            for t in team_rows:
                if "2026" in t["league"] and "WOMEN" in t["flight"].upper() and "ADULT" in t["league"].upper():
                    target = t
                    break
            if not target:
                raise RuntimeError("No 2026 Women's team found in results")

            print(f"Target: {target['team_name']} | {target['flight']}")

            # ── Navigate to team page ─────────────────────────────────────────
            target["_el"].click()
            wait_idle(page, 12_000)
            sleep(2)

            # ── Extract data ──────────────────────────────────────────────────
            data = extract_team_page(page)
            data["source_url"] = page.url
            data["usta_number_searched"] = MY_USTA_NUMBER

            # ── Output JSON ───────────────────────────────────────────────────
            print("\n" + "="*60)
            print("  EXTRACTED JSON FOR ONE TEAM:")
            print("="*60)
            print(json.dumps(data, indent=2, ensure_ascii=False))

            # Also save to file
            out = Path("data/test_one_team.json")
            out.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"\n  Saved to {out}")

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
