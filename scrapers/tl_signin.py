#!/usr/bin/env python3
"""
scrapers/tl_signin.py
Open the crawler's persistent browser profile at the USTA sign-in page and wait
while a person signs in by hand (USTA sometimes asks for a CAPTCHA, which the
crawler never answers). The session is then reused by scrape_tl_teams.py.

    ~/.venvs/tennis-dashboard/bin/python scrapers/tl_signin.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402
from scrapers.scrape_tennislink import LOGIN_URL  # noqa: E402
from scrapers.scrape_tl_teams import open_browser, signed_in  # noqa: E402

with sync_playwright() as p:
    ctx, page = open_browser(p)
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
    print("Sign in to USTA in the browser window (email, password, and the code if asked).", flush=True)
    while not signed_in(page):   # waits as long as it takes
        time.sleep(1)
    print("Signed in. You can leave the rest to the crawler.", flush=True)
    time.sleep(3)
    ctx.close()
