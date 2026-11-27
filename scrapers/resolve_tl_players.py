#!/usr/bin/env python3
"""
scrapers/resolve_tl_players.py
Identity pass over data/tl_team_matches_2026.json: turn names into people
without ever merging on name + area alone.

  1. Base identity = (normalized name, team id): the team a player played for
     in a match line, or the roster they're on. Championship teams are mapped
     to the local team they came from (by roster overlap) when possible.
  2. NV identities are joined to the USTA NV ratings list (data/ntrp_nv_2026.json).
     Name on the list exactly once -> all of that name's NV identities are that
     person (tagged with city, level, history_url).
  3. Name on the list 2+ times -> each candidate's own player page
     ("2026 Teams" table) decides which team identities are theirs. Our 429
     already have team lists (player_teams from the crawl); other candidates'
     pages are fetched once (--fetch, one login, same pacing) and cached in
     data/tl_candidate_teams_2026.json.
  4. Names not on the list (incl. out-of-state champ players) merge across
     teams only when nothing says they're different people; otherwise they
     stay separate and are flagged `ambiguous`.
  5. Sanity checks that block a merge: different roster NTRP, two teams in the
     same league+flight (USTA allows one team per flight), or both identities
     in the same match.

Writes `people` and `identity_person` into the same JSON file, plus `_resolve`
stats.

Usage:
  python3 scrapers/resolve_tl_players.py            # offline (uses cache only)
  python3 scrapers/resolve_tl_players.py --fetch    # log in for missing candidate pages
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import urllib.parse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scrape_tl_teams import (  # noqa: E402
    OUT, NTRP_LIST, YEAR, norm, team_id, champ_area, norm_area, _names, goto, _check_blocked,
    player_team_rows, is_mixed,
)

CAND_CACHE = ROOT / "data" / f"tl_candidate_teams_{YEAR}.json"
# Core players whose TennisLink name differs from ours (e.g. a changed last
# name), confirmed by Shirin: {our name: TennisLink name}.
ALIASES = {"Renee Johnson": "Renee Warner"}
# Appearances Shirin is checking by hand: kept as separate people, flagged.
# NV names Shirin confirmed are one person although the usual checks split them
# (roster NTRP differs, a holding-team roster, a renamed championship team).
ONE_PERSON = {"Adela Smith", "Zharmayne Dizon", "Jason Schieck", "Larry Fernandez",
              "Jeffrey Gromny"}
MANUAL_REVIEW: set[tuple[str, str]] = set()   # Amy Graybeal's Spanish Oaks Districts: confirmed hers


def _tl_pid(history_url: str | None) -> str | None:
    """TennisLink player id: the par1 of a ratings-list history_url, decoded.
    The same person has the same par1 on every year's list."""
    m = re.search(r"[?&]par1=([^&]+)", history_url or "")
    return urllib.parse.unquote(m.group(1)) if m else None


