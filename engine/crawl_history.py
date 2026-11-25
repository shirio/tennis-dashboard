"""
engine/crawl_history.py
Player-page match histories from the TennisLink team crawl
(data/tl_team_matches_<year>.json; see notes/tl-team-crawl.md).

Every crawled person gets a history: each (match id, line, side) ref in the
crawl's ``people`` section becomes one row in the same shape the player pages
already use (date, div, team, line, score, partners, opponents, ...), plus
person ids for partners and opponents so links and ratings never mix up
two people with the same name.

Pre-match ShiQ comes from ``shiq_timeline`` ({date: [before, after]}): players.json
for core players, data/shiq_crawl_people.json for everyone else.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _js(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def level_of(m: dict, line: dict) -> str:
    from engine.crawl_events import _level
    lv = _level(m, line)
    if not lv:  # championship labels like 'USTA ADULT 18 & OVER- W3.0', 'League National Adult 40 3.5F'
        x = re.search(r"(\d\.\d)", m.get("league") or "")
        lv = x.group(1) if x else ""
    return lv


def league_tag(m: dict) -> str:
    """Short league label: 18+, 40+, Fall, Weekday, Summer, Tri-Level, Districts, Sectionals, Nationals..."""
    if m.get("kind") == "champ":
        lv = (m.get("level") or "").upper()
        lg = (m.get("league") or "").upper()
        if "NATIONAL" in lv or "NATIONAL" in lg:
            return "Nationals"
        if "PLAYOFF" in lg:
            return "Playoff"
        if " - " in lv:  # 'USTA/INTERMOUNTAIN - NEVADA'
            return "Districts"
        return "Sectionals"
    lg = (m.get("league") or "").upper()
    if "PLAYOFF" in lg:
        return "Playoff"
    for pat, tag in [(r"TRI-?LEVEL", "Tri-Level"), (r"\b18 ?& ?OVER", "18+"), (r"\b40 ?& ?OVER", "40+"),
                     (r"\b55 ?& ?OVER", "55+"), (r"\b65 ?& ?OVER", "65+"), (r"\b70 ?& ?OVER", "70+"),
                     (r"WEEKDAY", "Weekday"), (r"SENIOR", "Senior"), (r"SUMMER", "Summer"),
                     (r"FLEX", "Flex"), (r"MIXED", "Mixed"), (r"FALL", "Fall"), (r"SPRING", "Spring")]:
        if re.search(pat, lg):
            return tag
    return ""


def champ_team_label(name: str) -> str:
    """'Nevada/Cunningham/18&Over3.0W' -> 'Nevada (Cunningham)'; 'NV/EDGE/...' -> 'NV (Edge)'."""
    parts = [x.strip() for x in name.split("/")]
    if len(parts) >= 3:
        def fix(s):  # keep TennisLink's own casing (NorCal, MOValley) unless it's all caps
            return s.title() if s.isupper() and len(s) > 3 else s
        return f"{fix(parts[0])} ({fix(parts[1])})"
    return name


def is_renamed(name: str) -> bool:
    """District/section winners are renamed STATE/CAPTAIN/DIVISION (e.g. NV/Song/40&Over3.5W)."""
    return (name or "").count("/") >= 2


# Renamed teams whose club name isn't in the crawl, from Shirin (2026-11-24/25)
KNOWN_ORIGINS = {
    "NV/EDGE/18&OVERW4.0": "Spanish Oaks",
}


def _team_sig(text: str):
    u = (text or "").upper().replace(" ", "")
    age = re.search(r"(18|40|55|65)&OVER", u)
    return (age.group(1) if age else None, "TRI" in u, set(re.findall(r"\d\.\d", u)))


def renamed_team_origins(crawl: dict) -> dict[str, str]:
    """{team id: original club team name} for renamed teams (Shirin, 2026-11-25).
    The original is the same age group / level (or Tri-Level) team most of the
    renamed team's players also played on, when that's a clear winner."""
    teams, people = crawl.get("teams", {}), crawl.get("people", {})
    members: dict[str, set] = {}
    for pid, p in people.items():
        for tid in p.get("teams", []):
            members.setdefault(tid, set()).add(pid)
    out = {}
    for tid, t in teams.items():
        name = t.get("name") or ""
        if not is_renamed(name):
            continue
        known = KNOWN_ORIGINS.get(name.upper().replace(" ", ""))
        if known:
            out[tid] = known
            continue
        age, tri, lv = _team_sig(name)
        cnt: dict[str, int] = {}
        for pid in members.get(tid, ()):
            for o in people[pid].get("teams", []):
                ot = teams.get(o) or {}
                on = ot.get("name") or o.split("|")[-1]
                if is_renamed(on):
                    continue
                a2, tri2, lv2 = _team_sig(" ".join([ot.get("league", ""), ot.get("flight", ""), o]))
                if tri != tri2 or (not tri and (a2 != age or not lv & lv2)):
                    continue
                cnt[on] = cnt.get(on, 0) + 1
        best = sorted(cnt.items(), key=lambda kv: -kv[1])
        if best and best[0][1] >= 3 and (len(best) == 1 or best[0][1] >= 2 * best[1][1]):
            out[tid] = best[0][0]
    return out


def _line_label(lbl: str) -> str:
    """'#1 Doubles' -> '1# Doubles' (the standings' spelling, which the pills read)."""
    m = re.match(r"#(\d+)\s+(Singles|Doubles)", lbl or "")
    return f"{m.group(1)}# {m.group(2)}" if m else lbl


def load_timelines(players: list[dict], crawl: dict, crawl_people_path: Path | None = None) -> dict[str, dict]:
    """{crawl person id: {date: [before, after]}}."""
    from engine import ratings as R
    from engine.crawl_events import person_keys
    pbn = R._build_players_by_name(players)
    keys = person_keys(crawl, pbn)
    out = {}
    for pid, k in keys.items():
        p = pbn.get(k)
        if p and p.get("shiq_timeline"):
            out[pid] = _js(p["shiq_timeline"])
    path = crawl_people_path or ROOT / "data" / "shiq_crawl_people.json"
    if path.exists():
        for pid, v in json.loads(path.read_text()).items():
            if pid not in out and v.get("timeline"):
                out[pid] = v["timeline"]
    return out


def person_player_keys(players: list[dict], crawl: dict) -> dict[str, dict]:
    """{crawl person id: players.json entry} for crawled people who are core players."""
    from engine import ratings as R
    from engine.crawl_events import person_keys
    pbn = R._build_players_by_name(players)
    return {pid: pbn[k] for pid, k in person_keys(crawl, pbn).items() if k in pbn}


def norm_name(s: str) -> str:
    """Same key the identity pass uses (scrapers.scrape_tl_teams.norm):
    accents dropped, punctuation such as ' and - turned into spaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def crawl_histories(crawl: dict, timelines: dict[str, dict]) -> dict[str, list[dict]]:
    """{person id: [history rows, oldest first]}."""
    ident = crawl.get("identity_person", {})
    teams = crawl.get("teams", {})
    champs = crawl.get("champ_flights", {})
    matches = crawl.get("matches", {})

    def pid_of(name, team_id):
        return ident.get(f"{norm_name(name)}#{team_id}")

    def pre(pid, date):
        v = (timelines.get(pid) or {}).get(date)
        return f"{float(v[0]):.2f}" if v else None

    origins = renamed_team_origins(crawl)
    by_name = dict(KNOWN_ORIGINS)
    for tid, o in origins.items():
        by_name.setdefault((teams[tid].get("name") or "").upper().replace(" ", ""), o)

    def team_name(team_id, raw):
        """Display name; renamed district/section winners show their club team's name."""
        t = teams.get(team_id) or {}
        if t.get("kind") == "champ":
            src = (champs.get(t.get("champ", "")) or {}).get("from_team")
            if src and src.split("|")[-1] and raw.upper().startswith("NEVADA/"):
                # our own club team that advanced (renamed STATE/CAPTAIN/DIVISION);
                # that league team may itself carry the renamed name
                club = src.split("|")[-1]
                if not is_renamed(club):
                    return club
                if src in origins:
                    return origins[src]
        if is_renamed(raw):
            return (origins.get(team_id) or by_name.get(raw.upper().replace(" ", ""))
                    or champ_team_label(raw))
        return raw

    out: dict[str, list[dict]] = {}
    for pid, person in crawl.get("people", {}).items():
        rows = []
        seen = set()
        for mid, lbl, side in person.get("matches", []):
            if (mid, lbl) in seen:
                continue
            seen.add((mid, lbl))
            m = matches.get(mid)
            if not m:
                continue
            line = next((ln for ln in m.get("lines", []) if ln.get("line") == lbl), None)
            if not line:
                continue
            hi = 0 if side == "home" else 1
            mine = line.get("players_home" if side == "home" else "players_away") or []
            theirs = line.get("players_away" if side == "home" else "players_home") or []
            tids = m.get("teams") or ["", ""]
            my_tid, op_tid = tids[hi], tids[1 - hi]
            partners = [n for n in mine if pid_of(n, my_tid) != pid]
            if len(partners) == len(mine):  # couldn't tell which name is her: drop one same-name entry
                partners = [n for n in mine if n.lower() != person["name"].lower()]
            p_ids = [pid_of(n, my_tid) for n in partners]
            o_ids = [pid_of(n, op_tid) for n in theirs]
            date = m.get("date", "")
            wko = not mine or not theirs or not line.get("winner")
            rows.append({
                "date": date, "mid": mid, "div": level_of(m, line) or "?", "league": league_tag(m),
                "league_full": m.get("league", ""), "flight": m.get("flight", ""),
                "my_team": team_name(my_tid, m.get("home" if side == "home" else "away", "")),
                "opp_team": team_name(op_tid, m.get("away" if side == "home" else "home", "")),
                "my_team_full": m.get("home" if side == "home" else "away", ""),
                "opp_team_full": m.get("away" if side == "home" else "home", ""),
                "line": _line_label(lbl), "score": line.get("score", ""),
                "won": line.get("winner") == side, "wko": wko,
                "dq": line.get("dq") or [],
                "partners": partners, "partner_ids": p_ids, "partner_r": [pre(x, date) for x in p_ids],
                "opps": theirs, "opp_ids": o_ids, "opp_r": [pre(x, date) for x in o_ids],
                "year_end": (m.get("year_end") or {}).get("counts"),
            })

        def k(r):
            mm = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", r["date"])
            return (int(mm.group(3)), int(mm.group(1)), int(mm.group(2))) if mm else (0, 0, 0)
        rows.sort(key=k)
        if rows:
            out[pid] = rows
    return out


class TimelinePit:
    """pit(_, date, _) -> ShiQ going into that date; .post(_, date, _) -> ShiQ after it.
    Same call shape as build_html._make_pit_rating, for one person."""

    def __init__(self, timeline: dict):
        self.tl = timeline or {}

    def __call__(self, _nk, date, _div=None):
        v = self.tl.get(date)
        return f"{float(v[0]):.2f}" if v else None

    def post(self, _nk, date, _div=None):
        v = self.tl.get(date)
        return float(v[1]) if v else None
