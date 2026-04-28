#!/usr/bin/env python3
"""
Walk through a player's sequential rating history match by match.

For each match: shows who they played, the score, expected win probability,
scenario signal, surprise, and exactly why the rating moved (or didn't).

Usage:
    python3 debug_player.py "Amy Arbeli"
    python3 debug_player.py "Anna Clark" --division 3.5
    python3 debug_player.py "Anna Clark" --division 3.0
"""
from __future__ import annotations
import argparse, json, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from engine.ratings import (
    _name_key, _parse_player_names, _parse_sets, _set_dominance,
    _scenario_signal, _cross_pair_expected, _match_adjustment,
    _sequential_match_adj, _detect_scorecard_swap,
    _date_str_sort_key, _ROUT_THRESHOLD,
    PLAYERS_JSON, STANDINGS_30, STANDINGS_35,
    MatchRecord, CourtEvent,
)

# ── Scenario descriptions keyed by signal table entries ──────────────────────
_2SET_DESC = {
    (True,  True,  True,  True):  "Rout win S1 + Rout win S2",
    (True,  False, True,  True):  "Even win S1 + Rout win S2 (finished dominant)",
    (True,  True,  True,  False): "Rout win S1 + Even win S2",
    (True,  False, True,  False): "Even win S1 + Even win S2",
    (False, True,  False, True):  "Rout loss S1 + Rout loss S2",
    (False, False, False, True):  "Even loss S1 + Rout loss S2 (fell apart at end)",
    (False, True,  False, False): "Rout loss S1 + Even loss S2",
    (False, False, False, False): "Even loss S1 + Even loss S2",
}
_3SET_DESC = {
    (False, False, True,  True,  True):  "Even loss S1 + Rout win S2 → won TB (momentum shifted, prevailed)",
    (True,  True,  False, False, True):  "Rout win S1 + Even loss S2 → won TB (blip in S2, still better)",
    (False, True,  True,  True,  True):  "Rout loss S1 + Rout win S2 → won TB (hard shift, won)",
    (False, False, True,  False, True):  "Even loss S1 + Even win S2 → won TB (slight shift, clinched)",
    (False, True,  True,  False, True):  "Rout loss S1 + Even win S2 → won TB (grinded it out)",
    (True,  False, False, False, True):  "Even win S1 + Even loss S2 → won TB (even match, clinched)",
    (True,  True,  False, True,  True):  "Rout win S1 + Rout loss S2 → won TB (scary, but clinched)",
    (True,  False, False, True,  True):  "Even win S1 + Rout loss S2 → won TB (barely survived)",
    (True,  False, False, True,  False): "Even win S1 + Rout loss S2 → lost TB (had lead, fell apart)",
    (True,  True,  False, True,  False): "Rout win S1 + Rout loss S2 → lost TB (dominated S1, collapsed)",
    (False, True,  True,  False, False): "Rout loss S1 + Even win S2 → lost TB (grinded back, still lost)",
    (True,  False, False, False, False): "Even win S1 + Even loss S2 → lost TB (had edge, slipped away)",
    (False, False, True,  False, False): "Even loss S1 + Even win S2 → lost TB (even match, momentum vs)",
    (True,  True,  False, False, False): "Rout win S1 + Even loss S2 → lost TB (dominated S1, lost TB)",
    (False, True,  True,  True,  False): "Rout loss S1 + Rout win S2 → lost TB (hard shift, couldn't close)",
    (False, False, True,  True,  False): "Even loss S1 + Rout win S2 → lost TB (routed S2, still lost)",
}


def _scenario_desc(sets: list, won: bool) -> str:
    if len(sets) < 2:
        return "no score"
    s1, s2 = sets[0], sets[1]
    s1_pw   = s1[2] == won
    s2_pw   = s2[2] == won
    s1_rout = _set_dominance(s1[0], s1[1]) > _ROUT_THRESHOLD
    s2_rout = _set_dominance(s2[0], s2[1]) > _ROUT_THRESHOLD
    if len(sets) == 3:
        return _3SET_DESC.get((s1_pw, s1_rout, s2_pw, s2_rout, won), "unrecognised 3-set scenario")
    return _2SET_DESC.get((s1_pw, s1_rout, s2_pw, s2_rout), "unrecognised 2-set scenario")


