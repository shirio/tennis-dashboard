#!/usr/bin/env python3
"""
Scrape per-court winners for UT matches via team schedule date links.
Same approach as scrape_co_court_winners.py but for UT areas (AM/PM).
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
    _extract_team_matches,
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
    if "PM" in sf_label:
        return "UT-PM"
    return "UT-AM"


def _subflight_display_name(sf_label: str) -> str:
    for prefix in ("UT-AM ", "UT-PM "):
        if sf_label.startswith(prefix):
            return sf_label[len(prefix):]
    return sf_label


def _click_team_link(page: Page, team_name: str) -> bool:
    team_links = page.evaluate("""() => {
        const tbl = document.getElementById('TeamSummary');
        const container = tbl || document;
        return Array.from(container.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('doPostBack') && a.innerText.trim().length > 2;
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for tl in team_links:
        if tl["text"] == team_name or team_name.lower() in tl["text"].lower():
            _click_postback(page, tl["href"])
            return True
    return False


def _navigate_to_subflight_team(page: Page, area: str, ntrp: str,
                                 sf_display: str, team_name: str) -> bool:
    ok = False
    for attempt in range(3):
        try:
            ok = _navigate_via_league_search(page, "Intermountain", "Utah", area, ntrp, 2026)
            if ok:
                break
        except Exception as e:
            print(f"    [retry {attempt+1}/3] navigation error: {e}")
            sleep(5)
    if not ok:
        return False
    if not _go_to_flight_page(page):
        return False

    sf_links = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('a')).filter(a => {
            const href = a.getAttribute('href') || '';
            return href.includes('rptSubFlightsForFlightSummary') && href.includes('__doPostBack');
        }).map(a => ({text: a.innerText.trim(), href: a.getAttribute('href')}));
    }""")
    for sl in sf_links:
        if sl["text"] == sf_display or sf_display in sl["text"]:
            _click_postback(page, sl["href"])
            break
    else:
        print(f"    subflight {sf_display!r} not found in {[s['text'] for s in sf_links]}")
        return False

    return _click_team_link(page, team_name)


def _normalize_date(d: str) -> str:
    parts = d.strip().split("/")
    if len(parts) == 3:
        return f"{int(parts[0])}/{int(parts[1])}/{parts[2]}"
    return d.strip()


def _merge_court_winners(existing_lines: list[dict], scraped_lines: list[dict]) -> int:
    applied = 0
    for el in existing_lines:
        if el.get("court_winner") is not None:
            continue
        for sl in scraped_lines:
            if sl.get("line") == el.get("line") and sl.get("result"):
                el["court_winner"] = sl["result"]
                applied += 1
                break
    return applied


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    all_work = []
    for div in ["30", "35"]:
        path = DATA_DIR / f"standings_ut_{div}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        ntrp = f"{div[0]}.{div[1]}"

        team_work = []
        for sf in data.get("subflights", []):
            if sf["flight_label"].startswith("Championships"):
                continue
            teams_matches: dict[str, list[dict]] = {}
            for m in sf.get("matches", []):
                if not any(ln.get("court_winner") is None for ln in m.get("lines", [])):
                    continue
                for field in ("home_team", "away_team"):
                    t = m.get(field, "")
                    if t:
                        teams_matches.setdefault(t, []).append(m)

            for team, matches in teams_matches.items():
                team_work.append({
                    "sf_label": sf["flight_label"],
                    "team": team,
                    "matches": matches,
                    "area": _area_for_subflight(sf["flight_label"]),
                    "sf_display": _subflight_display_name(sf["flight_label"]),
                })

        if team_work:
            all_work.append((path, data, ntrp, team_work))
            print(f"{path.name}: {len(team_work)} team visits needed")

    if not all_work:
        print("All UT courts already have winners!")
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

                seen_teams = set()
                file_applied = 0

                for tw in team_work:
                    team = tw["team"]
                    sf_label = tw["sf_label"]
                    team_key = f"{sf_label}|{team}"
                    if team_key in seen_teams:
                        continue
                    seen_teams.add(team_key)
                    teams_visited += 1

                    ok = _navigate_to_subflight_team(
                        page, tw["area"], ntrp, tw["sf_display"], team)
                    if not ok:
                        print(f"  [{teams_visited}] {team} in {sf_label}: FAILED")
                        continue

                    schedule = _extract_team_matches(page, team)
                    team_applied = 0

                    for sm in schedule:
                        if not sm.get("_date_link_href"):
                            continue
                        s_date = _normalize_date(sm["date"])

                        match_obj = None
                        for sf in data.get("subflights", []):
                            if sf["flight_label"] != sf_label:
                                continue
                            for m in sf.get("matches", []):
                                if not any(ln.get("court_winner") is None for ln in m.get("lines", [])):
                                    continue
                                if _normalize_date(m.get("date", "")) != s_date:
                                    continue
                                mh = m.get("home_team", "")
                                ma = m.get("away_team", "")
                                if team in (mh, ma) and (
                                    sm["opponent"].lower() in mh.lower() or
                                    sm["opponent"].lower() in ma.lower() or
                                    mh.lower() in sm["opponent"].lower() or
                                    ma.lower() in sm["opponent"].lower()
                                ):
                                    match_obj = m
                                    break
                            if match_obj:
                                break

                        if not match_obj:
                            continue

                        try:
                            _click_postback(page, sm["_date_link_href"])
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
                            print(f"    Error: {e}")
                            try:
                                page.go_back(wait_until="domcontentloaded", timeout=10_000)
                                sleep(1)
                            except Exception:
                                pass

                    print(f"  [{teams_visited}] {team}: +{team_applied} courts (total: {grand_total})")

                path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                remaining = sum(
                    1 for sf in data.get("subflights", [])
                    for m in sf.get("matches", [])
                    for ln in m.get("lines", [])
                    if ln.get("court_winner") is None
                )
                print(f"\n  {path.name}: +{file_applied} courts, {remaining} still unknown")

            print(f"\nDONE: +{grand_total} court winners, {teams_visited} teams visited")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