def _slug(n: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")


def _flight_of(tid: str) -> str:
    """'DISTRICT|LEAGUE|FLIGHT|TEAM' -> 'DISTRICT|LEAGUE|FLIGHT'."""
    return tid.rsplit("|", 1)[0]


# ---------------------------------------------------------------------------
# identities
# ---------------------------------------------------------------------------

def champ_to_local(d) -> dict[str, str]:
    """Map NV championship team ids to the local league team their players came
    from: most common local roster among the champ team's players (needs 2+)."""
    on_roster = collections.defaultdict(set)
    for tid, t in d["teams"].items():
        if t.get("kind") == "league":
            for r in t.get("roster", []):
                on_roster[norm(r["name"])].add(tid)
    known = set(d["teams"])  # every league team id we know of, crawled or not
    for v in d.get("player_teams", {}).values():
        known.update(v)
    if CAND_CACHE.exists():
        for v in json.loads(CAND_CACHE.read_text()).values():
            known.update(v)
    for m in d["matches"].values():   # who played for a team, incl. uncrawled teams
        if m["kind"] != "league":
            continue
        known.update(m["teams"])
        for ln in m["lines"]:
            for side, idx in (("home", 0), ("away", 1)):
                for p in ln[f"players_{side}"]:
                    on_roster[norm(p)].add(m["teams"][idx])
    players_of = collections.defaultdict(set)
    for m in d["matches"].values():
        if m["kind"] != "champ":
            continue
        for ln in m["lines"]:
            for side, idx in (("home", 0), ("away", 1)):
                for p in ln[f"players_{side}"]:
                    players_of[m["teams"][idx]].add(norm(p))
    out = {}
    for ctid, names in players_of.items():
        # only local teams of the same age group and level as the championship
        cname = ctid.split("|")[1].upper()
        lvl = set(re.findall(r"\d\.\d", cname))
        age = re.search(r"\b(18|40|55|65)\s*&\s*OVER", cname)

        tri = "TRI" in cname and "LEVEL" in cname

        def fits(tid):
            parts = tid.split("|")
            league, flight = parts[1].upper(), parts[2].upper()
            if tri != ("TRI" in league and "LEVEL" in league):
                return False
            flevels = set(re.findall(r"\d\.\d", flight))
            if lvl and flevels != lvl:          # same level(s) exactly
                return False
            return not age or f"{age.group(1)} & OVER" in league + " " + flight

        # Same team name in a fitting league flight wins outright. Otherwise go
        # by roster overlap: a team that wins Districts is renamed in its league
        # listing to STATE/CAPTAIN/DIVISION (e.g. SPANISH OAKS -> NV/EDGE/18&OVERW4.0).
        cteam = ctid.rsplit("|", 1)[1]
        same = [t for t in known if not t.startswith("CHAMP|")
                and t.rsplit("|", 1)[1] == cteam and fits(t)]
        if len(same) == 1:
            out[ctid] = same[0]
            continue
        renamed = [t for t in known if not t.startswith("CHAMP|") and
                   re.match(r"^[A-Z]{2}/[^/]+/", t.rsplit("|", 1)[1]) and fits(t)]
        votes = collections.Counter(t for n in sorted(names)
                                    for t in sorted(on_roster.get(n, ())) if fits(t))
        # ties broken by team key, so results don't depend on set order
        best = max(votes.items(), key=lambda kv: (kv[1], kv[0])) if votes else None
        if best and best[1] >= 2:
            out[ctid] = best[0]
        elif cteam.count("/") >= 2:
            # Sectionals/Nationals label "Nevada/Edge/18&Over4.0W" <-> league "NV/EDGE/18&OVERW4.0"
            cap = cteam.split("/")[1].strip().upper()
            hit = [t for t in renamed if t.rsplit("|", 1)[1].split("/")[1].strip().upper() == cap]
            if len(hit) == 1:
                out[ctid] = hit[0]
    return out


def build_identities(d) -> dict:
    c2l = champ_to_local(d)
    ids = {}

    def ident(name, tid, area):
        key = f"{norm(name)}#{tid}"
        it = ids.setdefault(key, {"name": name, "nname": norm(name), "team": tid,
                                  "local_team": c2l.get(tid, tid if not tid.startswith("CHAMP|") else None),
                                  "area": area.upper(), "ntrp": None, "matches": []})
        return it

    for tid, t in d["teams"].items():
        if t.get("kind") != "league":
            continue
        for r in t.get("roster", []):
            it = ident(r["name"], tid, norm_area(t.get("district", "")))
            if r.get("ntrp"):
                it["ntrp"] = r["ntrp"]
    for mid, m in d["matches"].items():
        for ln in m["lines"]:
            for side, idx in (("home", 0), ("away", 1)):
                tid = m["teams"][idx]
                area = norm_area(m.get("district", "")) if m["kind"] == "league" else \
                    champ_area(m["home"] if idx == 0 else m["away"], m.get("level", ""))
                for p in ln[f"players_{side}"]:
                    it = ident(p, tid, area)
                    ref = [mid, ln["line"], side]
                    if ref not in it["matches"]:
                        it["matches"].append(ref)
    # roster NTRP onto champ identities mapped to a local team
    for it in ids.values():
        if not it["ntrp"] and it["local_team"] and it["local_team"] != it["team"]:
            loc = ids.get(f"{it['nname']}#{it['local_team']}")
            if loc:
                it["ntrp"] = loc["ntrp"]
    return ids


def conflicts(a: dict, b: dict) -> str | None:
    if a["ntrp"] and b["ntrp"] and a["ntrp"] != b["ntrp"]:
        return "ntrp"
    ta, tb = a["local_team"] or a["team"], b["local_team"] or b["team"]
    if ta != tb and not ta.startswith("CHAMP|") and not tb.startswith("CHAMP|") \
            and _flight_of(ta) == _flight_of(tb):
        return "same_flight"
    if {r[0] for r in a["matches"]} & {r[0] for r in b["matches"]} and ta != tb:
        return "same_match"
    return None


def merge_groups(items: list[dict]) -> list[list[dict]]:
    """Greedy: add each identity to the first group it doesn't conflict with."""
    groups: list[list[dict]] = []
    for it in sorted(items, key=lambda x: -len(x["matches"])):
        for g in groups:
            if not any(conflicts(it, o) for o in g):
                g.append(it)
                break
        else:
            groups.append([it])
    return groups


# ---------------------------------------------------------------------------
# candidate team lists
# ---------------------------------------------------------------------------

def fetch_candidate_teams(rows: list[dict], cache: dict) -> None:
    from dotenv import load_dotenv
    from playwright.sync_api import sync_playwright
    from scrapers.scrape_tl_teams import open_browser, session_login

    load_dotenv(ROOT / ".env")
    with sync_playwright() as p:
        b, page = open_browser(p)   # crawler's profile: reuse the USTA session
        session_login(page)
        for i, r in enumerate(rows, 1):
            try:
                goto(page, r["history_url"])
                _check_blocked(page)
                teams = [team_id(x["district"], x["league"], x["flight"], x["team"])
                         for x in player_team_rows(page)
                         if x["year"] == YEAR and not is_mixed(x["league"], x["flight"])]
                cache[r["history_url"]] = teams
                print(f"  [{i}/{len(rows)}] {r['name']} ({r.get('city')}): {len(teams)} teams", flush=True)
            except RuntimeError:
                raise
            except Exception as e:
                print(f"  [{i}/{len(rows)}] {r['name']}: [warn] {e}", flush=True)
            if i % 10 == 0:
                CAND_CACHE.write_text(json.dumps(cache, indent=1))
        CAND_CACHE.write_text(json.dumps(cache, indent=1))
        b.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# Ratings lists by district code (data/ntrp_<code>_<year>.json). NV always;
# other states' lists, when crawled (out-of-state opponents), resolve that
# state's players the same way.
DISTRICTS = {"nv": "NEVADA", "ut": "UTAH", "id": "IDAHO", "co": "COLORADO",
             "mt": "MONTANA", "wy": "WYOMING", "nm": "NEW MEXICO"}


def load_area_lists() -> dict:
    """{district: {normalized name: [list rows]}} for every list present."""
    out = {"NEVADA": collections.defaultdict(list)}
    for r in json.loads(NTRP_LIST.read_text()):
        out["NEVADA"][norm(r["name"])].append(r)
    for code, district in DISTRICTS.items():
        f = ROOT / "data" / f"ntrp_{code}_{YEAR}.json"
        if code == "nv" or not f.exists():
            continue
        out[district] = collections.defaultdict(list)
        for r in json.loads(f.read_text()):
            out[district][norm(r["name"])].append(r)
    return out


def resolve(d, fetch: bool) -> dict:
    ids = build_identities(d)
    area_lists = load_area_lists()
    print("lists:", {a: sum(map(len, l.values())) for a, l in area_lists.items()})
    cache = json.loads(CAND_CACHE.read_text()) if CAND_CACHE.exists() else {}
    # our 429's team lists from the crawl, keyed by history_url
    for hu, tids in d.get("player_teams_by_url", {}).items():
        cache.setdefault(hu, tids)
    by_slug = d.get("player_teams", {})
    code_of = {v: k for k, v in DISTRICTS.items()}
    for area, lst in area_lists.items():
        pre = "" if area == "NEVADA" else code_of[area] + ":"
        for rows in lst.values():
            for r in rows:
                if pre + _slug(r["name"]) in by_slug and r.get("history_url"):
                    cache.setdefault(r["history_url"], by_slug[pre + _slug(r["name"])])

    # Section wildcard teams at Sectionals ("Intermountain/Simister/...") mix
    # players from several states: give each player the state where the
    # name otherwise appears, if that is exactly one state.
    areas_of = collections.defaultdict(set)
    for it in ids.values():
        if it["area"] != "INTERMOUNTAIN":
            areas_of[it["nname"]].add(it["area"])
    # The wildcard team's name also shows up as a local team in the player's
    # own state ("NEVADA|...|INTERMOUNTAIN/VANNAH/TRILEVEL4.0W"): that settles
    # it even when the name exists in two states (Megan Bell: NV and TX).
    team_areas = collections.defaultdict(set)
    for it in ids.values():
        if it["area"] != "INTERMOUNTAIN":
            team_areas[(it["nname"], it["team"].rsplit("|", 1)[-1])].add(it["area"])
    for it in ids.values():
        if it["area"] == "INTERMOUNTAIN":
            a = areas_of.get(it["nname"], set())
            same = team_areas.get((it["nname"], it["team"].rsplit("|", 1)[-1]), set())
            if len(same) == 1:
                it["area"] = next(iter(same))
            elif len(a) == 1:
                it["area"] = next(iter(a))
            elif not a:
                listed = [ar for ar, l in area_lists.items() if l.get(it["nname"])]
                if len(listed) == 1 and len(area_lists[listed[0]][it["nname"]]) == 1:
                    it["area"] = listed[0]

    by_name_area = collections.defaultdict(list)
    for it in ids.values():
        by_name_area[(it["nname"], it["area"])].append(it)

    if fetch:
        need = {}
        for (nn, area), items in by_name_area.items():
            lc = area_lists.get(area, {}).get(nn, [])
            if len(lc) >= 2 or (len(lc) == 1 and len(merge_groups(items)) > 1):
                for r in lc:
                    if r.get("history_url") and r["history_url"] not in cache:
                        need[r["history_url"]] = r
        print(f"fetching {len(need)} candidate player pages")
        if need:
            fetch_candidate_teams(list(need.values()), cache)

    people, id2p = {}, {}
    st = collections.Counter()

    def new_person(group, entry=None, flags=()):
        g0 = group[0]
        # Stable ids (reusable across crawls, states, years): the TennisLink
        # player id (par1 of the ratings-list history_url) when the person is
        # on a list; else x:sha1(normalized name|first team key)[:12].
        tl_pid = _tl_pid(entry.get("history_url")) if entry else None
        if tl_pid and tl_pid not in people:
            pid = tl_pid
        else:
            first = sorted(it["team"] for it in group)[0]
            base = "x:" + hashlib.sha1(f"{g0['nname']}|{first}".encode()).hexdigest()[:12]
            pid, k = base, 2
            while pid in people:
                pid, k = f"{base}-{k}", k + 1
        refs = [r for it in group for r in it["matches"]]
        people[pid] = {
            "name": g0["name"], "area": g0["area"],
            "city": entry.get("city") if entry else None,
            "level": (f"{entry.get('level')} {entry.get('rating_type', '')}".strip() if entry else None),
            "history_url": entry.get("history_url") if entry else None,
            "on_nv_list": bool(entry) and g0["area"] == "NEVADA",
            "on_list": bool(entry),
            "ntrp": next((it["ntrp"] for it in group if it["ntrp"]), None),
            "teams": sorted({it["team"] for it in group}),
            "matches": refs, "flags": sorted(set(flags)),
            "tl_pid": tl_pid, "unresolved": pid != tl_pid,
        }
        for it in group:
            id2p[f"{it['nname']}#{it['team']}"] = pid
            # also the plain lower-cased name, for readers that key on
            # name.lower() (apostrophes, hyphens: "jamie o'connell")
            id2p.setdefault(f"{it['name'].lower()}#{it['team']}", pid)

    review = {(norm(n), t) for n, t in MANUAL_REVIEW}
    for (nn, area), items in list(by_name_area.items()):
        held = [it for it in items if (nn, it["team"]) in review]
        for it in held:
            items.remove(it)
            new_person([it], None, ["ambiguous", "manual_review"])
        if not items:
            continue
        cands = area_lists.get(area, {}).get(nn, [])
        if len(cands) == 1:
            groups = merge_groups(items)
            owned = cache.get(cands[0].get("history_url"))
            def _covered(it):
                if (it["local_team"] or it["team"]) in owned:
                    return True
                if it["team"].startswith("CHAMP|"):
                    # championship team: same club name on the player's own list
                    club = it["team"].rsplit("|", 1)[1]
                    return not it["local_team"] or any(t.rsplit("|", 1)[1] == club for t in owned)
                return False
            if len(groups) > 1 and area == "NEVADA" and nn in {norm(n) for n in ONE_PERSON}:
                st["list_unique_confirmed_one"] += 1
                new_person(items, cands[0], ["rating_changed"])
                continue
            if len(groups) > 1 and owned is not None and all(_covered(it) for it in items):
                # the player's own team list covers every identity: one person
                # (NTRP differences are mid-year rating changes, e.g. DQs)
                st["list_unique_conflict_resolved_by_team_list"] += 1
                new_person(items, cands[0], ["rating_changed"])
                continue
            if len(groups) == 1:
                new_person(items, cands[0]); st["list_unique"] += 1
            else:  # NTRP/flight conflict: biggest group gets the list entry
                st["list_unique_conflict"] += 1
                for k, g in enumerate(groups):
                    new_person(g, cands[0] if k == 0 else None, ["ambiguous", "conflict"])
        elif len(cands) >= 2:
            st["dup_names_on_list"] += 1
            assigned = collections.defaultdict(list)
            left = []
            for it in items:
                team = it["local_team"] or it["team"]
                owners = [c for c in cands if team in cache.get(c.get("history_url"), [])]
                if len(owners) == 1:
                    assigned[owners[0]["history_url"]].append(it)
                else:
                    left.append(it)
            for hu, g in assigned.items():
                new_person(g, next(c for c in cands if c["history_url"] == hu))
            if left:
                st["dup_names_unresolved"] += 1
                for it in left:
                    new_person([it], None, ["ambiguous", "dup_name_on_list"])
            else:
                st["dup_names_resolved"] += 1
        else:
            groups = merge_groups(items)
            flags = ["not_on_nv_list"] if area == "NEVADA" else ["out_of_state"]
            if area != "NEVADA" and area in area_lists:
                flags.append("not_on_list")
            if len(groups) > 1 and area == "NEVADA" and nn in {norm(n) for n in ONE_PERSON}:
                new_person(items, None, flags + ["rating_changed"]); st["unlisted_confirmed_one"] += 1
            elif len(groups) == 1:
                new_person(items, None, flags); st["unlisted_merged"] += 1
            else:
                st["unlisted_ambiguous"] += 1
                for g in groups:
                    new_person(g, None, flags + ["ambiguous"])

    for ours, theirs in ALIASES.items():
        for pid, p in people.items():
            if norm(p["name"]) == norm(theirs) and p["area"] == "NEVADA":
                p.setdefault("aka", []).append(ours)
                d["player_teams"][_slug(ours)] = p["teams"]
    names_multi = collections.Counter(p["name"].lower() for p in people.values())
    st["identities"] = len(ids)
    st["people"] = len(people)
    st["people_flagged_ambiguous"] = sum(1 for p in people.values() if "ambiguous" in p["flags"])
    st["names_shared_by_2plus_people"] = sum(1 for c in names_multi.values() if c > 1)
    d["people"], d["identity_person"] = people, id2p
    d["_resolve"] = dict(st)
    return d


def main():
    global OUT, YEAR, CAND_CACHE, NTRP_LIST
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="log in and open candidate player pages for duplicate names")
    ap.add_argument("--year", type=int, default=2026)
    a = ap.parse_args()
    import scrapers.scrape_tl_teams as stt
    stt.set_year(a.year)
    OUT, YEAR = stt.OUT, a.year
    CAND_CACHE = ROOT / "data" / f"tl_candidate_teams_{YEAR}.json"
    yl = ROOT / "data" / f"ntrp_nv_{YEAR}.json"   # that year's list (players active then)
    if yl.exists():
        NTRP_LIST = yl
    d = json.loads(OUT.read_text())
    t0 = time.time()
    d = resolve(d, a.fetch)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(OUT)
    print(json.dumps(d["_resolve"], indent=1), f"\n{round(time.time() - t0, 1)} s")


if __name__ == "__main__":
    main()
