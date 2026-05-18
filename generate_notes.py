#!/usr/bin/env python3
# ===========================================================================
# IMPORTANT NOTES — read before editing or running this file
# ===========================================================================
# 1. This script is NOT called by rebuild.py — it must be run explicitly:
#    python3 generate_notes.py
# 2. After running, rebuild.py must also be run to regenerate the HTML with
#    the updated notes.
# 3. Prose rules: notes should INTERPRET meaning, not narrate numbers.
#    Scores are visible in the UI — don't retell them in prose.
# 4. Upsets collapse to a single sentence (do not list each one separately).
# 5. "Underperforming" label should only fire for MULTIPLE surprising losses,
#    not a single loss on an otherwise strong record.
# 6. Surprising losses should always be shown — do not suppress them when
#    upsets are also present.
# 7. These notes must be preserved unless the user explicitly says to remove them.
# ===========================================================================
"""
Generate per-division player notes (notes_30, notes_35).

Notes are narratively rich — they explain the player's story using:
- Specific match details (week, line, opponents, score descriptor)
- Team-rank context (most-deployed, etc.)
- Cross-division matches woven into the narrative when informative
- Qualitative judgments (sleeper, overrated, etc.)
"""
import json, re
from pathlib import Path
from collections import defaultdict

DATA = Path("data")


def _name_key(name):
    return re.sub(r"\s+", " ", name.lower().strip())


# ---------------------------------------------------------------------------
# Point-in-time rating lookup (mirrors build_html._pit_rating)
# ---------------------------------------------------------------------------

def _date_sort_key(d: str) -> tuple:
    try:
        m, day, y = d.split("/")
        return (int(y), int(m), int(day))
    except Exception:
        return (0, 0, 0)


def _make_pit_lookup(players, sfx: str):
    """Return a callable pit(name_key, match_date) -> float | None.

    Returns the player's rating going INTO match_date:
      1. Timeline entry for that exact date (player played → pre-match snapshot)
      2. Most recent timeline entry strictly BEFORE match_date
      3. Baseline (player hadn't played yet in this division)
      4. None (player unknown)
    """
    timelines: dict[str, dict] = {}
    baselines: dict[str, float] = {}
    for p in players:
        k = _name_key(p.get("name", ""))
        tl = p.get(f"rating_timeline_{sfx}") or {}
        if tl:
            timelines[k] = {d: float(v) for d, v in tl.items()}
        bl = p.get("dynamic_rating_baseline")
        if bl is not None:
            baselines[k] = float(bl)

    def pit(name_key: str, match_date: str):
        tl = timelines.get(name_key)
        bl = baselines.get(name_key)
        if not tl:
            return bl
        if match_date in tl:
            return tl[match_date]
        mk = _date_sort_key(match_date)
        prior = [(d, v) for d, v in tl.items() if _date_sort_key(d) < mk]
        if prior:
            return max(prior, key=lambda kv: _date_sort_key(kv[0]))[1]
        return bl  # no prior entries → pre-season baseline

    return pit


def _comeback_shape(score: str, require_dominant_s2: bool = False) -> bool:
    """Return True if the score represents a comeback: player lost set 1, won set 2, won TB.

    Works by identifying the tiebreak winner's side and checking whether they
    dropped the first set.  Only meaningful for 3-set tiebreak matches.

    If require_dominant_s2 is True, also requires that S2 was dominant
    (opponent conceded ≤2 games) — the shape change must be dramatic.
    """
    sets = re.findall(r"(\d+)-(\d+)", score)
    if len(sets) != 3:
        return False
    tb = sets[2]
    if tb not in [("1", "0"), ("0", "1")]:
        return False
    # The tiebreak winner's side: home=True if tb is "1-0"
    player_is_home = (tb == ("1", "0"))
    s1a, s1b = int(sets[0][0]), int(sets[0][1])
    s2a, s2b = int(sets[1][0]), int(sets[1][1])
    s1_player = s1a if player_is_home else s1b
    s1_opp    = s1b if player_is_home else s1a
    s2_player = s2a if player_is_home else s2b
    s2_opp    = s2b if player_is_home else s2a
    # Comeback = player lost the first set
    if s1_player >= s1_opp:
        return False
    # Optional: S2 must be dominant (opponent got ≤2 games)
    if require_dominant_s2 and s2_opp > 2:
        return False
    return True


def _score_descriptor(score: str) -> str:
    """
    Describe the shape of a score string:
      'lopsided'     → any bagel (6-0) OR every set conceded ≤1
      'dominant'     → at least one set conceded ≤2 (but no bagel); e.g. 6-2 6-4, 6-2 6-2
      'clear'        → tightest set conceded exactly 3; e.g. 6-3 6-3, 6-3 6-4
      'tight'        → ALL sets conceded ≥4; e.g. 6-4 6-4, 7-5 6-4, 7-6 6-4
      '3-set tiebreak' → has a 1-0 or 0-1 third-set tiebreak

    Straight-sets rule: "tight" requires EVERY set to be close. If any
    single set was dominant (≤2 conceded), the straight-set match is
    "dominant" — 6-2 6-4 is dominant, not tight.
    This does NOT apply to 3-set tiebreaks: 6-1 1-6 1-0 is already
    handled above as "3-set tiebreak" regardless of individual set shapes.
    """
    if not score:
        return ""
    sets = re.findall(r"(\d+)-(\d+)", score)
    if not sets:
        return ""
    has_tiebreak = any((a, b) in [("1", "0"), ("0", "1")] for a, b in sets)
    three_set = len(sets) >= 3

    # Regular sets (exclude 1-0 tiebreak set)
    regular = [(int(a), int(b)) for a, b in sets if not (int(a) <= 1 and int(b) <= 1)]

    if three_set and has_tiebreak:
        return "3-set tiebreak"
    if not regular:
        return ""

    # min_conceded: fewest games conceded in any single set — if this is low,
    # at least one set was dominant regardless of how close the other was.
    min_conceded = min(min(a, b) for a, b in regular)

    if min_conceded == 0:
        return "lopsided"                  # at least one bagel
    if min_conceded == 1 and max(min(a, b) for a, b in regular) <= 1:
        return "lopsided"                  # all sets ≤1 conceded (e.g. 6-1 6-0)
    if min_conceded <= 1:
        return "dominant"                  # one set was 6-0/6-1, other was closer
    if min_conceded <= 2:
        return "dominant"                  # at least one set ≤2 conceded (e.g. 6-2 6-4)
    if min_conceded == 3:
        return "clear"                     # tightest set was 6-3 (e.g. 6-3 6-3, 6-3 6-4)
    return "tight"                         # ALL sets ≥4 conceded (6-4 6-4, 7-5 6-4)


def _score_phrase(score: str, won: bool) -> str:
    """
    Translate a raw score into a concise qualitative phrase for note text.
    Never returns raw numbers — only shape/drama language.
    Returns empty string when the result is too ordinary to mention.
    """
    desc = _score_descriptor(score)
    if not desc:
        return ""

    if desc == "3-set tiebreak":
        return "in a third-set tiebreak"

    sets = re.findall(r"(\d+)-(\d+)", score)
    reg = [(int(a), int(b)) for a, b in sets if not (int(a) <= 1 and int(b) <= 1)]
    has_bagel = any(min(a, b) == 0 for a, b in reg)

    if won:
        if has_bagel:
            return "with a bagel"
        if desc == "lopsided":
            return "in a rout"
        if desc == "dominant":
            return "comfortably"
        if desc == "tight":
            return "in a tight match"
        return ""   # clear/straight — uninteresting, say nothing
    else:
        if has_bagel:
            return "bageled in a set"
        if desc == "lopsided":
            return "in a rout"
        if desc == "dominant":
            return "decisively"
        if desc == "tight":
            return "in a tight match"
        return ""


def _week_number(date: str, all_dates: list[str]) -> str:
    """Return W1, W2, etc. based on chronological order of match dates."""
    from datetime import datetime
    try:
        parsed_dates = sorted(
            set(all_dates), key=lambda d: datetime.strptime(d, "%m/%d/%Y")
        )
        if date in parsed_dates:
            return f"W{parsed_dates.index(date) + 1}"
    except Exception:
        pass
    return ""


def _opp_label(m, rating_lookup=None):
    """Format opponent names with ratings.
    Prefers PIT ratings stored in the match record (opp_pit_ratings dict),
    falls back to rating_lookup if provided."""
    pieces = []
    pit_map = m.get("opp_pit_ratings") or {}
    for n in m["opp_names"]:
        r = pit_map.get(_name_key(n))
        if r is None and rating_lookup:
            r = rating_lookup.get(_name_key(n))
        pieces.append(f"{n} ({r:.2f})" if r is not None else n)
    return " + ".join(pieces)


def _line_short(line_label: str) -> str:
    """e.g. '1# Singles' → 'S1', '3# Doubles' → 'D3'."""
    m = re.match(r"(\d+)#\s*(Singles|Doubles)", line_label or "")
    if not m:
        return ""
    n, kind = m.group(1), m.group(2)
    return f"{'S' if kind == 'Singles' else 'D'}{n}"


# Numerical tier for each line — used to detect deployment arc changes.
# Global ordering (for arc direction): S1 > D1 > S2 > D2 > D3
_LINE_TIER = {
    "1# Singles": 5, "2# Singles": 3,
    "1# Doubles": 4, "2# Doubles": 2, "3# Doubles": 1,
}

# Within-type tier: compare singles to singles, doubles to doubles only.
# This prevents nonsensical comparisons like "D1 player vs S1 teammate."
_SINGLES_TIER = {"1# Singles": 2, "2# Singles": 1}
_DOUBLES_TIER = {"1# Doubles": 3, "2# Doubles": 2, "3# Doubles": 1}


def _line_tier(line: str) -> int:
    return _LINE_TIER.get(line, 2)


def _line_type(line: str) -> str:
    """Return 'S' for singles lines, 'D' for doubles lines."""
    if "Singles" in (line or ""):
        return "S"
    if "Doubles" in (line or ""):
        return "D"
    return ""


def _within_type_tier(line: str) -> int:
    t = _line_type(line)
    if t == "S":
        return _SINGLES_TIER.get(line, 0)
    if t == "D":
        return _DOUBLES_TIER.get(line, 0)
    return 0


def _arc_outcome(m: dict, bl=None) -> str:  # bl: float | None
    """
    Short outcome phrase for use inside a tier-arc sentence.
    Qualitative only — no raw scores.
    """
    phrase = _score_phrase(m.get("score", ""), m["won"])

    opp_avg = m.get("opp_avg")
    opp_qual = ""
    if bl is not None and opp_avg is not None:
        gap = opp_avg - bl
        if m["won"] and gap < -0.20:
            opp_qual = " vs much weaker opp"
        elif not m["won"] and gap > 0.20:
            opp_qual = " vs much stronger opp"
        elif not m["won"] and gap < -0.10:
            opp_qual = " vs weaker opp"

    if not m["won"]:
        base = "lost"
        if phrase == "in a rout":
            base = "routed"
            phrase = ""
        elif phrase == "bageled in a set":
            base = "bageled"
            phrase = ""
        return (f"{base} {phrase}".strip() + opp_qual).strip()
    else:
        if phrase:
            return (f"won {phrase}" + opp_qual).strip()
        return ("won" + opp_qual).strip()


def _other_div_line_summary(other_matches: list) -> str:
    """
    Summarise deployment line(s) in the other division.
    E.g. 'D2' or 'D2/D3'.
    """
    lines = sorted({_line_short(m["line"]) for m in other_matches if m["line"]},
                   key=lambda x: (x[0], x[1:]))
    return "/".join(lines) if lines else ""


def _describe_match(m, rating_lookup, all_dates_in_division, include_week=True,
                    include_score=True, include_partner=False,
                    player_bl=None) -> str:
    """Short phrase describing a match: 'W2 D1 alongside Liu lost to Dexter+Doe'.

    Qualitative shape (tight/rout/tiebreak/bagel) appended only when interesting.
    Never shows raw scores.
    """
    wk = _week_number(m["date"], all_dates_in_division) if include_week else ""
    line = _line_short(m["line"])
    verb = "beat" if m["won"] else "lost to"
    opp = _opp_label(m, rating_lookup)

    partner_clause = ""
    if include_partner and m.get("partner"):
        partner_clause = f" alongside {m['partner']}"

    prefix = f"{wk} {line}" if wk else line
    base = f"{prefix}{partner_clause} {verb} {opp}"

    if include_score and m.get("score"):
        phrase = _score_phrase(m["score"], m["won"])
        if not phrase:
            return base
        # Tiebreak always worth adding; other shape words only when not obvious
        if phrase == "in a third-set tiebreak":
            return f"{base} in a third-set tiebreak"
        if player_bl is not None and m.get("opp_avg") is not None:
            gap = m["opp_avg"] - player_bl
            # Predictable large-gap result — shape adds nothing
            if (not m["won"] and gap >= 0.20) or (m["won"] and gap <= -0.20):
                return base
        return f"{base} {phrase}"
    return base


# Concise display names for teams — used wherever a team name appears in note text.
# Full all-caps names (as stored in data) → short readable form.
_TEAM_SHORT = {
    "ALL AMERICAN TENNIS CENTER": "AATC",
    "ANTHEM CC": "Anthem",
    "CLUB RIDGES": "Ridges",
    "DESERT PALM": "Desert Palm",
    "DRAGONRIDGE CC": "Dragonridge",
    "DTC #1": "DTC #1",
    "DTC #2": "DTC #2",
    "DTC #3": "DTC #3",
    "DTC #4": "DTC #4",
    "LAKE LAS VEGAS SPORTS CLUB": "LLV",
    "LIFE TIME FITNESS/GV": "LTF",
    "RED ROCK CC": "Red Rock",
    "RED ROCK CC #1": "Red Rock #1",
    "RED ROCK CC #2": "Red Rock #2",
    "SOUTHERN HIGHLANDS": "SoHi",
    "SPANISH OAKS": "Sp. Oaks",
    "SPANISH TRAIL": "Sp. Trail",
    "SPANISH TRAIL #1": "Sp. Trail #1",
    "SPANISH TRAIL #2": "Sp. Trail #2",
    "STIRLING CLUB": "Stirling",
    "SUMMERLIN ARBORS": "Summerlin",
    "TPC": "TPC",
    "WHITNEY MESA PARK": "Whitney Mesa",
}


