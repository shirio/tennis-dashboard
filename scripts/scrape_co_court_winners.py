#!/usr/bin/env python3
"""
Scrape per-court winners for CO matches via team schedule date links.

Navigates to each team with unknown court winners, clicks through their
match schedule date links to reach scorecards, and extracts mark.gif
court winner indicators.

Usage:
    python3 scripts/scrape_co_court_winners.py
    python3 scripts/scrape_co_court_winners.py --div 30   # just 3.0
"""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path
from time import sleep as _sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright, Page
from scrapers.scrape_tennislink import (
    login, _wait_for_network, _navigate_via_league_search,
    _go_to_flight_page, _parse_match_detail_page,
    _extract_team_matches, _get_state_config,
    DELAY,
)

DATA_DIR = Path("data")


def sleep(secs: float = DELAY):
    _sleep(secs)


def _click_postback(page: Page, href: str):
    m = re.search(r"__doPostBack\('([^']+)','([^']*)'\)", href)
    if m:
        page.evaluate(f"__doPostBack('{m.group(1)}', '{m.group(2)}')")
        _wait_for_network(page, 15_000)
        sleep(2)


def _area_for_subflight(sf_label: str) -> str:
    """Map subflight label to CO area name."""
    sl = sf_label.upper()
    if "MOUNTAINS" in sl:
        return "CO-MOUNTAINS"
    if "NORTHERN" in sl or "NOCO" in sl:
        return "CO-NORTHERN COLORADO"
    if "SOUTHERN" in sl or "SOCO" in sl:
        return "CO-SOUTHERN COLORADO"
    if "WESTERN" in sl or "WS " in sl:
        return "CO-WESTERN SLOPE"
    if "CHAMPIONSHIP" in sl:
        return "CO-DENVER METRO"
    return "CO-DENVER METRO"


def _subflight_display_name(sf_label: str) -> str:
    """Extract display name for subflight navigation.
    e.g. 'CO-DENVER METRO SOUTH I' -> 'SOUTH I'
    """
    for prefix in ("CO-DENVER METRO ", "CO-MOUNTAINS ", "CO-NORTHERN COLORADO ",
                    "CO-SOUTHERN COLORADO ", "CO-WESTERN SLOPE "):
        if sf_label.upper().startswith(prefix.upper()):
            return sf_label[len(prefix):]
    return sf_label


def _navigate_to_team_in_area(page: Page, area: str, ntrp: str, team_name: str) -> bool:
    """Navigate to a specific team page in the given area."""
    ok = _navigate_via_league_search(page, "Intermountain", "Colorado", area, ntrp, 2026)
    if not ok:
        return False

    # Go to flight page
    if not _go_to_flight_page(page):
        # We might be on a team page already — check if it's the right team
        return False

    # Find the subflight containing this team, or try Team Standings
    # Click Team Standings tab
    for a in page.query_selector_all("a"):
        try:
            txt = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
            if txt == "Team Standings" and "doPostBack" in href:
                a.click()
                _wait_for_network(page, 12_000)
                sleep(1)
                break
        except Exception:
            pass

    return _click_team_link(page, team_name)


def _navigate_to_subflight_team(page: Page, area: str, ntrp: str,
                                 sf_display: str, team_name: str) -> bool:
    """Navigate to a team via area -> flight -> subflight -> team."""
    ok = False
    for attempt in range(3):
        try:
            ok = _navigate_via_league_search(page, "Intermountain", "Colorado", area, ntrp, 2026)
            if ok:
                break
        except Exception as e:
            print(f"    [retry {attempt+1}/3] navigation error: {e}")
            sleep(5)
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

    clicked = False
    for sl in sf_links:
        if sl["text"].upper() == sf_display.upper() or sf_display.upper() in sl["text"].upper():
            _click_postback(page, sl["href"])
            clicked = True
            break

    if not clicked:
        # Try single-letter subflight links
        for a in page.query_selector_all("a"):
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                if txt.upper() == sf_display.upper() and "doPostBack" in href:
                    _click_postback(page, href)
                    clicked = True
                    break
            except Exception:
                pass

    if not clicked:
        return False

    return _click_team_link(page, team_name)


