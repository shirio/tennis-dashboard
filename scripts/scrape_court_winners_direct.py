#!/usr/bin/env python3
"""
Re-scrape per-court winners via direct TennisLink scorecard URLs.

For each match with unknown court_winner values and a tl_match_id,
navigates directly to the scorecard page and extracts mark.gif winners.
Much faster than navigating through team pages.

Usage:
    python3 scripts/scrape_court_winners_direct.py              # all states
    python3 scripts/scrape_court_winners_direct.py --state CO   # just CO
    python3 scripts/scrape_court_winners_direct.py --dry-run    # count only
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
from time import sleep

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv()

DATA_DIR = Path("data")
BASE_URL = "https://tennislink.usta.com/Leagues/Main/StatsAndStandings.aspx"


def _collect_work(states: list[str]) -> list[tuple[Path, dict, list[dict]]]:
    """Find all matches needing court winners across specified states."""
    work = []
    for state in states:
        for div in ["30", "35"]:
            path = DATA_DIR / f"standings_{state.lower()}_{div}.json"
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            matches = []
            for sf in data.get("subflights", []):
                for m in sf.get("matches", []):
                    if not m.get("tl_match_id"):
                        continue
                    if not m.get("lines"):
                        continue
                    if any(ln.get("court_winner") is None for ln in m["lines"]):
                        matches.append(m)
            if matches:
                work.append((path, data, matches))
                print(f"  {path.name}: {len(matches)} matches need court winners")
    return work


def _merge_winners(existing_lines: list[dict], scraped_lines: list[dict]) -> int:
    """Merge scraped court winners into existing lines. Returns count applied."""
    applied = 0
    for el in existing_lines:
        if el.get("court_winner") is not None:
            continue
        line_label = el.get("line", "")
        for sl in scraped_lines:
            if sl.get("line") == line_label and sl.get("result"):
                el["court_winner"] = sl["result"]
                applied += 1
                break
    return applied


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save-every", type=int, default=50,
                        help="Save progress every N matches")
    args = parser.parse_args()

    states = [args.state.upper()] if args.state else ["CO", "UT", "ID"]

    print("Scanning for matches needing court winners...")
    work = _collect_work(states)
    total_matches = sum(len(matches) for _, _, matches in work)

    if not total_matches:
        print("All courts already have winners!")
        return

    print(f"\nTotal: {total_matches} matches to scrape")

    if args.dry_run:
        return

    username = os.getenv("TENNISLINK_USER", "")
    password = os.getenv("TENNISLINK_PASS", "")
    if not username or not password:
        print("ERROR: Set TENNISLINK_USER and TENNISLINK_PASS in .env")
        sys.exit(1)

    from playwright.sync_api import sync_playwright
    from scrapers.scrape_tennislink import (
        login, _wait_for_network, _parse_match_detail_page,
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        try:
            login(page, username, password)

            grand_total = 0
            match_idx = 0

            for path, data, matches in work:
                print(f"\n{'='*60}")
                print(f"Processing {path.name} ({len(matches)} matches)")
                print(f"{'='*60}")

                file_applied = 0

                for i, m in enumerate(matches):
                    match_idx += 1
                    tl_id = m["tl_match_id"]
                    url = f"{BASE_URL}?t=12&par1={tl_id}&par2=0&par3=0"

                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        _wait_for_network(page, 15_000)
                        sleep(1.5)

                        lines = _parse_match_detail_page(page)
                        if lines:
                            applied = _merge_winners(m.get("lines", []), lines)
                            file_applied += applied
                            grand_total += applied
                            if applied:
                                status = f"+{applied}"
                            else:
                                status = "no new"
                        else:
                            status = "no data"

                        if match_idx % 20 == 0 or i == len(matches) - 1:
                            print(f"  [{match_idx}/{total_matches}] {path.name} "
                                  f"match {tl_id}: {status} "
                                  f"(total applied: {grand_total})")

                    except Exception as e:
                        print(f"  [{match_idx}/{total_matches}] match {tl_id}: ERROR {e}")

                    # Save periodically
                    if match_idx % args.save_every == 0:
                        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                        remaining = sum(
                            1 for sf in data.get("subflights", [])
                            for m2 in sf.get("matches", [])
                            for ln in m2.get("lines", [])
                            if ln.get("court_winner") is None
                        )
                        print(f"  [checkpoint] Saved {path.name} ({remaining} still unknown)")

                    sleep(0.5)

                # Final save for this file
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
                remaining = sum(
                    1 for sf in data.get("subflights", [])
                    for m2 in sf.get("matches", [])
                    for ln in m2.get("lines", [])
                    if ln.get("court_winner") is None
                )
                print(f"\n  {path.name}: applied {file_applied} court winners, {remaining} still unknown")

            print(f"\n{'='*60}")
            print(f"DONE: {grand_total} court winners applied across {total_matches} matches")

        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    main()
