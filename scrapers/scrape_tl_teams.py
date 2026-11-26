#!/usr/bin/env python3
"""
scrapers/scrape_tl_teams.py
Team-first TennisLink crawl of 2026 match results.

Instead of crawling every player's history, crawl each 2026 non-Mixed team once
and share its matches with everyone who played in them.

  Step A  For each of our NV players (players/nv/*.html), open their TennisLink
          player page and read "2026 Teams this member is registered on".
          Skip Mixed. Dedupe teams by district|league|flight|team name
          (TennisLink's own team ids in links are re-encrypted on every visit,
          so they can't be used as keys).
  Step B  Each new team is opened once (by clicking it on the player page):
          roster, every league match id (ViewScore(<id>,7)), and its
          Championship Advancements (Districts / Sectionals / beyond). Each
          championship flight is opened once and all its match ids
          (ViewScore(<id>,12)) are collected.
  Step C  Every match id is fetched once (dedupe across teams):
            league match  StatsAndStandings.aspx?t=7&par1=<id>&par2=2026&par3=0
            champ match   StatsAndStandings.aspx?t=12&par1=<id>&par2=2026&par3=0
          Lines, player names and winners come from the match page.

Output data/tl_team_matches_2026.json:
  teams          {team_id: {name, section, district, league, flight, kind, roster, url}}
  champ_flights  {champ_id: {name, level, date, match_ids}}
  matches        {match_id: {tl_id, kind, date, league, flight, district, section,
                             status, home, away, teams:[home_tid, away_tid],
                             lines:[{line, players_home, players_away, score, winner}]}}
  player_teams   {our player slug: [team ids]}
  players        {name_key: {name, area, ntrp, team_ids, matches:[[match_id, line, side]]}}
                 -- EVERYONE seen on a crawled roster or match page.
  _progress      resumability bookkeeping;  _timing  seconds per step.

Usage:
  python3 scrapers/scrape_tl_teams.py --names "Shi L Oskooi,Kim Springer"
  python3 scrapers/scrape_tl_teams.py            # all players/nv/*.html (resumable)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scrapers.scrape_tennislink import (  # noqa: E402
    BASE_URL, PLAYER_SEARCH_URL, login, _wait_for_network,
    _search_player_and_get_nv_row, _extract_roster, _parse_match_detail_page,
)

YEAR = 2026
SS_URL = f"{BASE_URL}/leagues/Main/StatsAndStandings.aspx"
OUT = ROOT / "data" / f"tl_team_matches_{YEAR}.json"
NTRP_LIST = ROOT / "data" / "ntrp_nv_2026.json"
PLAYERS_JSON = ROOT / "data" / "players.json"
PLAYER_DIR = ROOT / "players" / "nv"
MIN_GAP = 1.6          # seconds between page loads
SAVE_EVERY = 3         # teams

_last_load = [0.0]


def _pace():
    gap = time.time() - _last_load[0]
    if gap < MIN_GAP:
        time.sleep(MIN_GAP - gap)
    _last_load[0] = time.time()


def goto(page, url):
    _pace()
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    _wait_for_network(page, 12_000)


def click(page, selector):
    _pace()
    page.click(selector, timeout=10_000)
    _wait_for_network(page, 15_000)


def _check_blocked(page):
    t = (page.title() or "").lower() + page.url.lower()
    if any(k in t for k in ("captcha", "access denied", "account.usta.com")):
        raise RuntimeError(f"Blocked / logged out at {page.url} ({page.title()})")


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def strip_subflight(flight: str) -> str:
    return re.sub(r"\s*/\s*[A-Z0-9]{1,3}\s*$", "", flight.strip())


def team_id(district, league, flight, name) -> str:
    return "|".join([district.strip().upper(), league.strip().upper(),
                     strip_subflight(flight).upper(), name.strip().upper()])


def is_mixed(league: str, flight: str) -> bool:
    return "MIXED" in (league + " " + flight).upper()


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def load_store() -> dict:
    if OUT.exists():
        d = json.loads(OUT.read_text())
    else:
        d = {}
    for k in ("teams", "champ_flights", "matches", "player_teams", "players"):
        d.setdefault(k, {})
    d.setdefault("_progress", {"players_done": [], "teams_done": [],
                               "champs_done": [], "match_queue": {}})
    d.setdefault("_timing", {"player_page": [], "team": [], "champ_flight": [],
                             "match": []})
    return d


def save_store(d: dict):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(OUT)


def add_player(d, name, area, team_tid=None, ntrp=None, ref=None):
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        return
    key = f"{norm(name)}|{(area or '').upper()}"
    p = d["players"].setdefault(key, {"name": name, "area": (area or "").upper(),
                                      "ntrp": None, "team_ids": [], "matches": []})
    if ntrp and not p["ntrp"]:
        p["ntrp"] = ntrp
    if team_tid and team_tid not in p["team_ids"]:
        p["team_ids"].append(team_tid)
    if ref and ref not in p["matches"]:
        p["matches"].append(ref)


# ---------------------------------------------------------------------------
# Step A: player page -> 2026 teams
# ---------------------------------------------------------------------------

def open_player_page(page, name, history_url):
    if history_url:
        goto(page, history_url)
    else:
        _pace()
        name_a, _ = _search_player_and_get_nv_row(page, name)
        if not name_a:
            return False
        _pace()
        name_a.click()
        _wait_for_network(page, 12_000)
    _check_blocked(page)
    return bool(page.query_selector("a[id*='rptPlayerName'][id$='LinkButton4']"))


def player_team_rows(page) -> list[dict]:
    """Rows of every 'YYYY Teams this member is registered on' table."""
    return page.evaluate(r"""() => {
        const out = []; let year = null;
        for (const tr of document.querySelectorAll('tr')) {
            const m = tr.innerText.match(/(\d{4}) Teams this member is registered on/);
            if (m && !tr.querySelector("a[id$='LinkButton4']")) { year = +m[1]; continue; }
            const a = tr.querySelector("a[id*='rptPlayerName'][id$='LinkButton4']");
            if (!a) continue;
            const tds = [...tr.querySelectorAll(':scope > td')].map(td => td.innerText.trim());
            out.push({year, el_id: a.id, team: a.innerText.trim(), section: tds[1] || '',
                      district: tds[2] || '', league: tds[3] || '', flight: tds[4] || ''});
        }
        return out;
    }""")


# ---------------------------------------------------------------------------
# Step B: team page
# ---------------------------------------------------------------------------

def parse_team_page(page) -> dict:
    txt = page.inner_text("body")
    hdr = re.search(r"Section\tDistrict/Area\tLeague\tFlight/SubFlight\n([^\n]*)", txt)
    sec, dist, league, flight = (hdr.group(1).split("\t") + ["", "", "", ""])[:4] if hdr else [""] * 4
    name_m = re.search(r"\nTeam: ([^\n]+)", txt)
    share = page.query_selector("#ctl00_mainContent_lnkShare")
    html = page.content()
    ids = list(dict.fromkeys(re.findall(r"ViewScore\((\d+),7,", html)))
    champs = page.evaluate(r"""() => [...document.querySelectorAll(
        "a[id*='rptChampAdvancementForTeamSummary']")].filter(a => a.innerText.trim()).map(a => {
            const tds = [...a.closest('tr').querySelectorAll('td')].map(t => t.innerText.trim());
            return {el_id: a.id, name: a.innerText.trim(), date: tds[tds.length - 1] || ''};
        })""")
    return {
        "name": name_m.group(1).strip() if name_m else "",
        "section": sec.strip(), "district": dist.strip(), "league": league.strip(),
        "flight": flight.strip(),
        "url": (SS_URL.rsplit("/", 1)[0] + "/" + share.get_attribute("href")) if share else "",
        "roster": _extract_roster(page),
        "match_ids": ids, "champs": champs,
    }


def crawl_team(page, d, tid, row) -> None:
    t0 = time.time()
    click(page, f"#{row['el_id']}")
    _check_blocked(page)
    info = parse_team_page(page)
    team = {"name": info["name"] or row["team"], "section": info["section"] or row["section"],
            "district": info["district"] or row["district"], "league": info["league"] or row["league"],
            "flight": info["flight"] or row["flight"], "kind": "league",
            "roster": info["roster"], "url": info["url"], "match_ids": info["match_ids"],
            "champs": [{"name": c["name"], "date": c["date"]} for c in info["champs"]]}
    d["teams"][tid] = team
    for r in info["roster"]:
        add_player(d, r["name"], team["district"], tid, r.get("ntrp"))
    for mid in info["match_ids"]:
        d["_progress"]["match_queue"].setdefault(f"L{mid}", {"tl_id": mid, "t": 7})
    d["_timing"]["team"].append(round(time.time() - t0, 2))

    # championship flights reachable from this team
    first = True
    for c in info["champs"]:
        ckey = f"{c['name']}|{c['date']}"
        if ckey in d["_progress"]["champs_done"]:
            continue
        t1 = time.time()
        if not first:
            goto(page, team["url"])
        first = False
        crawl_champ(page, d, ckey, c, tid, team["url"], on_team_page=True)
        d["_progress"]["champs_done"].append(ckey)
        d["_timing"]["champ_flight"].append(round(time.time() - t1, 2))


_SUBFLIGHT_SEL = "a[id*='rptChampHeaderPart2'][id$='lnkCPSubFlightForTeamStandings']"


def _open_champ(page, team_url: str, name: str, el_id: str | None, on_team_page: bool) -> bool:
    """From the team page, click the Championship Advancement called `name`."""
    if not on_team_page:
        goto(page, team_url)
    if not el_id:
        el = next((a for a in page.query_selector_all("a[id*='rptChampAdvancementForTeamSummary']")
                   if (a.inner_text() or "").strip() == name), None)
        if not el:
            return False
        el_id = el.get_attribute("id")
    click(page, f"#{el_id}")
    return True


def crawl_champ(page, d, ckey: str, c: dict, tid: str, team_url: str,
                on_team_page: bool = False) -> None:
    """Collect every match id of a championship flight. Nationals (and some
    Sectionals) are split into subflights: pools / round robin, semifinals,
    3rd-4th playoff. The flight's Match Summary only lists one of them, so
    open each subflight's Match Summary in turn."""
    if not _open_champ(page, team_url, c["name"], c.get("el_id"), on_team_page):
        print(f"  [warn] championship {ckey}: link not found on {tid}")
        return
    txt = page.inner_text("body")
    hm = re.search(r"Championship\tLevel\n([^\t\n]+)\t([^\n]+)", txt)
    subs = [(a.get_attribute("id"), (a.inner_text() or "").strip().lstrip("/ ").strip())
            for a in page.query_selector_all(_SUBFLIGHT_SEL)]
    cids: list[str] = []
    if len(subs) <= 1:
        click(page, "#ctl00_mainContent_lnkMatchSummaryForCPFlight")
        cids = re.findall(r"ViewScore\((\d+),12,", page.content())
    else:
        for i, (sid, label) in enumerate(subs):
            if i:
                _open_champ(page, team_url, c["name"], None, False)
            click(page, f"#{sid}")
            click(page, "#ctl00_mainContent_lnkMatchSummaryForCPFlight")
            cids += re.findall(r"ViewScore\((\d+),12,", page.content())
    cids = list(dict.fromkeys(cids))
    old = d["champ_flights"].get(ckey, {})
    if not cids and old.get("match_ids"):
        print(f"  [warn] {ckey}: no matches found this time; keeping the {len(old['match_ids'])} known")
        cids = old["match_ids"]
    d["champ_flights"][ckey] = {"name": hm.group(1).strip() if hm else c["name"],
                                "level": hm.group(2).strip() if hm else "",
                                "date": c["date"], "match_ids": cids,
                                "subflights": [s[1] for s in subs],
                                "from_team": old.get("from_team", tid)}
    for mid in cids:
        d["_progress"]["match_queue"].setdefault(f"C{mid}", {"tl_id": mid, "t": 12, "champ": ckey})


