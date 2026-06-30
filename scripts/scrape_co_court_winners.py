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
from engine.normalize import normalize_standings_file
from scrapers.scrape_tennislink import (
    login, _wait_for_network, _navigate_via_league_search,
    _go_to_flight_page, _parse_match_detail_page,
    _extract_team_matches, _get_state_config,
    _scrape_championship_page,
    STANDINGS_SEARCH_URL, abs_url,
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


def _navigate_to_team_direct_search(page: Page, ntrp: str, team_name: str) -> bool:
    """
    Search TennisLink directly by team name (bypasses flight/subflight hierarchy).
    Used as a last-resort fallback when subflight navigation fails.
    """
    page.goto(STANDINGS_SEARCH_URL, wait_until="domcontentloaded", timeout=30_000)
    sleep(1)

    def _js_set_by_text(short_id: str, match_text: str, postback: bool = False):
        full = f"ctl00_mainContent_{short_id}"
        page.evaluate(f"""(() => {{
            const el = document.getElementById('{full}');
            if (!el) return;
            const target = {json.dumps(match_text.lower())};
            for (const opt of el.options) {{
                if (opt.text.toLowerCase().includes(target)) {{ el.value = opt.value; break; }}
            }}
        }})()""")
        if postback:
            name = f"ctl00$mainContent${short_id}"
            page.evaluate(f"__doPostBack('{name}', '')")
            _wait_for_network(page, 8_000)
            sleep(1)

    _js_set_by_text("ddlDivisionForTeams", "adult 18", postback=True)
    _js_set_by_text("ddlSection", "intermountain", postback=True)
    _js_set_by_text("ddlDivisionForTeams", "adult 18")
    page.evaluate("""(() => {
        const el = document.getElementById('ctl00_mainContent_ddlChampYear');
        if (!el) return;
        for (const opt of el.options) { if (opt.text.includes('2026')) { el.value = opt.value; break; } }
    })()""")
    _js_set_by_text("ddlNTRPLevel", ntrp)
    _js_set_by_text("ddlGender", "female")
    for maybe_id in ["ddlDistrict", "ddlDistrictForTeams"]:
        exists = page.evaluate(f"!!document.getElementById('ctl00_mainContent_{maybe_id}')")
        if exists:
            _js_set_by_text(maybe_id, "colorado")
            break

    page.evaluate("__doPostBack('ctl00$mainContent$btnSearchTeamByName', '')")
    _wait_for_network(page, 15_000)
    sleep(3)

    rows = page.evaluate("""(() => {
        const results = [];
        document.querySelectorAll('table tr').forEach(tr => {
            const tds = Array.from(tr.querySelectorAll('td'));
            if (tds.length >= 4) {
                const link = tds[0].querySelector('a');
                if (link && link.innerText.trim().length > 2)
                    results.push({team: link.innerText.trim(), href: link.getAttribute('href') || ''});
            }
        });
        return results;
    })()""")

    team_lower = team_name.lower()
    for r in rows:
        r_lower = r["team"].lower()
        if r_lower == team_lower or team_lower in r_lower or r_lower in team_lower:
            href = r["href"]
            if href.startswith("javascript:"):
                page.evaluate(href)
            else:
                page.goto(abs_url(href), wait_until="domcontentloaded", timeout=30_000)
            _wait_for_network(page, 12_000)
            sleep(2)
            print(f"    [direct-search] found and navigated to {r['team']!r}")
            return True

    print(f"    [direct-search] {team_name!r} not found in search results ({len(rows)} rows)")
    return False


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


def _is_championship_sf(sf_label: str) -> bool:
    return "championship" in sf_label.lower()


def _get_advancement_links(page: Page) -> list[dict]:
    """Return rptChampAdvancementForTeamSummary postback links from the current page."""
    return page.evaluate("""() => {
        const results = [];
        for (const a of document.querySelectorAll('a')) {
            const href = a.getAttribute('href') || '';
            if (href.includes('rptChampAdvancementForTeamSummary') && href.includes('doPostBack')) {
                results.push({text: a.innerText.trim(), href});
            }
        }
        return results;
    }""")


def _find_regular_season_sf(data: dict, team: str) -> dict | None:
    """Return the first non-championship subflight that contains this team."""
    for sf in data.get("subflights", []):
        if _is_championship_sf(sf["flight_label"]):
            continue
        for m in sf.get("matches", []):
            if team in (m.get("home_team", ""), m.get("away_team", "")):
                return sf
    return None


def _get_champ_courts_for_team(
    page: Page, team: str, ntrp: str,
    reg_area: str, reg_sf_display: str,
    all_champ_matches: list[dict],
) -> int:
    """
    Navigate to team's regular-season page, find championship advancement links,
    follow each to the team's championship schedule, and scrape court winners.
    Returns total courts filled.
    """
    # First pass: collect all advancement links from the regular-season page
    if not _navigate_to_subflight_team(page, reg_area, ntrp, reg_sf_display, team):
        if not _navigate_to_team_in_area(page, reg_area, ntrp, team):
            if not _navigate_to_team_direct_search(page, ntrp, team):
                return 0
    adv_links = _get_advancement_links(page)
    if not adv_links:
        print(f"      no championship advancement links found")
        return 0

    print(f"      {len(adv_links)} advancement link(s): {[a['text'] for a in adv_links]}")
    total = 0

    for adv in adv_links:
        # Re-navigate to the regular-season page for each link (reliable base)
        if not _navigate_to_subflight_team(page, reg_area, ntrp, reg_sf_display, team):
            if not _navigate_to_team_in_area(page, reg_area, ntrp, team):
                if not _navigate_to_team_direct_search(page, ntrp, team):
                    continue

        # Click advancement link → championship standings page for this level
        _click_postback(page, adv["href"])

        # Scrape the championship page (handles View Score clicks internally)
        result = _scrape_championship_page(page)
        if result is None:
            print(f"      [{adv['text']}] _scrape_championship_page returned None")
            continue

        _, champ_matches = result
        print(f"      [{adv['text']}] {len(champ_matches)} matches on championship page")

        team_lower = team.lower()
        for cm in champ_matches:
            cm_home = cm.get("home_team", "")
            cm_away = cm.get("away_team", "")
            # Only process matches involving our team
            if (team_lower not in cm_home.lower() and cm_home.lower() not in team_lower and
                    team_lower not in cm_away.lower() and cm_away.lower() not in team_lower):
                continue

            cm_lines = cm.get("lines", [])
            if not cm_lines:
                continue

            cm_date = _normalize_date(cm.get("date", ""))

            # Find matching entry in all_champ_matches
            for m in all_champ_matches:
                if not any(ln.get("court_winner") is None for ln in m.get("lines", [])):
                    continue
                m_date = _normalize_date(m.get("date", ""))
                if m_date != cm_date:
                    continue
                mh = m.get("home_team", "")
                ma = m.get("away_team", "")
                if (cm_home.lower() in mh.lower() or mh.lower() in cm_home.lower() or
                        cm_away.lower() in ma.lower() or ma.lower() in cm_away.lower() or
                        cm_home.lower() in ma.lower() or cm_away.lower() in mh.lower()):
                    applied = _merge_court_winners(m.get("lines", []), cm_lines)
                    total += applied
                    break

    return total


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
        champ_teams_seen: set[str] = set()

        for sf in data.get("subflights", []):
            is_champ = _is_championship_sf(sf["flight_label"])
            teams_matches: dict[str, list[dict]] = {}
            for m in sf.get("matches", []):
                if not any(ln.get("court_winner") is None for ln in m.get("lines", [])):
                    continue
                for team_field in ("home_team", "away_team"):
                    t = m.get(team_field, "")
                    if t:
                        teams_matches.setdefault(t, []).append(m)

            for team, matches in teams_matches.items():
                if is_champ:
                    # Collect ALL championship matches for this team in one entry
                    # so we handle every advancement link in one navigation session.
                    if team in champ_teams_seen:
                        continue
                    champ_teams_seen.add(team)

                    # Gather every championship match needing winners for this team
                    all_champ = []
                    for sf2 in data.get("subflights", []):
                        if not _is_championship_sf(sf2["flight_label"]):
                            continue
                        for m2 in sf2.get("matches", []):
                            if not any(ln.get("court_winner") is None
                                       for ln in m2.get("lines", [])):
                                continue
                            if team in (m2.get("home_team", ""), m2.get("away_team", "")):
                                all_champ.append(m2)

                    reg_sf = _find_regular_season_sf(data, team)
                    if not reg_sf:
                        print(f"  [warn] {team}: championship team with no regular season sf")
                        continue

                    team_work.append({
                        "sf_label": "Championships",
                        "team": team,
                        "matches": all_champ,
                        "area": None,
                        "sf_display": None,
                        "is_championship": True,
                        "reg_sf_area": _area_for_subflight(reg_sf["flight_label"]),
                        "reg_sf_display": _subflight_display_name(reg_sf["flight_label"]),
                    })
                else:
                    team_work.append({
                        "sf_label": sf["flight_label"],
                        "team": team,
                        "matches": matches,
                        "area": _area_for_subflight(sf["flight_label"]),
                        "sf_display": _subflight_display_name(sf["flight_label"]),
                        "is_championship": False,
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
                    is_champ = tw.get("is_championship", False)

                    team_key = f"{sf_label}|{team}"
                    if team_key in seen_teams:
                        continue
                    seen_teams.add(team_key)

                    teams_visited += 1

                    if is_champ:
                        # Championship: navigate via regular-season page → advancement links
                        print(f"  [{teams_visited}] {team} [champ]:")
                        team_applied = _get_champ_courts_for_team(
                            page, team, ntrp,
                            tw["reg_sf_area"], tw["reg_sf_display"],
                            tw["matches"],
                        )
                        file_applied += team_applied
                        grand_total += team_applied
                        if team_applied or teams_visited % 10 == 0:
                            print(f"    +{team_applied} courts (total: {grand_total})")
                        # Periodic save after championship teams too
                        if teams_visited % args.save_every == 0:
                            path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                        continue

                    area = tw["area"]
                    sf_display = tw["sf_display"]

                    # Navigate to team (with fallbacks if subflight not found)
                    ok = _navigate_to_subflight_team(page, area, ntrp, sf_display, team)
                    if not ok:
                        ok = _navigate_to_team_in_area(page, area, ntrp, team)
                    if not ok:
                        ok = _navigate_to_team_direct_search(page, ntrp, team)
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
                normalize_standings_file(path)
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
