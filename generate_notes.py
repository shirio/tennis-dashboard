#!/usr/bin/env python3
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


def _score_descriptor(score: str) -> str:
    """
    Describe the shape of a score string:
      'lopsided' → any bagel (6-0) OR both sets conceded ≤1 (straight sets)
      'dominant' → both sets conceded ≤2 (straight sets)
      'clear' → 6-3 or similar (straight sets)
      'tight' → 6-4 / 7-5 / 7-6 (straight sets)
      '3-set tiebreak' → has a 1-0 or 0-1 third-set tiebreak
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

    min_games = min(min(a, b) for a, b in regular)
    max_conceded = max(min(a, b) for a, b in regular)

    if min_games == 0:
        return "lopsided"
    if max_conceded <= 1:
        return "lopsided"
    if max_conceded == 2:
        return "dominant"
    if max_conceded == 3:
        return "clear"
    return "tight"


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


def _opp_label(m, rating_lookup):
    pieces = []
    for n in m["opp_names"]:
        r = rating_lookup.get(_name_key(n))
        pieces.append(f"{n} ({r:.2f})" if r else n)
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
        return f"{base} — {phrase}"
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
        pt = team_by_name.get(_name_key(pn), "")
        if pt == home_team:
            home_votes += 1
        elif pt == away_team:
            away_votes += 1
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


def main():
    players = json.loads((DATA / "players.json").read_text())
    pbn = {_name_key(p.get("name", "")): p for p in players if p.get("name")}
    rating = {_name_key(p.get("name", "")): p.get("dynamic_rating_baseline")
              for p in players}
    # Player name → primary team lookup (for swap detection in new-format lines)
    team_by_name = {}
    for p in players:
        norm = _name_key(p.get("name", ""))
        for tf in ("team", "team_30", "team_35"):
            tv = p.get(tf)
            if tv:
                team_by_name[norm] = tv
        if p.get("team"):
            team_by_name[norm] = p["team"]

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

                    for name in w_names:
                        k = _name_key(name)
                        opp_rs = [rating.get(_name_key(n)) for n in l_names]
                        opp_rs = [r for r in opp_rs if r is not None]
                        partners = [n for n in w_names if _name_key(n) != k]
                        matches_by_player[k].append({
                            "date": m.get("date", ""), "line": line_label,
                            "won": True, "opp_names": l_names,
                            "opp_avg": sum(opp_rs) / len(opp_rs) if opp_rs else None,
                            "score": score,
                            "partner": partners[0] if partners else None,
                            "walkover": walkover,
                            "winner_team": winner_team,
                            "higher_teammate": _closest_higher_teammate(
                                winner_team, line_label, k),
                        })
                    for name in l_names:
                        k = _name_key(name)
                        opp_rs = [rating.get(_name_key(n)) for n in w_names]
                        opp_rs = [r for r in opp_rs if r is not None]
                        partners = [n for n in l_names if _name_key(n) != k]
                        matches_by_player[k].append({
                            "date": m.get("date", ""), "line": line_label,
                            "won": False, "opp_names": w_names,
                            "opp_avg": sum(opp_rs) / len(opp_rs) if opp_rs else None,
                            "score": score,
                            "partner": partners[0] if partners else None,
                            "walkover": walkover,
                            "loser_team": loser_team,
                            "higher_teammate": _closest_higher_teammate(
                                loser_team, line_label, k),
                        })
        division_data[sfx] = {
            "all_dates": sorted(all_dates),
            "matches_by_player": matches_by_player,
            "n_weeks": len(all_dates),
        }

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
            other_matches = [m for m in other_data["matches_by_player"].get(pk, [])
                            if not m.get("walkover")]
            walkover_only_this = (
                not this_matches
                and len(this_data["matches_by_player"].get(pk, [])) > 0
            )

            bl = p.get("dynamic_rating_baseline")
            dr = p.get(f"rating_{sfx}")
            gr = p.get("global_rating")
            wl_this = p.get(f"wl_record_{sfx}", "")
            wl_other = p.get(f"wl_record_{other_sfx}", "")

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
                and m["opp_avg"] is not None and m["opp_avg"] - bl >= 0.15
            ]

            tiebreak_wins = [m for m in wins_this if _is_tiebreak(m)]

            surprising_wins = [
                m for m in this_matches
                if m["won"] and m["opp_avg"] and m["opp_avg"] - bl > 0.05
            ]
            # Surprising losses: lost to someone below your baseline.
            # Use 0.03 not 0.05 — even a small rating edge matters when you lose.
            surprising_losses = [
                m for m in this_matches
                if not m["won"] and m["opp_avg"] and bl - m["opp_avg"] > 0.03
            ]

            # Competitive close losses: lost a tight match (tiebreak or 6-4/7-5 type)
            # against a notably stronger opponent.  This is a positive signal — the
            # player competed above their level even in defeat.
            # Threshold: opponent ≥0.15 above player.
            competitive_losses = [
                m for m in losses_this
                if m["opp_avg"] is not None
                and m["opp_avg"] - bl >= 0.15
                and (
                    _is_tiebreak(m)
                    or _score_descriptor(m.get("score", "")) == "tight"
                )
            ]

            # Line-split story: grinds out tiebreaks at lower lines but outmatched at top.
            # Only use tiebreak wins not already highlighted individually as upsets.
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
                    desc = _describe_match(best, rating, other_data["all_dates"],
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

                # Versatility note for high-deploy multi-line players.
                # "Flex weapon" only applies when results back it up (winning record).
                # A player deployed everywhere who keeps losing isn't a weapon —
                # they're just core rotation that hasn't found their level.
                if n_this >= 3 and len(line_types) >= 3:
                    lt_str = "/".join(sorted(line_types, key=lambda x: (x[0], x[1])))
                    win_rate = len(wins_this) / n_this
                    if win_rate > 0.5:
                        parts.append(f"Captain's flex weapon — {lt_str}.")

                # For very low n_this + rich other_matches, weave the cross-division story
                has_rich_cross = (
                    n_this <= 1 and len(other_matches) >= 1
                    and any(
                        m["opp_avg"] and abs(m["opp_avg"] - bl) > 0.10
                        for m in other_matches
                    )
                )

                # Describe surprising wins (with rich detail)
                if surprising_wins:
                    best = max(surprising_wins,
                               key=lambda m: (m["opp_avg"] or 0) - bl)
                    desc = _describe_match(best, rating, this_data["all_dates"],
                                          include_partner=True)
                    opp_r = best["opp_avg"]
                    gap = opp_r - bl if opp_r else 0
                    if gap > 0.25 and len(best["opp_names"]) > 1:
                        parts.append(
                            f"Upset: {desc} — implies playing at {opp_r:.2f}+ level."
                        )
                    else:
                        parts.append(f"Upset: {desc}.")

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
                        opp = _opp_label(m, rating)
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
                        _describe_match(m, rating, this_data["all_dates"])
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
                            _describe_match(m, rating, this_data["all_dates"])
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
                            desc = _describe_match(worst, rating, this_data["all_dates"],
                                                  include_partner=True)
                            parts.append(f"Surprising loss: {desc.replace(' lost to ', ' to ', 1)}.")
                        else:
                            # Small-gap, non-tiebreak loss — summarise the pattern.
                            _sl_lines = sorted(
                                {_line_short(m["line"]) for m in _sl_not_top if m["line"]},
                                key=lambda x: (x[0], x[1:])
                            )
                            _sl_line_str = "/".join(_sl_lines) if _sl_lines else "doubles"
                            if len(_sl_not_top) > 1:
                                parts.append(
                                    f"Underperforming at {_sl_line_str} — "
                                    f"multiple losses to slightly lower-rated opponents."
                                )
                            else:
                                parts.append(
                                    f"Underperforming at {_sl_line_str} — "
                                    f"lost to a slightly lower-rated opponent."
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
                            opp = _opp_label(m, rating)
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

                    # Tiebreak wins (not already part of a line-split narrative)
                    if tiebreak_wins and (len(tiebreak_wins) >= 2 or
                                          (len(this_matches) >= 3
                                           and not surprising_wins
                                           and not lopsided_losses)):
                        wks = sorted({_week_number(m["date"], this_data["all_dates"])
                                      for m in tiebreak_wins})
                        wks_str = "/".join(w for w in wks if w)
                        if wks_str and len(tiebreak_wins) >= 2:
                            parts.append(f"Won 3-set tiebreaks in {wks_str}.")
                        elif wks_str:
                            parts.append(f"Won a 3-set tiebreak in {wks_str}.")

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
                        desc = _describe_match(m0, rating, this_data["all_dates"],
                                               include_score=False)
                        if m0["won"]:
                            parts.append(f"{desc}; won in 3 sets.")
                        else:
                            parts.append(f"{desc}; split sets before losing tiebreak.")
                    else:
                        desc = _describe_match(m0, rating, this_data["all_dates"],
                                               player_bl=bl)
                        opp_gap = (m0.get("opp_avg") or 0) - bl
                        if not m0["won"] and opp_gap >= 0.25:
                            # Large-gap predictable loss — tell the captain what the
                            # result means (very little) rather than just listing it.
                            line_s = _line_short(m0["line"]) or m0["line"]
                            opp_label = _opp_label(m0, rating)
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
                            desc = _describe_match(most_sig, rating,
                                                   other_data["all_dates"],
                                                   include_week=False,
                                                   player_bl=bl)
                            score_desc = _score_descriptor(most_sig["score"])
                            # Reframe the description with cross-listing info
                            if opp_cross_listing:
                                line_s = _line_short(most_sig["line"]) or most_sig["line"]
                                opp = _opp_label(most_sig, rating)
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
                                # Tell the true story: natural lower-line player,
                                # filled in at the top when needed.
                                _anchor_name = _em_ht[0] if _em_ht else "anchor"
                                _anchor_line = _line_short(_em_ht[2]) if _em_ht else _el
                                _tier_arc_note = (
                                    f"Natural {_el} with {_anchor_name} above;"
                                    f" played {_ll} by vacancy ({_lo})."
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
                        pass  # mixed record, no standout story

                # Cross-division addendum — weave in naturally when there's an arc.
                # When we already have a tier-arc story, the other-div record + typical
                # line tells you where the player actually belongs.
                cross_mentioned = any(
                    f"In {other_div}:" in p_ or f"in {other_div}" in p_
                    for p_ in parts
                )
                if wl_other and not cross_mentioned:
                    if _arc_covered and other_matches:
                        _odl = _other_div_line_summary(other_matches)
                        _line_clause = f" at {_odl}" if _odl else ""
                        parts.append(
                            f"More settled in {other_div} — {wl_other}{_line_clause}."
                        )
                    else:
                        parts.append(f"Also {wl_other} in {other_div}.")

            note = " ".join(parts).strip()
            if len(note) > 400:
                note = note[:397] + "..."

            p[notes_field] = note
            n_updated += 1

        print(f"{div_label}: {n_updated} player notes generated")

    (DATA / "players.json").write_text(json.dumps(players, indent=2, ensure_ascii=False))
    print("Saved players.json")


if __name__ == "__main__":
    main()
