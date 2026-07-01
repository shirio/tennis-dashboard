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

# TennisLink prints a page-level legend explaining the (DQ)/(DQ)*/* markers
# once per match. Our scraper sometimes glues that legend onto whichever
# court happened to be parsed last — even when nobody in that specific court
# is disqualified. It must never be treated as a player name.
_DQ_LEGEND_RE = re.compile(
    r"disqualified|match awarded to opposing team", re.IGNORECASE
)

# "PlayerName - (DQ)*" or "PlayerName - (DQ)" — the real, correctly-placed
# per-player marker (always attached to the actual DQ'd player's own name).
_DQ_MARKER_RE = re.compile(r"\s*-\s*\(DQ\)\*?\s*$", re.IGNORECASE)


def _nkey(n: str) -> str:
    return re.sub(r"\s+", " ", n.strip().lower())


def _is_noise(name: str) -> bool:
    s = name.strip().lower()
    if s in _NOISE:
        return True
    if re.match(r"^\d+:\d+\s*(am|pm|midnight|noon)$", s, re.I):
        return True
    if _DQ_LEGEND_RE.search(name):
        return True
    return False


def _extract_dq_players(raw: str) -> list[str]:
    """Return names in `raw` that carry the real 'Name - (DQ)*' marker."""
    out = []
    for tok in raw.split("/"):
        tok = tok.strip()
        if tok and _DQ_MARKER_RE.search(tok):
            out.append(_DQ_MARKER_RE.sub("", tok).strip())
    return out


def _strip_dq_marker(raw: str) -> str:
    """Remove '- (DQ)*' suffixes from names, leaving the plain name."""
    parts = [p.strip() for p in raw.split("/")]
    return " / ".join(_DQ_MARKER_RE.sub("", p).strip() for p in parts if p.strip())


def _clean_names(raw: str) -> list[str]:
    if not raw or raw.strip().upper() in ("N/A", "NA", ""):
        return []
    cleaned = []
    for n in raw.split("/"):
        n = n.strip()
        if not n or _is_noise(n):
            continue
        n = _DQ_MARKER_RE.sub("", n).strip()
        if n:
            cleaned.append(n)
    return cleaned


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
            ph, pa = ln.get("players_home", ""), ln.get("players_away", "")
            ln["players_home"], ln["players_away"] = pa, ph
        return True
    return False


def _fix_player_alignment(match: dict, player_teams: dict[str, set[str]]) -> bool:
    """Swap players_home/away if they don't match the team alignment.

    Detects cases where result fields were previously flipped to match-level
    coordinates but players_home/away remained in scorecard coordinates.
    """
    home_team = (match.get("home_team") or "").strip().lower()
    away_team = (match.get("away_team") or "").strip().lower()
    if not home_team or not away_team or not player_teams:
        return False

    ph_on_home = 0
    ph_on_away = 0
    for ln in match.get("lines", []):
        for name in _clean_names(ln.get("players_home", "")):
            teams = player_teams.get(_nkey(name), set())
            if home_team in teams:
                ph_on_home += 1
            elif away_team in teams:
                ph_on_away += 1

    if ph_on_away > ph_on_home and ph_on_away >= 2:
        for ln in match.get("lines", []):
            ph, pa = ln.get("players_home", ""), ln.get("players_away", "")
            ln["players_home"], ln["players_away"] = pa, ph
        return True
    return False


def normalize_match(match: dict, player_teams: dict[str, set[str]] | None = None) -> None:
    """Add canonical winner/loser fields to every line in a match."""
    home_team = (match.get("home_team") or "").strip()
    away_team = (match.get("away_team") or "").strip()
    _fix_swapped_results(match)
    if player_teams:
        _fix_player_alignment(match, player_teams)
    reliable = _match_result_reliable(match)

    for ln in match.get("lines", []):
        raw_home = ln.get("players_home", "") or ""
        raw_away = ln.get("players_away", "") or ""

        # Capture the real "Name - (DQ)*" markers, then rewrite players_home/
        # players_away as plain, clean names — stripping both the DQ marker
        # suffix and any page-level legend text that got glued on by mistake
        # (see _DQ_LEGEND_RE). dq_players is the single source of truth for
        # rendering the "(DQ)" badge and the whole-line "invalid match" styling.
        dq_players = _extract_dq_players(raw_home) + _extract_dq_players(raw_away)
        if dq_players:
            ln["dq_players"] = dq_players
        ln["players_home"] = _strip_dq_marker(raw_home) if "(DQ)" in raw_home else raw_home
        ln["players_away"] = _strip_dq_marker(raw_away) if "(DQ)" in raw_away else raw_away
        # Legend noise (and the standalone "Note -" that precedes it) never
        # carries a real name — drop it entirely rather than leaving an
        # orphaned fragment behind.
        if "(DQ)" in raw_home or "note -" in raw_home.lower():
            ln["players_home"] = " / ".join(
                p for p in ln["players_home"].split(" / ") if not _is_noise(p)
            )
        if "(DQ)" in raw_away or "note -" in raw_away.lower():
            ln["players_away"] = " / ".join(
                p for p in ln["players_away"].split(" / ") if not _is_noise(p)
            )

        ph = _clean_names(ln["players_home"])
        pa = _clean_names(ln["players_away"])

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


