#!/usr/bin/env python3
"""
Re-scrape NV Championship match scorecards to populate per-court winners.
Uses direct URL access to each match's scorecard page.
Merges results into existing line data without replacing player/score info.
"""
import json
import os
import re
import sys
from pathlib import Path
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

from playwright.sync_api import sync_playwright
from scrapers.scrape_tennislink import login, _wait_for_network

DATA_DIR = Path("data")
STANDINGS_PATH = DATA_DIR / "standings_nv_30.json"
BASE_URL = "https://tennislink.usta.com/Leagues/Main/StatsAndStandings.aspx"


def _parse_champ_scorecard(page) -> list[dict]:
    """Parse championship scorecard for per-court winners using mark.gif column position."""
    return page.evaluate("""() => {
        const panel = document.getElementById('ctl00_mainContent_Panel1')
            || document.getElementById('ctl00_mainContent_pnlCPScorecard');
        if (!panel) return [];
        const courts = [];
        for (const tr of panel.querySelectorAll('tr')) {
            const tds = Array.from(tr.querySelectorAll('td'));
            if (tds.length < 7) continue;
            const label = tds[0].innerText.trim();
            const m = label.match(/#(\\d+) (Singles|Doubles)/);
            if (!m) continue;
            let winner = '';
            tds.forEach((td, i) => {
                const imgs = td.querySelectorAll('img');
                for (const img of imgs) {
                    if (img.src && img.src.includes('mark.gif')) {
                        if (i <= 2) winner = 'home';
                        else if (i >= 4) winner = 'away';
                    }
                }
            });
            const lineNum = parseInt(m[1]);
            const lineType = m[2];
            const lineLabel = `${lineNum}# ${lineType}`;
            courts.push({line: lineLabel, result: winner});
        }
        return courts;
    }""")


def main():
    data = json.loads(STANDINGS_PATH.read_text())

    champ_matches = []
    for sf in data.get("subflights", []):
        if "champion" not in sf.get("flight_label", "").lower():
            continue
        for m in sf.get("matches", []):
            tl_id = m.get("tl_match_id")
            if tl_id and m.get("lines"):
                champ_matches.append(m)

    if not champ_matches:
        print("No championship matches with lines found")
        return

    print(f"Found {len(champ_matches)} championship matches to re-scrape")

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            login(page, username, password)
            total_applied = 0

            for i, m in enumerate(champ_matches):
                tl_id = m["tl_match_id"]
                url = f"{BASE_URL}?t=12&par1={tl_id}&par2=0&par3=0"
                print(f"\n[{i+1}/{len(champ_matches)}] Match {tl_id}: "
                      f"{m.get('date')} {m.get('home_team')} vs {m.get('away_team')}")

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    _wait_for_network(page, 15_000)
                    sleep(2)

                    scraped = _parse_champ_scorecard(page)

                    if scraped:
                        applied = 0
                        for sl in scraped:
                            for el in m.get("lines", []):
                                if el.get("line") == sl["line"] and sl["result"]:
                                    if not el.get("result"):
                                        el["result"] = sl["result"]
                                        applied += 1
                        total_applied += applied
                        print(f"  Applied {applied}/{len(scraped)} court winners")
                        for sl in scraped:
                            print(f"    {sl['line']}: {sl['result']}")
                    else:
                        print(f"  No court data parsed from scorecard")

                except Exception as e:
                    print(f"  Error: {e}")

                sleep(1)

            STANDINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            print(f"\nSaved {STANDINGS_PATH} — {total_applied} total court winners applied")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
