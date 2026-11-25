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
    shiq_timeline {date: [ShiQ going in, ShiQ after]} across every level
    shiq_lines    {"match id|line": [ShiQ going in, ShiQ after]}, one per line

Players with no scored match keep their baseline, as before.
"""
from __future__ import annotations

import json

from engine import ratings as R
from engine import ratings_v9 as V9


def _div_key(division: str) -> str | None:
    if division == "3.5":
        return "35"
    if division in ("3.0", "2.5"):
        return "30"
    return None


# Starting points from the previous season's crawl (tested 2026-11-25 on the
# 2026 matches: first-half accuracy 74.7% -> 75.6%, Brier 0.1716 -> 0.1678;
# little change late in the season, when 2026 results dominate).
PRIOR_YEAR = 2025
PRIOR_MIN_MATCHES = 3        # crawled-only players: 2025 ShiQ replaces "level - 0.25"
PRIOR_SETTLED_MATCHES = 5    # enough 2025 matches to drop the provisional boost
PRIOR_CORE_WEIGHT = 0.5      # core players: blend with the Dec 2025 TennisRecord baseline


def prior_year_ratings(crawl: dict, keys: dict[str, str], pbn: dict,
                       year: int = PRIOR_YEAR) -> tuple[dict[str, float], dict[str, int]]:
    """End-of-season ShiQ from the previous year's crawl, keyed like this
    year's events. People are matched across years by their TennisLink
    history link (stable per person), NV names by players.json key.
    Everyone starts the old season at their roster level - 0.25."""
    from engine import crawl_events as C
    old = C.load_crawl(year)
    if not old:
        return {}, {}
    by_url = {v.get("history_url"): keys[pid] for pid, v in crawl.get("people", {}).items()
              if v.get("history_url") and pid in keys}
    ev, old_keys = C.crawl_events(old, pbn, [])
    remap = {k: by_url.get(old["people"][pid].get("history_url")) or k
             for pid, k in old_keys.items()}
    start: dict[str, float] = {}
    for pid, k in old_keys.items():
        person = old["people"][pid]
        try:
            b = float(person.get("ntrp")) - 0.25     # that season's roster level
        except (TypeError, ValueError):
            b = C.baseline_for(person)
        if b is not None:
            start.setdefault(remap[k], b)
    events = []
    for e in ev:
        if sum(V9.games_won(e.score)) <= 0 or len(e.winner_keys) != len(e.loser_keys):
            continue
        e.winner_keys = [remap.get(x, x) for x in e.winner_keys]
        e.loser_keys = [remap.get(x, x) for x in e.loser_keys]
        events.append(e)
    events.sort(key=R._date_sort_key)
    res = V9.run_v9(events, start, provisional_keys=frozenset(start))
    return dict(res.rating), dict(res.matches)


def compute(players: list[dict], use_crawl: bool = True):
    """Run v9 over the standings data plus the TennisLink team crawl."""
    from engine import crawl_events as C
    pbn = R._build_players_by_name(players)
    events = R._collect_court_events(R._discover_standings_files(), pbn)
    baselines = V9.baselines_from_players(players)
    provisional: set[str] = set()
    crawl = C.load_crawl() if use_crawl else None
    people = {}
    if crawl:
        extra, keys = C.crawl_events(crawl, pbn, events)
        events += extra
        for pid, k in keys.items():
            if k.startswith("tl::"):
                baselines[k] = C.baseline_for(crawl["people"][pid])
                provisional.add(k)
                people[k] = crawl["people"][pid]
        prior, n_prior = prior_year_ratings(crawl, keys, pbn)
        for k, r in prior.items():
            n = n_prior[k]
            if k.startswith("tl::") and n >= PRIOR_MIN_MATCHES:
                baselines[k] = r
            elif not k.startswith("tl::") and n >= PRIOR_SETTLED_MATCHES \
                    and baselines.get(k) is not None:
                baselines[k] = PRIOR_CORE_WEIGHT * r + (1 - PRIOR_CORE_WEIGHT) * baselines[k]
            if n >= PRIOR_SETTLED_MATCHES:
                provisional.discard(k)
    events.sort(key=R._date_sort_key)
    res = V9.run_v9(events, baselines, timelines=True,
                    provisional_keys=frozenset(provisional))
    res.crawl_people = people
    return pbn, res


def _all_levels_timeline(res, k: str) -> dict[str, list[float]]:
    """date -> [ShiQ going into that date's first match, ShiQ after its last],
    across every level and league (rating_timeline_30/35 cover 3.0/3.5 only)."""
    by_date: dict[str, list[float]] = {}
    # res.timeline entries were created in match order, so the first entry
    # for a date holds the earliest pre and later entries the later posts.
    for (_div, date), (pre, post) in res.timeline.get(k, {}).items():
        if date in by_date:
            by_date[date][1] = round(post, 4)
        else:
            by_date[date] = [round(pre, 4), round(post, 4)]
    return by_date


def _line_timeline(res, k: str) -> dict[str, list[float]]:
    """{"match id|line": [ShiQ going in, ShiQ after]}: each line's own change,
    so two matches on one day each show theirs."""
    return {ref: [round(a, 4), round(b, 4)] for ref, (a, b) in res.line_timeline.get(k, {}).items()}


def _write_crawl_people(res) -> int:
    """ShiQ for crawled players who aren't in players.json."""
    out = {}
    for k, person in getattr(res, "crawl_people", {}).items():
        if k not in res.matches:
            continue
        sm = res.singles_matches.get(k, 0)
        out[k[4:]] = {"name": person["name"], "area": person.get("area"),
                      "level": person.get("level"),
                      "shiq": round(res.rating[k], 4), "matches": res.matches[k],
                      "shiq_singles": round(res.singles(k), 4) if sm else None,
                      "singles_matches": sm,
                      "timeline": _all_levels_timeline(res, k),
                      "lines": _line_timeline(res, k)}
    (R.PLAYERS_JSON.parent / "shiq_crawl_people.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False))
    return len(out)


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
                      "shiq_singles_matches", "shiq_timeline", "shiq_lines"):
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
        p["shiq_timeline"] = _all_levels_timeline(res, k)
        p["shiq_lines"] = _line_timeline(res, k)
        for (division, date), (pre, post) in res.timeline.get(k, {}).items():
            dk = _div_key(division)
            if dk is None:
                continue
            p[f"rating_timeline_{dk}"][date] = round(pre, 4)
            p[f"rating_post_timeline_{dk}"][date] = round(post, 4)
        n += 1
    n_crawl = 0
    if own and save:
        R._save(R.PLAYERS_JSON, players)
        n_crawl = _write_crawl_people(res)
    print(f"  [shiq v9] {n} players rated from {len(res.pre_match)} court lines"
          f" (+{n_crawl} crawled players not in players.json)")
    return {"players": n, "lines": len(res.pre_match)}


if __name__ == "__main__":
    apply_v9()
