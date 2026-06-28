"""
Normalize per-court match results into canonical winner/loser fields.

Every line in the standings data gets a `court_winner` field ("home", "away",
or null) that is the SINGLE SOURCE OF TRUTH for who won that court.

Derived fields added per line:
  - court_winner: "home" | "away" | None
  - winner_names: list[str]  (player names on winning side)
  - loser_names:  list[str]  (player names on losing side)
  - winner_team:  str        (team name of winning side)
  - loser_team:   str        (team name of losing side)

All downstream code (build_html.py, ratings.py) MUST use these fields only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DATA_DIR = Path("data")

_NOISE = frozenset({
    "12:00 midnight", "12:00 noon", "dbl. default", "default",
    "n/a", "na", "n", "a", "(dq)", "note -",
})


def _is_noise(name: str) -> bool:
    s = name.strip().lower()
    if s in _NOISE:
        return True
    if re.match(r"^\d+:\d+\s*(am|pm|midnight|noon)$", s, re.I):
        return True
    return False


def _clean_names(raw: str) -> list[str]:
    if not raw or raw.strip().upper() in ("N/A", "NA", ""):
        return []
    return [n.strip() for n in raw.split("/") if n.strip() and not _is_noise(n)]


def _match_result_reliable(match: dict) -> bool:
    """Check if per-court result fields agree with match-level score."""
    lines = match.get("lines", [])
    twh = match.get("team_wins_home")
    twa = match.get("team_wins_away")
    if twh is None or twa is None:
        return False
    rh = sum(1 for ln in lines if (ln.get("result") or "").lower() == "home")
    ra = sum(1 for ln in lines if (ln.get("result") or "").lower() == "away")
    return rh == twh and ra == twa


def _fix_swapped_results(match: dict) -> bool:
    """Detect and fix home/away swap between scorecard and stored match.

    The scraper stores home_team from the team schedule perspective, but
    the result field comes from the scorecard which may have swapped
    home/away. If results are inverted relative to match-level score,
    flip all result fields.
    """
    lines = match.get("lines", [])
    twh = match.get("team_wins_home")
    twa = match.get("team_wins_away")
    if twh is None or twa is None:
        return False
    rh = sum(1 for ln in lines if (ln.get("result") or "").lower() == "home")
    ra = sum(1 for ln in lines if (ln.get("result") or "").lower() == "away")
    if rh == twh and ra == twa:
        return False
    if rh == twa and ra == twh:
        for ln in lines:
            r = (ln.get("result") or "").lower()
            if r == "home":
                ln["result"] = "away"
            elif r == "away":
                ln["result"] = "home"
        return True
    return False


def normalize_match(match: dict) -> None:
    """Add canonical winner/loser fields to every line in a match."""
    home_team = (match.get("home_team") or "").strip()
    away_team = (match.get("away_team") or "").strip()
    _fix_swapped_results(match)
    reliable = _match_result_reliable(match)

    for ln in match.get("lines", []):
        ph = _clean_names(ln.get("players_home", ""))
        pa = _clean_names(ln.get("players_away", ""))

        # Determine court_winner using the best available signal
        cw = _determine_court_winner(ln, reliable)
        ln["court_winner"] = cw

        if cw == "home":
            ln["winner_team"] = home_team
            ln["loser_team"] = away_team
            ln["winner_names"] = ph
            ln["loser_names"] = pa
        elif cw == "away":
            ln["winner_team"] = away_team
            ln["loser_team"] = home_team
            ln["winner_names"] = pa
            ln["loser_names"] = ph
        else:
            ln["winner_team"] = ""
            ln["loser_team"] = ""
            ln["winner_names"] = []
            ln["loser_names"] = []


def _determine_court_winner(ln: dict, result_reliable: bool) -> str | None:
    """Return "home", "away", or None for a single court line.

    Signal priority:
    0. Existing court_winner already set (e.g. by tennisrecord cross-reference)
    1. Existing winner_team/loser_team (explicitly set by scraper, e.g. NV)
    2. result field ("home"/"away") IF match-level totals validate it
    3. None — genuinely unknown
    """
    # Signal 0: court_winner already resolved (e.g. tennisrecord cross-reference)
    existing = (ln.get("court_winner") or "").lower()
    if existing in ("home", "away"):
        return existing

    wt = (ln.get("winner_team") or "").strip()
    lt = (ln.get("loser_team") or "").strip()

    # Signal 1: explicit winner_team already set
    if wt or lt:
        return ln.get("result") or ("home" if wt else "away")

    # Signal 2: result field validated by match-level totals
    result = (ln.get("result") or "").lower()
    if result in ("home", "away") and result_reliable:
        return result

    # Can't determine — don't guess
    return None


def normalize_standings_file(path: Path) -> int:
    """Normalize all matches in a standings JSON file. Returns count of lines processed."""
    data = json.loads(path.read_text())
    count = 0
    for sf in data.get("subflights", []):
        for m in sf.get("matches", []):
            normalize_match(m)
            count += len(m.get("lines", []))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return count


def normalize_districts_file(path: Path) -> int:
    """Normalize all matches in a districts JSON file."""
    data = json.loads(path.read_text())
    count = 0
    for m in data.get("matches", []):
        normalize_match(m)
        count += len(m.get("lines", []))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return count


def normalize_all() -> None:
    """Normalize all standings and districts files in data/."""
    total = 0
    unknown = 0
    for path in sorted(DATA_DIR.glob("standings_*_*.json")):
        n = normalize_standings_file(path)
        data = json.loads(path.read_text())
        unk = sum(
            1 for sf in data.get("subflights", [])
            for m in sf.get("matches", [])
            for ln in m.get("lines", [])
            if ln.get("court_winner") is None
        )
        total += n
        unknown += unk
        print(f"  [normalize] {path.name}: {n} lines, {unk} unknown winners")

    for path in sorted(DATA_DIR.glob("districts_*_*.json")):
        n = normalize_districts_file(path)
        data = json.loads(path.read_text())
        unk = sum(
            1 for m in data.get("matches", [])
            for ln in m.get("lines", [])
            if ln.get("court_winner") is None
        )
        total += n
        unknown += unk
        print(f"  [normalize] {path.name}: {n} lines, {unk} unknown winners")

    print(f"  [normalize] Total: {total} lines, {unknown} unknown winners"
          f" ({100*unknown/total:.1f}%)" if total else "")