def _team_short(team: str) -> str:
    """Return the concise display name for a team, falling back to title-case."""
    return _TEAM_SHORT.get(team.upper() if team else "", team)


def _is_lopsided_loss(m):
    """Lopsided = any bagel set OR both sets conceded ≤1 games."""
    if m["won"]:
        return False
    return _score_descriptor(m["score"]) == "lopsided"


def _is_lopsided_win(m):
    if not m["won"]:
        return False
    return _score_descriptor(m["score"]) == "lopsided"


def _is_tiebreak(m):
    return _score_descriptor(m["score"]) == "3-set tiebreak"


def _resolve_line_sides(ln: dict, match: dict, team_by_name: dict):
    """
    Resolve a line dict (either old winners/losers format OR new
    players_home/players_away/result format) into:
      (w_names, l_names, winner_team, loser_team, is_walkover)
    Returns None if the line cannot be resolved.
    """
    def _is_default(s):
        s = (s or "").strip().upper()
        return not s or s in ("N/A", "N/A / N/A", "DEFAULT", "NOT AVAILABLE")

    # ── Old format ──────────────────────────────────────────────────────────
    w_raw = ln.get("winners", "")
    l_raw = ln.get("losers", "")
    if w_raw and l_raw:
        w_names = [n.strip() for n in w_raw.split("/") if n.strip()]
        l_names = [n.strip() for n in l_raw.split("/") if n.strip()]
        walkover = any(n.upper() == "N/A" for n in w_names + l_names)
        return (w_names, l_names,
                ln.get("winner_team", ""), ln.get("loser_team", ""),
                walkover)

    # ── New format: players_home / players_away / result ────────────────────
    ph = ln.get("players_home", "")
    pa = ln.get("players_away", "")
    result = ln.get("result", "").strip().lower()
    if not ph or not pa or result not in ("home", "away"):
        return None
    if _is_default(ph) or _is_default(pa):
        return None

    home_team = match.get("home_team", "")
    away_team = match.get("away_team", "")

    # Detect scorecard swap: if majority of players_home names belong to
    # the away team, the columns are swapped.
    home_votes = away_votes = 0
    for pn in [x.strip() for x in ph.split("/") if x.strip()]:
        pt_set = team_by_name.get(_name_key(pn)) or set()
        if isinstance(pt_set, str):
            pt_set = {pt_set}
        if home_team in pt_set:
            home_votes += 1
        elif away_team in pt_set:
            away_votes += 1

    # Tie-break: when home/away votes are equal (common with cross-listed players
    # who appear in both teams), use winner_team / loser_team from the line data
    # to determine the correct column assignment.
    if home_votes == away_votes and home_votes > 0:
        wt_field = ln.get("winner_team", "")
        lt_field = ln.get("loser_team", "")
        if wt_field and lt_field:
            # Count how many players_home names map to the line's winner/loser teams.
            wt_upper = wt_field.strip().upper()
            lt_upper = lt_field.strip().upper()
            wt_home_votes = lt_home_votes = 0
            for pn in [x.strip() for x in ph.split("/") if x.strip()]:
                pt_set = team_by_name.get(_name_key(pn)) or set()
                if isinstance(pt_set, str):
                    pt_set = {pt_set}
                pt_upper = {t.upper() for t in pt_set}
                if wt_upper in pt_upper:
                    wt_home_votes += 1
                if lt_upper in pt_upper:
                    lt_home_votes += 1
            # If more home-column players belong to the line's loser_team than
            # winner_team, the columns are swapped.
            if lt_home_votes > wt_home_votes:
                away_votes = home_votes + 1   # force swap
            elif wt_home_votes > lt_home_votes:
                home_votes = away_votes + 1   # force no swap

    is_swapped = away_votes > home_votes

    if is_swapped:
        ph, pa = pa, ph

    # result refers to which TEAM won (home_team or away_team)
    if result == "home":
        w_raw, l_raw = ph, pa
        winner_team, loser_team = home_team, away_team
    else:
        w_raw, l_raw = pa, ph
        winner_team, loser_team = away_team, home_team

    w_names = [n.strip() for n in w_raw.split("/") if n.strip()]
    l_names = [n.strip() for n in l_raw.split("/") if n.strip()]
    return w_names, l_names, winner_team, loser_team, False


def _ordinal_suffix(n: int) -> str:
    """Return ordinal suffix for rank numbers: 1st, 2nd, 3rd, 4th..."""
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _compose_subflight_summary(sf_label, div_label, teams, pbt, sfx, pending_matches):
    """
    Generate a 2-4 sentence narrative summary for a subflight.

    Args:
        sf_label: "A" or "B"
        div_label: "3.0" or "3.5"
        teams: list of team dicts from standings JSON
        pbt: dict mapping (team_upper, sfx) -> [player_obj, ...]
        sfx: "30" or "35"
        pending_matches: list of pending match dicts in this subflight
    """

    def _rec(t):
        return f"{t.get('team_wins', 0)}-{t.get('team_losses', 0)}"

    ranked = sorted(teams, key=lambda t: (-t.get("team_wins", 0), -t.get("indiv_wins", 0)))
    if not ranked:
        return ""

    leader_wins = ranked[0].get("team_wins", 0)
    sf_id = f"{div_label}{sf_label}"   # e.g. "3.030A" — just use label directly

    parts = []

    # --- Sentence 1: leader(s) ---
    leaders = [t for t in ranked if t.get("team_wins", 0) == leader_wins]
    if len(leaders) == 1:
        t = leaders[0]
        short = _team_short(t["team_name"])
        if t.get("team_losses", 0) == 0:
            parts.append(f"{short} leads {sf_label} undefeated at {_rec(t)}.")
        else:
            parts.append(f"{short} leads {sf_label} at {_rec(t)}.")
    elif len(leaders) == 2:
        names = f"{_team_short(leaders[0]['team_name'])} and {_team_short(leaders[1]['team_name'])}"
        parts.append(f"{names} share the {sf_label} lead at {_rec(leaders[0])}.")
    else:
        names = ", ".join(_team_short(t["team_name"]) for t in leaders)
        parts.append(f"{names} are all tied atop {sf_label} at {_rec(leaders[0])}.")

    # --- Sentence 2: chasers (within 2 wins of leader, not already a leader) ---
    # With 4-5 games still remaining in the season, being 2 wins back is
    # still a live title race — use a 2-win gap as the contention cutoff.
    chasers = [t for t in ranked
               if t.get("team_wins", 0) >= leader_wins - 2 and t not in leaders]
    if chasers:
        c_strs = [f"{_team_short(t['team_name'])} ({_rec(t)})" for t in chasers]
        if len(c_strs) == 1:
            parts.append(f"{c_strs[0]} is in contention.")
        elif len(c_strs) <= 4:
            parts.append(f"{', '.join(c_strs[:-1])}, and {c_strs[-1]} are in contention.")
        else:
            # Too many to list — just say how many at what record
            parts.append(
                f"{len(c_strs)} teams are within 2 of the lead ({_rec(chasers[0])})."
            )

    # --- Sentence 3: bottom of the table ---
    fading = [t for t in ranked if t.get("team_wins", 0) < leader_wins - 2]
    if fading:
        fnames = [_team_short(t["team_name"]) for t in fading[:3]]
        if len(fading) == 1:
            parts.append(f"{fnames[0]} ({_rec(fading[0])}) is fading.")
        elif len(fading) == 2:
            parts.append(
                f"{fnames[0]} ({_rec(fading[0])}) and {fnames[1]} ({_rec(fading[1])}) "
                f"are at the bottom."
            )
        else:
            parts.append(
                f"{', '.join(fnames[:-1])}, and {fnames[-1]} are at the bottom."
            )

    # --- Sentence 4: top player(s) / biggest risers across this subflight ---
    sf_teams_upper = {t["team_name"].upper() for t in teams}
    sf_players = []
    for tu in sf_teams_upper:
        sf_players.extend(pbt.get((tu, sfx), []))

    # Minimum current rating floor: only highlight players actually competing
    # at the division level (avoids low-baseline outliers dominating the list)
    riser_floor = 2.85 if sfx == "30" else 3.15

    risers = []
    for p in sf_players:
        bl = p.get("dynamic_rating_baseline")
        dr = p.get(f"rating_{sfx}")
        wl = p.get(f"wl_record_{sfx}", "") or ""
        if bl is None or dr is None or not wl or "-" not in str(wl):
            continue
        if dr < riser_floor:
            continue  # below competitive floor for this division
        try:
            w, l = map(int, str(wl).split("-"))
        except ValueError:
            continue
        if w + l < 2:
            continue
        delta = dr - bl
        if delta >= 0.08:
            risers.append((p, delta))

    risers.sort(key=lambda x: -x[1])

    player_snippets = []
    seen = set()
    for p, delta in risers[:2]:
        name = p.get("name", "")
        if name in seen:
            continue
        seen.add(name)
        bl = p.get("dynamic_rating_baseline")
        dr = p.get(f"rating_{sfx}")
        team = p.get(f"team_{sfx}") or p.get("team", "")
        wl = p.get(f"wl_record_{sfx}", "") or ""
        team_s = _team_short(team)
        wl_clause = f", {wl}" if wl else ""
        player_snippets.append(f"{name} ({team_s}, {bl:.2f}→{dr:.2f}{wl_clause})")

    if player_snippets:
        if len(player_snippets) == 1:
            parts.append(f"Biggest riser: {player_snippets[0]}.")
        else:
            parts.append(f"Top risers: {player_snippets[0]}; {player_snippets[1]}.")

    # --- Sentence 5: key pending matchup between top-2 teams ---
    top2 = {t["team_name"] for t in ranked[:2]}
    for m in pending_matches:
        ht, at = m.get("home_team", ""), m.get("away_team", "")
        if ht in top2 and at in top2:
            date = m.get("date", "")
            parts.append(
                f"{_team_short(ht)} vs {_team_short(at)} ({date}) decides the {sf_label} title."
            )
            break

    return " ".join(parts).strip()


def _generate_subflight_summaries(players):
    """Generate subflight_summary text and write back to both standings files."""
    # Build player lookup by (team_upper, sfx)
    pbt: dict = defaultdict(list)
    for p in players:
        for sfx_ in ("30", "35"):
            tv = (p.get(f"team_{sfx_}") or "").upper()
            if tv:
                pbt[(tv, sfx_)].append(p)

    for fname, sfx, div_label in [
        ("standings_women_30.json", "30", "3.0"),
        ("standings_women_35.json", "35", "3.5"),
    ]:
        data = json.loads((DATA / fname).read_text())
        for sf in data.get("subflights", []):
            sf_label = sf.get("flight_label", "?")
            teams = sf.get("teams", [])
            pending = [m for m in sf.get("matches", []) if m.get("pending")]
            sf["subflight_summary"] = _compose_subflight_summary(
                sf_label, div_label, teams, pbt, sfx, pending
            )
        (DATA / fname).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"{div_label}: subflight summaries written to {fname}")


_COMMON_FIRST_NAMES = {
    "kim", "kimberly", "chris", "alex", "sam", "taylor", "jordan", "morgan", "casey",
    "ashley", "jessica", "jennifer", "sarah", "emily", "amanda", "melissa",
    "stephanie", "nicole", "rachel", "megan", "lauren", "brittany", "amber",
    "heather", "anna", "kate", "katie", "kelly", "kerry", "keri", "lisa",
    "linda", "maria", "mary", "michelle", "amy", "andrea", "angela", "ann",
    "dana", "diana", "donna", "elizabeth", "erin", "grace", "hannah", "holly",
    "jade", "jamie", "jane", "janet", "karen", "katie", "laura", "leah",
    "lynn", "meg", "natalie", "nina", "paige", "robin", "rose", "sara",
    "shannon", "tina", "tracy", "victoria", "wendy",
}


def _pname(full_name: str) -> str:
    """First name normally; first + last when first name is very common."""
    name = full_name.strip()
    # TennisLink sometimes stores names in ALL-CAPS — normalize to title case.
    if name and name == name.upper() and name.replace(" ", "").isalpha():
        name = name.title()
    parts = name.split()
    if not parts:
        return full_name
    first = parts[0]
    if first.lower() in _COMMON_FIRST_NAMES and len(parts) >= 2:
        return f"{first} {parts[-1]}"
    return first