def refresh_champs(page, d) -> int:
    """Re-collect every known championship flight (all subflights). Returns
    how many new match ids were queued."""
    before = len(d["_progress"]["match_queue"])
    for ckey, cf in list(d["champ_flights"].items()):
        team = d["teams"].get(cf.get("from_team", ""), {})
        if not team.get("url"):
            print(f"  [warn] {ckey}: no team url")
            continue
        name, date = ckey.rsplit("|", 1)
        try:
            crawl_champ(page, d, ckey, {"name": name, "date": date}, cf["from_team"], team["url"])
        except RuntimeError:
            raise
        except Exception as e:
            print(f"  [warn] {ckey}: {e}")
        print(f"  {ckey}: {len(d['champ_flights'][ckey]['match_ids'])} matches "
              f"{d['champ_flights'][ckey].get('subflights') or ''}", flush=True)
    return len(d["_progress"]["match_queue"]) - before


# ---------------------------------------------------------------------------
# Step C: match page
# ---------------------------------------------------------------------------

def _dedouble(s: str) -> str:
    s = s.strip()
    h = len(s) // 2
    return s[:h] if len(s) % 2 == 0 and s[:h] == s[h:] else s


def parse_match_header(txt: str, kind: str) -> dict:
    out = {}
    if kind == "L":
        m = re.search(r"Flight\tUSTA Section\tDistrict/Area\tLeague\n([^\n]+)", txt)
        if m:
            f = (m.group(1).split("\t") + [""] * 4)[:4]
            out.update(flight=f[0].strip(), section=f[1].strip(), district=f[2].strip(), league=f[3].strip())
        m = re.search(r"\n\t([^\n]+)\nTeam ID:[^\n]*Vs\.\t([^\n]+)\nTeam ID", txt)
        if m:
            out.update(home=m.group(1).strip(), away=m.group(2).strip())
        m = re.search(r"Status:\s*([^\n]+?)\s{2,}", txt)
        if m:
            out["status"] = m.group(1).strip()
    else:
        m = re.search(r"Championship\tLevel\n([^\t\n]+)\t([^\n]+)", txt)
        if m:
            out.update(league=m.group(1).strip(), level=m.group(2).strip())
        m = re.search(r"Home Team:\s*([^\n]+)\nTeam ID:[^\n]*Visiting Team:\s*([^\n]+)\nTeam ID", txt)
        if m:
            out.update(home=_dedouble(m.group(1)), away=_dedouble(m.group(2).split("\t")[0]))
    m = re.search(r"Date Match Played:\s*(\d+/\d+/\d{4})", txt) or \
        re.search(r"Date Scheduled:\s*(\d+/\d+/\d{4})", txt)
    if m:
        out["date"] = m.group(1)
    return out


