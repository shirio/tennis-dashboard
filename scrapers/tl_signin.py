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
    # waits as long as it takes. USTA may leave you on any page after signing
    # in, so check in the background (same cookies, no navigation in your tab)
    # whether TennisLink now opens without a redirect to the sign-in page.
    def tl_ok():
        try:
            r = ctx.request.get(LOGIN_URL, timeout=30_000)
            u = r.url.lower()   # signed in: TennisLink sends Login.aspx on to Home.aspx
            return "tennislink.usta.com" in u and "login.aspx" not in u
        except Exception:
            return False
    while not (signed_in(page) or tl_ok()):
        time.sleep(10)
    print("Signed in. You can leave the rest to the crawler.", flush=True)
    time.sleep(3)
    ctx.close()