def _compose_team_note(  # noqa: C901
    tname, wins, losses, n_team_weeks,
    match_history,          # [{won, score, opp, date}] team-level match results
    every_week_names,
    sometimes_names,
    line_wl,
    player_line_records,    # [(name, line_short, W, L)]
    strong_pairs,           # [(n1, n2, line_short, W, L)]
    missing_stars,          # [(name, baseline)] — rostered but unplayed, high baseline
    rising_players,         # [(name, bl, dr, wl_str)] sorted by delta desc
    falling_players,        # [(name, bl, dr, wl_str)]
    overperforming_singles, # [(name, W, L, baseline)] good singles despite low bl
    is_top_rated_roster,    # bool
    div_best_singles,       # (name, W, L) or None
    div_best_singles_key,
    team_player_keys,
):
    """
    Story-driven team note.  Lead with the most distinctive fact for THIS team,
    then add 1-2 supporting facts.  Never follow a fixed template.
    """
    parts: list = []
    mentioned: set = set()
    superlative_used = False

    # ── Parse team match history ──────────────────────────────────────────────
    n_matches = len(match_history)
    close_losses, big_wins, big_losses = [], [], []
    for m in match_history:
        sc = m.get("score", "")
        sc_parts = sc.split("-")
        if len(sc_parts) != 2:
            continue
        try:
            tw, tl = int(sc_parts[0]), int(sc_parts[1])
        except ValueError:
            continue
        if m["won"]:
            if tw >= 4:
                big_wins.append(m)
        else:
            if tl - tw <= 1:         # 2–3 loss = competitive
                close_losses.append(m)
            if tl >= 4:              # 0–5 or 1–4 = blowout loss
                big_losses.append(m)

    contrasting = bool(big_wins) and bool(big_losses)

    # ── Line profile helpers ──────────────────────────────────────────────────
    enough = max(2, n_team_weeks - 1)
    perfect_lines = sorted(
        [ls for ls, (w, l) in line_wl.items() if l == 0 and w >= enough],
        key=lambda ls: ("S" not in ls, ls),
    )
    soft_lines = sorted(
        [(ls, w, l) for ls, (w, l) in line_wl.items() if l > w and w + l >= 2],
        key=lambda x: x[2] - x[1], reverse=True,
    )
    all_lines_winning = (
        bool(line_wl) and
        all(w >= l for ls, (w, l) in line_wl.items() if w + l >= 2)
    )
    best_player_at: dict = {}   # line_short -> (name, W, L)
    for name, ls, w, l in player_line_records:
        if ls not in best_player_at and l == 0 and w >= 2:
            best_player_at[ls] = (name, w, l)

    def _emit_line_profile(lines_to_show):
        """Build fused line-profile clause(s), marking names as mentioned."""
        nonlocal superlative_used
        clauses = []
        for ls in lines_to_show:
            if ls in best_player_at:
                bname, bw, _ = best_player_at[ls]
                pn = _pname(bname)
                bkey = _name_key(bname)
                is_div_best = (
                    not superlative_used
                    and div_best_singles_key == bkey
                    and div_best_singles_key in team_player_keys
                    and "S" in ls
                )
                if is_div_best:
                    clauses.append(
                        f"{ls} perfect, led by {pn} ({bw}–0), "
                        f"the biggest singles threat in the division"
                    )
                    superlative_used = True
                else:
                    clauses.append(f"{ls} perfect, led by {pn} ({bw}–0)")
                mentioned.add(pn)
            else:
                clauses.append(f"{ls} all perfect")
        if soft_lines and not any("soft spot" in p for p in parts):
            sl, sw, sl_l = soft_lines[0]
            clauses.append(f"{sl} is the soft spot ({sw}–{sl_l} record)")
        if clauses:
            parts.append("; ".join(clauses) + ".")

    # ══════════════════════════════════════════════════════════════════════════
    # LEAD — pick the single most distinctive fact for this team
    # ══════════════════════════════════════════════════════════════════════════

    if wins > 0 and losses == 0:
        # Undefeated — lead with strength descriptor
        if is_top_rated_roster:
            parts.append("Undefeated; top-rated roster in the division.")
        elif all_lines_winning:
            parts.append("Undefeated; strong across all lines.")
        else:
            parts.append(f"Undefeated at {wins}–{losses}.")
        # Follow up with line detail
        if perfect_lines:
            _emit_line_profile(perfect_lines)

    elif wins == 0 and missing_stars:
        # Winless but key players haven't appeared yet
        stars = [f"{_pname(n)} ({bl:.2f})" for n, bl in missing_stars[:2]]
        if len(stars) == 1:
            parts.append(f"0–{losses}; {stars[0]} hasn't played yet.")
        else:
            parts.append(f"0–{losses}; {' and '.join(stars)} haven't played yet.")

    elif wins == 0 and len(close_losses) >= 2:
        # Winless but consistently competitive
        parts.append(
            f"0–{losses} but {len(close_losses)} of {n_matches} "
            f"losses were 2–3 splits."
        )

    elif wins == 0 and len(close_losses) == 1:
        parts.append(f"0–{losses}; one 2–3 loss shows they can compete.")

    elif contrasting:
        # Mixed bag — big win and big blowout loss
        bw_m = big_wins[-1]
        bl_m = big_losses[-1]
        parts.append(
            f"Won big over {_team_short(bw_m['opp'])}, "
            f"got swept by {_team_short(bl_m['opp'])}."
        )

    elif perfect_lines:
        # Line dominance is the headline
        _emit_line_profile(perfect_lines)

    elif soft_lines and wins < losses:
        # Struggling team — soft spot is the story
        sl, sw, sl_l = soft_lines[0]
        parts.append(f"{sl} is the soft spot ({sw}–{sl_l} record).")

    # ══════════════════════════════════════════════════════════════════════════
    # SUPPORTING FACTS (1-2, non-redundant)
    # ══════════════════════════════════════════════════════════════════════════

    # Overperforming singles player (beating expectations given low baseline)
    if overperforming_singles:
        name, ow, ol, obl = overperforming_singles[0]
        pn = _pname(name)
        if pn not in mentioned:
            line_tag = " in singles" if ol == 0 else " at singles"
            parts.append(f"{pn} {ow}–{ol}{line_tag} despite low dynamic ({obl:.2f}).")
            mentioned.add(pn)

    is_undefeated = wins > 0 and losses == 0

    # Rising player — require ≥2 matches so 1 lucky win doesn't trigger this
    if rising_players:
        rname, rbl, rdr, rwl = rising_players[0]
        try:
            rw, rl = map(int, str(rwl).split("-"))
            enough_matches = (rw + rl) >= 2
        except (ValueError, AttributeError):
            enough_matches = False
        pn = _pname(rname)
        if pn not in mentioned and enough_matches:
            parts.append(f"{pn} ({rbl:.2f}→{rdr:.2f}, {rwl}) trending up.")
            mentioned.add(pn)

    # Falling player — suppress for undefeated teams (misleading context)
    if falling_players and not is_undefeated:
        fname_p, fbl, fdr, fwl = falling_players[0]
        pn = _pname(fname_p)
        if pn not in mentioned:
            parts.append(f"{pn} drops hardest: {fbl:.2f}→{fdr:.2f}.")
            mentioned.add(pn)

    # Strong backbone (if not yet implied by lead)
    if every_week_names and not any("deployed every week" in p for p in parts):
        ns = [_pname(n) for n in every_week_names[:4] if _pname(n) not in mentioned]
        if len(ns) == 1:
            parts.append(f"{ns[0]} deployed every week.")
        elif len(ns) == 2:
            parts.append(f"{ns[0]} + {ns[1]} both deployed every week.")
        elif ns:
            parts.append(f"{'/'.join(ns[:3])} all deployed every week.")

    # Soft spot (if not yet mentioned)
    if soft_lines and not any("soft spot" in p for p in parts):
        sl, sw, sl_l = soft_lines[0]
        parts.append(f"{sl} is the soft spot ({sw}–{sl_l} record).")

    # Individual line standouts (skip already-mentioned players and pair partners)
    covered_by_pair: set = set()
    if strong_pairs:
        n1, n2, pls, pw, pl = strong_pairs[0]
        covered_by_pair = {_pname(n1), _pname(n2)}
    skip = mentioned | covered_by_pair
    standout_count = 0
    for name, ls, w, l in player_line_records:
        pn = _pname(name)
        if pn in skip or w + l < 2:
            continue
        if (l == 0 and w >= 2) or (w >= 2 and w > l):
            parts.append(f"{pn} {w}–{l} at {ls}.")
            skip.add(pn)
            standout_count += 1
        if standout_count >= 2:
            break

    # Strong pair — only show if winning or even record (never highlight a losing pair)
    if strong_pairs:
        n1, n2, pls, pw, pl = strong_pairs[0]
        if pw >= pl and pw >= 2:     # winning or even, at least 2 wins
            pn1, pn2 = _pname(n1), _pname(n2)
            parts.append(f"{pn1}/{pn2} {pw}–{pl} at {pls}.")

    # Division superlative (if not fused into line profile)
    if not superlative_used and div_best_singles and div_best_singles_key in team_player_keys:
        bname, bw, bl_l = div_best_singles
        parts.append(f"{_pname(bname)} has the best singles record in the division.")

    # Cap at 4 sentences — pick the most front-loaded, highest-value ones
    return " ".join(parts[:4]).strip()


def _generate_team_notes(players, division_data):  # noqa: C901
    """
    Generate story-driven per-team notes and write back to both standings files.
    Each note leads with the most distinctive fact for that team rather than
    always following the same template.
    """
    pbn = {_name_key(p.get("name", "")): p for p in players if p.get("name")}

    for fname, sfx, div_label in [
        ("standings_women_30.json", "30", "3.0"),
        ("standings_women_35.json", "35", "3.5"),
    ]:
        data = json.loads((DATA / fname).read_text())
        mbp = division_data[sfx]["matches_by_player"]

        # Division floor for "low dynamic" detection
        div_floor = 2.50 if sfx == "30" else 3.00

        # ── Division-wide average baseline (for top-rated roster detection) ──
        all_baselines = [
            p.get("dynamic_rating_baseline")
            for p in players
            if p.get(f"team_{sfx}") and p.get("dynamic_rating_baseline") is not None
        ]
        div_avg_bl = sum(all_baselines) / len(all_baselines) if all_baselines else 3.0

        # ── Per-team match date sets ──────────────────────────────────────────
        team_dates: dict = defaultdict(set)
        team_match_history: dict = defaultdict(list)  # team_upper -> [{won,score,opp,date}]
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if m.get("pending"):
                    continue
                date = m.get("date", "")
                ht = m.get("home_team", "")
                at = m.get("away_team", "")
                htu, atu = ht.upper(), at.upper()
                hw = m.get("team_wins_home", 0) or 0
                aw = m.get("team_wins_away", 0) or 0
                if date:
                    if htu:
                        team_dates[htu].add(date)
                        team_match_history[htu].append(
                            {"won": hw > aw, "score": f"{hw}-{aw}", "opp": at, "date": date}
                        )
                    if atu:
                        team_dates[atu].add(date)
                        team_match_history[atu].append(
                            {"won": aw > hw, "score": f"{aw}-{hw}", "opp": ht, "date": date}
                        )
        for tu in team_match_history:
            team_match_history[tu].sort(key=lambda x: x["date"])

        # ── Player records bucketed by team ──────────────────────────────────
        player_records_by_team: dict = defaultdict(lambda: defaultdict(list))
        for pk, matches in mbp.items():
            p = pbn.get(pk)
            if not p:
                continue
            team = (p.get(f"team_{sfx}") or "").upper()
            if not team:
                continue
            for m in matches:
                if not m.get("walkover"):
                    player_records_by_team[team][pk].append(m)

        # ── Division-wide best singles record ─────────────────────────────────
        div_singles: dict = {}
        for pk, matches in mbp.items():
            singles = [m for m in matches
                       if "Singles" in m.get("line", "") and not m.get("walkover")]
            if len(singles) < 2:
                continue
            w = sum(1 for m in singles if m["won"])
            div_singles[pk] = (w, len(singles) - w)

        best_singles_key = None
        best_singles = None
        for pk, (w, l) in div_singles.items():
            if best_singles_key is None:
                best_singles_key = pk
                p = pbn.get(pk)
                best_singles = (p.get("name", pk) if p else pk, w, l)
                continue
            bw, bl_l = div_singles[best_singles_key]
            beats = (
                (l == 0 and bl_l > 0) or
                (l == bl_l == 0 and w > bw) or
                (l == bl_l and w > bw)
            )
            if beats:
                best_singles_key = pk
                p = pbn.get(pk)
                best_singles = (p.get("name", pk) if p else pk, w, l)

        # ── Generate note per team ────────────────────────────────────────────
        for sf in data.get("subflights", []):
            for t in sf.get("teams", []):
                tname = t["team_name"]
                tu = tname.upper()

                tdates = team_dates.get(tu, set())
                n_team_weeks = len(tdates)
                pr = player_records_by_team.get(tu, {})
                mh = team_match_history.get(tu, [])

                wins = t.get("team_wins", 0)
                losses = t.get("team_losses", 0)

                if n_team_weeks == 0:
                    t["notes"] = ""
                    continue

                # -- All rostered players for this team --
                from_pbt = [
                    p for p in players
                    if (p.get(f"team_{sfx}") or "").upper() == tu
                    and p.get("dynamic_rating_baseline") is not None
                ]

                # -- Missing stars: rostered players who haven't played yet --
                threshold = div_avg_bl + 0.05
                missing_stars = sorted(
                    [
                        (p.get("name", ""), p["dynamic_rating_baseline"])
                        for p in from_pbt
                        if _name_key(p.get("name", "")) not in pr
                        and p["dynamic_rating_baseline"] >= threshold
                    ],
                    key=lambda x: -x[1],
                )[:2]

                # -- Is this the top-rated roster in the division? --
                if from_pbt:
                    team_avg_bl = sum(
                        p["dynamic_rating_baseline"] for p in from_pbt
                    ) / len(from_pbt)
                    # Compare to all other teams' averages
                    all_team_avgs = []
                    for p2 in players:
                        t2 = (p2.get(f"team_{sfx}") or "").upper()
                        if t2 and p2.get("dynamic_rating_baseline") is not None:
                            all_team_avgs.append((t2, p2["dynamic_rating_baseline"]))
                    from collections import defaultdict as _dd
                    avgs_by_team: dict = _dd(list)
                    for t2, bl2 in all_team_avgs:
                        avgs_by_team[t2].append(bl2)
                    team_roster_avgs = {
                        t2: sum(bls) / len(bls)
                        for t2, bls in avgs_by_team.items() if bls
                    }
                    is_top_rated_roster = (
                        bool(team_roster_avgs) and
                        team_avg_bl >= max(team_roster_avgs.values()) - 0.005
                    )
                else:
                    is_top_rated_roster = False

                # -- Backbone players --
                every_week_names, sometimes_names, all_deploy = [], [], []
                for pk, matches in pr.items():
                    p = pbn.get(pk)
                    if not p:
                        continue
                    name = p.get("name", pk)
                    dates = {m["date"] for m in matches}
                    all_deploy.append((name, len(matches), dates))
                all_deploy.sort(key=lambda x: -x[1])
                for name, n, dates in all_deploy:
                    if tdates and dates >= tdates:
                        every_week_names.append(name)
                    elif n >= max(1, n_team_weeks - 1):
                        sometimes_names.append(name)

                # -- Team line W-L (deduplicated) --
                line_results: dict = defaultdict(list)
                seen_dl: set = set()
                for pk, matches in pr.items():
                    for m in matches:
                        ls = _line_short(m.get("line", ""))
                        if not ls:
                            continue
                        key = (m["date"], ls)
                        if key in seen_dl:
                            continue
                        seen_dl.add(key)
                        line_results[ls].append(m["won"])
                line_wl = {ls: (sum(rs), len(rs) - sum(rs))
                           for ls, rs in line_results.items()}

                # -- Per-player line records --
                player_line_recs = []
                for pk, matches in pr.items():
                    p = pbn.get(pk)
                    if not p:
                        continue
                    name = p.get("name", pk)
                    by_line: dict = defaultdict(list)
                    for m in matches:
                        ls = _line_short(m.get("line", ""))
                        if ls:
                            by_line[ls].append(m["won"])
                    for ls, rs in by_line.items():
                        w, l = sum(rs), len(rs) - sum(rs)
                        player_line_recs.append((name, ls, w, l))
                player_line_recs.sort(
                    key=lambda x: (
                        -(x[3] == 0 and x[2] >= 2),
                        -(x[2] / (x[2] + x[3])) if (x[2] + x[3]) > 0 else 0,
                        -x[2],
                    )
                )

                # -- Doubles pairs (deduplicated) --
                pair_results: dict = defaultdict(list)
                seen_pair_dates: set = set()
                for pk, matches in pr.items():
                    p = pbn.get(pk)
                    if not p:
                        continue
                    name = p.get("name", pk)
                    for m in matches:
                        partner = m.get("partner")
                        if not partner:
                            continue
                        ls = _line_short(m.get("line", ""))
                        if not ls.startswith("D"):
                            continue
                        pair_key = (tuple(sorted([name, partner])), ls)
                        dedup = (pair_key, m["date"])
                        if dedup in seen_pair_dates:
                            continue
                        seen_pair_dates.add(dedup)
                        pair_results[pair_key].append(m["won"])
                strong_pairs = sorted(
                    [
                        (pair[0], pair[1], ls, sum(rs), len(rs) - sum(rs))
                        for (pair, ls), rs in pair_results.items()
                        if len(rs) >= 2
                    ],
                    key=lambda x: (
                        -(x[4] == 0 and x[3] >= 2),
                        -(x[3] / (x[3] + x[4])) if (x[3] + x[4]) > 0 else 0,
                        -x[3],
                    ),
                )

                # -- Player trajectories (rising / falling / overperforming) --
                rising_players, falling_players, overperforming_singles = [], [], []
                for pk, matches in pr.items():
                    p = pbn.get(pk)
                    if not p:
                        continue
                    name = p.get("name", pk)
                    bl = p.get("dynamic_rating_baseline")
                    dr = p.get(f"rating_{sfx}")
                    wl_str = p.get(f"wl_record_{sfx}", "") or ""
                    if bl is None or dr is None:
                        continue
                    delta = dr - bl
                    # Only track trajectories for players who are actually
                    # competing at this division's level (not fill-ins with
                    # very low baselines whose swings are noise).
                    meaningful = bl >= div_floor + 0.15
                    if delta >= 0.10 and wl_str and meaningful:
                        rising_players.append((name, bl, dr, wl_str))
                    elif delta <= -0.08 and wl_str and meaningful:
                        falling_players.append((name, bl, dr, wl_str))
                    # Overperforming singles: undefeated with ≥2 singles wins
                    # despite baseline clearly below division expectation
                    singles_ms = [
                        m for m in matches if "Singles" in m.get("line", "")
                    ]
                    sw = sum(1 for m in singles_ms if m["won"])
                    sl_ = len(singles_ms) - sw
                    if sw >= 2 and sl_ == 0 and bl < div_floor + 0.20:
                        overperforming_singles.append((name, sw, sl_, bl))

                rising_players.sort(key=lambda x: -(x[2] - x[1]))
                falling_players.sort(key=lambda x: (x[2] - x[1]))
                overperforming_singles.sort(key=lambda x: -x[1])

                t["notes"] = _compose_team_note(
                    tname=tname,
                    wins=wins,
                    losses=losses,
                    n_team_weeks=n_team_weeks,
                    match_history=mh,
                    every_week_names=every_week_names,
                    sometimes_names=sometimes_names,
                    line_wl=line_wl,
                    player_line_records=player_line_recs,
                    strong_pairs=strong_pairs,
                    missing_stars=missing_stars,
                    rising_players=rising_players,
                    falling_players=falling_players,
                    overperforming_singles=overperforming_singles,
                    is_top_rated_roster=is_top_rated_roster,
                    div_best_singles=best_singles,
                    div_best_singles_key=best_singles_key,
                    team_player_keys=set(pr.keys()),
                )

        (DATA / fname).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"{div_label}: team notes written to {fname}")