_STATES = {"ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO",
           "CONNECTICUT", "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS",
           "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA", "MAINE", "MARYLAND",
           "MASSACHUSETTS", "MICHIGAN", "MINNESOTA", "MISSISSIPPI", "MISSOURI", "MONTANA",
           "NEBRASKA", "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO", "NEW YORK",
           "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA",
           "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS", "UTAH",
           "VERMONT", "VIRGINIA", "WASHINGTON", "WEST VIRGINIA", "WISCONSIN", "WYOMING",
           "INTERMOUNTAIN"}


def norm_area(raw: str) -> str:
    """'UTAH - UT-SALT LAKE CITY' -> 'UTAH'; 'NORTHERN ILLINOIS' stays."""
    return (raw or "").split(" - ")[0].strip().upper()


def champ_area(team_label: str, level: str) -> str:
    """Area of a championship team: the state prefix of labels like
    'Colorado/Huber/18&Over3.0W' (INTERMOUNTAIN = section wildcard team),
    else the district in the level ('USTA/INTERMOUNTAIN - NEVADA - ...')."""
    if "/" in team_label:
        head = team_label.split("/")[0].strip().upper()
        if head in _STATES or head in ("NV", "UT", "ID", "CO", "MT", "WY", "NM"):
            return {"NV": "NEVADA", "UT": "UTAH", "ID": "IDAHO", "CO": "COLORADO",
                    "MT": "MONTANA", "WY": "WYOMING", "NM": "NEW MEXICO"}.get(head, head)
    parts = [x.strip() for x in level.split(" - ")]
    if len(parts) >= 2:
        return parts[1].upper()
    return level.strip().upper()


