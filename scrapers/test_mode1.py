"""
Test Mode 1 scraping: find subflight URLs, scrape standings and matches.
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

from scrapers.scrape_tennislink import (
    login, run_mode1, get_subflight_links, scrape_subflight,
    load_json, OUTPUT_STANDINGS_30
)
from playwright.sync_api import sync_playwright

def main():
    username = os.getenv("TENNISLINK_USER","")
    password = os.getenv("TENNISLINK_PASS","")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width":1280,"height":900})
        page = ctx.new_page()
        try:
            login(page, username, password)

            # Test: find 3.0 Women subflight links
            print("\n=== FINDING 3.0 WOMEN SUBFLIGHT URLs ===")
            links_30 = get_subflight_links(page, "3.0", 2026)
            print(f"Found {len(links_30)} subflights:")
            for l in links_30:
                print(f"  {l['flight_label']}: {l['url']}")

            if links_30:
                # Scrape the first one
                print(f"\n=== SCRAPING FIRST SUBFLIGHT: {links_30[0]['flight_label']} ===")
                data = scrape_subflight(page, links_30[0])
                print(f"Teams ({len(data['teams'])}):")
                for t in data['teams']:
                    print(f"  {t}")
                print(f"\nMatches ({len(data['matches'])}):")
                for m in data['matches'][:5]:
                    print(f"  {m['date']} {m['home_team']} vs {m['away_team']} | {m.get('team_wins_home')}-{m.get('team_wins_away')} | pending={m.get('pending')}")
        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
