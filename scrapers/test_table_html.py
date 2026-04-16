"""Dump actual HTML of player search result table to find link formats."""
import os, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()
from playwright.sync_api import sync_playwright

BASE_URL = "https://tennislink.usta.com"

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

            # Player search - look for an NV player
            for first, last in [("Shirin", "Oskooi"), ("Anna", "White"), ("Sarah", "Jones")]:
                page.goto(f"{BASE_URL}/Leagues/Main/StatsAndStandings.aspx?SearchType=3",
                          wait_until="domcontentloaded", timeout=30_000)
                time.sleep(2)
                page.fill("#ctl00_mainContent_txtFirstName", first, timeout=3_000)
                page.fill("#ctl00_mainContent_txtLastName", last, timeout=3_000)
                page.click("#ctl00_mainContent_btnSearchStatsAndStandings", timeout=3_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12_000)
                except Exception:
                    pass
                time.sleep(1)

                result_div = page.query_selector("#ctl00_mainContent_divSearchResulstsByNameForPlayers")
                if result_div:
                    html = result_div.inner_html()
                    print(f"\n=== RESULT DIV for '{first} {last}' ({len(html)} chars) ===")
                    print(html[:3000])
                    print("---")
                    # Extract all links from the div
                    for a in result_div.query_selector_all("a[href]"):
                        href = a.get_attribute("href") or ""
                        txt = (a.inner_text() or "").strip()
                        print(f"  LINK [{txt[:60]!r}] -> {href[:120]}")
                    break  # just need one example

        finally:
            ctx.close()
            browser.close()

if __name__ == "__main__":
    main()