# Some leagues (e.g. NTA Summer) label lines "1 Female Doubles" plus a
# "Scoring Format: ..." note; rewrite to the "1# Doubles" form the shared
# parser understands and blank the note.
_FIX_LINE_LABELS = r"""() => {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n, skip = false; const orig = [];
  while ((n = w.nextNode())) {
    const t = n.nodeValue.trim();
    if (!t) continue;
    if (skip) { n.nodeValue = ''; skip = false; continue; }
    let m = t.match(/^(\d+)\s+(?:Female|Male|Women'?s|Men'?s|Mixed)?\s*(Singles|Doubles)$/i);
    let lvl = null;
    if (!m) {   // Tri-Level / combo: "4.0 Doubles #1"
      const k = t.match(/^(\d\.\d)\s+(Singles|Doubles)\s*#\s*(\d+)$/i);
      if (k) { m = [t, k[3], k[2]]; lvl = k[1]; }
    }
    if (!m) {   // Tri-Level championship scorecards: "4.0 1 Doubles"
      const c = t.match(/^(\d\.\d)\s+(\d+)\s+(Singles|Doubles)$/i);
      if (c) { orig.push(c[1]); n.nodeValue = '#' + c[2] + ' ' + c[3][0].toUpperCase() + c[3].slice(1).toLowerCase(); continue; }
    }
    if (m) { orig.push(lvl); n.nodeValue = m[1] + '# ' + m[2][0].toUpperCase() + m[2].slice(1).toLowerCase(); }
    else if (/^Scoring Format:/i.test(t)) { skip = !/:\s*\S/.test(t); n.nodeValue = ''; }
  }
  return orig; }"""


