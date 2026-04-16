#!/usr/bin/env python3
"""
Generate per-division player notes (notes_30, notes_35) using normalized match data.
Notes are division-specific and focus on what's INTERESTING, not repeating column data.
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
        player_matches = defaultdict(list)  # name_key -> [{date, line, won, opp_names, opp_ratings, score, winner_team, loser_team}]

        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if m.get("pending"):
                    continue
                for ln in m.get("lines", []):
                    winners_raw = ln.get("winners", "")
                    losers_raw = ln.get("losers", "")
                    if not winners_raw or not losers_raw:
                        continue

                    winner_names = [n.strip() for n in winners_raw.split("/")]
                    loser_names = [n.strip() for n in losers_raw.split("/")]

                    for wn in winner_names:
                        wk = _name_key(wn)
                        opp_ratings = [rating.get(_name_key(ln), None) for ln in loser_names]
                        opp_ratings = [r for r in opp_ratings if r is not None]
                        partner_names = [n for n in winner_names if _name_key(n) != wk]
                        player_matches[wk].append({
                            "date": m.get("date", ""),
                            "line": ln.get("line", ""),
                            "won": True,
                            "opp_names": loser_names,
                            "opp_avg": sum(opp_ratings) / len(opp_ratings) if opp_ratings else None,
                            "score": ln.get("score", ""),
                            "partner": partner_names[0] if partner_names else None,
                            "match_teams": f'{m.get("home_team")} vs {m.get("away_team")}',
                        })

                    for ln2 in loser_names:
                        lk = _name_key(ln2)
                        opp_ratings = [rating.get(_name_key(wn), None) for wn in winner_names]
                        opp_ratings = [r for r in opp_ratings if r is not None]
                        partner_names = [n for n in loser_names if _name_key(n) != lk]
                        player_matches[lk].append({
                            "date": m.get("date", ""),
                            "line": ln.get("line", ""),
                            "won": False,
                            "opp_names": winner_names,
                            "opp_avg": sum(opp_ratings) / len(opp_ratings) if opp_ratings else None,
                            "score": ln.get("score", ""),
                            "partner": partner_names[0] if partner_names else None,
                            "match_teams": f'{m.get("home_team")} vs {m.get("away_team")}',
                        })

        # Count total match weeks
        dates = set()
        for sf in data.get("subflights", []):
            for m in sf.get("matches", []):
                if not m.get("pending") and m.get("date"):
                    dates.add(m["date"])
        n_weeks = len(dates)

        # Generate notes per player
        notes_field = f"notes_{sfx}"
        n_updated = 0

        for p in players:
            pk = _name_key(p.get("name", ""))
            matches = player_matches.get(pk, [])
            if not matches:
                p[notes_field] = ""
                continue

            bl = p.get("dynamic_rating_baseline")
            dr = p.get("current_division_rating")
            gr = p.get("global_rating")
            wl = p.get(f"wl_record_{sfx}", "")
            lines_raw = p.get(f"lines_played_{sfx}", [])
            other_sfx = "35" if sfx == "30" else "30"
            other_wl = p.get(f"wl_record_{other_sfx}", "")

            if bl is None:
                p[notes_field] = ""
                continue

            delta = (dr - bl) if dr else 0
            n_matches = len(matches)
            wins = [m for m in matches if m["won"]]
            losses = [m for m in matches if not m["won"]]
            deploy_rate = n_matches / n_weeks if n_weeks else 0

            # Parse line types
            line_labels = [m["line"] for m in matches]

            # Find surprising results
            surprising_wins = []
            surprising_losses = []
            for m in matches:
                if m["opp_avg"] is None:
                    continue
                gap = bl - m["opp_avg"]  # positive = player is favorite
                if m["won"] and gap < -0.05:
                    # Upset win (beat higher-rated opponent)
                    surprising_wins.append(m)
                elif m["won"] and gap > 0.25:
                    # Stomped much weaker opponent — not notable
                    pass
                elif not m["won"] and gap > 0.05:
                    # Upset loss (lost to lower-rated opponent)
                    surprising_losses.append(m)
                elif not m["won"] and gap < -0.25:
                    # Expected loss against much stronger — not notable
                    pass

            # Build note
            parts = []

            # 1. Most interesting result (concise — no scores, just the takeaway)
            if surprising_wins:
                best = max(surprising_wins, key=lambda m: (m["opp_avg"] or 0) - bl)
                opp_r = best["opp_avg"]
                gap = opp_r - bl if opp_r else 0
                if gap > 0.15:
                    parts.append(f"Upset win vs {'+'.join(best['opp_names'])} ({opp_r:.2f}).")
                else:
                    parts.append(f"Beat higher-rated {'+'.join(best['opp_names'])}.")

            if surprising_losses:
                worst = max(surprising_losses, key=lambda m: bl - (m["opp_avg"] or 0))
                opp_r = worst["opp_avg"]
                parts.append(f"Lost to lower-rated {'+'.join(worst['opp_names'])} ({opp_r:.2f}).")

            # 2. Deployment context — only flag truly meaningful signals:
            #    S1/D1 for a low-baseline player = positive signal
            #    D3 for a high-baseline player = negative signal
            #    Everything else (S2, D2) is neutral and not worth noting
            has_top_line = any(ll in ("1# Singles", "1# Doubles") for ll in line_labels)
            has_d3 = any(ll == "3# Doubles" for ll in line_labels)
            all_d3 = all(ll == "3# Doubles" for ll in line_labels)
            div_floor = 2.50 if sfx == "30" else 3.00
            high_baseline = bl >= div_floor + 0.35  # e.g., 2.85+ in 3.0 or 3.35+ in 3.5

            if has_top_line and bl < div_floor + 0.20:
                parts.append("Playing S1/D1 despite low baseline.")
            elif all_d3 and high_baseline:
                parts.append("Only deployed at D3.")

            if n_matches <= 1:
                parts.append("Limited data.")

            # 3. Rating trajectory (only the WHY, not the numbers)
            if delta > 0.20:
                parts.append("Biggest riser on this roster.")
            elif delta < -0.10:
                if surprising_losses:
                    parts.append("Losses to weaker opponents drive the decline.")
                elif deploy_rate < 0.4:
                    parts.append("Low deployment reinforces downgrade.")

            # 4. Cross-division addendum (brief)
            if other_wl:
                other_div = "3.5" if sfx == "30" else "3.0"
                parts.append(f"Also {other_wl} in {other_div}.")

            note = " ".join(parts).strip()
            if len(note) > 250:
                note = note[:247] + "..."

            if not note:
                note = ""

            p[notes_field] = note
            n_updated += 1

        print(f"{div_label}: {n_updated} player notes generated")

    (DATA / "players.json").write_text(json.dumps(players, indent=2, ensure_ascii=False))
    print("Saved players.json")


if __name__ == "__main__":
    main()