def _load_team_aliases() -> dict[str, str]:
    """Load team name aliases from data/team_aliases.json. Keys are lowercased."""
    path = DATA_DIR / "team_aliases.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return {k.lower(): v for k, v in raw.items() if not k.startswith("_")}
    except Exception:
        return {}


def _canon(name: str, aliases: dict[str, str]) -> str:
    """Return canonical team name, replacing alias if known."""
    return aliases.get(name.strip().lower(), name)


def _apply_team_aliases(data: dict, aliases: dict[str, str]) -> int:
    """Replace alias team names with canonical names in all match + team fields.
    Returns number of substitutions made."""
    if not aliases:
        return 0
    changes = 0
    for sf in data.get("subflights", []):
        for t in sf.get("teams", []):
            canon = _canon(t.get("team_name", ""), aliases)
            if canon != t.get("team_name", ""):
                t["team_name"] = canon
                changes += 1
        for m in sf.get("matches", []):
            for fld in ("home_team", "away_team"):
                canon = _canon(m.get(fld, ""), aliases)
                if canon != m.get(fld, ""):
                    m[fld] = canon
                    changes += 1
            for ln in m.get("lines", []):
                for fld in ("winner_team", "loser_team"):
                    v = ln.get(fld, "")
                    canon = _canon(v, aliases)
                    if canon != v:
                        ln[fld] = canon
                        changes += 1
    return changes


def _load_player_teams() -> dict[str, set[str]]:
    """Load player→team mapping from players.json for alignment detection."""
    player_teams: dict[str, set[str]] = {}
    players_path = DATA_DIR / "players.json"
    if not players_path.exists():
        return player_teams
    try:
        players = json.loads(players_path.read_text())
        for p in players:
            nk = _nkey(p.get("name", ""))
            if not nk:
                continue
            for field in ("team_30", "team_35", "team"):
                t = (p.get(field) or "").strip()
                if t:
                    player_teams.setdefault(nk, set()).add(t.lower())
    except Exception:
        pass
    return player_teams


def normalize_standings_file(path: Path, player_teams: dict[str, set[str]] | None = None,
                             aliases: dict[str, str] | None = None) -> int:
    """Normalize all matches in a standings JSON file. Returns count of lines processed."""
    data = json.loads(path.read_text())
    if aliases:
        _apply_team_aliases(data, aliases)
    count = 0
    for sf in data.get("subflights", []):
        for m in sf.get("matches", []):
            normalize_match(m, player_teams)
            count += len(m.get("lines", []))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return count


def normalize_districts_file(path: Path, player_teams: dict[str, set[str]] | None = None) -> int:
    """Normalize all matches in a districts JSON file."""
    data = json.loads(path.read_text())
    count = 0
    for m in data.get("matches", []):
        normalize_match(m, player_teams)
        count += len(m.get("lines", []))
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return count


def normalize_all() -> None:
    """Normalize all standings and districts files in data/."""
    player_teams = _load_player_teams()
    if player_teams:
        print(f"  [normalize] Loaded {len(player_teams)} player→team mappings")
    aliases = _load_team_aliases()
    if aliases:
        print(f"  [normalize] Loaded {len(aliases)} team alias(es)")

    total = 0
    unknown = 0
    for path in sorted(DATA_DIR.glob("standings_*_*.json")):
        n = normalize_standings_file(path, player_teams, aliases)
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
        n = normalize_districts_file(path, player_teams)
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