_NOT_A_NAME = re.compile(r"tie[- ]?break|scoring format|^\s*(rd|nd|st|th)\b|pro set|match tb"
                         r"|^note\b|^timed match|^tie$|^\(dq\)|^\*\s*-|awarded to|disqualified|^dbl\.? default", re.I)
_DQ_SUFFIX = re.compile(r"\s*-\s*\(DQ\)\*?\s*$", re.I)


def _names(s: str) -> list[str]:
    out = []
    for p in (s or "").split("/"):
        p = _DQ_SUFFIX.sub("", p).strip()
        if p and not _NOT_A_NAME.search(p):
            out.append(p)
    return out


# Last match date that counts toward each year-end NTRP rating (per Shirin:
# 2026 cutoff Nov 8; later matches count toward the next year). Summer
# leagues never count toward year-end ratings.
YEAR_END_CUTOFF = {2025: (11, 9), 2026: (11, 8)}   # 2025: assumed by Shirin


def tag_year_end(m: dict) -> None:
    """Set m['year_end'] = {counts, rating_year, reason}."""
    try:
        mo, dy, yr = map(int, m["date"].split("/"))
    except (ValueError, AttributeError):
        m["year_end"] = {"counts": None, "rating_year": None, "reason": "no date"}
        return
    if "SUMMER" in (m.get("league", "") + " " + m.get("level", "")).upper():
        m["year_end"] = {"counts": False, "rating_year": None, "reason": "summer league"}
        return
    cut = YEAR_END_CUTOFF.get(yr)
    if cut is None:
        m["year_end"] = {"counts": True, "rating_year": None, "reason": f"no {yr} cutoff set"}
    elif (mo, dy) > cut:
        m["year_end"] = {"counts": True, "rating_year": yr + 1, "reason": f"after {cut[0]}/{cut[1]}/{yr}"}
    else:
        m["year_end"] = {"counts": True, "rating_year": yr, "reason": ""}


def clean_store(d: dict) -> None:
    """Re-clean stored line names (footnotes, "(DQ)" suffixes) and rebuild the
    everyone-seen players index from rosters + matches."""
    d["players"] = {}
    for tid, t in d["teams"].items():
        if t.get("kind") == "league":
            for r in t.get("roster", []):
                add_player(d, r["name"], norm_area(t.get("district", "")), tid, r.get("ntrp"))
    for mkey, m in d["matches"].items():
        tag_year_end(m)
        areas = ([norm_area(m.get("district", ""))] * 2 if m["kind"] == "league" else
                 [champ_area(n, m.get("level", "")) for n in (m["home"], m["away"])])
        for ln in m["lines"]:
            dq = []
            for side in ("home", "away"):
                raw = ln[f"players_{side}"]
                dq += [_DQ_SUFFIX.sub("", p).strip() for p in raw if _DQ_SUFFIX.search(p)]
                ln[f"players_{side}"] = [n for p in raw for n in _names(p)]
            if dq:
                ln["dq"] = dq
            for side, idx in (("home", 0), ("away", 1)):
                for p in ln[f"players_{side}"]:
                    add_player(d, p, areas[idx], m["teams"][idx], ref=[mkey, ln["line"], side])


