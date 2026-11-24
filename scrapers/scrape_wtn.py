#!/usr/bin/env python3
"""
scrapers/scrape_wtn.py
Snapshot USTA World Tennis Number (WTN) singles + doubles for our NV players.

One login in a visible browser, then for each player:
  1. Name search (services.usta.com .../profile/search/public) -> NV matches + UAID
  2. playerInfo JSON (www.usta.com/usta/api?type=playerInfo) for each NV match,
     called from inside the logged-in usta.com page (same as the site does).
  3. If the JSON call fails, fall back to opening the player's profile page and
     reading the playerInfo response the page loads itself.
Resumable: players already in the output file are skipped (use --redo to retry
statuses such as error,not_found).

Needs: pip install playwright python-dotenv  (Chromium via `playwright install chromium`)
Reads TENNISLINK_USER / TENNISLINK_PASS from .env (the USTA account TennisLink uses).
Writes data/wtn_snapshot_<date>.json. No tokens or credentials are saved.

Usage:
    python3 scrapers/scrape_wtn.py --test "Kant Kodali,Brad Evans"
    python3 scrapers/scrape_wtn.py      # every NV player page: players/nv/ at git FETCH_HEAD, else the local folder
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from scrapers.scrape_tennislink import login  # noqa: E402

SEARCH_PAGE = "https://www.usta.com/en/home/play/player-search.html"
PROFILE = "https://www.usta.com/en/home/play/player-search/profile.html#uaid={}&tab=tournaments"

FEED_JS = """async ([fname, lname, tryInfo]) => {
  const tok = (await (await fetch('/libs/granite/csrf/token.json')).json()).token;
  const sr = await fetch('https://services.usta.com/v1/dataexchange/profile/search/public', {
     method: 'POST', headers: {'content-type': 'application/json', 'accept': 'application/json'},
     body: JSON.stringify({pagination: {pageSize: '51', currentPage: '1'}, selection: {name: {fname, lname}}})});
  const s = await sr.json();
  const hits = (s.data || []).filter(h => h.state === 'NV' || (h.district && h.district.name === 'Nevada'));
  const out = [];
  for (const h of hits) {
    if (!tryInfo) { out.push({hit: h, status: 0, info: null, err: 'skipped'}); continue; }
    const r = await fetch('/usta/api?type=playerInfo', {method: 'POST',
       headers: {'content-type': 'application/json', 'accept': 'application/json', 'CSRF-Token': tok},
       body: JSON.stringify({selection: {uaid: String(h.uaid)},
                             output: {ratings: 'true', extendedProfile: 'true', wtn: 'true', genderIdentity: 'true'}})});
    const txt = await r.text();
    let d = null; try { d = JSON.parse(txt).data[0]; } catch (e) {}
    out.push({hit: h, status: r.status, info: d, err: d ? null : txt.slice(0, 300)});
  }
  const others = (s.data || []).filter(h => !hits.includes(h)).slice(0, 15).map(h => ({uaid: h.uaid, city: h.city, state: h.state}));
  return {searchStatus: sr.status, total: (s.pagination || {}).totalResults, out, others};
}"""


def slim(info: dict | None) -> dict:
    if not info:
        return {}
    rt = info.get("ratings") or {}
    ntrp = rt.get("ntrp") or {}
    wtn = {}
    for w in rt.get("wtn") or []:
        wtn[w["type"].lower()] = {k: w.get(k) for k in (
            "tennisNumber", "confidence", "gameZoneLower", "gameZoneUpper",
            "lastPlayed", "ratingDate", "updatedAt", "source", "proZone")}
    return {
        "usta_name": info.get("name"), "city": info.get("city"), "gender": info.get("gender"),
        "itf_tennis_id": info.get("itfTennisId"),
        "ntrp": {k: ntrp.get(k) for k in ("ntrpRating", "ratingType", "ratingDate")} if ntrp else None,
        "wtn_singles": wtn.get("single"), "wtn_doubles": wtn.get("double"),
    }


def via_profile_page(page, uaid: str) -> dict | None:
    """Fallback: load the profile page and read the playerInfo JSON it fetches."""
    got = []
    def onr(r):
        if "type=playerInfo" in r.url:
            try:
                got.append(r.json())
            except Exception:
                pass
    page.on("response", onr)
    try:
        page.goto(PROFILE.format(uaid), wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_selector("text=ITF Tennis ID", timeout=20_000)
    except Exception:
        page.wait_for_timeout(3000)
    finally:
        page.remove_listener("response", onr)
    page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=45_000)
    for g in got:
        try:
            return g["data"][0]
        except Exception:
            pass
    return None


def name_variants(name: str):
    parts = name.replace(",", " ").split()
    if len(parts) < 2:
        return []
    v = [(parts[0], parts[-1])]
    if len(parts) > 2:
        v.append((parts[0], " ".join(parts[1:])))     # compound surname
        v.append((parts[0], " ".join(parts[-2:])))
        v.append((" ".join(parts[:-1]), parts[-1]))   # two-word first name (Mary Ann)
    return v


STATE = {'feed_400s': 0}


def lookup(page, name: str, city_hint: str | None, test=False) -> dict:
    t = time.time()
    rec = {"name": name, "city_hint": city_hint, "candidates": [], "status": "not_found"}
    for fn, ln in name_variants(name):
        res = page.evaluate(FEED_JS, [fn, ln, STATE['feed_400s'] < 3])
        for o in res['out']:
            if o['status'] == 400:
                STATE['feed_400s'] += 1
            elif o['status'] == 200:
                STATE['feed_400s'] = 0
        if res["searchStatus"] in (403, 429) or any(o["status"] == 429 for o in res["out"]):
            raise Blocked(f"HTTP {res['searchStatus']} / {[o['status'] for o in res['out']]}")
        if test:
            print(json.dumps(res)[:1500])
        if not res["out"]:
            if res.get("others"):
                rec.setdefault("non_nv_candidates", {})[f"{fn} {ln}"] = res["others"]
            continue
        for o in res["out"]:
            uaid = str(o["hit"]["uaid"])
            info, src = o["info"], "feed"
            if not info:
                info, src = via_profile_page(page, uaid), "page"
                if not info and any(k in page.content().lower() for k in ("captcha", "are you a robot", "access denied")):
                    raise Blocked("challenge page")
            c = {"uaid": uaid, "search_city": o["hit"].get("city"), "search_state": o["hit"].get("state"), "source": src,
                 "feed_status": o["status"], **slim(info)}
            if not info:
                c["error"] = o["err"]
            rec["candidates"].append(c)
        rec["searched_as"] = f"{fn} {ln}"
        break
    cands = rec["candidates"]
    if not cands and rec.get("non_nv_candidates"):
        rec["status"] = "no_nv_match"
    if len(cands) == 1:
        rec["status"] = "ok"
    elif len(cands) > 1:
        same = [c for c in cands if city_hint and (c.get("search_city") or "").lower() == city_hint.lower()]
        women = [c for c in (same or cands) if c.get("gender") == "FEMALE"]
        pick = same if len(same) == 1 else (women if len(women) == 1 else None)
        if pick:
            rec["status"] = "ok"
            rec["picked_by"] = "city" if len(same) == 1 else "gender"
            rec["candidates"] = pick + [c for c in cands if c is not pick[0]]
        else:
            rec["status"] = "ambiguous"
    if rec["status"] == "ok" and rec["candidates"][0].get("error"):
        rec["status"] = "error"
    rec["seconds"] = round(time.time() - t, 2)
    return rec


def nv_players() -> list[tuple[str, str]]:
    """(id, name) for every NV player page (players/nv/*.html), names from data/players.json."""
    import subprocess
    files = subprocess.check_output(["git", "ls-tree", "--name-only", "FETCH_HEAD", "players/nv/"],
                                    cwd=ROOT, text=True).split()
    ids = sorted(f.split("/")[-1][:-5] for f in files if f.endswith(".html"))
    if not ids:
        ids = sorted(f.stem for f in (ROOT / "players" / "nv").glob("*.html"))
    by_id = {}
    for p in json.loads((ROOT / "data" / "players.json").read_text()):
        if "id" in p:
            by_id.setdefault(p["id"], p["name"])
    return [(i, by_id[i]) for i in ids if i in by_id]


def resolve_by_ntrp(done: dict) -> None:
    """Ambiguous same-name NV matches: keep the one whose NTRP matches ours (players.json)."""
    ours = {}
    for p in json.loads((ROOT / "data" / "players.json").read_text()):
        if p.get("id") and p.get("ntrp_rating"):
            ours.setdefault(p["id"], p["ntrp_rating"].split()[0])
    for r in done.values():
        if r["status"] != "ambiguous" or r["id"] not in ours:
            continue
        want = float(ours[r["id"]])
        hit = [c for c in r["candidates"] if (c.get("ntrp") or {}).get("ntrpRating") == want]
        if len(hit) == 1:
            r["status"], r["picked_by"] = "ok", "ntrp"
            r["candidates"] = hit + [c for c in r["candidates"] if c is not hit[0]]


class Blocked(Exception):
    pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", help="Comma-separated names; print raw responses, no output file")
    ap.add_argument("--out", default=str(ROOT / "data" / f"wtn_snapshot_{dt.date.today():%Y-%m-%d}.json"))
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", default="error", help="Comma-separated statuses to retry on resume")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    user, pw_ = os.getenv("TENNISLINK_USER"), os.getenv("TENNISLINK_PASS")
    if not user or not pw_:
        print("TENNISLINK_USER / TENNISLINK_PASS not set")
        return 1
    city = {}
    for r in json.loads((ROOT / "data" / "ntrp_nv_2026.json").read_text()):
        city.setdefault(r["name"].lower(), r.get("city"))

    out = Path(args.out)
    done = {} if args.test or not out.exists() else {r["id"]: r for r in json.loads(out.read_text())["players"]}
    names = [(n.strip(), n.strip()) for n in args.test.split(",")] if args.test else nv_players()
    todo = [(i, n) for i, n in names if i not in done or done[i]["status"] in args.redo.split(",")]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(names)} players, {len(todo)} to do")

    def save():
        if args.test:
            return
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps({"snapshot_date": f"{dt.date.today()}", "players": sorted(done.values(), key=lambda r: r["id"])}, indent=1))
        tmp.replace(out)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            login(page, user, pw_)
            page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(2500)
            try:
                page.get_by_role("button", name="Reject All").click(timeout=5000)
            except Exception:
                pass
            page.get_by_text("SIGN IN", exact=True).first.click(timeout=10_000, no_wait_after=True)
            for _ in range(60):                     # SSO redirect can be slow
                page.wait_for_timeout(1000)
                try:
                    if "player-search" in page.url and not page.get_by_text("SIGN IN", exact=True).first.is_visible():
                        break
                except Exception:
                    pass
            if "player-search" not in page.url:
                page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(3000)
            fails = 0
            t0 = time.time()
            for i, (pid, n) in enumerate(todo, 1):
                try:
                    try:
                        r = lookup(page, n, city.get(n.lower()), test=bool(args.test))
                    except Blocked:
                        raise
                    except Exception:                 # e.g. "Failed to fetch" right after sign-in
                        page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=45_000)
                        page.wait_for_timeout(4000)
                        r = lookup(page, n, city.get(n.lower()), test=bool(args.test))
                    fails = 0
                except Blocked as e:
                    print(f"  BLOCKED on {n}: {e} -- stopping")
                    break
                except Exception as e:
                    r = {"name": n, "status": "error", "error": str(e)[:300], "candidates": []}
                    fails += 1
                r["id"] = pid
                r["snapshot_at"] = dt.datetime.now().isoformat(timespec="seconds")
                done[pid] = r
                c = r["candidates"][0] if r["candidates"] else {}
                s, d = c.get("wtn_singles") or {}, c.get("wtn_doubles") or {}
                print(f"  [{i}/{len(todo)}] {n}: {r['status']} S={s.get('tennisNumber')} D={d.get('tennisNumber')} "
                      f"src={c.get('source')} {r.get('seconds')}s", flush=True)
                if i % 10 == 0:
                    save()
                if fails >= 5:
                    print("  5 errors in a row, stopping")
                    break
                time.sleep(args.pause)
            print(f"done in {round(time.time() - t0)} s")
        finally:
            if not args.test:
                resolve_by_ntrp(done)
            save()
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
