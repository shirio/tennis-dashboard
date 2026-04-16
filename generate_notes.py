#!/usr/bin/env python3
"""
Generate per-division player notes (notes_30, notes_35) using normalized match data.
Notes explain what's INTERESTING — why the rating moved (or didn't), surprising results,
and cross-division context. Never repeat data visible in other columns.
"""
import json, re
from pathlib import Path
from collections import defaultdict

DATA = Path("data")


def _name_key(name):
    return re.sub(r"\s+", " ", name.lower().strip())


def main():
    players = json.loads((DATA / "players.json").read_text())
    pbn = {_name_key(p.get("name", "")): p for p in players if p.get("name")}
    rating = {_name_key(p.get("name", "")): p.get("dynamic_rating_baseline") for p in players}

    for div_label, fname, sfx in [
        ("3.0", "standings_women_30.json", "30"),
        ("3.5", "standings_women_35.json", "35"),
    ]:
        data = json.loads((DATA / fname).read_text())

        # Collect per-player match details FOR THIS DIVISION ONLY
        player_matches = defaultdict(list)

        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if m.get("pending"):
                    continue
                for ln in m.get("lines", []):
                    winners_raw = ln.get("winners", "")
                    losers_raw = ln.get("losers", "")
                    if not winners_raw or not losers_raw:
                        continue

                    is_walkover = (
                        winners_raw.strip().upper() == "N/A"
                        or losers_raw.strip().upper() == "N/A"
                        or not winners_raw.strip()
                        or not losers_raw.strip()
                    )
                    winner_names = [n.strip() for n in winners_raw.split("/")]
                    loser_names = [n.strip() for n in losers_raw.split("/")]

                    for wn in winner_names:
                        wk = _name_key(wn)
                        opp_ratings = [rating.get(_name_key(n)) for n in loser_names]
                        opp_ratings_clean = [r for r in opp_ratings if r is not None]
                        partner_names = [n for n in winner_names if _name_key(n) != wk]
                        player_matches[wk].append({
                            "date": m.get("date", ""), "line": ln.get("line", ""),
                            "won": True, "opp_names": loser_names,
                            "opp_avg": sum(opp_ratings_clean) / len(opp_ratings_clean) if opp_ratings_clean else None,
                            "score": ln.get("score", ""),
                            "partner": partner_names[0] if partner_names else None,
                            "walkover": is_walkover,
                        })

                    for ln2 in loser_names:
                        lk = _name_key(ln2)
                        opp_ratings = [rating.get(_name_key(wn)) for wn in winner_names]
                        opp_ratings_clean = [r for r in opp_ratings if r is not None]
                        partner_names = [n for n in loser_names if _name_key(n) != lk]
                        player_matches[lk].append({
                            "date": m.get("date", ""), "line": ln.get("line", ""),
                            "won": False, "opp_names": winner_names,
                            "opp_avg": sum(opp_ratings_clean) / len(opp_ratings_clean) if opp_ratings_clean else None,
                            "score": ln.get("score", ""),
                            "partner": partner_names[0] if partner_names else None,
                            "walkover": is_walkover,
                        })

        # Count total match weeks
        dates = set()
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if not m.get("pending") and m.get("date"):
                    dates.add(m["date"])
        n_weeks = len(dates)

        notes_field = f"notes_{sfx}"
        n_updated = 0

        for p in players:
            pk = _name_key(p.get("name", ""))
            all_matches = player_matches.get(pk, [])
            # Filter out walkovers for analysis
            matches = [m for m in all_matches if not m.get("walkover")]
            walkover_only = len(all_matches) > 0 and len(matches) == 0

            bl = p.get("dynamic_rating_baseline")
            dr = p.get(f"rating_{sfx}")
            gr = p.get("global_rating")
            other_sfx = "35" if sfx == "30" else "30"
            other_wl = p.get(f"wl_record_{other_sfx}", "")
            other_div = "3.5" if sfx == "30" else "3.0"

            if bl is None or (not all_matches and not walkover_only):
                p[notes_field] = ""
                continue

            delta = (dr - bl) if dr else 0
            wins = [m for m in matches if m["won"]]
            losses = [m for m in matches if not m["won"]]
            n_matches = len(matches)
            line_labels = [m["line"] for m in matches]
            deploy_rate = n_matches / n_weeks if n_weeks else 0

            # Detect conditions
            all_opps_below = (
                all(m["opp_avg"] is not None and m["opp_avg"] < bl - 0.05 for m in wins)
                if wins else False
            )
            surprising_wins = [m for m in matches if m["won"] and m["opp_avg"] and m["opp_avg"] - bl > 0.05]
            surprising_losses = [m for m in matches if not m["won"] and m["opp_avg"] and bl - m["opp_avg"] > 0.05]

            def _opp_label(m):
                pieces = []
                for n in m["opp_names"]:
                    r = rating.get(_name_key(n))
                    pieces.append(f"{n} ({r:.2f})" if r else n)
                return " + ".join(pieces)

            # --- BUILD NOTE ---
            parts = []

            if walkover_only:
                parts.append("Only match was a default — no competitive data.")
            elif n_matches == 0:
                pass
            else:
                # Lead with the most interesting result
                if surprising_wins:
                    best = max(surprising_wins, key=lambda m: (m["opp_avg"] or 0) - bl)
                    label = _opp_label(best)
                    opp_r = best["opp_avg"]
                    if opp_r and (opp_r - bl) > 0.15 and len(best["opp_names"]) > 1:
                        parts.append(f"Upset win vs {label} — implies playing at {opp_r:.2f}+ level.")
                    elif opp_r and (opp_r - bl) > 0.15:
                        parts.append(f"Upset win vs {label}.")
                    else:
                        parts.append(f"Beat higher-rated {label}.")

                if surprising_losses:
                    worst = max(surprising_losses, key=lambda m: bl - (m["opp_avg"] or 0))
                    parts.append(f"Lost to lower-rated {_opp_label(worst)}.")

                # For multi-match players, tell the fuller story
                if n_matches >= 3:
                    doubles_matches = [m for m in matches if "Doubles" in m["line"]]
                    singles_matches = [m for m in matches if "Singles" in m["line"]]
                    line_types = set()
                    for ll in line_labels:
                        if "1# Singles" in ll: line_types.add("S1")
                        elif "2# Singles" in ll: line_types.add("S2")
                        elif "1# Doubles" in ll: line_types.add("D1")
                        elif "2# Doubles" in ll: line_types.add("D2")
                        elif "3# Doubles" in ll: line_types.add("D3")

                    # Every-week player with line versatility
                    if deploy_rate >= 0.9 and len(line_types) >= 3:
                        parts.append(f"One of the most deployed players on the roster — {'/'.join(sorted(line_types))} across {n_matches} weeks.")
                    elif deploy_rate >= 0.9:
                        parts.append(f"Deployed every week.")

                    # Mention the notable loss if there is one and we haven't already
                    if losses and not surprising_losses:
                        best_loss = min(losses, key=lambda m: abs((m["opp_avg"] or bl) - bl))
                        is_3set = "1-0" in best_loss["score"] or "0-1" in best_loss["score"]
                        if is_3set:
                            parts.append(f"Only loss: tight 3-setter vs {_opp_label(best_loss)}.")

                    # If all doubles and opponents all weak, say so
                    if not singles_matches and doubles_matches and len(parts) <= 1:
                        avg_opp = sum(m["opp_avg"] for m in doubles_matches if m["opp_avg"]) / max(1, len([m for m in doubles_matches if m["opp_avg"]]))
                        if avg_opp < bl - 0.15:
                            parts.append(f"All doubles opponents well below baseline (avg {avg_opp:.2f}).")

                    # Add splits if there are both types and we have room
                    if doubles_matches and singles_matches and len(parts) <= 2:
                        d_wins = sum(1 for m in doubles_matches if m["won"])
                        d_losses = len(doubles_matches) - d_wins
                        s_wins = sum(1 for m in singles_matches if m["won"])
                        s_losses = len(singles_matches) - s_wins
                        parts.append(f"Doubles {d_wins}-{d_losses}, singles {s_wins}-{s_losses}.")

                # Explain WHY the rating is where it is
                if abs(delta) < 0.02:
                    if len(wins) >= 2 and all_opps_below:
                        parts.append("Rating flat: all opponents below baseline, ceiling-capped until tested against stronger competition.")
                    elif len(wins) == 1 and not losses and n_matches == 1:
                        m0 = matches[0]
                        is_d1 = m0["line"] in ("1# Singles", "1# Doubles")
                        is_3set = "1-0" in m0["score"] or "0-1" in m0["score"]
                        opp_below = m0["opp_avg"] and m0["opp_avg"] < bl
                        if is_d1 and is_3set and opp_below:
                            parts.append("Won a close D1 match but opponent was near/below baseline — proves D1 caliber but no upside evidence yet.")
                        elif opp_below:
                            parts.append("Won but opponent below baseline — no upside evidence yet.")
                    elif losses and wins:
                        pass  # wins and losses roughly cancel, nothing interesting to say
                elif n_matches == 1:
                    # Single match — explain the context with opponent specifics
                    m0 = matches[0]
                    is_d1 = m0["line"] in ("1# Singles", "1# Doubles")
                    is_3set = "1-0" in m0["score"] or "0-1" in m0["score"]
                    opp_label = _opp_label(m0)
                    if m0["won"]:
                        if m0.get("opp_avg") and m0["opp_avg"] < bl:
                            if is_d1 and is_3set:
                                parts.append(f"Tight D1 win vs {opp_label} — proves D1 caliber but no upside evidence yet.")
                            else:
                                parts.append(f"Won vs {opp_label} — no upside evidence yet.")
                    else:
                        if is_d1 and is_3set:
                            opp_r = m0["opp_avg"]
                            if opp_r and opp_r > bl:
                                parts.append(f"Took higher-rated {opp_label} to 3 sets at D1.")
                            else:
                                parts.append(f"Lost a tight 3-set D1 match vs {opp_label}.")
                        elif m0.get("opp_avg") and m0["opp_avg"] > bl + 0.05:
                            parts.append(f"Loss to higher-rated {opp_label} — expected.")
                        elif m0.get("opp_avg") and m0["opp_avg"] < bl - 0.05:
                            pass  # surprising_losses already handles this

                elif 0.02 <= delta < 0.15:
                    if all_opps_below and wins:
                        parts.append("Modest rise but ceiling-capped — hasn't faced anyone near baseline yet.")
                elif delta > 0.15:
                    parts.append("Biggest riser on this roster.")
                elif -0.10 <= delta <= -0.02:
                    if not parts:
                        parts.append(f"Slight decline.")
                elif delta < -0.10:
                    if surprising_losses:
                        parts.append("Losses to weaker opponents drive the decline.")

                # Deployment signals (only extremes)
                has_top = any(ll in ("1# Singles", "1# Doubles") for ll in line_labels)
                all_d3 = all(ll == "3# Doubles" for ll in line_labels) if line_labels else False
                div_floor = 2.50 if sfx == "30" else 3.00

                if has_top and bl < div_floor + 0.20:
                    parts.append("Playing S1/D1 despite low baseline.")
                elif all_d3 and bl >= div_floor + 0.35:
                    parts.append("Only deployed at D3.")

            # Cross-division context (explain the global diff if meaningful)
            if gr and dr and abs(gr - dr) > 0.05 and other_wl:
                if gr > dr:
                    parts.append(f"Global higher ({gr:.2f}) from {other_wl} in {other_div}.")
                else:
                    parts.append(f"Global lower ({gr:.2f}) — {other_wl} in {other_div} pulls it down.")
            elif other_wl:
                parts.append(f"Also {other_wl} in {other_div}.")

            note = " ".join(parts).strip()
            if len(note) > 300:
                note = note[:297] + "..."

            p[notes_field] = note
            n_updated += 1

        print(f"{div_label}: {n_updated} player notes generated")

    (DATA / "players.json").write_text(json.dumps(players, indent=2, ensure_ascii=False))
    print("Saved players.json")


if __name__ == "__main__":
    main()
