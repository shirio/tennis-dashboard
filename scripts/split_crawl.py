#!/usr/bin/env python3
"""
scripts/split_crawl.py
Split one year's combined crawl (data/tl_team_matches_<year>.json, which the
crawler and the identity pass work on) into one file per crawled state:
data/crawl/<year>/<code>.json (nv.json, ut.json, id.json, ...).

Each state file has the combined file's shape (teams, champ_flights, matches,
people, identity_person, player_teams_by_url, _resolve) restricted to that
state's share:
  - league teams that state's listed players are on (any district), and that
    state's own league teams;
  - every match of those teams, and championship matches that state's people
    played in;
  - every person who appears in those matches (opponents too), with only the
    match refs that are in this file.
A match or person in two states' files (interstate play) keeps the same id in
both, so loading all files and merging by id dedupes it (union a person's
match refs). Ids come straight from the combined file, so NV ids are unchanged.

    python3 scripts/split_crawl.py 2026 [--src FILE] [--out DIR]
"""
import argparse
import collections
import json
import re
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISTRICTS = {"nv": "NEVADA", "ut": "UTAH", "id": "IDAHO", "co": "COLORADO",
             "mt": "MONTANA", "wy": "WYOMING", "nm": "NEW MEXICO"}


def area_of_district(raw: str) -> str:
    return (raw or "").split(" - ")[0].strip().upper()


def canon_tid(k):
    """League team ids without the district's area ('TEXAS - HOUSTON|...' ->
    'TEXAS|...'), as scrape_tl_teams.team_id() now writes them; older crawls
    (2024, 2025) spelled match-header team ids with the area."""
    if not isinstance(k, str) or k.startswith("CHAMP|") or "|" not in k:
        return k
    p = k.split("|")
    p[0] = area_of_district(p[0])
    return "|".join(p)


