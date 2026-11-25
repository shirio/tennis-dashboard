#!/usr/bin/env python3
"""
scrapers/scrape_tennisrecord_history.py
Rebuild a player's past NTRP levels from TennisRecord team rosters (public, no login).

Method (from Shirin, 2026-11-24): the player's profile lists each year with a
match count. For each year with matches, open that year's match history, follow
the player's own Team link, and read the player's NTRP from that team's roster.
The roster shows the level in effect for that season (no rating letter).

Usage:
    python3 scrapers/scrape_tennisrecord_history.py --names "Ara Lee Stimson"
    python3 scrapers/scrape_tennisrecord_history.py --linked --out data/tr_rating_history.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = "https://www.tennisrecord.com"
UA = {"User-Agent": "Mozilla/5.0 (tennis-dashboard rating history)"}


def _get(sess: requests.Session, url: str, pause: float) -> BeautifulSoup:
    for attempt in range(3):
        try:
            r = sess.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            time.sleep(pause)
            return BeautifulSoup(r.text, "html.parser")
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def _rows(table) -> list[list[str]]:
    return [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in table.find_all("tr")]


def profile_years(soup: BeautifulSoup) -> dict[int, int]:
    """{year: matches} from the profile's 'All Matches' table."""
    for t in soup.find_all("table"):
        rows = _rows(t)
        if rows and rows[0] and rows[0][0] == "All Matches":
            out = {}
            for r in rows[1:]:
                if len(r) > 1 and re.fullmatch(r"\d{4}", r[0]):
                    out[int(r[0])] = int(r[1] or 0)
            return out
    return {}


def current_rating(soup: BeautifulSoup) -> dict:
    """Current published level, letter and date from the profile header."""
    m = re.search(r"\b([1-7]\.[05])\s+([A-Z])\s+(\d{1,2}/\d{1,2}/\d{4})", soup.get_text(" ", strip=True))
    return {"level": float(m.group(1)), "type": m.group(2), "date": m.group(3)} if m else {}


def profile_location(soup: BeautifulSoup, name: str) -> tuple[str, str] | None:
    """(city, state) from the profile header, e.g. 'Nicole Reyes (Las Vegas, NV)'."""
    txt = soup.get_text(" ", strip=True)
    m = re.search(re.escape(name) + r"\s*\(([^()]*?),\s*([A-Z]{2})\)", txt, re.I)
    return (m.group(1).strip(), m.group(2)) if m else None


def _loc_ok(loc, want) -> bool:
    return bool(loc and want and loc[1] == want[1])


def find_profile(sess, name: str, want, pause: float, s: str | None = None, max_s: int = 8):
    """The profile page for `name` whose home state matches `want` (city, state).
    TennisRecord tells same-name players apart with s=1, 2, ...; the bare name is s=1.
    Returns (soup, s, loc); soup is None when no profile matches."""
    q = quote(name)
    tries = [s] if s else [None] + [str(k) for k in range(2, max_s + 1)]
    first = None
    for k in tries:
        soup = _get(sess, f"{BASE}/adult/profile.aspx?playername={q}{'&s=' + k if k else ''}&mt=0&lt=0&yr=1", pause)
        loc = profile_location(soup, name)
        if first is None:
            first = (soup, k, loc)
        if s or not want or _loc_ok(loc, want):
            return soup, k, loc
        if loc is None and k:  # ran past the last same-name profile
            break
    return None, None, first[2] if first else None


def team_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """(team name, url) for each distinct team in the match history table, in page order."""
    seen, out = set(), []
    for a in soup.select("a[href*='teamprofile.aspx']"):
        href = a["href"]
        if href not in seen:
            seen.add(href)
            out.append((a.get_text(" ", strip=True), urljoin(BASE, href)))
    return out


def roster_levels(soup: BeautifulSoup) -> dict[str, str]:
    """{lowercased name: NTRP level} for everyone on a team roster."""
    out = {}
    for t in soup.find_all("table"):
        rows = _rows(t)
        if not rows or "NTRP" not in rows[0] or "Name" not in rows[0]:
            continue
        ni, li = rows[0].index("Name"), rows[0].index("NTRP")
        for r in rows[1:]:
            if len(r) > li and r[ni] and r[li]:
                out[r[ni].lower()] = r[li]
    return out


# Roster cache: {year: {lowercased name: (level, team)}}, filled from every roster opened.
SEEN: dict[int, dict[str, tuple[str, str]]] = {}


def _profile_ids() -> dict[str, str]:
    """{player name: TennisRecord s= id} from players.json profile URLs. TennisRecord
    uses s= to tell apart players who share a name; without it the bare name can
    land on someone in another section."""
    out = {}
    for p in json.loads((ROOT / "data" / "players.json").read_text()):
        m = re.search(r"[?&]s=(\d+)", p.get("profile_url") or "")
        if m:
            out[p["name"]] = m.group(1)
    return out


