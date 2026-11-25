# TennisLink team-first crawl (2026)

`scrapers/scrape_tl_teams.py` crawls each team once instead of each player's history,
then `scrapers/resolve_tl_players.py` turns names into people.
Output: `data/tl_team_matches_2026.json` (2025: `--year 2025`).

## TennisLink page patterns (Nov 2026)

| Page | How to reach it | What we read |
|---|---|---|
| Player page | `leagues/Main/StatsAndStandings.aspx?t=T-0&par1=<enc>&e=1` (`history_url` in `data/ntrp_nv_2026.json`) | "YYYY Teams this member is registered on": team, section, district, league, flight; each team is a postback link `…rptPlayerName_ctlNN_LinkButton4` |
| Team page | click that link (postback). "Link to this Page" = `t=3&par1=<enc>&par2=<year>&par3=0`, but `par1` is re-encrypted on every visit | roster (name + NTRP), every match id from `ViewScore(<matchnum>,7,…)`, "Championship Advancements" links |
| League match | `StatsAndStandings.aspx?t=7&par1=<matchnum>&par2=<year>&par3=0` | lines, players, scores, winner (radio / checkmark) |
| Championship flight | click a Championship Advancement on the team page, then "Match Summary" | match ids from `ViewScore(<id>,12,…)` |
| Championship match | `StatsAndStandings.aspx?t=12&par1=<id>&par2=<year>&par3=0` | same as a league match; team labels like `Nevada/Cunningham/18&Over3.0W` |

`printscorecard.aspx?matchnum=` is only a blank form.

## Keys

* Team: `DISTRICT|LEAGUE|FLIGHT (no subflight)|TEAM NAME` (no stable TennisLink team id).
* Match: `L<matchnum>` (league, 10 digits) or `C<id>` (championship, 7 digits).
* Line labels vary: `1# Singles`, `1 Female Doubles` (NTA Summer), `4.0 Doubles #1` (Tri-Level/combo;
  the level is kept as `level` on the line). `(DQ)` suffixes are stripped and listed in `dq`.

## Identities (`people`)

No page shows a player id or city, so: base identity = name + team; join to the USTA NV list
(unique name -> that person); duplicate names -> each candidate's own "2026 Teams" list decides
(cached locally in `data/tl_candidate_teams_2026.json`, not committed); merges are blocked by different roster NTRP, two
teams in one flight, or both in one match, unless the player's own team list covers every team
(mid-year rating change, tagged `rating_changed`). Anything else stays separate, flagged `ambiguous`.

`ALIASES` in resolve_tl_players.py maps core players whose TennisLink name differs (Renee Johnson -> Renee Warner); `MANUAL_REVIEW` keeps appearances being checked by hand separate (empty now).

Renamed teams: a team that wins Districts is renamed in its league listing to STATE/CAPTAIN/DIVISION
(SPANISH OAKS -> `NV/EDGE/18&OVERW4.0`), and appears at Sectionals/Nationals as `Nevada/Edge/18&Over4.0W`.
Championship teams are mapped back to league teams of the same age group and level: same club name, else
roster overlap (rosters plus everyone who played league lines for a team, crawled or not), else the
captain segment of a STATE/CAPTAIN/DIVISION label.

## Year-end rating window

Each match has `year_end` = {counts, rating_year, reason}. Summer leagues never count toward
year-end NTRP. Matches after the cutoff (2026: Nov 8; 2025: Nov 9, assumed; `YEAR_END_CUTOFF`) count toward the next year.

## Championship sub-flights

Nationals (and some other championships) are split into sub-flights (round robin, semifinals/final,
3rd/4th playoff); the flight Match Summary lists only one, so `crawl_champ` opens each sub-flight.
Tri-Level championship scorecards label lines "4.0 1 Doubles". `refresh_champs` re-collects known flights.
2025: `python3 scrapers/scrape_tl_teams.py --year 2025` -> data/tl_team_matches_2025.json.