def _click_team_link(page: Page, team_name: str) -> bool:
    """Click a team link on the current standings page."""
    team_links = page.evaluate("""() => {
        const tbl = document.getElementById('TeamSummary');
        const container = tbl || document;
        return Array.from(container.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('doPostBack') && a.innerText.trim().length > 2;
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")

    # Exact match first
    for tl in team_links:
        if tl["text"] == team_name:
            _click_postback(page, tl["href"])
            return True

    # Partial match
    for tl in team_links:
        if team_name.lower() in tl["text"].lower() or tl["text"].lower() in team_name.lower():
            _click_postback(page, tl["href"])
            return True

    return False


def _merge_court_winners(existing_lines: list[dict], scraped_lines: list[dict]) -> int:
    applied = 0
    for el in existing_lines:
        if el.get("court_winner") is not None:
            continue
        line_label = el.get("line", "")
        for sl in scraped_lines:
            if sl.get("line") == line_label and sl.get("result"):
                el["court_winner"] = sl["result"]
                applied += 1
                break
    return applied


def _normalize_date(d: str) -> str:
    parts = d.strip().split("/")
    if len(parts) == 3:
        return f"{int(parts[0])}/{int(parts[1])}/{parts[2]}"
    return d.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--div", default=None, help="Division (30 or 35)")
    parser.add_argument("--save-every", type=int, default=10)
    args = parser.parse_args()

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    divs = [args.div] if args.div else ["30", "35"]

    # Build work list: (path, data, [{sf, team, matches_needing}])
    all_work = []
    for div in divs:
        path = DATA_DIR / f"standings_co_{div}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        ntrp = f"{div[0]}.{div[1]}"

        team_work = []
        for sf in data.get("subflights", []):
            teams_matches: dict[str, list[dict]] = {}
            for m in sf.get("matches", []):
                if not any(ln.get("court_winner") is None for ln in m.get("lines", [])):
                    continue
                for team_field in ("home_team", "away_team"):
                    t = m.get(team_field, "")
                    if t:
                        teams_matches.setdefault(t, []).append(m)

            for team, matches in teams_matches.items():
                # Only include if this team hasn't been added yet (avoid dups)
                team_work.append({
                    "sf_label": sf["flight_label"],
                    "team": team,
                    "matches": matches,
                    "area": _area_for_subflight(sf["flight_label"]),
                    "sf_display": _subflight_display_name(sf["flight_label"]),
                })

        if team_work:
            all_work.append((path, data, ntrp, team_work))
            total_matches = sum(len(tw["matches"]) for tw in team_work)
            print(f"{path.name}: {len(team_work)} team visits, {total_matches} matches need winners")

    if not all_work:
        print("All courts already have winners!")
        return

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            login(page, username, password)

            grand_total = 0
            teams_visited = 0

            for path, data, ntrp, team_work in all_work:
                print(f"\n{'='*60}")
                print(f"Processing {path.name} (NTRP={ntrp})")
                print(f"{'='*60}")

                file_applied = 0
                # De-duplicate teams (same team might appear in home and away)
                seen_teams = set()

                for tw in team_work:
                    team = tw["team"]
                    sf_label = tw["sf_label"]

                    team_key = f"{sf_label}|{team}"
                    if team_key in seen_teams:
                        continue
                    seen_teams.add(team_key)

                    teams_visited += 1
                    area = tw["area"]
                    sf_display = tw["sf_display"]

                    # Navigate to team
                    ok = _navigate_to_subflight_team(page, area, ntrp, sf_display, team)
                    if not ok:
                        print(f"  [{teams_visited}] {team} in {sf_label}: FAILED to navigate")
                        continue

                    # Get team schedule
                    schedule = _extract_team_matches(page, team)
                    if not schedule:
                        print(f"  [{teams_visited}] {team}: no schedule found")
                        continue

                    team_applied = 0
                    for sched_match in schedule:
                        if not sched_match.get("_date_link_href"):
                            continue

                        s_date = _normalize_date(sched_match["date"])
                        s_opp = sched_match["opponent"]

                        # Find matching standings match that needs court winners
                        match_obj = None
                        for sf in data.get("subflights", []):
                            if sf["flight_label"] != sf_label:
                                continue
                            for m in sf.get("matches", []):
                                if not any(ln.get("court_winner") is None for ln in m.get("lines", [])):
                                    continue
                                m_date = _normalize_date(m.get("date", ""))
                                if m_date != s_date:
                                    continue
                                mh = m.get("home_team", "")
                                ma = m.get("away_team", "")
                                if (team in (mh, ma)) and (
                                    s_opp.lower() in mh.lower() or s_opp.lower() in ma.lower() or
                                    mh.lower() in s_opp.lower() or ma.lower() in s_opp.lower()
                                ):
                                    match_obj = m
                                    break
                            if match_obj:
                                break

                        if not match_obj:
                            continue

                        # Click date link to get scorecard
                        href = sched_match["_date_link_href"]
                        try:
                            _click_postback(page, href)
                            lines = _parse_match_detail_page(page)
                            if lines:
                                applied = _merge_court_winners(match_obj.get("lines", []), lines)
                                team_applied += applied
                                file_applied += applied
                                grand_total += applied

                            page.go_back(wait_until="domcontentloaded", timeout=15_000)
                            _wait_for_network(page, 10_000)
                            sleep(0.8)
                        except Exception as e:
                            print(f"    Error on {s_date} vs {s_opp}: {e}")
                            try:
                                page.go_back(wait_until="domcontentloaded", timeout=10_000)
                                sleep(1)
                            except Exception:
                                pass

                    if team_applied or teams_visited % 10 == 0:
                        print(f"  [{teams_visited}] {team}: +{team_applied} courts (total: {grand_total})")

                    # Save periodically
                    if teams_visited % args.save_every == 0:
                        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                        remaining = sum(
                            1 for sf in data.get("subflights", [])
                            for m2 in sf.get("matches", [])
                            for ln in m2.get("lines", [])
                            if ln.get("court_winner") is None
                        )
                        print(f"  [checkpoint] Saved {path.name} ({remaining} still unknown)")

                # Final save
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                remaining = sum(
                    1 for sf in data.get("subflights", [])
                    for m2 in sf.get("matches", [])
                    for ln in m2.get("lines", [])
                    if ln.get("court_winner") is None
                )
                print(f"\n  {path.name}: +{file_applied} courts, {remaining} still unknown")

            print(f"\n{'='*60}")
            print(f"DONE: +{grand_total} court winners, {teams_visited} teams visited")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
