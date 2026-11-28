#!/usr/bin/env python3
"""
scrapers/tl_roster_levels.py
Spot-check players' NTRP level on their team rosters, one season at a time, without
crawling matches. Input: a JSON list with name, tl_pid, history_url (or
search_url). For each player: open their TennisLink player page, pick one team
of that year (any state; prefers adult over mixed / combo / summer / tri-level),
open it and read their level off the roster.

    ~/.venvs/tennis-dashboard/bin/python scrapers/tl_roster_levels.py IN.json OUT.json [--years 2022,2023]
Output: {tl_pid: {name, levels: {year: {level, team, team_url, differs?}}, notes}};
years with no team are left out; "differs" lists two teams whose levels disagree.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from scrapers.scrape_tl_teams import (CAPTCHA_EXIT, CaptchaRequired, click, goto,  # noqa: E402
                                      _check_blocked, norm, open_browser, parse_team_page,
                                      player_team_rows, session_login)

AVOID = re.compile(r"MIXED|COMBO|SUMMER|TRI-?LEVEL|HOLDING|FLEX", re.I)


def pick(rows, year):
    rows = [r for r in rows if r["year"] == year]
    rows.sort(key=lambda r: (bool(AVOID.search(r["league"] + " " + r["flight"])),
                             not re.search(r"\d\.\d", r["flight"])))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inp")
    ap.add_argument("out")
    ap.add_argument("--year", help="ignored (older flag); see --years")
    ap.add_argument("--years", default="2022,2023,2024,2025,2026", help="years to read, comma list")
    a = ap.parse_args()
    a.years = [int(y) for y in a.years.split(",")]
    people = json.loads(Path(a.inp).read_text())
    out_p = Path(a.out)
    res = json.loads(out_p.read_text()) if out_p.exists() else {}
    with sync_playwright() as p:
        ctx, page = open_browser(p)
        try:
            session_login(page)
        except CaptchaRequired:
            print("[captcha] sign in with scrapers/tl_signin.py, then rerun", flush=True)
            ctx.close()
            sys.exit(CAPTCHA_EXIT)
        for i, pl in enumerate(people, 1):
            pid = pl["tl_pid"]
            if pid in res:
                continue
            goto(page, pl.get("history_url") or pl["search_url"])
            _check_blocked(page)
            rows = player_team_rows(page)
            rec = {"name": pl["name"], "levels": {}, "notes": []}
            # every year asked for (default: --years): one roster level per year
            for year in pl.get("years") or a.years:
                cands = pick(rows, year)
                if not cands:
                    continue
                # first (preferred) team gives the level; a second non-mixed/summer
                # team, when there is one, checks that the season is consistent
                pref = [r for r in cands if not AVOID.search(r["league"] + " " + r["flight"])]
                seen = []
                for r in (pref[:2] or cands[:1]):
                    click(page, f"#{r['el_id']}")
                    _check_blocked(page)
                    info = parse_team_page(page)
                    me = [x for x in info["roster"] if norm(x["name"]) == norm(pl["name"])]
                    goto(page, pl.get("history_url") or pl["search_url"])
                    seen.append({"level": me[0].get("ntrp") if me else None,
                                 "team": f"{r['district']}|{r['league']}|{r['flight']}|{r['team']}",
                                 "team_url": info["url"]})
                got = next((x for x in seen if x["level"]), seen[0])
                rec["levels"][str(year)] = got
                lvls = {x["level"] for x in seen if x["level"]}
                if len(lvls) > 1:
                    got["differs"] = [{"level": x["level"], "team": x["team"]} for x in seen]
                    rec["notes"].append(f"{year}: levels differ across teams ({', '.join(sorted(lvls))})")
                if not got["level"]:
                    rec["notes"].append(f"{year}: not on the roster of the teams checked")
            lv = {y: v["level"] for y, v in rec["levels"].items()}
            res[pid] = rec
            print(f"[{i}/{len(people)}] {pl['name']}: {lv or 'no teams 2022+'}", flush=True)
            out_p.write_text(json.dumps(res, indent=1, ensure_ascii=False))
        ctx.close()
    print(f"wrote {out_p} ({len(res)} players, "
          f"{sum(1 for r in res.values() if r['levels'])} with a level)")


if __name__ == "__main__":
    main()
