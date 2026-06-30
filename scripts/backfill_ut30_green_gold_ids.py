#!/usr/bin/env python3
"""
Backfill tl_match_id for UT-AM 3.0W AM Green and AM Gold subflights via the
dedicated Match Summary lookup (_fetch_tl_match_ids_for_subflight), which
fill_ut_match_ids.py's broken per-team navigation couldn't reach.

Targeted at the 5 DQ-affected matches in fix_ut_dq_matches.py that lack IDs.

Usage:
    python3 scripts/backfill_ut30_green_gold_ids.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from scrapers.scrape_tennislink import (
    login, _navigate_to_subflight, _fetch_tl_match_ids_for_subflight,
    _get_state_config, _match_key,
)

DATA = Path("data")
UT30_PATH = DATA / "standings_ut_30.json"
NTRP = "3.0"
YEAR = 2026


def main():
    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("[error] TENNISLINK_USER / TENNISLINK_PASS not set in .env")
        sys.exit(1)

    state_cfg = _get_state_config("UT")
    am_cfg = {**state_cfg, "areas": [{"area": "UT-AM", "flight_suffix": "AM"}]}

    data = json.loads(UT30_PATH.read_text())
    all_ids: dict[str, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        try:
            login(page, username, password)
            for label in ["3.0W AM Green", "3.0W AM Gold"]:
                print(f"\nNavigating to UT-AM {label}...")
                ok = _navigate_to_subflight(page, NTRP, YEAR, label, am_cfg)
                if not ok:
                    print(f"  [warn] failed to navigate to {label}")
                    continue
                ids = _fetch_tl_match_ids_for_subflight(page, NTRP, YEAR, label)
                print(f"  {label}: {len(ids)} match IDs fetched")
                all_ids.update(ids)
        finally:
            browser.close()

    # Merge into standings_ut_30.json by match_key
    applied = 0
    for sf in data.get("subflights", []):
        for m in sf.get("matches", []):
            if m.get("tl_match_id"):
                continue
            key = _match_key(m["date"], m["home_team"], m["away_team"])
            if key in all_ids:
                m["tl_match_id"] = all_ids[key]
                applied += 1
                print(f"  Applied ID {all_ids[key]} to {m['date']} {m['home_team']} vs {m['away_team']}")

    print(f"\nApplied {applied} match IDs")
    if applied:
        UT30_PATH.write_text(json.dumps(data, indent=2))
        print(f"Saved {UT30_PATH}")


if __name__ == "__main__":
    main()