def main():
    players = json.loads((DATA / "players.json").read_text())
    pbn = {_name_key(p.get("name", "")): p for p in players if p.get("name")}
    # baseline-only dict (used for lineup context where division is unknown)
    rating = {_name_key(p.get("name", "")): p.get("dynamic_rating_baseline")
              for p in players}
    # Per-division point-in-time rating lookups.
    # pit_by_sfx[sfx](name_key, match_date) returns the player's rating going
    # INTO that match — the pre-match sequential snapshot.
    pit_by_sfx = {
        "30": _make_pit_lookup(players, "30"),
        "35": _make_pit_lookup(players, "35"),
    }
    # Stable per-division rating dicts for contexts where match date is unavailable
    # (e.g. _closest_higher_teammate lineup lookup). Uses final season rating.
    rating_by_sfx = {
        "30": {_name_key(p.get("name", "")): (
                   p.get("rating_30") or p.get("dynamic_rating_baseline"))
               for p in players},
        "35": {_name_key(p.get("name", "")): (
                   p.get("rating_35") or p.get("dynamic_rating_baseline"))
               for p in players},
    }
    # Player name → all known teams (set) for swap detection in new-format lines.
    # Must include division-specific teams (team_30, team_35) so cross-listed
    # players are recognised correctly in both divisions' scorecards.
    team_by_name: dict[str, set] = {}
    for p in players:
        norm = _name_key(p.get("name", ""))
        teams_set: set = set()
        for tf in ("team", "team_30", "team_35"):
            tv = p.get(tf)
            if tv:
                teams_set.add(tv)
        if teams_set:
            team_by_name[norm] = teams_set

    # Load both divisions' match data for cross-division context
    division_data = {}   # sfx -> {"all_dates": [...], "matches_by_player": {...}, "data": {...}}
    for fname, sfx in [("standings_women_30.json", "30"),
                       ("standings_women_35.json", "35")]:
        data = json.loads((DATA / fname).read_text())
        matches_by_player = defaultdict(list)
        all_dates = set()
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if m.get("pending"):
                    continue
                if m.get("date"):
                    all_dates.add(m["date"])

                # Pre-build team lineups for this match so every player record can
                # know who their closest-higher-line teammate was that day.
                # Structure: team -> {line: [(name, rating), ...]}
                _match_lineup: dict = defaultdict(lambda: defaultdict(list))
                for _ln2 in m.get("lines", []):
                    _line2 = _ln2.get("line", "")
                    _resolved2 = _resolve_line_sides(_ln2, m, team_by_name)
                    if not _resolved2:
                        continue
                    _w2, _l2, _wt2, _lt2, _wo2 = _resolved2
                    for _team2, _names2 in [(_wt2, _w2), (_lt2, _l2)]:
                        if _team2 and _names2 and _line2:
                            for _n2 in _names2:
                                if _n2 and _n2.upper() != "N/A":
                                    _match_lineup[_team2][_line2].append(
                                        (_n2, rating.get(_name_key(_n2)))
                                    )

                def _closest_higher_teammate(team, line, self_key):
                    """Return (name, rating, line) of the nearest higher-tier
                    teammate on the SAME line type (singles vs singles, doubles vs
                    doubles).  Cross-type comparisons (D1 vs S1) are meaningless
                    for deployment context and are excluded."""
                    best = None
                    best_tier_diff = float("inf")
                    own_type = _line_type(line)
                    own_tier = _within_type_tier(line)
                    if not own_type:
                        return None
                    for other_line, players in _match_lineup.get(team, {}).items():
                        if _line_type(other_line) != own_type:
                            continue  # only compare same line type
                        diff = _within_type_tier(other_line) - own_tier
                        if diff <= 0:
                            continue
                        for tn, tr in players:
                            if _name_key(tn) == self_key:
                                continue
                            if diff < best_tier_diff or (
                                diff == best_tier_diff and (tr or 0) > (best[1] or 0)
                            ):
                                best = (tn, tr, other_line)
                                best_tier_diff = diff
                    return best

                for ln in m.get("lines", []):
                    resolved = _resolve_line_sides(ln, m, team_by_name)
                    if not resolved:
                        continue
                    w_names, l_names, winner_team, loser_team, walkover = resolved
                    line_label = ln.get("line", "")
                    score = ln.get("score", "")

                    _pit = pit_by_sfx[sfx]
                    _mdate = m.get("date", "")

                    def _build_record(name, own_names, opp_names, won, team):
                        from engine.ratings import _cross_pair_expected
                        k = _name_key(name)
                        # Point-in-time ratings going INTO this match
                        own_r   = _pit(k, _mdate)
                        partners = [n for n in own_names if _name_key(n) != k]
                        partner_r = _pit(_name_key(partners[0]), _mdate) if partners else None
                        opp_pit = {_name_key(n): _pit(_name_key(n), _mdate) for n in opp_names}
                        opp_rs  = [v for v in opp_pit.values() if v is not None]
                        opp_avg = sum(opp_rs) / len(opp_rs) if opp_rs else None
                        # Cross-pair expected win probability using PIT ratings
                        if own_r is not None and opp_rs:
                            expected_prob = _cross_pair_expected(
                                own_r, partner_r, opp_rs)
                        else:
                            expected_prob = None
                        return {
                            "date": _mdate, "line": line_label,
                            "won": won, "opp_names": opp_names,
                            "opp_avg": opp_avg,
                            "opp_pit_ratings": opp_pit,
                            "expected_prob": expected_prob,
                            "score": score,
                            "partner": partners[0] if partners else None,
                            "partner_pit_rating": partner_r,
                            "own_pit_rating": own_r,
                            "walkover": walkover,
                            "winner_team" if won else "loser_team": team,
                            "higher_teammate": _closest_higher_teammate(
                                team, line_label, k),
                        }

                    for name in w_names:
                        k = _name_key(name)
                        matches_by_player[k].append(
                            _build_record(name, w_names, l_names, True, winner_team))
                    for name in l_names:
                        k = _name_key(name)
                        matches_by_player[k].append(
                            _build_record(name, l_names, w_names, False, loser_team))
        division_data[sfx] = {
            "all_dates": sorted(all_dates),
            "matches_by_player": matches_by_player,
            "n_weeks": len(all_dates),
        }

    # Build pair win-rate lookup: frozenset({name_key, name_key}) -> (wins, losses)
    # Used to detect when "surprising" losses came against pairs that proved to be strong.
    # pair_records: sfx -> {frozenset_key: (W, L, win_opp_rating_sum, win_opp_n)}
    # win_opp_rating_sum / win_opp_n = avg opponent rating in wins (strength of schedule proxy).
    pair_records: dict[str, dict[str, tuple]] = {}
    for sfx, fname in [("30", "standings_women_30.json"), ("35", "standings_women_35.json")]:
        _pr: dict[str, tuple] = {}
        _data = json.loads((DATA / fname).read_text())
        for _sf in _data.get("subflights", []):
            for _m in _sf.get("matches", []):
                if _m.get("pending"):
                    continue
                for _ln in _m.get("lines", []):
                    if "Doubles" not in _ln.get("line", ""):
                        continue
                    for _side in ("players_home", "players_away"):
                        _names = [n.strip() for n in _ln.get(_side, "").split("/") if n.strip()]
                        if len(_names) != 2:
                            continue
                        _pk = str(frozenset(_name_key(n) for n in _names))
                        _winner = (_ln.get("winner_team") or "").upper()
                        _side_team = (_m.get("home_team") if _side == "players_home"
                                      else _m.get("away_team") or "").upper()
                        # Resolve actual team via player lookup (handles scorecard swaps)
                        _actual = None
                        for _n in _names:
                            _nk = _name_key(_n)
                            _p = pbn.get(_nk)
                            if _p and _p.get(f"team_{sfx}"):
                                _actual = _p[f"team_{sfx}"].upper()
                                break
                        _actual = _actual or _side_team
                        _won = _actual == _winner
                        _w, _l, _wr_sum, _wr_n = _pr.get(_pk, (0, 0, 0.0, 0))
                        # When this pair wins, accumulate opponent ratings for SoS tracking
                        _opp_rating_sum, _opp_n = 0.0, 0
                        if _won:
                            _opp_side = "players_away" if _side == "players_home" else "players_home"
                            _opp_names = [n.strip() for n in _ln.get(_opp_side, "").split("/") if n.strip()]
                            for _on in _opp_names:
                                _op = pbn.get(_name_key(_on))
                                _or = (_op.get("dynamic_rating_baseline") if _op else None)
                                if _or is not None:
                                    _opp_rating_sum += float(_or)
                                    _opp_n += 1
                        _pr[_pk] = (
                            _w + (1 if _won else 0),
                            _l + (0 if _won else 1),
                            _wr_sum + _opp_rating_sum,
                            _wr_n + _opp_n,
                        )
        pair_records[sfx] = _pr

    # Build per-team deployment rank (within each division)
    # team_deploy_rank[sfx][team] = [(player_name, n_matches), ...] sorted by n_matches desc
    team_deploy = defaultdict(lambda: defaultdict(list))
    for sfx in ("30", "35"):
        for pk, matches in division_data[sfx]["matches_by_player"].items():
            non_wo = [m for m in matches if not m.get("walkover")]
            if not non_wo:
                continue
            p = pbn.get(pk)
            if not p:
                continue
            team = p.get(f"team_{sfx}") or p.get("team", "")
            if team:
                team_deploy[sfx][team].append((p["name"], len(non_wo)))

    # Sort each team's deployment by n_matches desc
    for sfx, teams in team_deploy.items():
        for team, lst in teams.items():
            lst.sort(key=lambda x: -x[1])

    # Generate notes per player
    for sfx, div_label, other_sfx, other_div in [
        ("30", "3.0", "35", "3.5"),
        ("35", "3.5", "30", "3.0"),
    ]:
        notes_field = f"notes_{sfx}"
        this_data = division_data[sfx]
        other_data = division_data[other_sfx]
        n_updated = 0

        for p in players:
            pk = _name_key(p.get("name", ""))
            this_matches = [m for m in this_data["matches_by_player"].get(pk, [])
                           if not m.get("walkover")]
            _other_all = other_data["matches_by_player"].get(pk, [])
            other_matches = [m for m in _other_all if not m.get("walkover")]
            walkover_only_this = (
                not this_matches
                and len(this_data["matches_by_player"].get(pk, [])) > 0
            )

            bl = p.get(f"rating_{sfx}") or p.get("dynamic_rating_baseline")
            dr = p.get(f"rating_{sfx}")
            gr = p.get("global_rating")
            wl_this = p.get(f"wl_record_{sfx}", "")
            wl_other = p.get(f"wl_record_{other_sfx}", "")

            # Build a walkover-corrected display string for the other-division record.
            # Walkover lines are skipped by _resolve_line_sides (N/A side → returns None),
            # so they never appear in matches_by_player / other_matches. We infer them by
            # diffing the stored wl_record (which counts walkovers) vs real match counts.
            _real_other_w = sum(1 for m in other_matches if m["won"])
            _real_other_l = sum(1 for m in other_matches if not m["won"])
            _stored_other_w, _stored_other_l = _real_other_w, _real_other_l
            if wl_other and "-" in str(wl_other):
                try:
                    _stored_other_w, _stored_other_l = map(int, str(wl_other).split("-"))
                except (ValueError, AttributeError):
                    pass
            # Walkovers only produce a free win — there is no such thing as a walkover loss.
            # Any discrepancy in loss counts is a data anomaly; ignore it.
            _other_wo_wins = max(0, _stored_other_w - _real_other_w)
            if _other_wo_wins:
                _wo_str = "a walkover" if _other_wo_wins == 1 else f"{_other_wo_wins} walkovers"
                wl_other_display = f"{_real_other_w}-{_real_other_l} (plus {_wo_str})"
            else:
                wl_other_display = wl_other   # no walkovers — original value is fine

            if bl is None:
                p[notes_field] = ""
                continue

            if not this_matches and not walkover_only_this and not other_matches:
                p[notes_field] = ""
                continue

            # --- Compute key facts ---
            team_this = p.get(f"team_{sfx}") or p.get("team", "")
            team_other = p.get(f"team_{other_sfx}") or ""
            n_this = len(this_matches)
            n_weeks = this_data["n_weeks"]
            deploy_rate_this = n_this / n_weeks if n_weeks else 0

            # Detect record padding by defaults/walkovers:
            # if the official W-L record implies more matches than we have
            # competitive data for, the gap is defaults.
            _wl_wins, _wl_losses = 0, 0
            if wl_this and "-" in str(wl_this):
                try:
                    _wl_wins, _wl_losses = map(int, str(wl_this).split("-"))
                except (ValueError, AttributeError):
                    pass
            _record_padded = n_this < (_wl_wins + _wl_losses)

            # Team deployment rank
            team_depl = team_deploy[sfx].get(team_this, [])
            team_max_count = team_depl[0][1] if team_depl else 0
            # Tied for most deployed? (count how many players have n_this == team_max_count)
            top_deployed = [name for name, n in team_depl if n == team_max_count]
            is_team_max_tied = bool(team_depl) and n_this == team_max_count and n_this >= 3
            is_team_only_max = is_team_max_tied and len(top_deployed) == 1

            # Line versatility (for high-deploy players)
            line_types = set()
            for ll in [m["line"] for m in this_matches]:
                if "1# Singles" in ll: line_types.add("S1")
                elif "2# Singles" in ll: line_types.add("S2")
                elif "1# Doubles" in ll: line_types.add("D1")
                elif "2# Doubles" in ll: line_types.add("D2")
                elif "3# Doubles" in ll: line_types.add("D3")

            wins_this = [m for m in this_matches if m["won"]]
            losses_this = [m for m in this_matches if not m["won"]]

            # Pre-compute S/D split: all wins in one line type, all losses in the other.
            # Used early (before surprising_losses) to suppress "Tough loss" when the split
            # is already the primary story — the loss is just part of "all losses in doubles."
            _sd_split_pre = False
            if wins_this and losses_this:
                _win_ltypes_pre  = set(_line_type(m["line"]) for m in wins_this  if _line_type(m["line"]))
                _loss_ltypes_pre = set(_line_type(m["line"]) for m in losses_this if _line_type(m["line"]))
                _sd_split_pre = (
                    len(_win_ltypes_pre) == 1 and len(_loss_ltypes_pre) == 1
                    and _win_ltypes_pre != _loss_ltypes_pre
                )

            # Peer-level lopsided losses: surprising routs (opponent not >> player).
            # These are the genuinely "shouldn't have happened" losses.
            lopsided_losses = [
                m for m in losses_this
                if _is_lopsided_loss(m)
                and (m["opp_avg"] is None or m["opp_avg"] - bl < 0.15)
            ]

            # Top-line outmatched losses: deployed at D1/S1, clearly beaten by a
            # significantly stronger opponent.  Includes "dominant" scores (6-2 6-1)
            # not just bagels — a decisive top-line loss still tells the story.
            top_line_lopsided_losses = [
                m for m in losses_this
                if _score_descriptor(m.get("score", "")) in ("lopsided", "dominant")
                and _line_short(m["line"]) in ("D1", "S1")
                and _ep(m) is not None and _ep(m) < 0.40
            ]

            tiebreak_wins = [m for m in wins_this if _is_tiebreak(m)]

            # --- Cross-pair win probability classification ---
            # Uses point-in-time ratings (stored in each match record as expected_prob)
            # and the cross-pair model from engine.ratings — correct for doubles because
            # it weights all four individual matchups (top-vs-top, top-vs-bottom, etc.)
            # rather than comparing pair averages.
            #
            # Thresholds:
            #   surprising_wins:    expected_prob < 0.45  (was underdog, won)
            #   surprising_losses:  expected_prob > 0.55  (was favourite, lost)
            #   competitive_losses: expected_prob < 0.40  (heavy underdog, stayed close)

            def _ep(m):
                """expected_prob for match m, falling back to opp_avg-based estimate."""
                ep = m.get("expected_prob")
                if ep is not None:
                    return ep
                # Fallback for records without expected_prob (shouldn't happen normally)
                if m["opp_avg"] is None:
                    return None
                diff = bl - m["opp_avg"]
                from engine.ratings import _win_probability
                return _win_probability(diff)

            surprising_wins = [
                m for m in this_matches
                if m["won"] and _ep(m) is not None and _ep(m) < 0.45
            ]
            # Surprising losses: was a meaningful favourite (>55%) but lost.
            surprising_losses = [
                m for m in this_matches
                if not m["won"] and _ep(m) is not None and _ep(m) > 0.55
            ]

            # Competitive close losses: heavy underdog (<40%) who pushed to a tiebreak
            # or tight set — positive signal even in defeat.
            competitive_losses = [
                m for m in losses_this
                if _ep(m) is not None
                and _ep(m) < 0.40
                and (
                    _is_tiebreak(m)
                    or _score_descriptor(m.get("score", "")) == "tight"
                )
            ]

            # Line-split story: grinds out tiebreaks at lower lines but outmatched at top.
            # Exclude tiebreak wins already highlighted as upsets.
            _surprise_win_set = set(id(m) for m in surprising_wins)
            _fresh_tiebreak_wins = [m for m in tiebreak_wins
                                    if id(m) not in _surprise_win_set]
            _tiebreak_lines = {_line_short(m["line"]) for m in _fresh_tiebreak_wins}
            _top_loss_lines = {_line_short(m["line"]) for m in top_line_lopsided_losses}
            has_line_split = (
                bool(_fresh_tiebreak_wins) and bool(top_line_lopsided_losses)
                and _tiebreak_lines.isdisjoint(_top_loss_lines)
            )

            # --- Assemble note ---
            parts = []

            if walkover_only_this:
                parts.append("Only match was a default — no competitive data.")
            elif n_this == 0 and other_matches:
                # No matches in this division — note it but keep brief
                parts.append(f"No {div_label} matches played yet.")
                if len(other_matches) >= 1:
                    # Describe one interesting cross-division match
                    best = None
                    if any(m["won"] for m in other_matches):
                        best = max(
                            (m for m in other_matches if m["won"]),
                            key=lambda m: (m["opp_avg"] or 0),
                        )
                    else:
                        best = min(
                            other_matches,
                            key=lambda m: abs((m["opp_avg"] or bl) - bl),
                        )
                    desc = _describe_match(best, None, other_data["all_dates"],
                                          include_week=False, player_bl=bl)
                    parts.append(f"In {other_div}: {desc}.")
            else:
                # Lead with team-rank signal if notable.
                # Drop the team name — the reader is already on this player's roster.
                if is_team_only_max and n_this == n_weeks:
                    parts.append("Only every-week player.")
                elif is_team_max_tied and not is_team_only_max and n_this == n_weeks:
                    others = [n for n in top_deployed if n != p["name"]]
                    if len(others) == 1:
                        parts.append(f"Every-week player (with {others[0]}).")
                    else:
                        parts.append("Deployed every week.")
                elif is_team_only_max:
                    parts.append(f"Most-deployed player ({n_this}/{n_weeks} weeks).")
                elif is_team_max_tied:
                    parts.append(f"Among the most-deployed ({n_this}/{n_weeks} weeks).")

                # For very low n_this + rich other_matches, weave the cross-division story
                has_rich_cross = (
                    n_this <= 1 and len(other_matches) >= 1
                    and any(
                        m["opp_avg"] and abs(m["opp_avg"] - bl) > 0.10
                        for m in other_matches
                    )
                )

                # Summarise all surprising wins in ONE sentence, never a per-match list.
                # 1 upset  → "Upset: W3 D1 — beat X at 34% odds."
                # 2 upsets → "Two upsets: W3 D1 — beat X at 34% odds; W5 S2 — beat Y at 38% odds."
                # 3+ upsets → "N upsets this season — most surprising: W3 D1 — beat X at 34% odds."
                if surprising_wins:
                    def _upset_shape_clause(m):
                        """Concise shape word for an upset description."""
                        ph = _score_phrase(m.get("score", ""), True)
                        if ph == "in a third-set tiebreak":
                            return " in a tiebreak"
                        return f" {ph}" if ph else ""

                    def _upset_mini(m, include_line=True):
                        """Mini description for one upset match."""
                        _wk  = _week_number(m["date"], this_data["all_dates"])
                        _l   = _line_short(m["line"])
                        _ptr = m.get("partner")
                        _ptr_c = f" with {_ptr}" if _ptr else ""
                        _opp = _opp_label(m)
                        _shp = _upset_shape_clause(m)
                        _ep2 = _ep(m)
                        _prb = f" at {round(_ep2 * 100)}% odds" if _ep2 is not None else ""
                        _loc = f"{_wk} {_l}" if include_line else _wk
                        return f"{_loc}{_ptr_c} — beat {_opp}{_shp}{_prb}"

                    _count_words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}
                    # Sort most-surprising first (lowest win probability)
                    _sw_sorted = sorted(surprising_wins, key=lambda m: _ep(m) or 1.0)
                    _n = len(_sw_sorted)
                    if _n == 1:
                        parts.append(f"Upset: {_upset_mini(_sw_sorted[0])}.")
                    elif _n == 2:
                        parts.append(
                            f"Two upsets: {_upset_mini(_sw_sorted[0])};"
                            f" {_upset_mini(_sw_sorted[1])}."
                        )
                    else:
                        _nw = _count_words.get(_n, str(_n))
                        parts.append(
                            f"{_nw} upsets this season — most surprising:"
                            f" {_upset_mini(_sw_sorted[0])}."
                        )

                # Below-gate blemish: was a heavy favourite but didn't dominate.
                # Only emit when there's nothing more interesting to say.
                # Suppress when ANY of the following is true:
                #   • there are upsets (even 1) — the positive story speaks louder
                #   • there are surprising losses — far more interesting signal
                #   • there are competitive losses (close battles vs stronger opponents)
                # "Won but failed to dominate" is the lowest-value note we produce —
                # it's filler.  Reserve it for players whose entire note would otherwise
                # be "undefeated" with no other distinguishing moments.
                if (not surprising_wins
                        and not surprising_losses
                        and not competitive_losses):
                    _heavy_fav_blemishes = [
                        m for m in wins_this
                        if _ep(m) is not None and _ep(m) > 0.65
                        and _score_descriptor(m.get("score", "")) in ("tight", "clear", "")
                        and not m.get("walkover")
                    ]
                    if _heavy_fav_blemishes:
                        _blemish = max(_heavy_fav_blemishes, key=lambda m: _ep(m) or 0)
                        _ep_bl   = _ep(_blemish)
                        _wk_bl   = _week_number(_blemish["date"], this_data["all_dates"])
                        _l_bl    = _line_short(_blemish["line"])
                        _opp_bl  = _opp_label(_blemish)
                        _pct_bl  = round(_ep_bl * 100) if _ep_bl is not None else None
                        _shp_bl  = _score_descriptor(_blemish.get("score", ""))
                        _verb_bl = "scraped past" if _shp_bl == "tight" else "won but failed to dominate against"
                        _fav_bl  = f" as {_pct_bl}% favourite" if _pct_bl else ""
                        # If there are also surprising losses, "One blemish" is wrong —
                        # drop "One" so it doesn't imply this is the only negative.
                        _blemish_prefix = "Blemish" if surprising_losses else "One blemish"
                        parts.append(
                            f"{_blemish_prefix}: {_wk_bl} {_l_bl} — {_verb_bl} {_opp_bl}"
                            f"{_fav_bl}."
                        )

                # Competitive close losses — positive framing before the loss analysis.
                # Fires when player lost close matches against significantly stronger
                # opponents.  Fire when:
                #   • there are such losses (tiebreak or tight vs opp ≥0.15 above)
                #   • there are no surprising wins already telling the positive story
                #   • not already covered by a line-split narrative
                # Track insertion index so the arc block can drop this sentence
                # if it ends up covering the same match.
                _comp_loss_part_idx = None
                if competitive_losses and not surprising_wins and not has_line_split:
                    comp_d = [m for m in competitive_losses
                              if _line_type(m["line"]) == "D"]
                    comp_s = [m for m in competitive_losses
                              if _line_type(m["line"]) == "S"]
                    comp_d.sort(key=lambda m: -(m["opp_avg"] or 0))
                    comp_s.sort(key=lambda m: -(m["opp_avg"] or 0))

                    def _comp_mini(m):
                        """Opponent label only — no raw scores, no line, no week.
                        Mentions tiebreak when applicable."""
                        opp = _opp_label(m)
                        d = _score_descriptor(m.get("score", ""))
                        if d == "3-set tiebreak":
                            return f"tiebreak vs {opp}"
                        return opp

                    _comp_loss_part_idx = len(parts)
                    if comp_d and not comp_s:
                        minis = [_comp_mini(m) for m in comp_d[:2]]
                        if len(minis) == 1:
                            parts.append(
                                f"Close doubles loss to a stronger pair — {minis[0]}."
                            )
                        else:
                            parts.append(
                                f"Close doubles losses to stronger pairs — "
                                f"{minis[0]}; {minis[1]}."
                            )
                    elif comp_s and not comp_d:
                        parts.append(
                            f"Close singles loss to a stronger opponent — "
                            f"{_comp_mini(comp_s[0])}."
                        )
                    else:
                        best_c = max(competitive_losses, key=lambda m: m["opp_avg"] or 0)
                        parts.append(
                            f"Close loss to a stronger opponent — "
                            f"{_comp_mini(best_c)}."
                        )

                # Line-split: competitive at lower lines, outmatched at top line.
                # Fuse into one sentence — this is the whole story.
                if has_line_split:
                    wks = sorted({_week_number(m["date"], this_data["all_dates"])
                                  for m in _fresh_tiebreak_wins})
                    wks_str = "/".join(w for w in wks if w)
                    ll_str = "/".join(sorted(_tiebreak_lines,
                                            key=lambda x: (x[0], x[1:])))
                    tl_str = "/".join(sorted(_top_loss_lines,
                                            key=lambda x: (x[0], x[1:])))
                    top_descs = [
                        _describe_match(m, None, this_data["all_dates"])
                        for m in sorted(top_line_lopsided_losses,
                                        key=lambda m: m["date"])[:2]
                    ]
                    tiebreak_clause = (f"tiebreak wins in {wks_str}" if wks_str
                                       else "tiebreak wins")
                    parts.append(
                        f"Competitive at {ll_str} ({tiebreak_clause}) but "
                        f"outmatched at {tl_str}: " + "; ".join(top_descs) + "."
                    )
                else:
                    # Peer-level lopsided losses (these are genuinely surprising)
                    if lopsided_losses:
                        sorted_losses = sorted(lopsided_losses, key=lambda m: m["date"])
                        descs = [
                            _describe_match(m, None, this_data["all_dates"])
                            for m in sorted_losses[:2]
                        ]
                        if len(descs) == 1:
                            parts.append(f"Lopsided loss: {descs[0]}.")
                        else:
                            parts.append(f"Two lopsided losses: {descs[0]}; {descs[1]}.")
                    # Surprising losses: show even when top-line outmatching also present,
                    # since those are separate matches telling different stories.
                    # Exclude matches already covered by top_line_lopsided_losses.
                    _top_loss_ids = {id(m) for m in top_line_lopsided_losses}
                    _sl_not_top = [m for m in surprising_losses
                                   if id(m) not in _top_loss_ids]
                    if _sl_not_top and not lopsided_losses:
                        worst = max(_sl_not_top,
                                   key=lambda m: bl - (m["opp_avg"] or 0))
                        worst_gap = bl - (worst["opp_avg"] or bl)
                        if worst_gap >= 0.10 or _is_tiebreak(worst):
                            # Large gap OR a tiebreak — worth naming the specific match.
                            # Tiebreaks are always interesting: even a small-gap surprise
                            # tiebreak loss shows the player was competitive but couldn't close.
                            desc = _describe_match(worst, None, this_data["all_dates"],
                                                  include_partner=True)
                            _ep_sl = _ep(worst)
                            _prob_sl = (f" (as {round(_ep_sl * 100)}% favourite)"
                                        if _ep_sl is not None else "")
                            # Check if the opposing pair turned out to be strong on the season:
                            # undefeated (or ≥60% win rate) with 2+ matches AND their wins came
                            # against quality opponents (avg opp baseline ≥ threshold).
                            # Suppress "Tough loss" when the S/D split is already the primary story
                            # (the loss is already absorbed into "all losses in doubles").
                            _opp_names_sl = worst.get("opp_names", [])
                            _opp_pair_key = str(frozenset(_name_key(n) for n in _opp_names_sl))
                            _opp_pr = pair_records.get(sfx, {}).get(_opp_pair_key)
                            _div_qual_threshold = 2.75 if sfx == "30" else 3.10
                            _opp_pr_avg_qual = (
                                _opp_pr[2] / _opp_pr[3]
                                if _opp_pr and _opp_pr[3] > 0 else 0.0
                            )
                            _opp_pr_strong = (
                                not _sd_split_pre   # don't "Tough loss" when split is primary story
                                and _opp_pr is not None
                                and sum(_opp_pr[:2]) >= 2
                                and _opp_pr[0] / sum(_opp_pr[:2]) >= 0.60
                                and _opp_pr_avg_qual >= _div_qual_threshold  # won against quality opps
                            )
                            if _opp_pr_strong:
                                # Story-driven prose: use last names, no raw ratings (those were
                                # wrong/underrated — the whole point is the pair was better than
                                # the baseline numbers suggested). Don't repeat score detail
                                # (visible in the UI).
                                _opp_lnames = [n.split()[-1] for n in _opp_names_sl if n]
                                _opp_name_str = "/".join(_opp_lnames) if _opp_lnames else "the opponents"
                                _ptr_sl = worst.get("partner", "")
                                _ptr_fn = _pname(_ptr_sl) if _ptr_sl else ""
                                _ptr_clause = f" alongside {_ptr_fn}" if _ptr_fn else ""
                                _wk_sl = _week_number(worst["date"], this_data["all_dates"])
                                _ln_sl = _line_short(worst.get("line", ""))
                                _opp_record = f"{_opp_pr[0]}-{_opp_pr[1]}"
                                parts.append(
                                    f"Tough loss in {_wk_sl} {_ln_sl}{_ptr_clause} to "
                                    f"{_opp_name_str} — one of the division's top pairs "
                                    f"({_opp_record})."
                                )
                            else:
                                parts.append(
                                    f"Surprising loss: "
                                    f"{desc.replace(' lost to ', ' to ', 1)}"
                                    f"{_prob_sl}."
                                )
                        else:
                            # Small-gap, non-tiebreak losses — only note a pattern,
                            # not a single isolated loss (which says nothing about a 4-1 player).
                            if len(_sl_not_top) > 1:
                                _sl_lines = sorted(
                                    {_line_short(m["line"]) for m in _sl_not_top if m["line"]},
                                    key=lambda x: (x[0], x[1:])
                                )
                                _sl_line_str = "/".join(_sl_lines) if _sl_lines else "doubles"
                                parts.append(
                                    f"Underperforming at {_sl_line_str} — "
                                    f"multiple losses to slightly lower-rated opponents."
                                )

                    # Top-line lopsided losses without a tiebreak contrast.
                    # Format: "Outmatched at S1 by Opp (r) in W2."
                    # The line is already in the prefix — don't repeat it in the body.
                    if top_line_lopsided_losses and not has_line_split:
                        sorted_tlls = sorted(top_line_lopsided_losses,
                                             key=lambda m: m["date"])
                        tl_str = "/".join(sorted(_top_loss_lines,
                                                 key=lambda x: (x[0], x[1:])))

                        def _outmatched_short(m):
                            opp = _opp_label(m)
                            wk = _week_number(m["date"], this_data["all_dates"])
                            return f"{opp} in {wk}" if wk else opp

                        tl_descs = [_outmatched_short(m) for m in sorted_tlls[:2]]
                        if len(tl_descs) == 1:
                            parts.append(f"Outmatched at {tl_str} by {tl_descs[0]}.")
                        else:
                            parts.append(
                                f"Outmatched at {tl_str} — "
                                + "; ".join(tl_descs) + "."
                            )


                # ── Partner correlation ────────────────────────────────────────────
                # Philosophy: every sentence must tell something interesting —
                # not just repeat who was involved. Only emit when the story is
                # about WHAT it means (partner dependency, strength mismatch),
                # not just WHO was paired in losses.
                #
                # NEVER emit:
                #   • "N of M losses came with X" — data dump, not insight
                #   • "all N losses came with X" — same (reader can see who lost)
                #   • "Wins with X" when it's only 1 win and X is already in an
                #     upset sentence above — redundant
                #   • ", and others" appended to a go-to partner — dilutes the story
                #
                # DO emit:
                #   • "Wins reliably with X" (2+ wins with X, no losses with X) —
                #     reveals a go-to chemistry partner
                #   • "Loss with X came against stronger pair; loss with Y was
                #     avoidable" — actionable loss context
                #   • Partner tiebreak-pattern attribution — cross-player insight
                #   • "Partner-dependent" signal when wins require a specific carrier
                _dbl_losses_p = [m for m in losses_this
                                 if _line_type(m["line"]) == "D" and m.get("partner")]
                _dbl_wins_p   = [m for m in wins_this
                                 if _line_type(m["line"]) == "D" and m.get("partner")]
                if len(_dbl_losses_p) >= 2 and len(_dbl_wins_p) >= 2:
                    _loss_ptr_set = set(m["partner"] for m in _dbl_losses_p)
                    _win_ptr_set  = set(m["partner"] for m in _dbl_wins_p)
                    _unique_to_losses = _loss_ptr_set - _win_ptr_set
                    _unique_to_wins   = _win_ptr_set  - _loss_ptr_set
                    if _unique_to_losses and _unique_to_wins:
                        _n_dbl_l = len(_dbl_losses_p)
                        _has_s_wins = any(_line_type(m["line"]) == "S" for m in wins_this)
                        _also_solo = " and solo" if _has_s_wins else ""

                        # Build win clause — no "and others" appended
                        from collections import Counter as _WinPtrCnt
                        _win_ptr_counts = _WinPtrCnt(m["partner"] for m in _dbl_wins_p)
                        _repeated_win_ptrs = [p for p, c in _win_ptr_counts.items()
                                              if c >= 2 and p not in _loss_ptr_set]
                        _diverse_wins = len(_unique_to_wins) >= 3 or (
                            len(_unique_to_wins) >= 2 and not _repeated_win_ptrs
                        )

                        if _diverse_wins:
                            _wp_first = sorted(_unique_to_wins, key=lambda p: p.split()[-1])
                            _wp_str = "/".join(_pname(p) for p in _wp_first)
                            _win_clause = f"across a range of partners ({_wp_str}{_also_solo})"
                        elif _repeated_win_ptrs:
                            # Go-to partner — name them without "and others"
                            _rp_str = "/".join(_pname(p) for p in sorted(_repeated_win_ptrs))
                            _win_clause = f"reliably with {_rp_str}{_also_solo}"
                        else:
                            _wp_str = "/".join(_pname(p) for p in sorted(_unique_to_wins))
                            _win_clause = f"with {_wp_str}{_also_solo}"

                        # Loss context — only emit when it conveys insight
                        if _n_dbl_l == 2:
                            _dl_sorted = sorted(_dbl_losses_p, key=lambda m: -(m.get("opp_avg") or 0))
                            _strong_loss = _dl_sorted[0]
                            _weak_loss   = _dl_sorted[1]
                            _sl_opp = _strong_loss.get("opp_avg") or 0
                            _wl_opp = _weak_loss.get("opp_avg")   or 0
                            if _sl_opp - _wl_opp >= 0.06:
                                # Two losses with clearly different contexts — name the contrast.
                                # This is genuinely insightful: one loss is legitimate, one is avoidable.
                                _sl_ptr = _pname(_strong_loss["partner"])
                                _wl_ptr = _pname(_weak_loss["partner"])
                                parts.append(
                                    f"Wins {_win_clause}. "
                                    f"Loss with {_sl_ptr} came against a stronger pair; "
                                    f"loss with {_wl_ptr} was the avoidable one."
                                )
                            else:
                                # Opponent strengths are similar — only emit if there's a
                                # cross-player tiebreak-loss pattern worth noting.
                                _pattern_ptr = None
                                for _lm in _dbl_losses_p:
                                    _lptr = _lm.get("partner")
                                    if not _lptr: continue
                                    _lptr_key = _name_key(_lptr)
                                    _lptr_matches = this_data["matches_by_player"].get(_lptr_key, [])
                                    _lptr_tb_losses = [m for m in _lptr_matches
                                                       if not m["won"] and _is_tiebreak(m)]
                                    if len(_lptr_tb_losses) >= 3:
                                        _pattern_ptr = _pname(_lptr)
                                        break
                                if _pattern_ptr:
                                    _other_ptr = next(
                                        (_pname(m["partner"]) for m in _dbl_losses_p
                                         if m.get("partner") and _pname(m["partner"]) != _pattern_ptr),
                                        None
                                    )
                                    _already_named = _other_ptr and any(
                                        _other_ptr in p_ for p_ in parts
                                    )
                                    _other_clause = ("" if _already_named
                                                     else f"; loss with {_other_ptr} was the tighter call"
                                                     if _other_ptr else "")
                                    parts.append(
                                        f"Wins {_win_clause}. "
                                        f"Loss with {_pattern_ptr} fits {_pattern_ptr}'s "
                                        f"tiebreak-loss pattern{_other_clause}."
                                    )
                                elif _repeated_win_ptrs:
                                    # Has a clear go-to partner — emit the win story.
                                    # Skip the loss-partner naming (not insightful without a "why").
                                    parts.append(f"Wins {_win_clause}.")
                                # else: nothing insightful to say — skip entirely
                        else:
                            # 3+ losses — only emit if there's a tiebreak-loss pattern to cite.
                            # Naming who all the loss partners are is a data dump.
                            _pattern_ptr = None
                            for _lm in _dbl_losses_p:
                                _lptr = _lm.get("partner")
                                if not _lptr: continue
                                _lptr_key = _name_key(_lptr)
                                _lptr_matches = this_data["matches_by_player"].get(_lptr_key, [])
                                _lptr_tb_losses = [m for m in _lptr_matches
                                                   if not m["won"] and _is_tiebreak(m)]
                                if len(_lptr_tb_losses) >= 3:
                                    _pattern_ptr = _lptr.split()[0]
                                    break
                            if _pattern_ptr:
                                parts.append(
                                    f"Wins {_win_clause}. "
                                    f"Loss with {_pattern_ptr} fits {_pattern_ptr}'s "
                                    f"tiebreak-loss pattern."
                                )
                            elif _repeated_win_ptrs:
                                # Only the win story is interesting — skip loss-partner detail
                                parts.append(f"Wins {_win_clause}.")

                # Tiebreak-loss pattern: 3+ tiebreak losses is a distinct story —
                # signals inability to close tight matches and that it hurts partner records.
                # Fire even when a single tiebreak loss is already mentioned — the PATTERN
                # of three is a different, additive finding.
                # SUPPRESS when the majority of tiebreak losses are against opponents
                # rated significantly above the player's baseline (gap > 0.20) — those are
                # competitive performances against much stronger players, not a "can't close" flaw.
                _tb_losses = [m for m in losses_this if _is_tiebreak(m)]
                _tb_outmatched = [m for m in _tb_losses
                                  if _ep(m) is not None and _ep(m) < 0.40]
                _tb_pattern_legit = len(_tb_losses) >= 3 and len(_tb_outmatched) < len(_tb_losses) * 0.6
                if (_tb_pattern_legit
                        and not any("tiebreak losses" in p_.lower() for p_ in parts)):
                    _tb_partners = [m["partner"] for m in _tb_losses if m.get("partner")]
                    _tb_ptr_str = ""
                    if _tb_partners and len(set(_tb_partners)) >= 2:
                        _unique_tb_ptrs = list(dict.fromkeys(_tb_partners))
                        _names = ', '.join(_pname(p) for p in _unique_tb_ptrs[:3])
                        _tb_ptr_str = (
                            f" — {_names} have each taken a tiebreak loss alongside her"
                        )
                    parts.append(
                        f"{len(_tb_losses)} tiebreak losses this season{_tb_ptr_str}; "
                        f"struggles to close out tight matches."
                    )

                # Singles/doubles split: all wins in one line type, all losses in the other.
                # This is the primary story — name it before underperformance fires.
                _sd_split_fired = False
                if wins_this and losses_this:
                    _win_ltypes  = set(_line_type(m["line"]) for m in wins_this  if _line_type(m["line"]))
                    _loss_ltypes = set(_line_type(m["line"]) for m in losses_this if _line_type(m["line"]))
                    if (len(_win_ltypes) == 1 and len(_loss_ltypes) == 1
                            and _win_ltypes != _loss_ltypes
                            and not any("singles" in p_.lower() or "doubles" in p_.lower() for p_ in parts)):
                        _wtype = list(_win_ltypes)[0]
                        _ltype = list(_loss_ltypes)[0]
                        _wword = "singles" if _wtype == "S" else "doubles"
                        _lword = "singles" if _ltype == "S" else "doubles"
                        # Describe win quality
                        _sw_descs = [_score_descriptor(m.get("score","")) for m in wins_this]
                        _n_dom_sd = sum(1 for d in _sw_descs if d in ("lopsided","dominant","rout"))
                        _dom_clause = " — wins have been dominant" if _n_dom_sd >= len(wins_this) * 0.6 else ""
                        parts.append(
                            f"Undefeated in {_wword}{_dom_clause}; "
                            f"all losses in {_lword}."
                        )
                        _sd_split_fired = True

                # Dynamic-deployment pattern: player has roughly equal numbers of
                # singles and doubles matches (rare — most players specialize).
                # Articulate the W-L split per format so the reader sees whether
                # they're a "dynamic winner" (Megan Bell), "good at one not the
                # other" (Tayoni), or "balanced both ways" (rating stuck).
                if (not _sd_split_fired
                    and not any("singles" in p_.lower() or "doubles" in p_.lower() for p_ in parts)):
                    _s_matches = [m for m in this_matches if _line_type(m["line"]) == "S"]
                    _d_matches = [m for m in this_matches if _line_type(m["line"]) == "D"]
                    if (len(_s_matches) >= 3 and len(_d_matches) >= 3
                            and abs(len(_s_matches) - len(_d_matches)) <= 1):
                        _sw = sum(1 for m in _s_matches if m["won"])
                        _sl = len(_s_matches) - _sw
                        _dw = sum(1 for m in _d_matches if m["won"])
                        _dl = len(_d_matches) - _dw
                        # Categorize the deployment story
                        _both_good = _sw >= _sl and _dw >= _dl and (_sw + _dw) >= 4
                        _both_poor = _sw < _sl and _dw < _dl
                        _singles_only = _sw > _sl and _dw <= _dl
                        _doubles_only = _dw > _dl and _sw <= _sl
                        if _both_good:
                            parts.append(
                                f"Dynamically deployed across formats — wins both in singles "
                                f"({_sw}-{_sl}) and doubles ({_dw}-{_dl})."
                            )
                        elif _both_poor:
                            parts.append(
                                f"Deployed in both singles ({_sw}-{_sl}) and doubles "
                                f"({_dw}-{_dl}) but hasn't separated herself in either."
                            )
                        elif _singles_only:
                            parts.append(
                                f"Singles is the bright spot ({_sw}-{_sl}); doubles "
                                f"hasn't clicked ({_dw}-{_dl})."
                            )
                        elif _doubles_only:
                            parts.append(
                                f"Doubles is the bright spot ({_dw}-{_dl}); singles "
                                f"hasn't clicked ({_sw}-{_sl})."
                            )
                        else:
                            # Even split (e.g., 1-1 both)
                            parts.append(
                                f"Dynamically deployed across formats — singles "
                                f"({_sw}-{_sl}) and doubles ({_dw}-{_dl})."
                            )

                # High-baseline underperformance: a notably strong baseline player
                # with a ≤.500 record is underperforming expectations.
                # SUPPRESS when a S/D split already explains the mixed record.
                # Use dynamic_rating_baseline (the original pre-season number) not the
                # current computed rating, so the sentence reads naturally.
                _orig_bl = p.get("dynamic_rating_baseline")
                _div_floor_local = 2.50 if sfx == "30" else 3.00
                _high_bl_threshold = _div_floor_local + 0.35  # e.g. 2.85 for 3.0 div
                _is_high_bl = _orig_bl is not None and _orig_bl >= _high_bl_threshold
                _win_rate = len(wins_this) / n_this if n_this else 1.0
                _is_underperforming_bl = (
                    _is_high_bl
                    and _win_rate <= 0.50
                    and n_this >= 2
                    and not top_line_lopsided_losses
                    and not _sd_split_fired          # split explains the record — not underperformance
                    and not any("nderperform" in p_.lower() for p_ in parts)
                    and not any("Outmatched" in p_ for p_ in parts)
                )
                if _is_underperforming_bl:
                    parts.append(
                        f"Below expectations for a {_orig_bl:.2f} baseline — "
                        f"losses to opponents she should be beating."
                    )

                # Undefeated with all opponents below baseline — describe the
                # dominance qualitatively rather than labeling it "ceiling-capped."
                # (The ceiling-capped concept is misleading for a top player whose
                # opponents are just lower-rated because she's at the top of the field.)
                all_opps_below = bool(wins_this) and all(
                    m["opp_avg"] is not None and m["opp_avg"] < bl - 0.05
                    for m in wins_this
                )
                if (len(wins_this) >= 2 and not losses_this and all_opps_below
                        and not any("Undefeated" in p_ for p_ in parts)
                        and not surprising_wins):
                    # Compute dominance metrics across all wins
                    _all_reg_sets = []
                    for _wm in wins_this:
                        _sets = re.findall(r"(\d+)-(\d+)", _wm.get("score", ""))
                        _reg = [(int(a), int(b)) for a, b in _sets
                                if not (int(a) <= 1 and int(b) <= 1)]
                        _all_reg_sets.extend(_reg)
                    _all_straight = not any(_is_tiebreak(m) for m in wins_this)
                    _max_conceded = max(
                        (min(a, b) for a, b in _all_reg_sets), default=None
                    )
                    if _max_conceded is not None and _max_conceded <= 1:
                        _game_word = "game" if _max_conceded == 1 else "games"
                        parts.append(
                            f"Undefeated — no opponent has won more than "
                            f"{_max_conceded} {_game_word} off her in any set."
                        )
                    elif _all_straight:
                        parts.append("Undefeated in straight sets.")
                    else:
                        avg_opp = sum(m["opp_avg"] for m in wins_this if m["opp_avg"]) / len(
                            [m for m in wins_this if m["opp_avg"]]
                        )
                        parts.append(
                            f"Undefeated but all opponents below baseline "
                            f"(avg {avg_opp:.2f})."
                        )

                # Single-match story (Prexy case)
                # Drop "Only X match:" prefix — just describe it directly.
                # Pass player_bl so obvious large-gap results suppress their score.
                if n_this == 1 and not surprising_wins and not surprising_losses \
                   and not lopsided_losses and not top_line_lopsided_losses \
                   and not competitive_losses:
                    m0 = this_matches[0]
                    score_desc = _score_descriptor(m0["score"])
                    if score_desc == "3-set tiebreak":
                        _ep0 = _ep(m0)
                        if m0["won"] and _ep0 is not None and _ep0 > 0.60:
                            # Heavy favourite who barely won — the story is the blemish.
                            _pct0  = round(_ep0 * 100)
                            _wk0   = _week_number(m0["date"], this_data["all_dates"])
                            _l0    = _line_short(m0["line"])
                            _opp0  = _opp_label(m0)
                            parts.append(
                                f"One blemish: {_wk0} {_l0} — scraped past {_opp0}"
                                f" in 3 sets as {_pct0}% favourite."
                            )
                        elif m0["won"]:
                            desc = _describe_match(m0, None, this_data["all_dates"],
                                                   include_score=False)
                            parts.append(f"{desc}; won in 3 sets.")
                        else:
                            desc = _describe_match(m0, None, this_data["all_dates"],
                                                   include_score=False)
                            parts.append(f"{desc}; split sets before losing tiebreak.")
                    else:
                        desc = _describe_match(m0, None, this_data["all_dates"],
                                               player_bl=bl)
                        opp_gap = (m0.get("opp_avg") or 0) - bl
                        if not m0["won"] and opp_gap >= 0.25:
                            # Large-gap predictable loss — tell the captain what the
                            # result means (very little) rather than just listing it.
                            line_s = _line_short(m0["line"]) or m0["line"]
                            opp_label = _opp_label(m0)
                            pad_clause = (" Default win pads the record."
                                          if _record_padded else "")
                            parts.append(
                                f"{line_s} loss to {opp_label} — "
                                f"expected result, minimal data.{pad_clause}"
                            )
                        else:
                            parts.append(f"{desc}.")

                # Weave in cross-division context when informative
                if has_rich_cross and other_matches:
                    # Pick the most meaningful cross-division match
                    # Prefer: close match vs much-higher-rated opp (like Prexy vs McNair)
                    most_sig = None
                    for m in other_matches:
                        if m["opp_avg"] and abs(m["opp_avg"] - bl) > 0.10:
                            if most_sig is None:
                                most_sig = m
                                continue
                            # Prefer matches vs higher-rated, close scores
                            sig_score_desc = _score_descriptor(m["score"])
                            if sig_score_desc == "3-set tiebreak":
                                most_sig = m
                                break
                    if most_sig:
                        opp_r = most_sig["opp_avg"] or bl
                        opp_gap = opp_r - bl
                        # Suppress detail when the result is entirely predictable —
                        # player lost to much-higher-rated opponents (gap > 0.20).
                        # The wl_other fallback below will add a brief record note.
                        if not most_sig["won"] and opp_gap > 0.20:
                            pass  # not informative — let wl_other handle it
                        else:
                            # Check if opponent is cross-listed in THIS division
                            opp_names = most_sig["opp_names"]
                            opp_cross_listing = ""
                            if len(opp_names) == 1:
                                op = pbn.get(_name_key(opp_names[0]))
                                if op:
                                    op_team_this = op.get(f"team_{sfx}") or ""
                                    if op_team_this:
                                        opp_cross_listing = (
                                            f", on {_team_short(op_team_this)}'s {div_label} roster"
                                        )
                            desc = _describe_match(most_sig, None,
                                                   other_data["all_dates"],
                                                   include_week=False,
                                                   player_bl=bl)
                            score_desc = _score_descriptor(most_sig["score"])
                            # Reframe the description with cross-listing info
                            if opp_cross_listing:
                                line_s = _line_short(most_sig["line"]) or most_sig["line"]
                                opp = _opp_label(most_sig)
                                phrase = _score_phrase(most_sig.get("score", ""), most_sig["won"])
                                phrase_clause = f" — {phrase}" if phrase else ""
                                if not most_sig["won"] and score_desc == "3-set tiebreak":
                                    parts.append(
                                        f"In {other_div}: pushed {opp}{opp_cross_listing} "
                                        f"to a third-set tiebreak at {line_s}."
                                    )
                                elif most_sig["won"]:
                                    parts.append(
                                        f"In {other_div}: beat {opp}{opp_cross_listing} "
                                        f"at {line_s}{phrase_clause}."
                                    )
                                else:
                                    parts.append(
                                        f"In {other_div}: {line_s} loss to {opp}"
                                        f"{opp_cross_listing}{phrase_clause}."
                                    )
                            else:
                                parts.append(f"In {other_div}: {desc}.")

                            # Add qualitative judgment for Prexy-type case
                            if (not most_sig["won"] and score_desc == "3-set tiebreak"
                                    and opp_gap > 0.10):
                                parts.append(
                                    "Qualitative signal her ceiling is higher than baseline."
                                )

                # Rating trajectory (only when not already explained)
                if dr is not None:
                    delta = dr - bl
                    if abs(delta) >= 0.12 and delta > 0 and not surprising_wins:
                        parts.append("Biggest riser on this roster.")
                    elif (delta < -0.10 and not lopsided_losses
                          and not surprising_losses
                          and not top_line_lopsided_losses
                          and not competitive_losses):
                        parts.append("Rating down without a clear single-match driver.")

                # ---- Deployment arc + teammate context ----
                # Before deciding whether to call something a "promotion" or
                # "calibration", ask: WHY was the player at that line?
                # A player at S2 with a much higher-rated teammate at S1 is there
                # for the obvious reason — that's not a demotion, that's just order.
                # A player "promoted" to S1 when S2 is defaulted isn't a real
                # promotion — they were the only singles player available.
                #
                # Only tell the arc story when it's the primary finding (≤1 existing
                # sentence already in parts) and the arc adds real information.

                _sorted = sorted(this_matches, key=lambda m: m["date"])
                _tier_arc_note = None
                _arc_covered = False

                # NOTE: We intentionally do NOT add a "strategically kept below
                # lower-rated teammate" label here.  The same rating inversion can
                # mean two opposite things:
                #   • Captain knows better (Shi/Darian at D1 despite low baseline)
                #   • Player's rating is inflated and captain is right to deploy lower
                # Results ARE the validation, and those stories are already told by
                # the surprising-wins, lopsided-losses, and arc patterns above.
                # Premature labeling of a deployment as "strategic" vs "demotion"
                # without that results context is more misleading than helpful.

                # We DO use higher_teammate to understand arc context (vacancy
                # detection, natural-slot detection) — see arc block below.

                if (len(_sorted) >= 2
                        and not has_line_split
                        and not top_line_lopsided_losses
                        and len(parts) < 2):
                    _first_tier = _line_tier(_sorted[0]["line"])
                    _last_tier = _line_tier(_sorted[-1]["line"])
                    _tier_delta = _first_tier - _last_tier
                    # Only tell the arc story when both anchor matches are the same
                    # line type (singles vs singles or doubles vs doubles).
                    # Cross-type arcs (D1→S2, S2→D1) don't tell a coherent story.
                    _same_type_arc = (
                        _line_type(_sorted[0]["line"])
                        == _line_type(_sorted[-1]["line"])
                        and bool(_line_type(_sorted[0]["line"]))
                    )
                    if abs(_tier_delta) >= 2 and _same_type_arc:
                        _em = _sorted[0]   # representative early match
                        _lm = _sorted[-1]  # representative late match
                        _el = _line_short(_em["line"])
                        _ll = _line_short(_lm["line"])
                        _eo = _arc_outcome(_em, bl)
                        _lo = _arc_outcome(_lm, bl)

                        # Teammate context (same-type only, already enforced by
                        # _closest_higher_teammate):
                        # "early_natural" = the lower slot was expected because a
                        # much higher-rated same-type teammate occupied the line above.
                        _em_ht = _em.get("higher_teammate")
                        _lm_ht = _lm.get("higher_teammate")
                        _early_natural = (
                            _em_ht is not None
                            and _em_ht[1] is not None
                            and bl is not None
                            and _em_ht[1] > bl + 0.10
                        )

                        if _tier_delta > 0:
                            # Step DOWN. Only meaningful when:
                            # - early match was a LOSS (not tactical re-use)
                            # - the higher slot wasn't just the natural order
                            if not _em["won"] and not _early_natural:
                                _late_desc = _score_descriptor(_lm.get("score", ""))
                                if (not _lm["won"] and _late_desc in ("tight",)
                                        and "tight sets" not in _lo):
                                    _lo_note = f"{_lo} — competitive"
                                else:
                                    _lo_note = _lo
                                _tier_arc_note = (
                                    f"Tried at {_el} ({_eo}), "
                                    f"moved down to {_ll} ({_lo_note})."
                                )
                                _arc_covered = True
                        else:
                            # Step UP — positive arc, but need to check if the
                            # "promotion" was real or just by vacancy.
                            #
                            # Vacancy case: player was at their natural lower slot
                            # (higher-rated anchor above them) and then played the
                            # top line when that anchor wasn't available.
                            _vacancy = (
                                _early_natural        # lower slot was the natural one
                                and _lm_ht is None    # alone at the top in the later match
                            )
                            if _vacancy:
                                # Tell the true story: filled in at the higher line
                                # when the usual anchor wasn't available.
                                _anchor_name = _em_ht[0] if _em_ht else "anchor"
                                _tier_arc_note = (
                                    f"Filled in at {_ll} when {_anchor_name}"
                                    f" was unavailable ({_lo})."
                                )
                            else:
                                _tier_arc_note = (
                                    f"Started at {_el} ({_eo}), "
                                    f"moved up to {_ll} ({_lo})."
                                )
                            _arc_covered = True

                if _tier_arc_note and not any(
                    "Tried at" in p_ or "Started at" in p_ or "At " in p_
                    for p_ in parts
                ):
                    # The arc already narrates the competitive match in detail —
                    # drop the competitive_losses sentence to avoid repeating it.
                    if _arc_covered and _comp_loss_part_idx is not None:
                        parts.pop(_comp_loss_part_idx)
                    parts.append(_tier_arc_note)

                # Deployment extremes (only if arc or existing note didn't cover it)
                played_s1 = any(m["line"] == "1# Singles" for m in this_matches)
                played_d1 = any(m["line"] == "1# Doubles" for m in this_matches)
                has_top = played_s1 or played_d1
                top_label = (
                    "S1/D1" if (played_s1 and played_d1)
                    else "S1" if played_s1
                    else "D1"
                )
                all_d3 = (
                    all(m["line"] == "3# Doubles" for m in this_matches)
                    if this_matches else False
                )
                div_floor = 2.50 if sfx == "30" else 3.00
                already_covered = (
                    _arc_covered
                    or bool(top_line_lopsided_losses)
                    or bool(competitive_losses)
                    or has_line_split
                    or any("outmatched" in p_.lower() or "Deployed at" in p_ for p_ in parts)
                )
                if (has_top and bl < div_floor + 0.20 and not already_covered):
                    parts.append(f"Playing {top_label} despite low baseline.")
                elif (all_d3 and bl >= div_floor + 0.35
                      and not any("D3" in p_ for p_ in parts)):
                    parts.append("Only deployed at D3.")

                # Fallback: for players with matches but nothing above fired
                if not parts:
                    if len(wins_this) >= 2 and not losses_this:
                        avg_opp = None
                        opps = [m["opp_avg"] for m in wins_this if m["opp_avg"]]
                        if opps:
                            avg_opp = sum(opps) / len(opps)
                        _all_straight_fb = not any(_is_tiebreak(m) for m in wins_this)
                        if avg_opp and avg_opp < bl - 0.15 and _all_straight_fb:
                            parts.append(f"Undefeated in straight sets.")
                        elif avg_opp and avg_opp < bl - 0.15:
                            parts.append(
                                f"Undefeated vs opponents below baseline "
                                f"(avg {avg_opp:.2f})."
                            )
                        else:
                            parts.append(f"Undefeated in {div_label}.")
                    elif losses_this and wins_this:
                        # Mixed record with nothing dramatic — produce an insight that goes
                        # BEYOND the visible column data (never echo W-L or line counts).
                        # Priority: trajectory arc > win quality > loss context.
                        _sorted_fb = sorted(this_matches, key=lambda m: m["date"])
                        _all_dates_list = sorted(this_data["all_dates"],
                                                 key=lambda d: _date_sort_key(d))
                        _n_total_dates = len(_all_dates_list)
                        _cutoff = _all_dates_list[_n_total_dates // 3] if _n_total_dates >= 3 else None

                        # Trajectory: all losses in the first third, wins dominating since
                        _early_losses = ([m for m in losses_this
                                          if _cutoff and _date_sort_key(m["date"]) <= _date_sort_key(_cutoff)]
                                         if _cutoff else [])
                        _late_losses  = [m for m in losses_this if m not in _early_losses]
                        _late_wins    = ([m for m in wins_this
                                          if _cutoff and _date_sort_key(m["date"]) > _date_sort_key(_cutoff)]
                                         if _cutoff else [])

                        if _early_losses and not _late_losses and len(_late_wins) >= 2:
                            # Found form: early stumble, winning since
                            _early_loss = _early_losses[0]
                            _el_opp = _opp_label(_early_loss)
                            _el_wk  = _week_number(_early_loss["date"], this_data["all_dates"])
                            # Are the later wins dominant?
                            _late_sdesc = [_score_descriptor(m.get("score","")) for m in _late_wins]
                            _n_dominant = sum(1 for d in _late_sdesc if d in ("lopsided","dominant","rout"))
                            _dom_clause = (f", often dominantly" if _n_dominant >= len(_late_wins) // 2
                                           else "")
                            parts.append(
                                f"Struggled early — {_el_wk} loss to {_el_opp} — "
                                f"but has won {len(_late_wins)} straight since{_dom_clause}."
                            )
                        else:
                            # Describe the one loss contextually without naming W-L numbers
                            _loss = sorted(losses_this, key=lambda m: m["date"])[-1]
                            _loss_ep  = _ep(_loss)
                            _loss_sd  = _score_descriptor(_loss.get("score", ""))
                            _loss_opp = _opp_label(_loss)
                            _loss_wk  = _week_number(_loss["date"], this_data["all_dates"])
                            _loss_opp_avg = _loss.get("opp_avg") or 0
                            if _loss_sd == "3-set tiebreak":
                                parts.append(
                                    f"Solid overall; the one blemish was a tiebreak loss "
                                    f"to {_loss_opp} in {_loss_wk}."
                                )
                            elif _loss_ep is not None and _loss_ep > 0.55:
                                # Check if wins have been dominant — stronger framing
                                _wq = [_score_descriptor(m.get("score","")) for m in wins_this]
                                _n_dom_wq = sum(1 for d in _wq if d in ("lopsided","dominant","rout"))
                                if _n_dom_wq >= len(wins_this) * 0.6:
                                    parts.append(
                                        f"Dominant in wins; the one loss to {_loss_opp} in "
                                        f"{_loss_wk} is the only blemish."
                                    )
                                else:
                                    parts.append(
                                        f"Solid overall; the one loss to {_loss_opp} in "
                                        f"{_loss_wk} was unexpected."
                                    )
                            elif _loss_opp_avg > bl + 0.15:
                                # Loss to clearly stronger opponents — contextualise as expected
                                parts.append(
                                    f"Loss in {_loss_wk} came against significantly "
                                    f"stronger opponents ({_loss_opp}); wins have been consistent."
                                )
                            else:
                                # Describe win quality instead of the loss
                                _win_descs = [_score_descriptor(m.get("score","")) for m in wins_this]
                                _n_dom_w = sum(1 for d in _win_descs if d in ("lopsided","dominant","rout"))
                                if _n_dom_w >= len(wins_this) * 0.6:
                                    parts.append("Wins have been dominant; one loss hasn't changed the picture.")
                                else:
                                    parts.append(
                                        f"Competitive season — loss to {_loss_opp} in {_loss_wk} "
                                        f"is the main blemish."
                                    )

                # Cross-division addendum — weave in naturally when there's an arc.
                # When we already have a tier-arc story, the other-div record + typical
                # line tells you where the player actually belongs.
                cross_mentioned = any(
                    f"In {other_div}:" in p_ or f"in {other_div}" in p_
                    for p_ in parts
                )
                if wl_other and not cross_mentioned:
                    _odl = _other_div_line_summary(other_matches) if other_matches else ""
                    _line_clause = f" at {_odl}" if _odl else ""
                    # Ceiling-cap framing: if strong here but struggling in the other div,
                    # frame the cross-div note as a ceiling signal rather than just a record.
                    # Use real (non-walkover) counts for the ceiling/upside logic.
                    _other_wins, _other_losses = _real_other_w, _real_other_l
                    _strong_here = len(wins_this) >= 2 and len(wins_this) >= len(losses_this)
                    _struggling_other = _other_losses >= 2 and _other_wins <= _other_losses
                    # High-upside signal: player is struggling in other div but their losses
                    # there are tiebreaks against opponents rated significantly above baseline.
                    # That's "competitive when outmatched" — the opposite of a ceiling story.
                    _other_tb_losses = [m for m in other_matches
                                        if not m["won"] and _is_tiebreak(m)]
                    _other_bl = p.get("dynamic_rating_baseline") or bl
                    _other_strong_tb = [m for m in _other_tb_losses
                                        if m.get("opp_avg") and m["opp_avg"] - _other_bl > 0.20]
                    _is_high_upside = (
                        _struggling_other
                        and len(_other_strong_tb) >= 1
                        and len(_other_tb_losses) >= 2
                        and len(_other_strong_tb) >= len(_other_tb_losses) * 0.5
                    )
                    if _strong_here and _is_high_upside:
                        parts.append(
                            f"Competitive in {other_div} despite tougher opponents — "
                            f"tiebreak losses to much stronger players suggest more upside "
                            f"than the {wl_other_display} record shows."
                        )
                    elif _strong_here and _struggling_other:
                        parts.append(
                            f"{wl_other_display} in {other_div}{_line_clause} "
                            f"suggests she's near her ceiling in this division."
                        )
                    else:
                        parts.append(f"Also {wl_other_display} in {other_div}{_line_clause}.")

            note = " ".join(parts).strip()
            # Safety valve: if the note is still too long, drop trailing sentences
            # one at a time rather than cutting mid-word.  400 chars is generous;
            # if we're hitting it the blemish-suppression logic above should have
            # already trimmed the least-interesting content.
            _NOTE_MAX = 400
            if len(note) > _NOTE_MAX:
                # Try dropping the last sentence (the cross-div context line) first,
                # then keep dropping until we're under the limit.
                _sentences = [s.strip() for s in note.split(". ") if s.strip()]
                while len(". ".join(_sentences) + ".") > _NOTE_MAX and len(_sentences) > 1:
                    _sentences.pop()
                note = ". ".join(_sentences)
                if not note.endswith("."):
                    note += "."

            p[notes_field] = note
            n_updated += 1

        print(f"{div_label}: {n_updated} player notes generated")

    (DATA / "players.json").write_text(json.dumps(players, indent=2, ensure_ascii=False))
    print("Saved players.json")

    # Generate team notes and subflight summaries in both standings files
    # Re-read players after notes have been saved so ratings are fresh
    players = json.loads((DATA / "players.json").read_text())
    _generate_team_notes(players, division_data)
    _generate_subflight_summaries(players)


if __name__ == "__main__":
    main()
