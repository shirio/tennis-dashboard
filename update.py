#!/usr/bin/env python3
"""
update.py – Master weekly update script.

Run:  python3 update.py

Steps:
  1. Mode 1 TennisLink scraper  → standings_women_30/35.json
  2. Scan scorecards for unknown players, lookup on tennisrecord.com
  3. Retry pending_tennisrecord_lookup players
  4. Mode 2 TennisLink scraper  → matches_all_players.json
  5. Cross-source validation    → validation_errors.json
  6. engine/ratings.py          → recompute all ratings
  7. Rebuild women_30/35.html
  8. Print summary
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs

# ---------------------------------------------------------------------------
# Bootstrap: make sure project root is importable
# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

from scrapers.scrape_tennislink import (
    login,
    run_mode1,
    run_mode2,
    _compute_player_stats_from_scorecards,
    OUTPUT_STANDINGS_30,
    OUTPUT_STANDINGS_35,
    OUTPUT_MATCHES_ALL,
    PLAYERS_JSON,
)
from scrape_players import (
    fetch as tr_fetch,
    parse_team_roster,
    parse_player_profile,
    parse_match_history,
    merge_players,
    sort_players,
    slugify,
    BASE_URL as TR_BASE,
    ENTRY_URL as TR_ENTRY_URL,
    DEFAULT_DELAY as TR_DELAY,
)
from engine.ratings import run_ratings
from engine.build_html import build_dashboards

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR         = Path("data")
VALIDATION_JSON  = DATA_DIR / "validation_errors.json"
PLAYERS_JSON_P   = PLAYERS_JSON          # Path object from scrape_tennislink


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Player-name parsing (doubles have "A / B")
# ---------------------------------------------------------------------------

def parse_player_names(field: str) -> list[str]:
    if not field:
        return []
    cleaned = re.sub(r",?\s*\d+-\d+.*$", "", field).strip()
    parts = re.split(r"\s*/\s*", cleaned)
    return [p.strip() for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# STEP 2 – Scan Mode 1 scorecards for unknown players
# ---------------------------------------------------------------------------

def collect_scorecard_players(standings_files: list[Path]) -> list[dict]:
    """
    Walk all match lines in the standings files and return a list of
    {name, team, division, match_id} for every player name found.
    """
    found = []
    for path in standings_files:
        data = load_json(path, {})
        ntrp = data.get("ntrp", "?")
        for sf in data.get("subflights", []):
            sf_label = sf.get("flight_label", "")
            division = f"{ntrp} Women {sf_label}".strip()
            for match in sf.get("matches", []):
                if match.get("pending"):
                    continue
                for line in match.get("lines", []):
                    home_names = parse_player_names(line.get("players_home", ""))
                    away_names = parse_player_names(line.get("players_away", ""))
                    home_team  = match.get("home_team", "")
                    away_team  = match.get("away_team", "")
                    mid        = match.get("match_id", "")
                    for n in home_names:
                        found.append({"name": n, "team": home_team,
                                      "division": division, "match_id": mid})
                    for n in away_names:
                        found.append({"name": n, "team": away_team,
                                      "division": division, "match_id": mid})
    return found


def _is_valid_player_name(name: str) -> bool:
    """Return False for obvious non-player entries from bad scrape output."""
    if not name or len(name) < 2:
        return False
    nl = name.lower().strip()
    # Reject entries containing team-summary garbage patterns
    _garbage_patterns = [
        r'\(home team\)', r'\(visiting team\)', r'\d+\s*wins?$',
        r'\d+\.\d+%$', r'total team score', r'game winning',
        r'^(a|b|n)$',  # single navigation letters
    ]
    for pat in _garbage_patterns:
        if re.search(pat, nl):
            return False
    # Reject entries that are clearly website navigation/footer text
    _nav_exact = {
        'sitemap', 'privacy policy', 'terms of use', 'cookie policy',
        'contact us', 'careers', 'internships', 'umpire policy',
        'safe play disciplinary list', 'usta apps', 'usta connect portal',
        'api developer portal', 'accessibility statement', 'find your account',
        'learn more', 'retired', 'want to find more tennis?',
        '© 2026 usta all rights reserved', 'life time fitness',
    }
    if nl in _nav_exact:
        return False
    # Must contain at least one letter (not just numbers/symbols)
    if not re.search(r'[a-zA-Z]', name):
        return False
    return True


def find_unknown_players(scorecard_players: list[dict], existing: list[dict]) -> list[dict]:
    """Return scorecard entries whose name is not in existing players list."""
    known = {p["name"].lower().strip() for p in existing}
    seen  = set()
    unknowns = []
    for sp in scorecard_players:
        k = sp["name"].lower().strip()
        if k and k not in known and k not in seen and _is_valid_player_name(sp["name"]):
            seen.add(k)
            unknowns.append(sp)
    return unknowns


def _team_roster_url_for(team_name: str, existing_players: list[dict],
                          session: requests.Session) -> Optional[str]:
    """
    Find a tennisrecord team roster URL by:
    1. Looking at profile_urls of existing players on the same team
       and inferring the teamprofile URL
    2. Falling back to a fresh crawl of the entry page
    """
    # Method 1: derive from a player on the same team
    team_lower = team_name.lower().strip()
    for p in existing_players:
        if p.get("team", "").lower().strip() == team_lower:
            # profile_url looks like /adult/profile.aspx?playername=X&s=Y
            # tennisrecord's teamprofile URL follows teamprofile.aspx?...
            # We don't store the team URL directly, so fall through to method 2
            break

    # Method 2: not feasible without re-crawling the league finder.
    # Just return None and let the caller add a pending player.
    return None


def build_player_stub(name: str, team: str, division: str) -> dict:
    return {
        "id": slugify(name),
        "name": name,
        "tennisrecord_id": None,
        "profile_url": None,
        "team": team,
        "division": division,
        "dynamic_rating_baseline": None,
        "current_division_rating": None,
        "global_rating": None,
        "ntrp_rating": None,
        "wl_record": None,
        "lines_played": None,
        "lines_html": None,
        "notes": None,
        "pending_tennisrecord_lookup": True,
    }


def lookup_player_on_tennisrecord(name: str, session: requests.Session) -> Optional[dict]:
    """
    Try fetching the player's tennisrecord profile by name.
    Returns {ntrp_full, dynamic_rating, profile_url, tennisrecord_id} or None.
    """
    from urllib.parse import quote
    profile_url_path = f"/adult/profile.aspx?playername={quote(name)}"
    full_url = TR_BASE + profile_url_path

    html = tr_fetch(full_url, session, delay=TR_DELAY,
                    use_cache=False, cache_only=False)
    if not html:
        return None

    # Check if tennisrecord returned a real profile vs a search results page
    soup = BeautifulSoup(html, "html.parser")
    # If we got a profile page it should have "Dynamic Rating" somewhere
    text = soup.get_text(" ", strip=True)
    if "dynamic rating" not in text.lower() and "estimated" not in text.lower():
        # Might be a disambiguation or search-results page; look for a link to a profile
        links = soup.find_all("a", class_="link",
                              href=lambda h: h and "profile.aspx" in h)
        name_lower = name.lower()
        for link in links:
            if link.get_text(strip=True).lower() == name_lower:
                new_url = TR_BASE + link["href"]
                html2 = tr_fetch(new_url, session, delay=TR_DELAY,
                                 use_cache=False, cache_only=False)
                if html2:
                    html = html2
                    profile_url_path = link["href"]
                    from scrape_players import extract_s_param
                    tennisrecord_id = extract_s_param(link["href"])
                    break
        else:
            return None

    profile_data = parse_player_profile(html)
    if not profile_data.get("dynamic_rating") and not profile_data.get("ntrp_full"):
        return None

    from scrape_players import extract_s_param
    return {
        "ntrp_full":        profile_data.get("ntrp_full"),
        "dynamic_rating":   profile_data.get("dynamic_rating"),
        "profile_url":      profile_url_path,
        "tennisrecord_id":  extract_s_param(profile_url_path),
    }


def step2_resolve_unknown_players(
        standings_files: list[Path],
        existing: list[dict],
        session: requests.Session) -> tuple[list[dict], int]:
    """
    Find players in scorecards not yet in players.json.
    Try tennisrecord lookup. Add stubs with pending flag if not found.
    Returns (updated_players_list, n_new_added).
    """
    scorecard_players = collect_scorecard_players(standings_files)
    unknowns = find_unknown_players(scorecard_players, existing)

    if not unknowns:
        print(f"  [step2] All scorecard players already known.")
        return existing, 0

    print(f"  [step2] {len(unknowns)} new player(s) found in scorecards.")
    new_players = []

    for i, up in enumerate(unknowns, 1):
        name = up["name"]
        print(f"    [{i}/{len(unknowns)}] {name} ({up['team']})")

        tr_data = lookup_player_on_tennisrecord(name, session)
        stub = build_player_stub(name, up["team"], up["division"])

        if tr_data:
            stub["profile_url"]             = tr_data["profile_url"]
            stub["tennisrecord_id"]          = tr_data["tennisrecord_id"]
            stub["ntrp_rating"]              = tr_data["ntrp_full"] or None
            stub["dynamic_rating_baseline"]  = tr_data["dynamic_rating"]
            stub.pop("pending_tennisrecord_lookup", None)
            print(f"      → found on tennisrecord: rating={tr_data['dynamic_rating']}")
        else:
            print(f"      → not found – flagged pending_tennisrecord_lookup")

        new_players.append(stub)

    updated = merge_players(existing, new_players)
    updated = sort_players(updated)
    return updated, len(unknowns)


# ---------------------------------------------------------------------------
# STEP 3 – Retry pending_tennisrecord_lookup players
# ---------------------------------------------------------------------------

def step3_retry_pending(players: list[dict], session: requests.Session) -> tuple[list[dict], int]:
    """Re-try tennisrecord lookup for all pending players. Returns (players, n_resolved)."""
    pending = [p for p in players if p.get("pending_tennisrecord_lookup")]
    if not pending:
        print(f"  [step3] No pending tennisrecord lookups.")
        return players, 0

    print(f"  [step3] Retrying {len(pending)} pending tennisrecord lookup(s).")
    resolved = 0

    for p in pending:
        name = p["name"]
        tr_data = lookup_player_on_tennisrecord(name, session)
        if tr_data:
            p["profile_url"]            = tr_data["profile_url"]
            p["tennisrecord_id"]         = tr_data["tennisrecord_id"]
            p["ntrp_rating"]             = (tr_data["ntrp_full"] or "").rstrip("ABCS") or None
            p["dynamic_rating_baseline"] = tr_data["dynamic_rating"]
            p.pop("pending_tennisrecord_lookup", None)
            resolved += 1
            print(f"    → resolved: {name}  (rating={tr_data['dynamic_rating']})")
        else:
            print(f"    → still pending: {name}")

    return sort_players(players), resolved


# ---------------------------------------------------------------------------
# STEP 3.5 – Scrape per-player match histories from tennisrecord.com
# ---------------------------------------------------------------------------

def step35_update_ntrp_letters(
        players: list[dict],
        session: requests.Session,
        year: int = 2026) -> tuple[list[dict], int]:
    """
    Fetch the full tennisrecord.com Nevada ratings table in ONE request and
    update ntrp_rating + dynamic_rating_baseline for every matched player.
    Replaces the old per-player fetch approach.

    Returns (updated_players, n_updated).
    """
    from scrapers.scrape_tennisrecord import fetch_ratings_table, update_players as _tr_update
    import re as _re

    print(f"  [step3.5] Bulk-fetching tennisrecord.com ratings table …")
    try:
        records = fetch_ratings_table()
    except Exception as e:
        print(f"  [step3.5] ERROR fetching ratings: {e}")
        return players, 0

    # Re-save players first so the in-file update picks up current state,
    # then reload after the bulk update.
    save_json(PLAYERS_JSON_P, players)
    _tr_update(records)
    updated_players = load_json(PLAYERS_JSON_P, players)

    # Count how many ntrp_rating fields now have a space (letter present)
    n_with_letter = sum(
        1 for p in updated_players
        if _re.search(r'\d\.\d\s+[A-Z]', p.get("ntrp_rating", ""))
    )
    n_updated = n_with_letter
    print(f"  [step3.5] {n_with_letter} players now have NTRP rating with letter")
    return updated_players, n_updated


# ---------------------------------------------------------------------------
# STEP 5 – Cross-source validation
# ---------------------------------------------------------------------------

def _parse_team_score(score: str) -> Optional[tuple[int, int]]:
    """Parse '3-2' → (3, 2). Returns None if not parseable."""
    if not score:
        return None
    m = re.search(r"(\d+)\s*-\s*(\d+)", score.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def _count_line_wins(lines: list[dict]) -> tuple[int, int]:
    """Count (home_wins, away_wins) from line result fields."""
    hw = aw = 0
    for ln in lines:
        r = (ln.get("result") or "").upper().strip()
        if r in ("W", "WIN", "HOME"):
            hw += 1
        elif r in ("L", "LOSS", "AWAY"):
            aw += 1
    return hw, aw


def step5_validate(standings_files: list[Path], matches_all_path: Path) -> list[dict]:
    """
    For every non-pending match in Mode 1 standings, find its match_id in
    Mode 2 matches_all_players.json and check:
      a. Line totals match the team score
      b. All 5 lines are present
      c. Player names consistent

    Prints a warning for each discrepancy.
    Returns list of error dicts written to validation_errors.json.
    """
    # Build Mode 2 index
    all_matches: list[dict] = load_json(matches_all_path, [])
    mode2_by_id: dict[str, dict] = {
        m["match_id"]: m for m in all_matches if m.get("match_id")
    }

    errors = []
    pending_validation = 0

    for path in standings_files:
        data = load_json(path, {})
        ntrp = data.get("ntrp", "?")
        for sf in data.get("subflights", []):
            for match in sf.get("matches", []):
                mid = match.get("match_id")
                if not mid or match.get("pending"):
                    continue

                m2 = mode2_by_id.get(mid)
                if not m2:
                    # Mode 2 hasn't fetched this match yet – not an error, just pending
                    pending_validation += 1
                    continue

                m1_lines = match.get("lines", [])
                m2_lines = m2.get("lines", [])

                error_base = {
                    "match_id":   mid,
                    "ntrp":       ntrp,
                    "date":       match.get("date"),
                    "home_team":  match.get("home_team"),
                    "away_team":  match.get("away_team"),
                    "m1_score":   match.get("score"),
                }

                # (a) Line totals
                m1_score_tuple = _parse_team_score(match.get("score", ""))
                m2_hw, m2_aw   = _count_line_wins(m2_lines)
                if m1_score_tuple and m2_lines:
                    m1_hw, m1_aw = m1_score_tuple
                    if m1_hw != m2_hw or m1_aw != m2_aw:
                        msg = (f"SCORE MISMATCH match {mid}: "
                               f"Mode1={m1_hw}-{m1_aw}  Mode2={m2_hw}-{m2_aw}")
                        print(f"  WARNING: {msg}")
                        errors.append({**error_base, "type": "score_mismatch",
                                        "m1_lines_score": f"{m1_hw}-{m1_aw}",
                                        "m2_lines_score": f"{m2_hw}-{m2_aw}"})

                # (b) Line count
                if m1_lines and m2_lines and len(m1_lines) != len(m2_lines):
                    msg = (f"LINE COUNT MISMATCH match {mid}: "
                           f"Mode1={len(m1_lines)}  Mode2={len(m2_lines)}")
                    print(f"  WARNING: {msg}")
                    errors.append({**error_base, "type": "line_count_mismatch",
                                    "m1_count": len(m1_lines), "m2_count": len(m2_lines)})

                # (c) Player name consistency (per line, by line number)
                if m1_lines and m2_lines:
                    m1_by_line = {ln.get("line"): ln for ln in m1_lines if ln.get("line")}
                    m2_by_line = {ln.get("line"): ln for ln in m2_lines if ln.get("line")}
                    for lnum in m1_by_line:
                        if lnum not in m2_by_line:
                            continue
                        for side in ("players_home", "players_away"):
                            n1 = set(n.lower() for n in
                                     parse_player_names(m1_by_line[lnum].get(side, "")))
                            n2 = set(n.lower() for n in
                                     parse_player_names(m2_by_line[lnum].get(side, "")))
                            if n1 and n2 and n1 != n2:
                                msg = (f"PLAYER MISMATCH match {mid} line {lnum} {side}: "
                                       f"Mode1={n1}  Mode2={n2}")
                                print(f"  WARNING: {msg}")
                                errors.append({**error_base, "type": "player_mismatch",
                                                "line": lnum, "side": side,
                                                "m1_players": sorted(n1),
                                                "m2_players": sorted(n2)})

    save_json(VALIDATION_JSON, errors)
    return errors, pending_validation


# ---------------------------------------------------------------------------
# STEP 4 – Mode 2 for players with new/missing data
# ---------------------------------------------------------------------------

def _players_needing_mode2(players: list[dict]) -> list[dict]:
    """Players who lack match history data and have a tennislink_id or can be searched."""
    return [p for p in players
            if not p.get("wl_record")      # no match history yet
            and not p.get("pending_tennisrecord_lookup")]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env (see .env.example)")
        sys.exit(1)

    year = 2026
    standings_files = [OUTPUT_STANDINGS_30, OUTPUT_STANDINGS_35]

    # Counters for final summary
    n_standings_matches_before = sum(
        len(sf.get("matches", []))
        for path in standings_files
        for sf in load_json(path, {}).get("subflights", [])
    )

    print("\n" + "="*60)
    print("  STEP 1 – TennisLink Mode 1: standings + match results")
    print("="*60)

    session_requests = requests.Session()
    session_requests.headers["User-Agent"] = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "TennisDashboard/1.0"
    )

    # ── Playwright browser session (steps 1 + 4) ────────────────────────────
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            login(page, username, password)

            # Step 1
            run_mode1(page, year)

            # Compute per-player W/L and lines played from scorecard data
            standings_30 = load_json(OUTPUT_STANDINGS_30, {})
            standings_35 = load_json(OUTPUT_STANDINGS_35, {})
            _compute_player_stats_from_scorecards([
                ("3.0", standings_30.get("subflights", [])),
                ("3.5", standings_35.get("subflights", [])),
            ])

            n_standings_matches_after = sum(
                len(sf.get("matches", []))
                for path in standings_files
                for sf in load_json(path, {}).get("subflights", [])
            )
            n_new_standings = n_standings_matches_after - n_standings_matches_before

            # ── Steps 2 + 3 (tennisrecord, no Playwright needed) ────────────
            print("\n" + "="*60)
            print("  STEP 2 – Scan scorecards for unknown players")
            print("="*60)
            existing_players = load_json(PLAYERS_JSON_P, [])
            existing_players, n_new_players = step2_resolve_unknown_players(
                standings_files, existing_players, session_requests)
            save_json(PLAYERS_JSON_P, existing_players)

            print("\n" + "="*60)
            print("  STEP 3 – Retry pending tennisrecord lookups")
            print("="*60)
            existing_players, n_resolved = step3_retry_pending(
                existing_players, session_requests)
            save_json(PLAYERS_JSON_P, existing_players)

            n_still_pending_tr = sum(
                1 for p in existing_players if p.get("pending_tennisrecord_lookup"))

            print("\n" + "="*60)
            print("  STEP 3.5 – Tennisrecord: NTRP letter ratings")
            print("="*60)
            existing_players, n_ntrp_updated = step35_update_ntrp_letters(
                existing_players, session_requests, year)
            save_json(PLAYERS_JSON_P, existing_players)

            # Step 4 – Mode 2 disabled for now (per-player history scrape is too slow)
            print("\n" + "="*60)
            print("  STEP 4 – TennisLink Mode 2: SKIPPED (disabled)")
            print("="*60)
            n_matches_before = len(load_json(OUTPUT_MATCHES_ALL, []))
            # run_mode2(page)  # re-enable when needed
            n_matches_after = n_matches_before
            n_new_profile_matches = 0

        finally:
            context.close()
            browser.close()

    # ── Steps 5–8 (no browser) ───────────────────────────────────────────────
    print("\n" + "="*60)
    print("  STEP 5 – Cross-source validation")
    print("="*60)
    errors, n_pending_validation = step5_validate(standings_files, OUTPUT_MATCHES_ALL)
    n_errors = len(errors)
    if n_errors:
        print(f"  {n_errors} validation error(s) written to {VALIDATION_JSON}")
    else:
        print(f"  No validation errors.")
    if n_pending_validation:
        print(f"  {n_pending_validation} match(es) not yet in Mode 2 (pending validation)")

    print("\n" + "="*60)
    print("  STEP 6 – Recompute ratings")
    print("="*60)
    ratings_summary = run_ratings()

    print("\n" + "="*60)
    print("  STEP 7 – Rebuild HTML dashboards")
    print("="*60)
    build_dashboards()

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("  SUMMARY")
    print("="*60)
    print(f"  New standings matches  : {n_new_standings}")
    print(f"  New profile matches    : {n_new_profile_matches}")
    print(f"  New players added      : {n_new_players}")
    print(f"  Tennisrecord resolved  : {n_resolved}")
    print(f"  Pending TR lookups     : {n_still_pending_tr}")
    print(f"  Validation errors      : {n_errors}")
    print(f"  Pending validations    : {n_pending_validation}")
    print(f"  Ratings updated        : {ratings_summary.players_updated}")
    print(f"  Ratings skipped        : {ratings_summary.players_skipped}")
    print()
    if n_errors:
        print(f"  !! Review {VALIDATION_JSON} for details on {n_errors} mismatch(es)")
    print("  Done.")


if __name__ == "__main__":
    main()
