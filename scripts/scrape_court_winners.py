#!/usr/bin/env python3
"""
Scrape per-court winners from tennisrecord.com match result pages.

Each match result page (matchresults.aspx?mid=X) has arrow images indicating
which side won each court:
  - arrowhead_left.png  → home team won that court
  - arrowhead_right.png → visiting/away team won that court

Strategy:
  1. For each team in our standings, fetch their tennisrecord team profile
     to discover all match IDs (mid values)
  2. For each match with unknown court winners, fetch the match result page
     and parse the arrow indicators
  3. Match tennisrecord results to our TennisLink data by date + team names
  4. Write court_winner fields into standings JSON

Usage:
    python3 scripts/scrape_court_winners.py
    python3 scripts/scrape_court_winners.py --state UT
    python3 scripts/scrape_court_winners.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")
CACHE_PATH = DATA_DIR / "tennisrecord_match_results_cache.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}

_COURT_MAP = {
    "Singles #1": "1# Singles", "Singles #2": "2# Singles",
    "Doubles #1": "1# Doubles", "Doubles #2": "2# Doubles", "Doubles #3": "3# Doubles",
}


def _norm_date(d: str) -> str:
    d = d.strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    parts = d.split("/")
    if len(parts) == 3:
        try:
            return f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
        except ValueError:
            pass
    return d


def _fetch_url(url: str) -> str | None:
    for attempt in range(3):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                return None
    return None


def _fetch_team_match_ids(team_name: str) -> list[str]:
    """Fetch all tennisrecord match IDs for a team from its profile page."""
    encoded = urllib.parse.quote(team_name)
    url = f"https://www.tennisrecord.com/adult/teamprofile.aspx?teamname={encoded}&year=2026"
    html = _fetch_url(url)
    if not html:
        return []
    return re.findall(r'matchresults\.aspx\?year=2026&(?:amp;)?mid=(\d+)', html)


def _parse_match_result_page(html: str) -> dict | None:
    """Parse a tennisrecord match result page.

    Returns {
        "date": "MM/DD/YYYY",
        "home_team": str,
        "away_team": str,
        "courts": {"1# Singles": "home"|"away", ...},
        "players": {"1# Singles": {"home": [...], "away": [...]}, ...}
    }
    """
    # Extract date (in a separate <td> after the label)
    date_m = re.search(
        r'Scheduled Date:.*?<td[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</td>',
        html, re.DOTALL)
    if not date_m:
        return None
    date = _norm_date(date_m.group(1))

    # Extract team names from the results table
    # Pattern: team names are in the first table with "Courts Won"
    team_names = re.findall(
        r'<a[^>]*teamprofile[^>]*>([^<]+)</a>', html)
    if len(team_names) < 2:
        return None
    home_team = team_names[0].strip()
    away_team = team_names[1].strip()

    # Parse per-court winners from arrow images
    courts = {}
    players = {}

    # Split into court sections
    sections = re.split(r'((?:Singles|Doubles)\s*#\s*\d)', html)
    for i in range(1, len(sections), 2):
        court_label = sections[i].strip()
        court_html = sections[i + 1] if i + 1 < len(sections) else ""

        line_label = None
        for tr_key, tl_key in _COURT_MAP.items():
            normalized = re.sub(r'\s+', ' ', court_label)
            if normalized == re.sub(r'\s+', ' ', tr_key):
                line_label = tl_key
                break
        if not line_label:
            continue

        # Find arrow direction
        arrow_m = re.search(r'arrowhead_(left|right)\.png', court_html)
        if arrow_m:
            direction = arrow_m.group(1)
            courts[line_label] = "home" if direction == "right" else "away"

        # Extract player names from this section
        # Home players are before "Score", away players are after
        name_links = re.findall(
            r'<a[^>]*profile\.aspx[^>]*>([^<]+)</a>', court_html)
        # Also get names with ratings: "Name (rating)"
        name_ratings = re.findall(
            r'([A-Z][a-zA-Z\' .-]+?)\s*\([\d.]+\)', court_html)

        court_players = {"home": [], "away": []}
        if name_links:
            # Determine which names are home vs away
            # Home names appear before the score, away after
            score_idx = court_html.find("arrowhead_")
            if score_idx < 0:
                score_idx = len(court_html) // 2

            for nm in name_links:
                nm_idx = court_html.find(nm)
                if nm_idx < score_idx:
                    court_players["home"].append(nm.strip())
                else:
                    court_players["away"].append(nm.strip())

        players[line_label] = court_players

    if not courts:
        return None

    return {
        "date": date,
        "home_team": home_team,
        "away_team": away_team,
        "courts": courts,
        "players": players,
    }


def _fetch_and_parse_match(mid: str) -> tuple[str, dict | None]:
    url = f"https://www.tennisrecord.com/adult/matchresults.aspx?year=2026&mid={mid}"
    html = _fetch_url(url)
    if not html:
        return mid, None
    return mid, _parse_match_result_page(html)


def _match_tr_to_tl(tr_result: dict, tl_matches: list[dict]) -> dict | None:
    """Find the TennisLink match that corresponds to a tennisrecord result."""
    tr_date = tr_result["date"]
    tr_home = tr_result["home_team"].lower()
    tr_away = tr_result["away_team"].lower()

    for m in tl_matches:
        tl_date = _norm_date(m.get("date", ""))
        if tl_date != tr_date:
            continue
        tl_home = (m.get("home_team") or "").lower()
        tl_away = (m.get("away_team") or "").lower()

        # Fuzzy match: check if team names share significant words
        def _words(s):
            return set(re.findall(r'[a-z]{3,}', s))

        home_match = (
            _words(tr_home) & _words(tl_home)
            or tr_home in tl_home or tl_home in tr_home
        )
        away_match = (
            _words(tr_away) & _words(tl_away)
            or tr_away in tl_away or tl_away in tr_away
        )

        if home_match and away_match:
            return m

        # Try swapped (tennisrecord might have different home/away)
        home_swap = (
            _words(tr_home) & _words(tl_away)
            or tr_home in tl_away or tl_away in tr_home
        )
        away_swap = (
            _words(tr_away) & _words(tl_home)
            or tr_away in tl_home or tl_home in tr_away
        )
        if home_swap and away_swap:
            return m

    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-teams", type=int, default=0)
    parser.add_argument("--workers", type=int, default=10)
    args = parser.parse_args()

    states = [args.state.upper()] if args.state else ["UT", "CO", "ID"]

    # Load standings data
    matches_data = []
    all_tl_matches = []
    team_names_by_state = defaultdict(set)

    for state in states:
        st = state.lower()
        for div in ["30", "35"]:
            path = DATA_DIR / f"standings_{st}_{div}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            matches_data.append((state, div, data))
            for sf in data.get("subflights", []):
                for t in sf.get("teams", []):
                    team_names_by_state[state].add(t["team_name"])
                for m in sf.get("matches", []):
                    has_unknown = any(
                        ln.get("court_winner") is None
                        for ln in m.get("lines", []))
                    if has_unknown:
                        all_tl_matches.append(m)

    total_unknown = sum(
        sum(1 for ln in m.get("lines", []) if ln.get("court_winner") is None)
        for m in all_tl_matches
    )
    print(f"Courts with unknown winner: {total_unknown} across {len(all_tl_matches)} matches")

    all_teams = []
    for state in states:
        for t in sorted(team_names_by_state[state]):
            all_teams.append((state, t))
    print(f"Total teams to look up: {len(all_teams)}")

    if args.max_teams:
        all_teams = all_teams[:args.max_teams]
        print(f"  (limited to {args.max_teams})")

    # Load cache
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}

    # Phase 1: Discover match IDs from team profiles
    print(f"\n=== Phase 1: Discovering match IDs from team profiles ===")
    team_match_ids = {}
    teams_to_fetch = [(s, t) for s, t in all_teams if t not in cache.get("_teams", {})]
    cached_teams = [(s, t) for s, t in all_teams if t in cache.get("_teams", {})]

    print(f"  {len(cached_teams)} teams cached, {len(teams_to_fetch)} to fetch")

    if "_teams" not in cache:
        cache["_teams"] = {}

    all_mids = set()
    for state, team in cached_teams:
        mids = cache["_teams"][team]
        all_mids.update(mids)

    if teams_to_fetch:
        def _fetch_team(args_tuple):
            state, team = args_tuple
            return state, team, _fetch_team_match_ids(team)

        fetched = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_fetch_team, (s, t)): (s, t)
                       for s, t in teams_to_fetch}
            for future in as_completed(futures):
                state, team = futures[future]
                try:
                    _, _, mids = future.result()
                except Exception as e:
                    print(f"    [error] {team}: {e}", flush=True)
                    mids = []
                cache["_teams"][team] = mids
                all_mids.update(mids)
                fetched += 1
                if fetched % 25 == 0:
                    print(f"  Team profiles: {fetched}/{len(teams_to_fetch)} "
                          f"({len(all_mids)} match IDs found)", flush=True)
                    CACHE_PATH.write_text(json.dumps(cache))

        CACHE_PATH.write_text(json.dumps(cache))
        print(f"  Fetched {fetched} team profiles, {len(all_mids)} unique match IDs")

    # Phase 2: Fetch match result pages for unknown matches
    print(f"\n=== Phase 2: Fetching match results ===")

    # Filter to only mids we haven't cached yet
    cached_results = {k: v for k, v in cache.items() if k != "_teams" and v is not None}
    mids_to_fetch = [mid for mid in all_mids if mid not in cached_results]
    print(f"  {len(cached_results)} results cached, {len(mids_to_fetch)} to fetch")

    if mids_to_fetch:
        fetched = 0
        errors = 0
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {executor.submit(_fetch_and_parse_match, mid): mid
                       for mid in mids_to_fetch}
            for future in as_completed(futures):
                mid = futures[future]
                try:
                    _, result = future.result()
                except Exception as e:
                    result = None
                    errors += 1
                cache[mid] = result
                fetched += 1
                if fetched % 50 == 0:
                    CACHE_PATH.write_text(json.dumps(cache))
                    parsed = sum(1 for v in cache.values()
                                 if isinstance(v, dict) and "courts" in v)
                    print(f"  Match results: {fetched}/{len(mids_to_fetch)} "
                          f"({parsed} with court data, {errors} errors)", flush=True)

        CACHE_PATH.write_text(json.dumps(cache))
        parsed = sum(1 for v in cache.values()
                     if isinstance(v, dict) and "courts" in v)
        print(f"  Fetched {fetched} match results ({parsed} with court data)")

    # Phase 3: Cross-reference and apply
    print(f"\n=== Phase 3: Cross-referencing with TennisLink data ===")

    tr_results = [v for k, v in cache.items()
                  if k != "_teams" and isinstance(v, dict) and "courts" in v]
    print(f"  {len(tr_results)} tennisrecord results with court data")

    total_applied = 0
    total_name_fixes = 0

    for state, div, data in matches_data:
        changed = False
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                has_unknown = any(
                    ln.get("court_winner") is None
                    for ln in m.get("lines", []))
                if not has_unknown:
                    continue

                # Try to find matching tennisrecord result
                tr_match = _match_tr_to_tl(
                    {"date": _norm_date(m.get("date", "")),
                     "home_team": m.get("home_team", ""),
                     "away_team": m.get("away_team", "")},
                    [])  # dummy — we search tr_results instead

                # Search tr_results for this match
                tl_date = _norm_date(m.get("date", ""))
                tl_home = (m.get("home_team") or "").lower()
                tl_away = (m.get("away_team") or "").lower()

                def _words(s):
                    return set(re.findall(r'[a-z]{3,}', s))

                matched_tr = None
                for tr in tr_results:
                    if tr["date"] != tl_date:
                        continue
                    tr_h = tr["home_team"].lower()
                    tr_a = tr["away_team"].lower()

                    h_ok = bool(_words(tr_h) & _words(tl_home)) or tr_h in tl_home or tl_home in tr_h
                    a_ok = bool(_words(tr_a) & _words(tl_away)) or tr_a in tl_away or tl_away in tr_a

                    if h_ok and a_ok:
                        matched_tr = tr
                        break

                    # Try swapped
                    h_sw = bool(_words(tr_h) & _words(tl_away)) or tr_h in tl_away or tl_away in tr_h
                    a_sw = bool(_words(tr_a) & _words(tl_home)) or tr_a in tl_home or tl_home in tr_a
                    if h_sw and a_sw:
                        # Home/away are swapped between TR and TL
                        swapped = dict(tr)
                        swapped["courts"] = {}
                        for court, winner in tr["courts"].items():
                            swapped["courts"][court] = "away" if winner == "home" else "home"
                        matched_tr = swapped
                        break

                if not matched_tr:
                    continue

                for ln in m.get("lines", []):
                    line_label = ln.get("line", "")
                    if line_label in matched_tr["courts"] and ln.get("court_winner") is None:
                        ln["court_winner"] = matched_tr["courts"][line_label]
                        total_applied += 1
                        changed = True

                    # Also fix noise player names from TR data
                    if line_label in matched_tr.get("players", {}):
                        tr_players = matched_tr["players"][line_label]
                        home_raw = ln.get("players_home", "") or ""
                        if re.search(r'12:00\s', home_raw, re.I):
                            tr_home_names = tr_players.get("home", [])
                            if tr_home_names:
                                ln["players_home"] = " / ".join(tr_home_names)
                                total_name_fixes += 1
                                changed = True

        if changed and not args.dry_run:
            path = DATA_DIR / f"standings_{state.lower()}_{div}.json"
            path.write_text(json.dumps(data, indent=2))
            print(f"  Updated {path}")

    print(f"\nApplied court_winner to {total_applied} courts")
    if total_name_fixes:
        print(f"Fixed {total_name_fixes} noise player names")

    # Summary
    remaining = 0
    for _, _, data in matches_data:
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                for ln in m.get("lines", []):
                    if ln.get("court_winner") is None:
                        remaining += 1
    print(f"Courts still unknown: {remaining} (was {total_unknown})")


if __name__ == "__main__":
    main()
