#!/usr/bin/env python3
"""
scrapers/scrape_tr_matches.py
Pull each player's full match history for one year from TennisRecord
(public, no login), including fall leagues, Districts, Sectionals and Mixed.

Every row on the player's match-history page is saved; filtering to the
matches that count toward NTRP happens downstream (see engine/bump.py).

Usage:
    python3 scrapers/scrape_tr_matches.py --names "Shi L Oskooi"
    python3 scrapers/scrape_tr_matches.py --nv-pages            # the NV player-page set
    python3 scrapers/scrape_tr_matches.py --nv-pages --year 2026 --out data/tr_matches_2026.json
    python3 scrapers/scrape_tr_matches.py --targets data/tr_targets_bump_backtest.json --year 2024

Resumable: players already in the output file are skipped. One request at a
time with a pause between players.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://www.tennisrecord.com"
UA = {"User-Agent": "Mozilla/5.0 (tennis-dashboard match history)"}


def _num(s: str):
    try:
        return float(s)
    except ValueError:
        return None


def _people(td) -> list[dict]:
    """Names (with TennisRecord query) and the rating shown under each."""
    out = []
    for a in td.find_all("a"):
        q = parse_qs(urlparse(a.get("href", "")).query)
        rating = None
        nxt = a.find_next_sibling(string=True)
        sib = a.next_sibling
        # rating text "(3.05)" / "(-----)" follows a <br>
        txt = ""
        while sib is not None and getattr(sib, "name", None) != "a":
            txt += sib.get_text() if hasattr(sib, "get_text") else str(sib)
            sib = sib.next_sibling
        m = re.search(r"\(([\d.]+)\)", txt)
        if m:
            rating = float(m.group(1))
        out.append({"name": a.get_text(strip=True),
                    "tr_playername": (q.get("playername") or [""])[0],
                    "tr_s": (q.get("s") or [""])[0],
                    "rating": rating})
    return out


def parse_history(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tables = [t for t in soup.find_all("table") if t.find("th", string="Match Date")]
    if not tables:
        return []
    rows = []
    for tr in tables[0].find_all("tr")[1:]:
        td = tr.find_all("td")
        if len(td) < 10:
            continue
        lg_a = td[1].find("a")
        res_a = td[7].find("a")
        mid = None
        if res_a:
            mid = (parse_qs(urlparse(res_a.get("href", "")).query).get("mid") or [None])[0]
        rows.append({
            "date": td[0].get_text(strip=True),
            "league": td[1].get_text(" ", strip=True),
            "league_href": lg_a.get("href") if lg_a else None,
            "team": td[2].get_text(" ", strip=True),
            "court": td[3].get_text(strip=True),
            "partner": _people(td[4]),
            "opponents": _people(td[5]),
            "wl": td[6].get_text(strip=True),
            "score": td[7].get_text(" ", strip=True),
            "tr_mid": mid,
            "match_rating": _num(td[8].get_text(strip=True)),
            "rating_after": _num(td[9].get_text(strip=True)),
            "match_rating_raw": td[8].get_text(strip=True),
        })
    return rows


def fetch(sess: requests.Session, playername: str, s: str, year: int) -> list[dict]:
    url = f"{BASE}/adult/matchhistory.aspx?year={year}&playername={quote(playername)}"
    if s:
        url += f"&s={s}"
    for attempt in range(3):
        try:
            r = sess.get(url, headers=UA, timeout=60)
            r.raise_for_status()
            return parse_history(r.text)
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5 * (attempt + 1))
    return []


def nv_page_players() -> list[dict]:
    """Players with a page under players/nv/, with their TennisRecord name."""
    ids = {p.stem for p in (ROOT / "players" / "nv").glob("*.html")}
    players = json.loads((ROOT / "data" / "players.json").read_text())
    out = []
    for p in players:
        if p.get("id") not in ids:
            continue
        q = parse_qs(urlparse(p.get("profile_url") or "").query)
        out.append({"id": p["id"], "name": p["name"],
                    "tr_playername": (q.get("playername") or [p["name"]])[0],
                    "tr_s": (q.get("s") or [""])[0]})
        ids.discard(p["id"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--names", nargs="*", default=[])
    ap.add_argument("--nv-pages", action="store_true")
    ap.add_argument("--targets", default=None,
                    help="JSON list of {id, name, tr_playername, tr_s}")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--out", default=None)
    ap.add_argument("--pause", type=float, default=1.5)
    args = ap.parse_args()

    out_path = Path(args.out or ROOT / "data" / f"tr_matches_{args.year}.json")
    data = json.loads(out_path.read_text()) if out_path.exists() else {}
    targets = [{"id": n, "name": n, "tr_playername": n, "tr_s": ""} for n in args.names]
    if args.nv_pages:
        targets += nv_page_players()
    if args.targets:
        targets += json.loads(Path(args.targets).read_text())

    sess = requests.Session()
    todo = [t for t in targets if t["id"] not in data]
    print(f"{len(todo)} to fetch ({len(targets) - len(todo)} already done)")
    for i, t in enumerate(todo, 1):
        try:
            rows = fetch(sess, t["tr_playername"], t["tr_s"], args.year)
            data[t["id"]] = {"name": t["name"], "tr_playername": t["tr_playername"],
                             "tr_s": t["tr_s"], "matches": rows,
                             "fetched": time.strftime("%Y-%m-%dT%H:%M:%S")}
        except Exception as e:   # keep going; record the failure
            data[t["id"]] = {"name": t["name"], "error": str(e)}
        if i % 10 == 0 or i == len(todo):
            out_path.write_text(json.dumps(data, indent=1))
            print(f"  {i}/{len(todo)}")
        time.sleep(args.pause)
    out_path.write_text(json.dumps(data, indent=1))


if __name__ == "__main__":
    main()
