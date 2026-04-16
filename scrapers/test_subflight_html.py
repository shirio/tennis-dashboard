"""Dump Match Summary and Team Standings HTML from subflight page."""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin

BASE_URL = "https://tennislink.usta.com"
SUBFLIGHT_URL = f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?t=6&par1=DB00D7F3EAD0B9C685B45B1A79&par2=2026&par3=0"

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

            page.goto(SUBFLIGHT_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)

            # Click Team Standings tab
            print("=== TEAM STANDINGS ===")
            for a in page.query_selector_all("a"):
                if "Team Standings" in (a.inner_text() or ""):
                    a.click()
                    try: page.wait_for_load_state("networkidle", timeout=10_000)
                    except: pass
                    time.sleep(1.5)
                    break

            main_div = page.query_selector("#ctl00_mainContent_UpdatePanel1")
            if main_div:
                # Find the standings table
                for tbl in main_div.query_selector_all("table"):
                    txt = (tbl.inner_text() or "")
                    if "DTC" in txt and "ANTHEM" in txt:
                        print(tbl.inner_html()[:4000])
                        # Also print text rows
                        print("\n--- TABLE TEXT ROWS ---")
                        for tr in tbl.query_selector_all("tr"):
                            cells = [td.inner_text().strip() for td in tr.query_selector_all("td,th")]
                            if cells:
                                print(f"  {cells}")
                        break

            # Click Match Summary tab
            print("\n\n=== MATCH SUMMARY ===")
            page.goto(SUBFLIGHT_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            for a in page.query_selector_all("a"):
                if "Match Summary" in (a.inner_text() or ""):
                    a.click()
                    try: page.wait_for_load_state("networkidle", timeout=10_000)
                    except: pass
                    time.sleep(1.5)
                    break

            main_div = page.query_selector("#ctl00_mainContent_UpdatePanel1")
            if main_div:
                # Find the match summary table with Match IDs
                for tbl in main_div.query_selector_all("table"):
                    txt = (tbl.inner_text() or "")
                    if "1011779" in txt or "DTC #2" in txt:
                        print("Found match table!")
                        print(tbl.inner_html()[:5000])
                        # Print text rows
                        print("\n--- TABLE ROWS ---")
                        for tr in tbl.query_selector_all("tr"):
                            cells = [td.inner_text().strip().replace('\n',' ').replace('\t',' ') for td in tr.query_selector_all("td,th")]
                            if cells:
                                print(f"  {cells}")
                        break

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
