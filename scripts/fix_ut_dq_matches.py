#!/usr/bin/env python3
"""
Resolve per-court winners for UT 3.0 matches involving a disqualified (DQ)
player, where team_wins_home/away (TL's adjusted, authoritative team score)
doesn't match the as-played court_winner sum.

Root cause: TennisLink applies a default ruling on the DQ'd player's specific
court, adjusting the team-level score beyond what the other courts show as
played. normalize.py correctly refuses to guess for these matches (leaves
court_winner=None for ALL lines, even though most courts are unambiguous).

This script re-fetches each affected match (live, for those with tl_match_id;
otherwise re-parsed from existing data with orientation correction via player
name matching against home_team/away_team), determines per-court winners,
and applies them ONLY to courts where the result is self-consistent — i.e.
where applying the detected winners to the non-DQ courts plus the DQ
court's forced default outcome reconciles exactly with team_wins_home/away.
The DQ court itself is left unresolved (court_winner=None) since the actual
played result there isn't what determined the standings.

Usage:
    python3 scripts/fix_ut_dq_matches.py
    python3 scripts/fix_ut_dq_matches.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from diff_update import login, _fetch_match_details

DATA = Path("data")
UT30_PATH = DATA / "standings_ut_30.json"


def _last_name(raw: str) -> str:
    raw = raw.strip().lower()
    raw = re.sub(r"\s*-\s*\(dq\)\*?", "", raw)
    if "," in raw:
        return raw.split(",")[0].strip()
    parts = raw.split()
    return parts[-1] if parts else raw


_LEGEND_NOISE = re.compile(r"note\s*-|disqualif|awarded|standing|match\b", re.I)


def _name_set(raw: str) -> set[str]:
    if not raw or raw.strip().upper() in ("N/A", "NA", ""):
        return set()
    names = set()
    for part in re.split(r"\s*/\s*", raw):
        part = part.strip()
        if part and part.upper() not in ("N/A", "NA") and not _LEGEND_NOISE.search(part):
            names.add(_last_name(part))
    return names


def _is_dq_line(line: dict) -> bool:
    """True if this line has a genuine 'PlayerName - (DQ)*' marker.

    Distinct from the long disclaimer/legend text ("Note - / (DQ) - This
    player has been disqualified...") that TennisLink sometimes appends to
    the last court's player field — that's scraping noise, not a second
    DQ'd player, and must not be mistaken for one.
    """
    text = (line.get("players_home", "") or "") + " / " + (line.get("players_away", "") or "")
    for token in text.split("/"):
        token = token.strip()
        if re.search(r"-\s*\(DQ\)\*?\s*$", token):
            return True
    return False


def find_affected_matches(data: dict) -> list[tuple[dict, dict]]:
    out = []
    for sf in data.get("subflights", []):
        for m in sf.get("matches", []):
            if m.get("pending"):
                continue
            lines = m.get("lines", [])
            if not lines:
                continue
            hw = m.get("team_wins_home", 0) or 0
            aw = m.get("team_wins_away", 0) or 0
            cw_home = sum(1 for l in lines if l.get("court_winner") == "home")
            cw_away = sum(1 for l in lines if l.get("court_winner") == "away")
            if (cw_home != hw or cw_away != aw) and any(_is_dq_line(l) for l in lines):
                out.append((sf, m))
    return out


def resolve_match(page, match: dict) -> int:
    """Re-fetch (if tl_match_id known) and resolve non-DQ courts. Returns count resolved."""
    lines = match.get("lines", [])
    ht = match.get("home_team", "")
    at = match.get("away_team", "")
    hw = match.get("team_wins_home", 0) or 0
    aw = match.get("team_wins_away", 0) or 0
    dq_lines = [l for l in lines if _is_dq_line(l)]
    non_dq_lines = [l for l in lines if not _is_dq_line(l)]

    if len(dq_lines) != 1:
        print(f"    [skip] {ht} vs {at}: expected exactly 1 DQ line, found {len(dq_lines)}")
        return 0

    scraped = None
    if match.get("tl_match_id"):
        scraped = _fetch_match_details(page, match["tl_match_id"])

    if not scraped:
        print(f"    [skip] {ht} vs {at}: no tl_match_id / fetch failed — can't re-verify live")
        return 0

    # Determine orientation via player-name matching against stored home/away
    def _norm(s): return re.sub(r"\s+", "", s).lower()
    scraped_by_line = {_norm(s["line"]): s for s in scraped}

    home_overlap = away_overlap = 0
    for stored_ln in non_dq_lines:
        key = _norm(stored_ln.get("line", ""))
        sc = scraped_by_line.get(key)
        if not sc:
            continue
        st_home = _name_set(stored_ln.get("players_home", ""))
        st_away = _name_set(stored_ln.get("players_away", ""))
        sc_home = _name_set(sc.get("players_home", ""))
        sc_away = _name_set(sc.get("players_away", ""))
        home_overlap += len(sc_home & st_home) + len(sc_away & st_away)
        away_overlap += len(sc_home & st_away) + len(sc_away & st_home)

    if home_overlap == 0 and away_overlap == 0:
        print(f"    [skip] {ht} vs {at}: couldn't determine orientation")
        return 0
    flipped = away_overlap > home_overlap

    # Apply resolved winners to non-DQ courts only
    resolved = 0
    non_dq_home_wins = non_dq_away_wins = 0
    for stored_ln in non_dq_lines:
        key = _norm(stored_ln.get("line", ""))
        sc = scraped_by_line.get(key)
        if not sc or not sc.get("result"):
            continue
        result = sc["result"]
        if flipped:
            result = "away" if result == "home" else "home"
        if result == "home":
            non_dq_home_wins += 1
        else:
            non_dq_away_wins += 1

    # Sanity check: non-DQ wins + 1 (DQ court, awarded to whichever side makes
    # the total match team_wins_home/away) must reconcile exactly.
    dq_must_go_home = (non_dq_home_wins + 1 == hw and non_dq_away_wins == aw)
    dq_must_go_away = (non_dq_away_wins + 1 == aw and non_dq_home_wins == hw)
    if not (dq_must_go_home or dq_must_go_away):
        print(f"    [skip] {ht} vs {at}: non-DQ courts ({non_dq_home_wins}-{non_dq_away_wins}) "
              f"don't reconcile with team score ({hw}-{aw}) even with 1 DQ adjustment")
        return 0

    for stored_ln in non_dq_lines:
        key = _norm(stored_ln.get("line", ""))
        sc = scraped_by_line.get(key)
        if not sc or not sc.get("result"):
            continue
        result = sc["result"]
        if flipped:
            result = "away" if result == "home" else "home"
        ph = [n.strip() for n in stored_ln.get("players_home", "").split("/")
              if n.strip() and not _LEGEND_NOISE.search(n)]
        pa = [n.strip() for n in stored_ln.get("players_away", "").split("/")
              if n.strip() and not _LEGEND_NOISE.search(n)]
        stored_ln["court_winner"] = result
        if result == "home":
            stored_ln["winner_team"] = ht
            stored_ln["loser_team"] = at
            stored_ln["winner_names"] = ph
            stored_ln["loser_names"] = pa
        else:
            stored_ln["winner_team"] = at
            stored_ln["loser_team"] = ht
            stored_ln["winner_names"] = pa
            stored_ln["loser_names"] = ph
        resolved += 1

    # DQ line: leave court_winner=None (the as-played result there isn't what
    # determined the standings) but note the default ruling explicitly.
    dq_lines[0]["court_winner"] = None
    dq_lines[0]["winner_team"] = ""
    dq_lines[0]["loser_team"] = ""
    dq_lines[0]["dq_default"] = "home" if dq_must_go_home else "away"

    print(f"    [OK] {ht} vs {at}: resolved {resolved}/{len(non_dq_lines)} non-DQ courts, "
          f"DQ court awarded to {'home' if dq_must_go_home else 'away'} by default")
    return resolved


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("[error] TENNISLINK_USER / TENNISLINK_PASS not set in .env")
        sys.exit(1)

    data = json.loads(UT30_PATH.read_text())
    affected = find_affected_matches(data)
    print(f"Found {len(affected)} DQ-affected mismatched matches")

    total_resolved = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            login(page, username, password)
            for sf, m in affected:
                print(f"  {m['date']} {m['home_team']} vs {m['away_team']}")
                total_resolved += resolve_match(page, m)
        finally:
            browser.close()

    print(f"\nTotal courts resolved: {total_resolved}")
    if not args.dry_run and total_resolved > 0:
        UT30_PATH.write_text(json.dumps(data, indent=2))
        print(f"Saved {UT30_PATH}")


if __name__ == "__main__":
    main()
