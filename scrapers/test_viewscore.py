"""
Test: what does ViewScore(matchID, ...) do? Look at JS and test navigation.
"""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright
from urllib.parse import urljoin

BASE_URL = "https://tennislink.usta.com"

# Direct URL to 3.0 Women A subflight (found in previous test)
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

            # Test: can we navigate directly to the subflight URL?
            print(f"Navigating to subflight URL: {SUBFLIGHT_URL}")
            page.goto(SUBFLIGHT_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            print(f"URL: {page.url}")
            print(f"Title: {page.title()}")
            page.screenshot(path="data/test_subflight_direct.png", full_page=False)
            main_div = page.query_selector("#ctl00_mainContent_UpdatePanel1")
            if main_div:
                print("Content:")
                print(main_div.inner_text()[:2000])

            # Look for ViewScore in page source
            content = page.content()
            idx = content.find("ViewScore")
            if idx >= 0:
                print(f"\nViewScore in source: {content[max(0,idx-100):idx+200]}")
            else:
                print("\nViewScore not found in source")

            # Look for match scorecard link pattern
            for kw in ["Scorecard", "scorecard", "ViewScore", "score_sheet", "GetScore"]:
                idx = content.find(kw)
                if idx >= 0:
                    print(f"\n{kw}: {content[max(0,idx-50):idx+200]}")

            # Click "Match Schedule" tab if present
            print("\n--- Clicking Match Schedule tab ---")
            for lnk in page.query_selector_all("a, button"):
                txt = (lnk.inner_text() or "").strip()
                if "Match Schedule" in txt or "Match Summary" in txt:
                    lnk.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except Exception:
                        pass
                    time.sleep(1.5)
                    print(f"Clicked: {txt}")
                    print(f"URL now: {page.url}")
                    page.screenshot(path="data/test_matchschedule.png", full_page=False)
                    # Look for ViewScore
                    content2 = page.content()
                    for match in re.finditer(r"ViewScore\((\d+),(\d+),", content2):
                        print(f"  ViewScore call: {match.group(0)}")
                    main_div2 = page.query_selector("#ctl00_mainContent_UpdatePanel1")
                    if main_div2:
                        print(main_div2.inner_text()[:2000])
                    break

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
