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
      'crushing' → any bagel (6-0) OR both sets conceded ≤1 (straight sets)
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

    min_games = min(min(a, b) for a, b in regular)  # lowest game count in any set
    max_conceded = max(min(a, b) for a, b in regular)

    # Any bagel (0 games) is a crushing signal for the loser, regardless of other set
    if min_games == 0:
        return "lopsided"
    # Both sets conceded ≤1 — also crushing
    if max_conceded <= 1:
        return "lopsided"
    if max_conceded == 2:
        return "dominant"
    if max_conceded == 3:
        return "clear"
    return "tight"


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


def _describe_match(m, rating_lookup, all_dates_in_division, include_week=True,
                    include_score=True) -> str:
    """Short phrase describing a match: 'W2 D1 loss to Shirey+Frazier (6-0 6-3)'."""
    wk = _week_number(m["date"], all_dates_in_division) if include_week else ""
    line = _line_short(m["line"])
    verb = "beat" if m["won"] else "lost to"
    # For concise form, use just last names of opponents
    opp = _opp_label(m, rating_lookup)
    prefix = f"{wk} {line}" if wk else line
    base = f"{prefix} {verb} {opp}"
    if include_score and m.get("score"):
        return f"{base} ({m['score']})"
    return base


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


def main():
    players = json.loads((DATA / "players.json").read_text())
    pbn = {_name_key(p.get("name", "")): p for p in players if p.get("name")}
    rating = {_name_key(p.get("name", "")): p.get("dynamic_rating_baseline")
              for p in players}

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
                for ln in m.get("lines", []):
                    w = ln.get("winners", "")
                    l = ln.get("losers", "")
                    if not w or not l:
                        continue
                    w_names = [n.strip() for n in w.split("/")]
                    l_names = [n.strip() for n in l.split("/")]
                    walkover = any(
                        n.upper() == "N/A"
                        for n in ([w.strip(), l.strip()] + w_names + l_names)
                    )
                    for name in w_names:
                        k = _name_key(name)
                        opp_rs = [rating.get(_name_key(n)) for n in l_names]
                        opp_rs = [r for r in opp_rs if r is not None]
                        partners = [n for n in w_names if _name_key(n) != k]
                        matches_by_player[k].append({
                            "date": m.get("date", ""), "line": ln.get("line", ""),
                            "won": True, "opp_names": l_names,
                            "opp_avg": sum(opp_rs) / len(opp_rs) if opp_rs else None,
                            "score": ln.get("score", ""),
                            "partner": partners[0] if partners else None,
                            "walkover": walkover,
                            "winner_team": ln.get("winner_team", ""),
                        })
                    for name in l_names:
                        k = _name_key(name)
                        opp_rs = [rating.get(_name_key(n)) for n in w_names]
                        opp_rs = [r for r in opp_rs if r is not None]
                        partners = [n for n in l_names if _name_key(n) != k]
                        matches_by_player[k].append({
                            "date": m.get("date", ""), "line": ln.get("line", ""),
                            "won": False, "opp_names": w_names,
                            "opp_avg": sum(opp_rs) / len(opp_rs) if opp_rs else None,
                            "score": ln.get("score", ""),
                            "partner": partners[0] if partners else None,
                            "walkover": walkover,
                            "loser_team": ln.get("loser_team", ""),
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

            # Top-line lopsided losses: deployed at D1/S1 and routed by stronger opponents.
            # Tells the "are they ready for the next level?" story.
            top_line_lopsided_losses = [
                m for m in losses_this
                if _is_lopsided_loss(m)
                and _line_short(m["line"]) in ("D1", "S1")
                and m["opp_avg"] is not None and m["opp_avg"] - bl >= 0.15
            ]

            tiebreak_wins = [m for m in wins_this if _is_tiebreak(m)]

            surprising_wins = [
                m for m in this_matches
                if m["won"] and m["opp_avg"] and m["opp_avg"] - bl > 0.05
            ]
            surprising_losses = [
                m for m in this_matches
                if not m["won"] and m["opp_avg"] and bl - m["opp_avg"] > 0.05
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
                    desc = _describe_match(best, rating, other_data["all_dates"], include_week=False)
                    parts.append(f"In {other_div}: {desc}.")
            else:
                # Lead with team-rank signal if notable
                if is_team_only_max and n_this == n_weeks:
                    parts.append(f"{team_this}'s only every-week player.")
                elif is_team_max_tied and not is_team_only_max and n_this == n_weeks:
                    # Tied for every-week — list the other names
                    others = [n for n in top_deployed if n != p["name"]]
                    if len(others) == 1:
                        parts.append(f"{team_this}'s every-week player (with {others[0]}).")
                    else:
                        parts.append(f"Deployed every week ({team_this}).")
                elif is_team_only_max:
                    parts.append(f"{team_this}'s most-deployed player ({n_this}/{n_weeks} weeks).")
                elif is_team_max_tied:
                    parts.append(f"Among {team_this}'s most-deployed ({n_this}/{n_weeks} weeks).")

                # Versatility note for high-deploy multi-line players
                if n_this >= 3 and len(line_types) >= 3:
                    lt_str = "/".join(sorted(line_types, key=lambda x: (x[0], x[1])))
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
                    desc = _describe_match(best, rating, this_data["all_dates"])
                    opp_r = best["opp_avg"]
                    gap = opp_r - bl if opp_r else 0
                    if gap > 0.25 and len(best["opp_names"]) > 1:
                        parts.append(
                            f"Upset: {desc} — implies playing at {opp_r:.2f}+ level."
                        )
                    else:
                        parts.append(f"Upset: {desc}.")

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
                    elif surprising_losses and not lopsided_losses \
                            and not top_line_lopsided_losses:
                        worst = max(surprising_losses,
                                   key=lambda m: bl - (m["opp_avg"] or 0))
                        desc = _describe_match(worst, rating, this_data["all_dates"])
                        parts.append(f"Surprising loss: {desc}.")

                    # Top-line lopsided losses without a tiebreak contrast
                    if top_line_lopsided_losses and not has_line_split:
                        sorted_tlls = sorted(top_line_lopsided_losses,
                                             key=lambda m: m["date"])
                        descs = [
                            _describe_match(m, rating, this_data["all_dates"])
                            for m in sorted_tlls[:2]
                        ]
                        tl_str = "/".join(sorted(_top_loss_lines,
                                                 key=lambda x: (x[0], x[1:])))
                        if len(descs) == 1:
                            parts.append(
                                f"Deployed at {tl_str} but outmatched: {descs[0]}."
                            )
                        else:
                            parts.append(
                                f"Deployed at {tl_str}, outmatched both times: "
                                + "; ".join(descs) + "."
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

                # Undefeated vs all-below-baseline (ceiling-capped) — even alongside other notes
                all_opps_below = bool(wins_this) and all(
                    m["opp_avg"] is not None and m["opp_avg"] < bl - 0.05
                    for m in wins_this
                )
                if (len(wins_this) >= 2 and not losses_this and all_opps_below
                        and not any("ceiling-capped" in p_ for p_ in parts)
                        and not surprising_wins):
                    avg_opp = sum(m["opp_avg"] for m in wins_this if m["opp_avg"]) / len(
                        [m for m in wins_this if m["opp_avg"]]
                    )
                    parts.append(
                        f"Undefeated but all opponents below baseline "
                        f"(avg {avg_opp:.2f}) — ceiling-capped."
                    )

                # Single-match story (Prexy case)
                if n_this == 1 and not surprising_wins and not surprising_losses \
                   and not lopsided_losses:
                    m0 = this_matches[0]
                    score_desc = _score_descriptor(m0["score"])
                    if score_desc == "3-set tiebreak":
                        # Use prose — omit redundant score
                        desc = _describe_match(m0, rating, this_data["all_dates"],
                                               include_score=False)
                        if m0["won"]:
                            parts.append(f"Only {div_label} match: {desc}; won after 3 sets.")
                        else:
                            parts.append(f"Only {div_label} match: {desc}; split sets before losing tiebreak.")
                    else:
                        desc = _describe_match(m0, rating, this_data["all_dates"])
                        parts.append(f"Only {div_label} match: {desc}.")

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
                                            f", on {op_team_this}'s {div_label} roster"
                                        )
                            desc = _describe_match(most_sig, rating,
                                                   other_data["all_dates"],
                                                   include_week=False)
                            score_desc = _score_descriptor(most_sig["score"])
                            # Reframe the description with cross-listing info
                            if opp_cross_listing:
                                # Construct a more narrative version
                                line_s = _line_short(most_sig["line"]) or most_sig["line"]
                                opp = _opp_label(most_sig, rating)
                                if not most_sig["won"] and score_desc == "3-set tiebreak":
                                    parts.append(
                                        f"In {other_div}: pushed {opp}{opp_cross_listing} "
                                        f"to 3 sets at {line_s} ({most_sig['score']})."
                                    )
                                elif most_sig["won"]:
                                    parts.append(
                                        f"In {other_div}: beat {opp}{opp_cross_listing} "
                                        f"at {line_s} ({most_sig['score']})."
                                    )
                                else:
                                    parts.append(
                                        f"In {other_div}: {line_s} loss to {opp}"
                                        f"{opp_cross_listing} ({most_sig['score']})."
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
                          and not surprising_losses):
                        parts.append("Rating down without a clear single-match driver.")

                # Deployment extremes (only if not already mentioned)
                has_top = any(ll in ("1# Singles", "1# Doubles")
                              for ll in [m["line"] for m in this_matches])
                all_d3 = (
                    all(m["line"] == "3# Doubles" for m in this_matches)
                    if this_matches else False
                )
                div_floor = 2.50 if sfx == "30" else 3.00
                if (has_top and bl < div_floor + 0.20
                        and not any("S1/D1" in p_ for p_ in parts)):
                    parts.append("Playing S1/D1 despite low baseline.")
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
                        if avg_opp and avg_opp < bl - 0.15:
                            parts.append(
                                f"Undefeated vs opponents below baseline "
                                f"(avg {avg_opp:.2f}) — ceiling-capped."
                            )
                        else:
                            parts.append(f"Undefeated in {div_label}.")
                    elif losses_this and wins_this:
                        pass  # mixed record, no standout story

                # Cross-division addendum (brief) if not already woven in
                cross_mentioned = any(
                    f"In {other_div}:" in p_ or f"in {other_div}" in p_
                    for p_ in parts
                )
                if wl_other and not cross_mentioned:
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
