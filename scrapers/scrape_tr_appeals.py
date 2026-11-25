#!/usr/bin/env python3
"""
scrapers/scrape_tr_appeals.py
For every A (appeal) rating on the USTA NV list, read the player's TennisRecord
estimated dynamic rating after their last match of the prior year. That tells us
which way the year-end computer rating most likely went before the appeal:
above the level -> bumped up, appealed down; below the band -> dropped, appealed up.

Public pages, no login. Writes data/tr_appeal_dynamics.json (resumable).

Usage:
    python3 -m scrapers.scrape_tr_appeals [--year 2025]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scrape_tennisrecord_history import BASE, _get, _profile_ids, _rows, find_profile  # noqa: E402

DATE = re.compile(r"\d\d/\d\d/\d{4}$")


def year_matches(soup) -> list[dict]:
    """Match rows (newest first) from a matchhistory page: date, league, match rating, dynamic after."""
    for t in soup.find_all("table"):
        rows = _rows(t)
        if rows and rows[0][:1] == ["Match Date"]:
            out = []
            for r in rows[1:]:
                if len(r) >= 10 and DATE.match(r[0]):
                    try:
                        dyn = float(r[-1])
                    except ValueError:
                        continue
                    out.append({"date": r[0], "league": r[1], "match": r[-2], "dyn": dyn})
            return out
    return []


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025, help="Season to read when the rating has no date")
    ap.add_argument("--out", default=str(ROOT / "data" / "tr_appeal_dynamics.json"))
    ap.add_argument("--pause", type=float, default=0.5)
    args = ap.parse_args(argv)

    rows = json.loads((ROOT / "data" / "ntrp_nv_2026.json").read_text())
    cnt = {}
    for r in rows:
        cnt[r["name"]] = cnt.get(r["name"], 0) + 1
    ids = _profile_ids()
    hist = {}
    for f in [ROOT / "data" / "rating_history_tr.json", *sorted((ROOT / "data" / "tr_rest").glob("p*.json"))]:
        if f.exists() and not f.name.endswith(".rosters.json"):
            for h in json.loads(f.read_text()):
                if h.get("loc") or h.get("s"):
                    hist[h["name"]] = h
    out = Path(args.out)
    done = {d["name"]: d for d in json.loads(out.read_text())} if out.exists() else {}
    sess = requests.Session()
    for r in rows:
        n = r["name"]
        if r.get("rating_type") != "A":
            continue
        # A year-end appeal dated 12/31/Y was made on the computer rating from season Y.
        d = r.get("rating_date") or ""
        year = int(d[-4:]) if d[-4:].isdigit() else args.year
        if n in done and done[n].get("year", args.year) == year:
            continue
        want = (r.get("city") or "", r.get("state") or "") if cnt[n] == 1 else None
        s = ids.get(n) or (hist.get(n) or {}).get("s")
        rec = {"name": n, "level": r["level"], "date": r.get("rating_date"), "year": year}
        try:
            prof, k, loc = find_profile(sess, n, want, args.pause, s)
            rec["loc"] = loc
            if prof is None:
                rec["unmatched"] = True
            else:
                rec["s"] = k
                txt = prof.get_text(" ", strip=True)
                m = re.search(r"([1-7]\.\d{4})\s+(\d{1,2}/\d{1,2}/\d{4})\s+Projected", txt)
                if m:
                    rec["dyn_now"], rec["dyn_now_date"] = float(m.group(1)), m.group(2)
                q = quote(n) + (f"&s={k}" if k else "")
                mh = _get(sess, f"{BASE}/adult/matchhistory.aspx?year={year}&playername={q}&mt=0&lt=0&yr=0", args.pause)
                ms = year_matches(mh)
                rec["matches"] = len(ms)
                if ms:
                    rec["last"] = ms[0]
        except Exception as e:  # keep going
            rec["error"] = str(e)
        done[n] = rec
        print(n, rec.get("last", {}).get("dyn"), rec.get("matches"), rec.get("error", ""), flush=True)
        out.write_text(json.dumps(list(done.values()), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
