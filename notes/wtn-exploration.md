# WTN exploration (2026-11-24)

Read-only probe of how to get World Tennis Number (WTN) singles/doubles for a player by name, ahead of a snapshot of NV players before the Dec 2 year-end ratings. Looked up 3 players. No bulk scraping, nothing committed, no tokens saved.

## Flow that works (UI-driven, headed Playwright)

1. Log in once via the existing `scrapers/scrape_tennislink.login()` (account.usta.com Auth0).
2. Open `https://www.usta.com/en/home/play/player-search.html`. It shows "SIGN IN" at first. Clicking it signs in through SSO with no second password prompt. (Reject the cookie banner first.)
3. Type the full name in the box (`input[placeholder^='Search by full name']`) and press Enter.
   - One exact match: goes straight to `.../player-search/profile.html#uaid=<UAID>&tab=tournaments`.
   - Several matches: a results list (URL `#searchText=...&page=1`). Click the row with the NV city.
4. The profile's "Player Overview" panel shows NTRP plus the WTN block. Wait for `text=ITF Tennis ID`.
5. Read the values from the page text, or (cleaner) from the `playerInfo` JSON the page loads itself, captured with `page.on("response")`.

Time: about 3.5–4.5 s per player end to end (search page load + search + profile load). About 6 s with the cookie banner on first load.

## APIs seen (the page's own calls)

| Call | Auth | What it gives |
|---|---|---|
| `POST https://services.usta.com/v1/dataexchange/profile/search/public` body `{"pagination":{"pageSize":"51","currentPage":"1"},"selection":{"name":{"fname":"Brad","lname":"Evans"}}}` | None. Worked from a logged-out context too, under 1 s | `name, uaid, city, state, section, district` per match. Filter on `state=="NV"` (and city) to pick the UAID. "Brad Evans" returned 8 people nationally, 1 in NV. |
| `POST https://www.usta.com/usta/api?type=playerInfo` body `{"selection":{"uaid":"<UAID>"},"output":{"ratings":"true","extendedProfile":"true","wtn":"true","genderIdentity":"true"}}` | Needs the AEM CSRF token (`/libs/granite/csrf/token.json`) and the page's session. A bare request outside the page gets 403. | `ratings.ntrp` (rating, type, date) and `ratings.wtn[]`, one entry per type |
| `GET https://www.usta.com/usta/api?type=playerRankings / playerBiography / playerRanklists&uaid=...`, `services.usta.com/v1/dataexchange/playhistory*` | session | rankings, bio, match history (not WTN) |

Keyed on **UAID** (USTA number). The name search returns the UAID.

Note: calling `/usta/api` directly from a script (outside the page's normal flow) was blocked by my tool-permission classifier, so I didn't confirm a direct-call recipe. The page-driven flow + passive response capture works and is what I'd build on. Making direct calls would need the owner's explicit OK.

### `ratings.wtn[]` entry

```json
{"confidence": 50, "ratingDate": "2026-11-18", "tennisNumber": 26.48, "type": "SINGLE",
 "gameZoneUpper": 23.67, "gameZoneLower": 29.39, "source": "MATCHUPS",
 "lastPlayed": "2026-06-06", "uaid": "2017239920", "tennisId": "EVA6337571",
 "updatedAt": "2026-11-18T06:37:17", "proZone": false}
```

- `type`: `SINGLE` / `DOUBLE`. Missing when the player has no rating of that type (Abatangelo had doubles only).
- `confidence` is 0–100. Seen: 40/50/60 shown as "Medium Confidence", 70/80 as "High Confidence".
- `gameZoneUpper` is the *better* (lower) number. The UI shows "GAME ZONE lower TO upper", e.g. 29.39 TO 23.67.
- `tennisId` is the ITF id, used by worldtennisnumber.com.
- All 3 players had `ratingDate`/updated 11/18/2026, so it looks like a weekly batch update.

## The 3 players

| Player (UAID) | NTRP | WTN singles | WTN doubles |
|---|---|---|---|
| Elizabeth Abatangelo, Las Vegas (11229866) | 3.0 C (12/31/25) | none | 30.43, High (70), zone 33.74–27.18, last played 06/06/2026 |
| Brad Evans, Henderson (2017239920) | — | 26.48, Medium (50), zone 29.39–23.67, last played 06/06/2026 | 22.97, High (80), zone 26.02–20.10, last played 10/10/2026 |
| Kant Kodali, Henderson (2020513878) | 3.5 S (07/23/26) | 27.67, Medium (40), zone 30.67–24.78, last played 10/31/2026 | 27.07, Medium (60), zone 30.36–23.90, last played 10/24/2026 |

All updated 11/18/2026.

## History

- **usta.com:** current values only. No chart or past values on the profile or in `playerInfo`.
- **worldtennisnumber.com** (`/eng/player-profile?tennis-id=<ITF id>`, linked as "SEE WTN PROFILE"): logged out, it shows the current numbers, a "Rating range, past year" min–max per type (e.g. singles 27.69–27.67), and match activity. "World Tennis Number history" says you have to log in. That's a separate ITF/ClubSpark account (its `/Account/Tokens` returned 401), not the USTA login, so I didn't sign in. Its data comes from `POST https://prd-itf-kube.clubspark.pro/graphql`.
- So for history going forward, **our own snapshots are the history**. Snapshot now, then weekly (after each Wednesday-ish update).

## Rate limit / bot check

- No CAPTCHA, challenge page, or 429 in about 8 profile loads and 5 searches.
- The pages carry Feroot PageGuard (`pageguard.feroot.com/.../collect`), OneTrust, and Zendesk. PageGuard is client-side script monitoring, not a bot wall, but go gently anyway.
- At about 4 s per player, roughly 2,300 NV players (`data/ntrp_nv_2026.json`) is about 2.5 hours sequentially. Better with a polite pause, in batches, resumable.

## Matching caveats for the bulk run

- Name search needs `fname` + `lname`. Hyphenated or compound surnames and nicknames will miss. Fall back to the TennisLink names or a manual list.
- Several NV people with the same name: disambiguate by city. If it's still ambiguous, flag it rather than guess.
- Save the UAID per player on the first pass. Later snapshots can go straight to `profile.html#uaid=<UAID>` and skip the search.

## Snapshot run, 2026-11-24 (scrapers/scrape_wtn.py)

- Output: `data/wtn_snapshot_2026-11-24.json` (not committed), with 429 NV player-page ids.
- How it works: the name search uses the public JSON feed. The direct `playerInfo` call returned 400 with an empty body, so WTN comes from each profile page's own `playerInfo` response (about 2 s per player, 1.5 s pause between players). It's a combination of the feed and the page.
- Main pass took 27 min. Three small re-runs cleaned up names with two-word first names and a first-lookup-after-sign-in fetch error.
- Results: 417 matched (348 with singles, 416 with doubles, all dated 11/18/2026). 2 ambiguous, 10 with no NV match.
- Tie-breaks: same name in NV is settled by city, then gender, then our NTRP rating.
- No CAPTCHA, 403, or 429 at any point.