def _fmt_r(r: float) -> str:
    return f"{r:.4f}"


def _explain_adj(
    player_r: float,
    rec: MatchRecord,
    running: dict[str, float],
) -> str:
    """Return a multi-line explanation block for one match."""
    SEQ_CAP = 0.15
    MIN_WIN_SURPRISE = 0.15
    BELOW_GATE_SCALE = 0.15

    expected = _cross_pair_expected(player_r, rec.partner_rating, rec.opponent_ratings)
    sets      = _parse_sets(rec.score)
    adj_final = _sequential_match_adj(player_r, rec)

    lines = []

    # Expected win prob
    pct = f"{expected*100:.1f}%"
    if expected >= 0.65:
        label = "heavy favourite"
    elif expected >= 0.55:
        label = "moderate favourite"
    elif expected >= 0.45:
        label = "roughly even"
    elif expected >= 0.35:
        label = "slight underdog"
    else:
        label = "heavy underdog"
    lines.append(f"  Expected win:       {pct}  ({label})")

    if not sets:
        lines.append("  Score:              not available — using outcome-only fallback")
        lines.append(f"  Adjustment:         {adj_final:+.4f}")
        return "\n".join(lines)

    raw_signal    = _scenario_signal(sets, rec.won)
    expected_sig  = 2.0 * expected - 1.0
    surprise      = raw_signal - expected_sig
    desc          = _scenario_desc(sets, rec.won)

    lines.append(f"  Scenario:           {desc}")
    lines.append(f"  Raw signal:        {raw_signal:+.2f}  (from scenario table)")
    lines.append(f"  Expected signal:   {expected_sig:+.3f}  (2 × {expected:.3f} − 1)")
    lines.append(f"  Surprise:          {surprise:+.3f}  (raw − expected)")

    # Singles amplifier
    singles = rec.partner_rating is None
    if singles:
        lines.append(f"  Singles ×1.25:      applied (cleaner 1v1 signal)")

    if rec.won and adj_final <= 0:
        # Below-gate win
        surplus = surprise
        gate_gap = MIN_WIN_SURPRISE - surplus
        penalty  = gate_gap * BELOW_GATE_SCALE
        lines.append(f"  Gate check:         surplus {surplus:+.3f} < {MIN_WIN_SURPRISE} gate → BELOW GATE")
        lines.append(f"  Below-gate penalty: −({MIN_WIN_SURPRISE} − {surplus:.3f}) × {BELOW_GATE_SCALE} = −{penalty:.4f}")
    elif rec.won:
        if expected < 0.50:
            win_cap = SEQ_CAP * (1.0 - expected)
            formula = f"SEQ_CAP × (1−{expected:.3f})  [linear — underdog]"
        else:
            win_cap = SEQ_CAP * 2.0 * (1.0 - expected) ** 2
            formula = f"SEQ_CAP × 2 × (1−{expected:.3f})²  [favourite]"
        lines.append(f"  Gate check:         surplus {surprise:+.3f} ≥ {MIN_WIN_SURPRISE} gate ✓")
        lines.append(f"  Win cap:           {win_cap:.4f}  ({formula})")
    else:
        # Loss
        loss_cap = SEQ_CAP * expected ** 2
        if expected < 0.50 and surprise >= 0.0:
            lines.append(f"  Underdog floor:     surprise {surprise:+.3f} ≥ 0 → adj floored at 0")
        elif expected < 0.50 and surprise < 0.0:
            lines.append(f"  Underdog floor:     surprise {surprise:+.3f} < 0 → floor NOT applied (lost worse than expected)")
        lines.append(f"  Loss cap:          {loss_cap:.4f}  (SEQ_CAP × {expected:.3f}²)")

    lines.append(f"  Adjustment:        {adj_final:+.4f}")
    return "\n".join(lines)


def _week_label(date: str, all_dates: list[str]) -> str:
    try:
        idx = all_dates.index(date)
        return f"W{idx + 1}"
    except ValueError:
        return "??"