def canon_team_ids(d):
    d["teams"] = {canon_tid(k): v for k, v in d["teams"].items()}
    for m in d["matches"].values():
        m["teams"] = [canon_tid(t) for t in m.get("teams", [])]
    for key in ("player_teams", "player_teams_by_url"):
        d[key] = {k: [canon_tid(t) for t in v] for k, v in d.get(key, {}).items()}
    for cf in d["champ_flights"].values():
        if "from_team" in cf:
            cf["from_team"] = canon_tid(cf["from_team"])
    for p in d["people"].values():
        p["teams"] = sorted({canon_tid(t) for t in p["teams"]})
    d["identity_person"] = {(k.split("#", 1)[0] + "#" + canon_tid(k.split("#", 1)[1]) if "#" in k else k): v
                            for k, v in d.get("identity_person", {}).items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("year", type=int)
    ap.add_argument("--src")
    ap.add_argument("--out")
    a = ap.parse_args()
    src = Path(a.src) if a.src else ROOT / "data" / f"tl_team_matches_{a.year}.json"
    out = Path(a.out) if a.out else ROOT / "data" / "crawl" / str(a.year)
    d = json.loads(src.read_text())
    canon_team_ids(d)

    # crawled states = district lists present for the year
    states = {c: s for c, s in DISTRICTS.items()
              if (ROOT / "data" / f"ntrp_{c}_{a.year}.json").exists()}
    url_state = {}
    for c in states:
        for r in json.loads((ROOT / "data" / f"ntrp_{c}_{a.year}.json").read_text()):
            if r.get("history_url"):
                url_state.setdefault(r["history_url"], c)
    code_of = {s: c for c, s in states.items()}

    # league team -> owning states
    team_owner = collections.defaultdict(set)
    by_url = d.get("player_teams_by_url", {})
    for hu, tids in by_url.items():
        c = url_state.get(hu)
        if c is None:  # NV players crawled before the lists were by year
            c = "nv"
        for t in tids:
            team_owner[t].add(c)
    for c_slug, tids in d.get("player_teams", {}).items():
        c = c_slug.split(":", 1)[0] if ":" in c_slug and c_slug.split(":", 1)[0] in states else "nv"
        for t in tids:
            team_owner[t].add(c)
    for tid, t in d["teams"].items():
        c = code_of.get(area_of_district(t.get("district", "")))
        if c and t.get("kind") == "league":
            team_owner[tid].add(c)

    # people per match, and person -> state of their own area
    match_people = collections.defaultdict(set)
    for pid, p in d["people"].items():
        for ref in p["matches"]:
            match_people[ref[0]].add(pid)
    person_state = {pid: code_of.get(p.get("area")) for pid, p in d["people"].items()}

    # championship match -> flights it is in (whole flights were crawled,
    # including matches between other sections' teams)
    flights_of = collections.defaultdict(set)
    for ck, cf in d["champ_flights"].items():
        for x in cf.get("match_ids", []):
            flights_of[str(x)].add(ck)

    # match -> states
    match_states = collections.defaultdict(set)
    for mid, m in d["matches"].items():
        for t in m.get("teams", []):
            match_states[mid] |= team_owner.get(t, set())
        if m.get("kind") == "champ":
            # the state(s) whose team led the crawl to this flight
            for ck in flights_of.get(str(m.get("tl_id")), ()):
                match_states[mid] |= team_owner.get(d["champ_flights"][ck].get("from_team", ""), set())
        if m.get("kind") == "champ" or not match_states[mid]:
            for pid in match_people.get(mid, ()):
                if person_state.get(pid):
                    match_states[mid].add(person_state[pid])

    # anything no state claims goes to NV, the home state (nothing is dropped)
    for mid in d["matches"]:
        if not match_states[mid]:
            match_states[mid].add("nv")
    claimed = set()
    for mid in d["matches"]:
        claimed |= match_people.get(mid, set())
    claimed |= {pid for pid, s in person_state.items() if s}

    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for c in sorted(states):
        mids = {mid for mid, ss in match_states.items() if c in ss}
        tids = {t for t, ss in team_owner.items() if c in ss and t in d["teams"]}
        for mid in mids:
            tids.update(t for t in d["matches"][mid].get("teams", []) if t in d["teams"])
        pids = {pid for mid in mids for pid in match_people.get(mid, ())}
        pids |= {pid for pid, s in person_state.items() if s == c}
        # roster-only people (on a team in this file, no match)
        pids |= {pid for pid, p in d["people"].items() if any(t in tids for t in p["teams"])}
        if c == "nv":
            pids |= set(d["people"]) - claimed - {pid for pid, p in d["people"].items()
                                                  if any(t in team_owner for t in p["teams"])}
        people = {}
        for pid in sorted(pids):
            p = dict(d["people"][pid])
            p["matches"] = [r for r in p["matches"] if r[0] in mids]
            p["teams"] = [t for t in p["teams"] if t in tids or t.startswith("CHAMP|")]
            people[pid] = p
        champ_ids = set()
        for mid in mids:
            m = d["matches"][mid]
            if m.get("kind") == "champ":
                champ_ids |= flights_of.get(str(m.get("tl_id")), set())
        f = {
            "year": a.year, "state": states[c], "code": c,
            "teams": {t: d["teams"][t] for t in sorted(tids)},
            "champ_flights": {k: d["champ_flights"][k] for k in sorted(champ_ids)},
            "matches": {mid: d["matches"][mid] for mid in sorted(mids)},
            "people": people,
            "identity_person": {k: v for k, v in d.get("identity_person", {}).items() if v in pids},
            "player_teams_by_url": {hu: t for hu, t in by_url.items()
                                    if url_state.get(hu, "nv") == c},
            "_resolve": d.get("_resolve", {}),
        }
        path = out / f"{c}.json"
        path.write_text(json.dumps(f, ensure_ascii=False, separators=(",", ":")))
        summary[c] = {"matches": len(mids), "teams": len(tids), "people": len(people),
                      "MB": round(path.stat().st_size / 1e6, 1)}
    # every match lands somewhere
    placed = {mid for mid, ss in match_states.items() if ss}
    summary["_unplaced_matches"] = len(set(d["matches"]) - placed)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
