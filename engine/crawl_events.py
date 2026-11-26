"""
engine/crawl_events.py
Court events from the TennisLink team crawl (data/tl_team_matches_<year>.json,
written by scrapers/scrape_tl_teams.py and scrapers/resolve_tl_players.py).

Each crawled line becomes an engine CourtEvent. Players are identified by
the identity pass's ``people`` ids, then mapped onto players.json keys
(same name key, Nevada, or an ``aka`` alias) so the crawl and the existing
standings data rate the same person. Crawled people who aren't in
players.json get their own key, ``tl::<person id>``.

Lines already present in the standings data are dropped so nothing is
counted twice. A line matches when it has the same winners and losers within
30 days (the standings and TennisLink can disagree on the date of a
rescheduled match).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from engine import ratings as R

ROOT = Path(__file__).resolve().parent.parent


def crawl_path(year: int = 2026) -> Path:
    return ROOT / "data" / f"tl_team_matches_{year}.json"


def load_crawl(year: int = 2026) -> Optional[dict]:
    p = crawl_path(year)
    return json.loads(p.read_text()) if p.exists() else None


def _level(m: dict, line: dict) -> str:
    """'3.0' style level for a line: the line's own (Tri-Level), else from flight/league."""
    if line.get("level"):
        return line["level"]
    for txt in (m.get("flight") or "", m.get("league") or ""):
        x = re.search(r"(\d\.\d)\s*W", txt.upper()) or re.search(r"\b(\d\.\d)\b", txt)
        if x:
            return x.group(1)
    return ""


def person_keys(crawl: dict, pbn: dict) -> dict[str, str]:
    """crawl person id -> engine player key."""
    nv_keys = {}
    for k, p in pbn.items():
        if (p.get("state") or "NV").upper() == "NV":
            nv_keys.setdefault(R._name_key(p.get("name", "")), k)
    out = {}
    for pid, person in crawl.get("people", {}).items():
        key = None
        if person.get("area") == "NEVADA":
            for nm in [person["name"]] + list(person.get("aka") or []):
                key = nv_keys.get(R._name_key(nm))
                if key:
                    break
        out[pid] = key or f"tl::{pid}"
    return out


def played_level(person: dict, matches: Optional[dict] = None) -> Optional[float]:
    """Lowest NTRP level a crawled player played at, from the levels printed
    on their match lines (or the league name). Players can play up but not
    down, so the lowest is the best guess at their own level. Combo leagues
    (6.0, 7.0, ...) don't say a player's own level and are skipped."""
    if not matches:
        return None
    found = []
    for mid, line, _side in person.get("matches") or []:
        m = matches.get(mid) or {}
        lv = next((l.get("level") for l in m.get("lines") or []
                   if l.get("line") == line and l.get("level")), None)
        if not lv:
            hit = re.search(r"(?<![\d.])([1-5]\.[05])(?![\d/])", m.get("league") or "")
            lv = hit.group(1) if hit else None
        try:
            v = float(lv)
        except (TypeError, ValueError):
            continue
        if 1.5 <= v <= 5.5:
            found.append(v)
    return min(found) if found else None


def baseline_for(person: dict, matches: Optional[dict] = None) -> Optional[float]:
    """Starting guess for a crawled player with no TennisRecord baseline:
    a quarter below their USTA level (mid-level). Out-of-state players at
    Sectionals/Nationals often have no level on record; with ``matches``
    (the crawl's match table) they start mid-level at the level they played."""
    lv = re.match(r"(\d\.\d)", person.get("level") or person.get("ntrp") or "")
    if lv:
        return float(lv.group(1)) - 0.25
    try:
        return float(person.get("ntrp")) - 0.25
    except (TypeError, ValueError):
        pass
    played = played_level(person, matches)
    return None if played is None else played - 0.25


def _sig(winners: list[str], losers: list[str]) -> tuple:
    # Score text differs between sources (match tiebreak "1-0" vs "10-7"), so
    # the signature is who won and who lost.
    return frozenset(winners), frozenset(losers)


def _day(date: str) -> int:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", date or "")
    if not m:
        return -10**6
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return y * 372 + mo * 31 + d


def crawl_events(crawl: dict, pbn: dict, existing: list[R.CourtEvent] = ()
                 ) -> tuple[list[R.CourtEvent], dict[str, str]]:
    keys = person_keys(crawl, pbn)
    side_people: dict[tuple[str, str, str], list[str]] = {}
    for pid, person in crawl.get("people", {}).items():
        for mid, line, side in person.get("matches", []):
            side_people.setdefault((mid, line, side), []).append(keys[pid])

    # sig -> [(day, existing event or None)]
    seen: dict[tuple, list[tuple[int, Optional[R.CourtEvent]]]] = {}
    for e in existing:
        seen.setdefault(_sig(e.winner_keys, e.loser_keys), []).append((_day(e.date), e))
    out = []
    for mid, m in crawl.get("matches", {}).items():
        for ln in m.get("lines", []):
            win = ln.get("winner")
            if win not in ("home", "away") or not ln.get("score"):
                continue
            home = side_people.get((mid, ln["line"], "home"), [])
            away = side_people.get((mid, ln["line"], "away"), [])
            if not home or not away or len(home) != len(away):
                continue
            w, l = (home, away) if win == "home" else (away, home)
            sig, day = _sig(w, l), _day(m["date"])
            dup = next((e for d, e in seen.get(sig, []) if abs(day - d) <= 30), False)
            if dup is not False:
                # Same line already in the standings data. The crawl has the date
                # the match was PLAYED; the standings keep the scheduled date of a
                # rescheduled match (e.g. scheduled 3/14, played 3/19). Use the
                # played date so ratings timelines line up with the crawl pages.
                if dup is not None:
                    if dup.date != m["date"]:
                        dup.date = m["date"]
                    dup.crawl_ref = f"{mid}|{ln['line']}"
                continue
            seen.setdefault(sig, []).append((day, None))
            out.append(R.CourtEvent(date=m["date"], match_id=mid, line_label=ln["line"],
                                    division=_level(m, ln), winner_keys=list(w),
                                    loser_keys=list(l), score=ln["score"]))
    return out, keys
