#!/usr/bin/env python3
"""
scrapers/scrape_rating_history.py
Save each player's TennisLink pages that may hold their NTRP rating history.

One login, in a visible (headed) browser window. For each player, opens the
player's page (history_url from data/ntrp_nv_2026.json), saves the HTML and a
screenshot, then follows any tab/link whose text mentions "rating" or "NTRP"
and saves those pages too. Files go to data/rating_history_raw/<slug>/.
Parse them afterwards; nothing here interprets the pages.

Usage:
    python3 scrapers/scrape_rating_history.py --names "Shi L Oskooi"     # probe one player
    python3 scrapers/scrape_rating_history.py --linked                   # our linked players
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from scrapers.scrape_tennislink import login  # noqa: E402

LIST_JSON = ROOT / "data" / "ntrp_nv_2026.json"
OUT = ROOT / "data" / "rating_history_raw"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _linked_names() -> list[str]:
    """Players who have a page in players/nv (our match DB, linked to the USTA list)."""
    from engine.build_directory import PLAYER_DIR  # noqa: WPS433
    slugs = {p.stem for p in (ROOT / PLAYER_DIR).glob("*.html")}
    rows = json.loads(LIST_JSON.read_text())
    return [r["name"] for r in rows if _slug(r["name"]) in slugs]


def _save(page, folder: Path, tag: str, shot: bool) -> None:
    (folder / f"{tag}.html").write_text(page.content(), encoding="utf-8")
    if shot:
        page.screenshot(path=str(folder / f"{tag}.png"), full_page=True)


def _probe_player(page, row: dict, shot: bool, follow: bool) -> dict:
    folder = OUT / _slug(row["name"])
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    page.goto(row["history_url"], wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        pass
    _save(page, folder, "00_player", shot)
    links = page.evaluate("""() => Array.from(document.querySelectorAll('a, input[type=submit], button'))
        .filter(e => e.offsetParent !== null)
        .map(e => ({text: (e.innerText || e.value || '').trim(), href: e.getAttribute('href') || '', id: e.id || ''}))
        .filter(x => x.text)""")
    (folder / "links.json").write_text(json.dumps(links, indent=1))
    saved = ["00_player"]
    if follow:
        targets = [x for x in links if re.search(r"rating|ntrp", x["text"], re.I)]
        for i, x in enumerate(targets[:6], 1):
            try:
                loc = page.locator(f"#{x['id']}") if x["id"] else page.get_by_text(x["text"], exact=True).first
                loc.click(timeout=10_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20_000)
                except Exception:
                    pass
                tag = f"{i:02d}_{_slug(x['text'])[:30]}"
                _save(page, folder, tag, shot)
                saved.append(tag)
                page.goto(row["history_url"], wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:  # keep going; this is a probe
                print(f"    could not follow {x['text']!r}: {e}")
    return {"name": row["name"], "seconds": round(time.time() - t0, 1), "saved": saved}


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--names", help="Comma-separated player names (as in the directory)")
    g.add_argument("--linked", action="store_true", help="All players with a page in players/nv")
    ap.add_argument("--no-follow", action="store_true", help="Only save the player page")
    ap.add_argument("--no-shots", action="store_true", help="Skip screenshots")
    ap.add_argument("--pause", type=float, default=1.5, help="Seconds between players")
    args = ap.parse_args(argv)

    load_dotenv(ROOT / ".env")
    user, pw_ = os.getenv("TENNISLINK_USER"), os.getenv("TENNISLINK_PASS")
    if not user or not pw_:
        print("TENNISLINK_USER / TENNISLINK_PASS not set")
        return 1
    by_name = {r["name"]: r for r in json.loads(LIST_JSON.read_text())}
    names = _linked_names() if args.linked else [n.strip() for n in args.names.split(",")]
    rows = [by_name[n] for n in names if n in by_name and by_name[n].get("history_url")]
    missing = [n for n in names if n not in by_name]
    if missing:
        print(f"  not on the list: {missing}")
    OUT.mkdir(parents=True, exist_ok=True)

    results = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        try:
            login(page, user, pw_)
            for i, row in enumerate(rows, 1):
                r = _probe_player(page, row, shot=not args.no_shots, follow=not args.no_follow)
                results.append(r)
                print(f"  [{i}/{len(rows)}] {r['name']}: {r['seconds']} s, saved {r['saved']}")
                time.sleep(args.pause)
        except Exception as e:
            print(f"  [error] {e}")
            try:
                page.screenshot(path=str(OUT / "error.png"), full_page=True)
            except Exception:
                pass
            return 1
        finally:
            (OUT / "run.json").write_text(json.dumps(results, indent=1))
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