def player_history(sess, name: str, pause: float, max_teams: int = 3, years_wanted=None,
                   s: str | None = None, use_cache: bool = True, want=None) -> dict:
    """`want` = (city, state) from the USTA list: pick the same-name profile from that state."""
    t0 = time.time()
    prof, s, loc = find_profile(sess, name, want, pause, s)
    if prof is None:
        return {"name": name, "current": {}, "years": {}, "loc": loc, "unmatched": True,
                "seconds": round(time.time() - t0, 1)}
    q = quote(name) + (f"&s={s}" if s else "")
    # The roster cache is keyed by name, so it can't tell same-name players apart.
    use_cache = use_cache and not s
    years = profile_years(prof)
    out = {"name": name, "current": current_rating(prof), "years": {}, "fetches": 1, "loc": loc}
    if s:
        out["s"] = s
    for y in sorted(years, reverse=True):
        if not years[y] or (years_wanted and y not in years_wanted):
            continue
        hit = SEEN.get(y, {}).get(name.lower()) if use_cache else None
        if hit:
            out["years"][str(y)] = {"matches": years[y], "level": hit[0], "team": hit[1], "cached": True}
            continue
        mh = _get(sess, f"{BASE}/adult/matchhistory.aspx?year={y}&playername={q}&mt=0&lt=0&yr=0", pause)
        out["fetches"] += 1
        rec = {"matches": years[y], "level": None, "team": None}
        for team, url in team_links(mh)[:max_teams]:
            levels = roster_levels(_get(sess, url, pause))
            out["fetches"] += 1
            if use_cache:
                for who, l in levels.items():
                    SEEN.setdefault(y, {}).setdefault(who, (l, team))
            lvl = levels.get(name.lower())
            if lvl:
                rec.update(level=lvl, team=team)
                break
        out["years"][str(y)] = rec
    out["seconds"] = round(time.time() - t0, 1)
    return out


def _linked_names() -> list[str]:
    from engine.build_directory import PLAYER_DIR  # noqa: WPS433
    slug = lambda n: re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")  # noqa: E731
    slugs = {p.stem for p in (ROOT / PLAYER_DIR).glob("*.html")}
    rows = json.loads((ROOT / "data" / "ntrp_nv_2026.json").read_text())
    return [r["name"] for r in rows if slug(r["name"]) in slugs]


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--names", help="Comma-separated player names")
    g.add_argument("--linked", action="store_true", help="All players with a page in players/nv")
    ap.add_argument("--out", help="JSON file to write (resumes: names already in it are skipped)")
    ap.add_argument("--shard", help="i/n: only every n-th name starting at i (run n copies in parallel)")
    ap.add_argument("--years", help="Year range to pull, e.g. 2022-2025 (default: all)")
    ap.add_argument("--pause", type=float, default=0.5, help="Seconds after each request")
    ap.add_argument("--no-cache", action="store_true", help="Don't use roster levels cached by name")
    args = ap.parse_args(argv)

    yw = None
    if args.years:
        a, _, b = args.years.partition("-")
        yw = set(range(int(a), int(b or a) + 1))
    names = _linked_names() if args.linked else [n.strip() for n in args.names.split(",")]
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        names = names[i::n]
    done = {}
    if args.out and Path(args.out).exists():
        done = {r["name"]: r for r in json.loads(Path(args.out).read_text())}
    for r in done.values():  # warm the roster cache on resume
        for y, v in r.get("years", {}).items():
            if v.get("level") and not v.get("cached"):
                SEEN.setdefault(int(y), {}).setdefault(r["name"].lower(), (v["level"], v["team"]))
    cache = Path(args.out).with_suffix(".rosters.json") if args.out else None
    if cache and cache.exists():
        for y, m in json.loads(cache.read_text()).items():
            for who, v in m.items():
                SEEN.setdefault(int(y), {}).setdefault(who, tuple(v))
    sess = requests.Session()
    ids = _profile_ids()
    want_of = {}
    lst = ROOT / "data" / "ntrp_nv_2026.json"
    if lst.exists():
        rows = json.loads(lst.read_text())
        cnt = {}
        for r in rows:
            cnt[r["name"]] = cnt.get(r["name"], 0) + 1
        want_of = {r["name"]: (r.get("city") or "", r.get("state") or "") for r in rows
                   if cnt[r["name"]] == 1 and r.get("state")}
    # Resumed entries pulled before the location check: verify the home state, re-pull if wrong.
    for n in [n for n in names if n in done and "loc" not in done[n] and want_of.get(n) and not ids.get(n)]:
        try:
            _, k, loc = find_profile(sess, n, want_of[n], args.pause)
        except Exception:
            continue
        if k is None and _loc_ok(loc, want_of[n]):
            done[n]["loc"] = loc
        else:
            print(f"[recheck] {n}: first profile is {loc}, wanted {want_of[n]} -> re-pull", flush=True)
            del done[n]
    for i, n in enumerate(names, 1):
        if n in done:
            continue
        try:
            r = player_history(sess, n, args.pause, years_wanted=yw, s=ids.get(n),
                               use_cache=not args.no_cache, want=None if ids.get(n) else want_of.get(n))
        except Exception as e:  # keep going; record the failure
            r = {"name": n, "error": str(e)}
        done[n] = r
        yrs = " ".join(f"{y}:{v['level'] or '?'}" for y, v in r.get("years", {}).items())
        print(f"[{i}/{len(names)}] {n}: {r.get('seconds')} s, {r.get('fetches')} fetches | {yrs or r.get('error')}", flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps(list(done.values()), indent=1))
            cache.write_text(json.dumps({str(y): m for y, m in SEEN.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