def fetch_match(page, d, mkey, q) -> None:
    t0 = time.time()
    goto(page, f"{SS_URL}?t={q['t']}&par1={q['tl_id']}&par2={YEAR}&par3=0")
    _check_blocked(page)
    kind = mkey[0]
    txt = page.inner_text("body")
    h = parse_match_header(txt, kind)
    raw = _parse_match_detail_page(page)
    levels = []
    if not raw:
        levels = page.evaluate(_FIX_LINE_LABELS)
        if levels:
            raw = _parse_match_detail_page(page)
    if len(levels) != len(raw):
        levels = [None] * len(raw)
    lines = []
    for ln, lvl in zip(raw, levels):
        lines.append({"line": re.sub(r"(\d)#\s*", r"#\1 ", ln["line"]).strip(),
                      "players_home": _names(ln["players_home"]),
                      "players_away": _names(ln["players_away"]),
                      "score": ln["score"], "winner": ln["result"]})
        if lvl:
            lines[-1]["level"] = lvl
    home, away = h.get("home", ""), h.get("away", "")
    if kind == "L":
        tids = [team_id(h.get("district", ""), h.get("league", ""), h.get("flight", ""), n)
                for n in (home, away)]
        areas = [h.get("district", "")] * 2
    else:
        ck = q.get("champ", "")
        tids = [f"CHAMP|{ck}|{n.upper()}" for n in (home, away)]
        areas = [champ_area(n, h.get("level", "")) for n in (home, away)]
        for tid, n in zip(tids, (home, away)):
            d["teams"].setdefault(tid, {"name": n, "kind": "champ", "champ": ck,
                                        "league": h.get("league", ""), "roster": []})
    m = {"tl_id": q["tl_id"], "kind": "league" if kind == "L" else "champ",
         "date": h.get("date", ""), "league": h.get("league", ""),
         "flight": h.get("flight", ""), "district": h.get("district", ""),
         "section": h.get("section", ""), "level": h.get("level", ""),
         "status": h.get("status", ""), "home": home, "away": away,
         "teams": tids, "lines": lines}
    d["matches"][mkey] = m
    for ln in lines:
        for side, idx in (("home", 0), ("away", 1)):
            for p in ln[f"players_{side}"]:
                add_player(d, p, areas[idx], tids[idx], ref=[mkey, ln["line"], side])
    d["_timing"]["match"].append(round(time.time() - t0, 2))


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def our_players(names: list[str] | None) -> list[dict]:
    hist = {r["name"]: r.get("history_url") for r in json.loads(NTRP_LIST.read_text())}
    by_id = {p["id"]: p["name"] for p in json.loads(PLAYERS_JSON.read_text()) if p.get("id")}
    slugs = sorted(p.stem for p in PLAYER_DIR.glob("*.html"))
    rows = [{"id": s, "name": by_id.get(s, s.replace("-", " ").title())} for s in slugs]
    if names:
        want = {norm(n) for n in names}
        rows = [r for r in rows if norm(r["name"]) in want]
        found = {norm(r["name"]) for r in rows}
        rows += [{"id": re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-"), "name": n}
                 for n in names if norm(n) not in found]
    for r in rows:
        r["history_url"] = hist.get(r["name"])
    return rows


def ntrp_players(gender: str | None = None) -> list[dict]:
    """Everyone on the USTA NV NTRP list (data/ntrp_nv_2026.json), women first.
    id = name slug (matches players/nv slugs, so the core players already done
    are skipped); names on the list more than once get the history_url's
    par1 appended so each person is opened."""
    rows = json.loads(NTRP_LIST.read_text())
    if gender:
        rows = [r for r in rows if r.get("gender") == gender]
    rows = sorted(rows, key=lambda r: (r.get("gender") != "F", r["name"]))
    slug = lambda n: re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-")
    count = {}
    for r in rows:
        count[slug(r["name"])] = count.get(slug(r["name"]), 0) + 1
    out = []
    for r in rows:
        s = slug(r["name"])
        if count[s] > 1:
            par1 = re.search(r"par1=([^&]+)", r.get("history_url") or "")
            s += "--" + re.sub(r"[^A-Za-z0-9]", "", par1.group(1) if par1 else r.get("city", ""))[:12]
        out.append({"id": s, "name": r["name"], "history_url": r.get("history_url"),
                    "gender": r.get("gender")})
    return out


def run(page, players: list[dict], fetch_matches: bool = True) -> dict:
    d = load_store()
    prog = d["_progress"]
    n_new_teams = 0
    for i, pl in enumerate(players, 1):
        if pl["id"] in prog["players_done"]:
            continue
        t0 = time.time()
        try:
            ok = open_player_page(page, pl["name"], pl.get("history_url"))
        except RuntimeError:
            save_store(d)
            raise
        except Exception as e:
            print(f"[{i}/{len(players)}] {pl['name']}: [warn] player page failed: {e}")
            continue
        d["_timing"]["player_page"].append(round(time.time() - t0, 2))
        if not ok:
            print(f"[{i}/{len(players)}] {pl['name']}: no player page / no teams")
            prog["players_done"].append(pl["id"])
            continue
        rows = [r for r in player_team_rows(page)
                if r["year"] == YEAR and not is_mixed(r["league"], r["flight"])]
        tids, on_pp, failed = [], True, False
        for r in rows:
            tid = team_id(r["district"], r["league"], r["flight"], r["team"])
            tids.append(tid)
            if tid in prog["teams_done"]:
                continue
            try:
                if not on_pp:
                    open_player_page(page, pl["name"], pl.get("history_url"))
                on_pp = False
                crawl_team(page, d, tid, r)
            except RuntimeError:
                save_store(d)
                raise
            except Exception as e:
                print(f"[{i}/{len(players)}] {pl['name']}: [warn] team {tid} failed: {e}")
                failed = True
                continue
            prog["teams_done"].append(tid)
            n_new_teams += 1
            print(f"[{i}/{len(players)}] {pl['name']}: team {tid}  "
                  f"({len(d['teams'][tid]['match_ids'])} matches)")
            if n_new_teams % SAVE_EVERY == 0:
                save_store(d)
        d["player_teams"][pl["id"]] = tids
        if pl.get("history_url"):
            d.setdefault("player_teams_by_url", {})[pl["history_url"]] = tids
        if not failed:  # leave the player to be retried on resume
            prog["players_done"].append(pl["id"])
        save_store(d)

    if fetch_matches:
        # matches with no lines yet (unplayed, or not parsed) are retried on resume
        queue = [(k, q) for k, q in prog["match_queue"].items()
                 if not d["matches"].get(k, {}).get("lines")]
        print(f"fetching {len(queue)} matches")
        for j, (k, q) in enumerate(queue, 1):
            try:
                fetch_match(page, d, k, q)
            except RuntimeError:
                save_store(d)
                raise
            except Exception as e:  # keep going; leave it queued
                print(f"  [warn] {k}: {e}")
            if j % 10 == 0:
                print(f"  {j}/{len(queue)} matches")
                save_store(d)
    clean_store(d)
    save_store(d)
    return d


def set_year(year: int) -> None:
    """Switch the season: which team table is read, match URLs, output file."""
    global YEAR, OUT
    YEAR = year
    OUT = ROOT / "data" / f"tl_team_matches_{year}.json"


def main():
    from dotenv import load_dotenv
    from playwright.sync_api import sync_playwright

    ap = argparse.ArgumentParser()
    ap.add_argument("--names", help="comma-separated player names (test run)")
    ap.add_argument("--no-matches", action="store_true", help="steps A+B only")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--year", type=int, default=YEAR, help="season (default %(default)s)")
    ap.add_argument("--ntrp-list", action="store_true",
                    help="everyone on the NV NTRP list (women first) instead of players/nv")
    ap.add_argument("--gender", choices=["F", "M"], help="with --ntrp-list: one gender only")
    a = ap.parse_args()
    set_year(a.year)
    load_dotenv(ROOT / ".env")
    if a.ntrp_list:
        players = ntrp_players(a.gender)
    else:
        players = our_players([n.strip() for n in a.names.split(",")] if a.names else None)
    print(f"{len(players)} players")
    with sync_playwright() as p:
        b = p.chromium.launch(headless=a.headless)
        page = b.new_context(viewport={"width": 1300, "height": 900}).new_page()
        login(page, os.environ["TENNISLINK_USER"], os.environ["TENNISLINK_PASS"])
        run(page, players, fetch_matches=not a.no_matches)
        b.close()


if __name__ == "__main__":
    main()
