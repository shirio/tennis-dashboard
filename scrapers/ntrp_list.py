#!/usr/bin/env python3
"""
scrapers/ntrp_list.py
Parse TennisLink "Find NTRP Rating Info" results pages into a player list.

The results come from
  https://tennislink.usta.com/leagues/reports/NTRP/AdvancedSearch.aspx
filled with: Championship Year, Section USTA/INTERMOUNTAIN, District NEVADA,
League blank, Gender All, Level All. The page is forward-only (you cannot
browser-back to the form after submitting), so always start a fresh search.

Each results row has: Name ("Last, First"), Gender, City, State,
Year End Rating Level, Year End Rating Date, Rating Type (see
engine/ntrp_defs.RATING_TYPES). The name links to the player's match history.

Usage (from saved HTML pages):
    python3 scrapers/ntrp_list.py --year 2026 page1.html [page2.html ...]
writes data/ntrp_nv_2026.json (merged with any existing file).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

BASE_URL = "https://tennislink.usta.com/leagues/reports/NTRP/"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_HEADER_MAP = {
    "name": "name_raw",
    "gender": "gender",
    "city": "city",
    "state": "state",
    "year end rating level": "level",
    "year end rating date": "rating_date",
    "rating type": "rating_type",
}


def display_name(raw: str) -> str:
    """'Allen, Maria' -> 'Maria Allen'; 'AKERMAN, ELAN' -> 'Elan Akerman'."""
    raw = re.sub(r"\s+", " ", (raw or "").strip())
    if "," in raw:
        last, first = raw.split(",", 1)
        name = f"{first.strip()} {last.strip()}".strip()
    else:
        name = raw
    if name.isupper():
        name = name.title()
    return name


def parse_results_html(html: str, base_url: str = BASE_URL) -> list[dict]:
    """Return one dict per player row found in a results page."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Find the header row (it may not be the first <tr>)
        cols = None
        hdr_idx = None
        for i, tr in enumerate(rows[:5]):
            cells = [c.get_text(" ", strip=True).lower() for c in tr.find_all(["th", "td"])]
            if "name" in cells and "gender" in cells and any("rating level" in c for c in cells):
                cols = [_HEADER_MAP.get(c) for c in cells]
                hdr_idx = i
                break
        if cols is None:
            continue
        for tr in rows[hdr_idx + 1:]:
            cells = tr.find_all("td", recursive=False) or tr.find_all("td")
            if len(cells) != len(cols):
                continue
            rec: dict = {}
            for key, cell in zip(cols, cells):
                if key:
                    rec[key] = cell.get_text(" ", strip=True)
            if not rec.get("name_raw"):
                continue
            link = tr.find("a", href=True)
            href = link["href"] if link else ""
            if href and not href.lower().startswith("javascript"):
                rec["history_url"] = urljoin(base_url, href)
            else:
                rec["history_url"] = None
            rec["name"] = display_name(rec["name_raw"])
            rec["gender"] = (rec.get("gender") or "").upper()[:1] or None
            rec["rating_type"] = (rec.get("rating_type") or "").strip().upper() or None
            lvl = (rec.get("level") or "").strip()
            rec["level"] = lvl if re.fullmatch(r"\d\.\d", lvl) else None
            out.append(rec)
    return out


def _key(rec: dict) -> str:
    return rec.get("history_url") or f"{rec['name_raw'].lower()}|{(rec.get('city') or '').lower()}"


def merge_records(existing: list[dict], new: list[dict]) -> list[dict]:
    by_key = {_key(r): r for r in existing}
    for r in new:
        by_key[_key(r)] = r
    return sorted(by_key.values(), key=lambda r: (r["name_raw"].lower(), r.get("city") or ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--district", default="nv")
    ap.add_argument("--replace", action="store_true", help="Overwrite instead of merging")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args(argv)

    new: list[dict] = []
    for f in args.files:
        recs = parse_results_html(Path(f).read_text(encoding="utf-8", errors="replace"))
        print(f"  {f}: {len(recs)} players")
        new.extend(recs)
    out_path = DATA_DIR / f"ntrp_{args.district}_{args.year}.json"
    existing = [] if args.replace or not out_path.exists() else json.loads(out_path.read_text())
    merged = merge_records(existing, new)
    out_path.write_text(json.dumps(merged, indent=1, ensure_ascii=False) + "\n")
    print(f"  wrote {out_path} ({len(merged)} players)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