def _load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def _collect_events_and_weeks(
    standings_path: Path,
    players_by_key: dict,
) -> tuple[list[CourtEvent], list[str]]:
    """Return (events, sorted_unique_dates)."""
    data    = _load_json(standings_path)
    ntrp    = data.get("ntrp", "?")
    team_lk = {k: p.get("team", "") for k, p in players_by_key.items()}
    events  = []
    dates: set[str] = set()
    seen: set[tuple] = set()

    for sf in data.get("subflights", []):
        for match in sf.get("matches", []):
            if match.get("pending"):
                continue
            date     = match.get("date", "")
            match_id = match.get("match_id", "")
            _swap    = _detect_scorecard_swap(match, team_lk)
            dates.add(date)

            for ln in match.get("lines", []):
                line_label = ln.get("line", "")
                key = (match_id, line_label)
                if key in seen:
                    continue
                seen.add(key)

                ph = (ln.get("players_home") or "").strip().upper()
                pa = (ln.get("players_away") or "").strip().upper()
                if not ph or ph in ("N/A", "DEFAULT", "NOT AVAILABLE"):
                    continue
                if not pa or pa in ("N/A", "DEFAULT", "NOT AVAILABLE"):
                    continue

                w_raw = ln.get("winners", "")
                l_raw = ln.get("losers", "")
                if not w_raw or not l_raw:
                    home_raw   = ln.get("players_home", "")
                    away_raw   = ln.get("players_away", "")
                    result_raw = ln.get("result", "").strip().lower()
                    if not home_raw or not away_raw or result_raw not in ("home", "away"):
                        continue
                    if _swap:
                        home_raw, away_raw = away_raw, home_raw
                    w_raw, l_raw = (home_raw, away_raw) if result_raw == "home" else (away_raw, home_raw)

                wk = [_name_key(n) for n in _parse_player_names(w_raw) if _name_key(n) in players_by_key]
                lk = [_name_key(n) for n in _parse_player_names(l_raw) if _name_key(n) in players_by_key]
                if not wk and not lk:
                    continue

                events.append(CourtEvent(
                    date=date, match_id=match_id, line_label=line_label,
                    division=ntrp, winner_keys=wk, loser_keys=lk,
                    score=ln.get("score", ""),
                ))

    sorted_dates = sorted(dates, key=_date_str_sort_key)
    return events, sorted_dates


