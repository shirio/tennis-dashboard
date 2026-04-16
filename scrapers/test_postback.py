"""Test: check URL and structure after form postback."""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
PLAYER_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3"
STANDINGS_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=2"

def main():
    username = os.getenv("TENNISLINK_USER","")
    password = os.getenv("TENNISLINK_PASS","")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width":1280,"height":900})
        page = ctx.new_page()
        try:
            page.goto(f"{BASE_URL}/Dashboard/Main/Login.aspx", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(1)
            page.fill("input[name='username']", username)
            page.keyboard.press("Enter")
            time.sleep(1)
            page.fill("input[type='password']", password)
            page.keyboard.press("Enter")
            page.wait_for_url("**/tennislink.usta.com/**", timeout=20_000)
            time.sleep(1)

            # Player search
            page.goto(PLAYER_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            page.fill("#ctl00_mainContent_txtFirstName", "Anna", timeout=3_000)
            page.fill("#ctl00_mainContent_txtLastName", "Clark", timeout=3_000)
            page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            time.sleep(2)

            print(f"URL after player search: {page.url}")
            print(f"Page title: {page.title()}")

            # Try to find the results div
            for sel in ["#ctl00_mainContent", "#mainContent", "form", ".mainContent",
                        "[id*='mainContent']", "[id*='SearchResult']", "[class*='result']",
                        "table.League", "table"]:
                el = page.query_selector(sel)
                if el:
                    txt = (el.inner_text() or "")[:300]
                    print(f"\nFound {sel!r}: {txt[:200]!r}")
                    break

            # Get all top-level divs with id
            print("\n--- Top divs with IDs ---")
            for div in page.query_selector_all("div[id]"):
                did = div.get_attribute("id") or ""
                if did and not did.startswith("ot-") and "google" not in did:
                    txt = (div.inner_text() or "").strip()[:100]
                    print(f"  #{did}: {txt!r}")

            # Look for tables
            tables = page.query_selector_all("table")
            print(f"\n--- Tables ({len(tables)}) ---")
            for i, tbl in enumerate(tables[:5]):
                tid = tbl.get_attribute("id") or ""
                tcls = tbl.get_attribute("class") or ""
                txt = (tbl.inner_text() or "").strip()[:200]
                print(f"  table[{i}] id={tid!r} class={tcls!r}")
                print(f"    {txt!r}")

            # Get page source around "Anna Clark"
            content = page.content()
            idx = content.find("Anna Clark")
            if idx >= 0:
                print(f"\n--- HTML around 'Anna Clark' (pos {idx}) ---")
                print(content[max(0,idx-200):idx+500])
            else:
                print("\n'Anna Clark' not found in page source")

            # Team search
            print("\n\n=== TEAM SEARCH (empty) ===")
            page.goto(STANDINGS_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            page.fill("#ctl00_mainContent_txtTeamName", "", timeout=3_000)
            page.click("#ctl00_mainContent_btnSearchTeamByName", timeout=3_000)
            try:
                page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception:
                pass
            time.sleep(2)
            print(f"URL after team search: {page.url}")
            content = page.content()
            # look for any team names
            for term in ["SPANISH", "ANTHEM", "TPC", "Error", "No results", "found"]:
                idx = content.find(term)
                if idx >= 0:
                    print(f"Found {term!r} at {idx}: {content[max(0,idx-50):idx+200]!r}")

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
