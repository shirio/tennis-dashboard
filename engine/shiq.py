"""
engine/shiq.py
Make ShiQ v9 (engine/ratings_v9.py) the rating every page shows.

run_ratings() (v8) still runs first, because it fills baselines, Dec numbers
and other fields the pages read. apply_v9() then overwrites the ShiQ fields
with v9 values:

    global_rating, current_division_rating, rating_30, rating_35
        -> v9 shared rating (doubles strength)
    rating_timeline_30/35      -> v9 rating going INTO each match date
    rating_post_timeline_30/35 -> v9 rating after the last match that date

and adds:

    shiq_doubles  v9 shared rating r
    shiq_singles  r + singles offset (only for players with a singles line)
    shiq_singles_matches / shiq_matches  match counts behind those numbers
    shiq_v8       the old v8 number, kept for reference

Players with no scored match keep their baseline, as before.
"""
from __future__ import annotations

from engine import ratings as R
from engine import ratings_v9 as V9


def _div_key(division: str) -> str | None:
    if division == "3.5":
        return "35"
    if division in ("3.0", "2.5"):
        return "30"
    return None


def compute(players: list[dict]):
    pbn = R._build_players_by_name(players)
    events = R._collect_court_events(R._discover_standings_files(), pbn)
    events.sort(key=R._date_sort_key)
    res = V9.run_v9(events, V9.baselines_from_players(players), timelines=True)
    return pbn, res


def apply_v9(players: list[dict] | None = None, save: bool = True) -> dict:
    own = players is None
    if own:
        players = R._load(R.PLAYERS_JSON, [])
    pbn, res = compute(players)
    n = 0
    for k, p in pbn.items():
        # Call right after run_ratings(), while global_rating is still v8's.
        p["shiq_v8"] = p.get("global_rating")
        if k not in res.matches:
            for f in ("shiq_singles", "shiq_doubles", "shiq_matches",
                      "shiq_singles_matches"):
                p.pop(f, None)
            bl = p.get("dynamic_rating_baseline")
            if bl is not None:
                for f in ("global_rating", "current_division_rating",
                          "rating_30", "rating_35"):
                    p[f] = bl
                for div in ("30", "35"):
                    p[f"rating_timeline_{div}"] = {}
                    p[f"rating_post_timeline_{div}"] = {}
            continue
        r = round(res.rating[k], 4)
        for f in ("global_rating", "current_division_rating", "rating_30", "rating_35"):
            p[f] = r
        p["shiq_doubles"] = r
        p["shiq_matches"] = res.matches[k]
        sm = res.singles_matches.get(k, 0)
        p["shiq_singles_matches"] = sm
        if sm:
            p["shiq_singles"] = round(res.singles(k), 4)
        else:
            p.pop("shiq_singles", None)
        for div in ("30", "35"):
            p[f"rating_timeline_{div}"] = {}
            p[f"rating_post_timeline_{div}"] = {}
        for (division, date), (pre, post) in res.timeline.get(k, {}).items():
            dk = _div_key(division)
            if dk is None:
                continue
            p[f"rating_timeline_{dk}"][date] = round(pre, 4)
            p[f"rating_post_timeline_{dk}"][date] = round(post, 4)
        n += 1
    if own and save:
        R._save(R.PLAYERS_JSON, players)
    print(f"  [shiq v9] {n} players rated from {len(res.pre_match)} court lines")
    return {"players": n, "lines": len(res.pre_match)}


if __name__ == "__main__":
    apply_v9()
