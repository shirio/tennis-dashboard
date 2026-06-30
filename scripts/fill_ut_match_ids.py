#!/usr/bin/env python3
"""
Fill missing tl_match_id values for UT matches by navigating to team
schedule pages and extracting ViewScore match IDs.

For each match without a tl_match_id, navigates to one of the teams
involved, finds the match on their schedule, and extracts the match ID
from the ViewScore link.
"""
from __future__ import annotations
import json, os, re, sys
from pathlib import Path
from time import sleep as _sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright, Page
from scrapers.scrape_tennislink import (
    login, _wait_for_network, _navigate_via_league_search,
    _go_to_flight_page, _parse_match_detail_page,
    BASE_URL, DELAY,
)

DATA_DIR = Path("data")
SCORECARD_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx"


def sleep(secs: float = DELAY):
    _sleep(secs)


def _click_postback(page: Page, href: str):
    m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if m:
        page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
        _wait_for_network(page, 15_000)
        sleep(2)


def _navigate_to_team_schedule(page: Page, area: str, ntrp: str,
                                subflight_name: str, team_name: str) -> bool:
    """Navigate to a team's match schedule page."""
    ok = _navigate_via_league_search(page, "Intermountain", "Utah", area, ntrp, 2026)
    if not ok:
        return False
    if not _go_to_flight_page(page):
        return False

    # Click subflight
    sf_links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptSubFlightsForFlightSummary') && href.includes('__doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for sl in sf_links:
        if sl["text"] == subflight_name:
            _click_postback(page, sl["href"])
            break
    else:
        print(f"    [warn] subflight {subflight_name!r} not found in {[s['text'] for s in sf_links]}")
        return False

    # Click team
    team_links = page.evaluate("""() => {
        const tbl = document.getElementById('TeamSummary');
        const container = tbl || document;
        return Array.from(container.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('doPostBack') && a.innerText.trim().length > 3;
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for tl in team_links:
        if team_name.lower() in tl["text"].lower():
            _click_postback(page, tl["href"])
            break
    else:
        # Try partial match
        for tl in team_links:
            if any(w in tl["text"].lower() for w in team_name.lower().split("-")[:1]):
                _click_postback(page, tl["href"])
                break
        else:
            print(f"    [warn] team {team_name!r} not found")
            return False

    # Click Match Schedule tab
    for a in page.query_selector_all("a"):
        try:
            txt = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
            if txt == "Match Schedule" and "doPostBack" in href:
                a.click()
                _wait_for_network(page, 12_000)
                sleep(2)
                break
        except Exception:
            pass

    return True


def _extract_schedule_match_ids(page: Page) -> list[dict]:
    """Extract match IDs from ViewScore links on the schedule page."""
    return page.evaluate("""() => {
        const results = [];
        for (const a of document.querySelectorAll('a')) {
            const onclick = a.getAttribute('onclick') || '';
            const m = onclick.match(/ViewScore\\((\\d+)/);
            if (m) {
                // Find the row context
                const tr = a.closest('tr');
                let date = '', opponent = '';
                if (tr) {
                    const tds = Array.from(tr.querySelectorAll('td'));
                    if (tds.length >= 2) date = tds[0].innerText.trim();
                    if (tds.length >= 3) opponent = tds[2].innerText.trim();
                }
                results.push({
                    matchId: parseInt(m[1]),
                    date: date,
                    opponent: opponent,
                });
            }
        }
        return results;
    }""")


def _get_area_for_subflight(sf_label: str) -> str:
    if "PM" in sf_label:
        return "UT-PM"
    return "UT-AM"


def _get_ntrp_for_file(filename: str) -> str:
    if "_35" in filename:
        return "3.5"
    return "3.0"


def _normalize_date(d: str) -> str:
    """Normalize date for comparison: strip leading zeros."""
    parts = d.strip().split("/")
    if len(parts) == 3:
        return f"{int(parts[0])}/{int(parts[1])}/{parts[2]}"
    return d.strip()


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    # Find matches needing IDs
    work = []
    for fname in ["standings_ut_30.json", "standings_ut_35.json"]:
        path = DATA_DIR / fname
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        matches_needing = []
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if not m.get("tl_match_id") and m.get("lines"):
                    if any(ln.get("court_winner") is None for ln in m["lines"]):
                        matches_needing.append((sf, m))
        if matches_needing:
            work.append((path, data, matches_needing))
            print(f"{fname}: {len(matches_needing)} matches need IDs")

    if not work:
        print("All matches have IDs!")
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            login(page, username, password)

            total_found = 0
            total_winners = 0

            for path, data, matches_needing in work:
                ntrp = _get_ntrp_for_file(path.name)
                print(f"\n{'='*60}")
                print(f"Processing {path.name} ({len(matches_needing)} matches, NTRP={ntrp})")

                # Group by team to minimize navigation
                team_matches: dict[str, list[tuple[dict, dict]]] = {}
                for sf, m in matches_needing:
                    # Use home_team as the navigation target
                    team = m.get("home_team", "")
                    sf_label = sf.get("flight_label", "")
                    key = f"{sf_label}|{team}"
                    team_matches.setdefault(key, []).append((sf, m))

                for key, items in team_matches.items():
                    sf_label, team = key.split("|", 1)
                    area = _get_area_for_subflight(sf_label)
                    # Extract subflight display name from flight_label
                    # e.g. "UT-AM 3.0W AM Teal" -> "3.0W AM Teal"
                    sf_display = sf_label
                    for prefix in ("UT-AM ", "UT-PM "):
                        sf_display = sf_display.replace(prefix, "")

                    print(f"\n  Navigating to {team} in {sf_label}...")
                    ok = _navigate_to_team_schedule(page, area, ntrp, sf_display, team)
                    if not ok:
                        print(f"    Failed to navigate")
                        continue

                    schedule = _extract_schedule_match_ids(page)
                    print(f"    Found {len(schedule)} matches on schedule")

                    for sf_obj, m in items:
                        m_date = _normalize_date(m.get("date", ""))
                        m_away = m.get("away_team", "")

                        # Find matching schedule entry
                        for sched in schedule:
                            s_date = _normalize_date(sched["date"])
                            if s_date == m_date and (
                                m_away.lower() in sched["opponent"].lower() or
                                sched["opponent"].lower() in m_away.lower()
                            ):
                                m["tl_match_id"] = sched["matchId"]
                                total_found += 1
                                print(f"    {m_date} vs {m_away}: ID={sched['matchId']}")

                                # Now scrape the scorecard
                                url = f"{SCORECARD_URL}?t=12&par1={sched['matchId']}&par2=0&par3=0"
                                try:
                                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                                    _wait_for_network(page, 15_000)
                                    sleep(1.5)
                                    lines = _parse_match_detail_page(page)
                                    if lines:
                                        for el in m.get("lines", []):
                                            if el.get("court_winner") is not None:
                                                continue
                                            for sl in lines:
                                                if sl.get("line") == el.get("line") and sl.get("result"):
                                                    el["court_winner"] = sl["result"]
                                                    total_winners += 1
                                                    break
                                except Exception as e:
                                    print(f"      Error scraping: {e}")
                                break

                # Save
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                remaining = sum(
                    1 for sf_obj in data.get("subflights", [])
                    for m2 in sf_obj.get("matches", [])
                    for ln in m2.get("lines", [])
                    if ln.get("court_winner") is None
                )
                print(f"\n  Saved {path.name}: {remaining} unknown courts remaining")

            print(f"\n{'='*60}")
            print(f"Found {total_found} match IDs, applied {total_winners} court winners")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