def debug_player(name: str, division: str | None = None):
    players_list = _load_json(PLAYERS_JSON)
    players_by_key = {_name_key(p.get("name", "")): p for p in players_list if p.get("name")}

    pk = _name_key(name)
    if pk not in players_by_key:
        # Try fuzzy match
        candidates = [k for k in players_by_key if name.lower() in k]
        if len(candidates) == 1:
            pk = candidates[0]
        elif len(candidates) > 1:
            print(f"Ambiguous name '{name}'. Matches: {[players_by_key[k]['name'] for k in candidates]}")
            return
        else:
            print(f"Player '{name}' not found. Try a partial name.")
            return

    player  = players_by_key[pk]
    pname   = player["name"]
    baseline = player.get("dynamic_rating_baseline")
    if baseline is None:
        print(f"{pname} has no baseline — cannot trace sequential rating.")
        return

    # Determine which division to trace
    pdiv = player.get("division", "")
    if division:
        div = division
    elif "3.5" in pdiv:
        div = "3.5"
    else:
        div = "3.0"

    standings = STANDINGS_30 if div == "3.0" else STANDINGS_35
    events, all_dates = _collect_events_and_weeks(standings, players_by_key)

    # Filter to this division and sort chronologically
    div_events = sorted(
        (ev for ev in events if ev.division == div),
        key=lambda e: _date_str_sort_key(e.date),
    )

    # Find all matches involving this player
    my_events = [ev for ev in div_events if pk in ev.winner_keys + ev.loser_keys]
    if not my_events:
        print(f"{pname} has no matches in {div} division.")
        return

    # ── Header ────────────────────────────────────────────────────────────────
    width = 62
    print("═" * width)
    print(f"  {pname.upper()}  ·  {div} Division")
    print(f"  Baseline: {_fmt_r(baseline)}")
    print("═" * width)

    # Run sequential for the WHOLE division so opponent ratings are accurate
    running: dict[str, float] = {k: p.get("dynamic_rating_baseline", 3.0)
                                  for k, p in players_by_key.items()
                                  if p.get("dynamic_rating_baseline") is not None}

    events_by_date: dict[str, list[CourtEvent]] = defaultdict(list)
    for ev in div_events:
        events_by_date[ev.date].append(ev)

    for date in sorted(events_by_date.keys(), key=_date_str_sort_key):
        today = events_by_date[date]

        involved = set()
        for ev in today:
            involved.update(ev.winner_keys + ev.loser_keys)
        snap = {k: running.get(k, players_by_key.get(k, {}).get("dynamic_rating_baseline", 3.0))
                for k in involved}

        updates: dict[str, float] = {}

        for ev in today:
            # ── Print this event if our player is in it ──────────────────────
            if pk in ev.winner_keys + ev.loser_keys:
                won     = pk in ev.winner_keys
                side_k  = ev.winner_keys if won else ev.loser_keys
                opp_k   = ev.loser_keys  if won else ev.winner_keys
                partner_k = [k for k in side_k if k != pk]

                partner_name = players_by_key[partner_k[0]]["name"] if partner_k else None
                partner_r    = snap.get(partner_k[0]) if partner_k else None
                opp_names    = [players_by_key[k]["name"] if k in players_by_key else k for k in opp_k]
                opp_rs       = [snap.get(k, players_by_key.get(k, {}).get("dynamic_rating_baseline", 3.0))
                                 for k in opp_k]

                prev_r = updates.get(pk, snap[pk])

                rec = MatchRecord(
                    opponent_ratings=opp_rs,
                    partner_rating=partner_r,
                    won=won,
                    date=date,
                    division=div,
                    match_id=ev.match_id,
                    line_label=ev.line_label,
                    score=ev.score,
                )
                adj = _sequential_match_adj(prev_r, rec)

                wl      = "WON ✓" if won else "LOST ✗"
                week    = _week_label(date, all_dates)
                print()
                print(f"  {week} · {date} · {ev.line_label}")
                if partner_name:
                    print(f"  With:   {partner_name} ({_fmt_r(partner_r)})")
                if len(opp_names) == 1:
                    print(f"  vs:     {opp_names[0]} ({_fmt_r(opp_rs[0])})")
                else:
                    opp_str = " + ".join(f"{n} ({_fmt_r(r)})" for n, r in zip(opp_names, opp_rs))
                    print(f"  vs:     {opp_str}")
                print(f"  Score:  {ev.score or '—'}  ·  {wl}")
                print()
                print(_explain_adj(prev_r, rec, snap))
                print()
                new_r = round(prev_r + adj, 4)
                arrow = "↑" if adj > 0.0001 else ("↓" if adj < -0.0001 else "→")
                print(f"  Rating: {_fmt_r(prev_r)} {arrow} {_fmt_r(new_r)}  ({adj:+.4f})")
                print("  " + "─" * (width - 2))

                updates[pk] = new_r

            # ── Process all other players normally (keep opponent ratings live) ─
            for side_keys, opp_keys, won_flag in [
                (ev.winner_keys, ev.loser_keys, True),
                (ev.loser_keys, ev.winner_keys, False),
            ]:
                for k in side_keys:
                    if k not in running:
                        continue
                    if k == pk:
                        continue   # already handled above
                    partners = [x for x in side_keys if x != k]
                    p_r  = snap.get(partners[0]) if partners else None
                    o_rs = [snap.get(x, running.get(x, 3.0)) for x in opp_keys] or [3.0]
                    r    = MatchRecord(
                        opponent_ratings=o_rs, partner_rating=p_r,
                        won=won_flag, date=date, division=div,
                        match_id=ev.match_id, line_label=ev.line_label, score=ev.score,
                    )
                    prev = updates.get(k, snap[k])
                    updates[k] = round(prev + _sequential_match_adj(prev, r), 4)

        running.update(updates)

    final = running.get(pk, baseline)
    print()
    print(f"  FINAL RATING: {_fmt_r(final)}  (started at {_fmt_r(baseline)}, Δ {final - baseline:+.4f})")
    print("═" * width)


def main():
    ap = argparse.ArgumentParser(description="Walk through a player's sequential rating history.")
    ap.add_argument("player", help='Player name, e.g. "Amy Arbeli"')
    ap.add_argument("--division", choices=["3.0", "3.5"], help="Force a specific division (default: player's primary)")
    args = ap.parse_args()
    debug_player(args.player, args.division)


if __name__ == "__main__":
    main()
