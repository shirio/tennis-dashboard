"""Test scorecard URL structure."""
import os, sys, time, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"
SCORECARD_URL = f"{BASE_URL}/Leagues/Scorecard/printscorecard.aspx?matchnum=1011779569"

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

            print(f"Navigating to: {SCORECARD_URL}")
            page.goto(SCORECARD_URL, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(2)
            print(f"URL: {page.url}")
            print(f"Title: {page.title()}")
            page.screenshot(path="data/test_scorecard.png", full_page=False)

            # Get full page content
            content = page.content()
            # Find scorecard data
            for kw in ["Line 1", "Line 2", "Line 3", "Singles", "Doubles", "score", "Score"]:
                idx = content.find(kw)
                if idx >= 0:
                    print(f"\n{kw} at {idx}: {content[max(0,idx-50):idx+300]!r}")
                    break

            # Get text content
            body = page.query_selector("body")
            if body:
                print("\n--- PAGE TEXT ---")
                print(body.inner_text()[:3000])

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
