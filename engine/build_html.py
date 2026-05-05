"""
engine/build_html.py
Generate women_30.html and women_35.html from standings + player data.
Tabs: Standings | Team Rosters | All Players | All Results
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

DATA_DIR = Path("data")
PLAYERS_JSON = DATA_DIR / "players.json"
STANDINGS_30 = DATA_DIR / "standings_women_30.json"
STANDINGS_35 = DATA_DIR / "standings_women_35.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def _esc(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _date_sort_key(d: str) -> tuple:
    """Convert M/D/YYYY string to (YYYY, MM, DD) tuple for correct chronological sort.

    Plain string sort fails for dates like 5/2 vs 5/16 because '5/1...' < '5/2...'
    alphabetically, placing week 9 (5/16) before week 7 (5/2).
    """
    try:
        m, day, y = d.split("/")
        return (int(y), int(m), int(day))
    except Exception:
        return (0, 0, 0)


# ---------------------------------------------------------------------------
# Team name abbreviations
# ---------------------------------------------------------------------------

_TEAM_ABBREV = {
    "lake las vegas sports club": "Lake LV",
    "red rock cc":                "Red Rock",
    "red rock cc #1":             "Red Rock #1",
    "red rock cc #2":             "Red Rock #2",
    "life time fitness/gv":       "LTF",
    "life time fitness":          "LTF",
    "anthem cc":                  "Anthem",
    "club ridges":                "Ridges",
    "desert palm":                "D. Palm",
    "southern highlands":         "SoHi",
    "spanish oaks":               "S. Oaks",
    "spanish trail":              "S. Trail",
    "spanish trail #1":           "S. Trail #1",
    "spanish trail #2":           "S. Trail #2",
    "summerlin arbors":           "Summerlin",
    "whitney mesa park":          "Whitney",
}


def _abbrev_team(name: str) -> str:
    """Return abbreviated team name if one is defined, else original."""
    return _TEAM_ABBREV.get((name or "").lower().strip(), name)


# ---------------------------------------------------------------------------
# Line label helpers
# ---------------------------------------------------------------------------

def _line_label_short(lnum: str) -> str:
    """Convert '1# Singles' -> 'S1', '2# Doubles' -> 'D2', etc."""
    m = re.match(r'^(\d+)#\s+(Singles|Doubles)', (lnum or "").strip())
    if not m:
        return lnum
    prefix = "S" if m.group(2) == "Singles" else "D"
    return f"{prefix}{m.group(1)}"


_LINE_PILL_COLORS = {
    "S1": "pill-s1", "S2": "pill-s2", "S3": "pill-s3",
    "D1": "pill-d1", "D2": "pill-d2", "D3": "pill-d3",
}


def _lines_pills_html(lines_played) -> str:
    """Render lines_played (list of court labels, possibly with 'xN' counts) as colored pills.

    Accepts entries like "D1", "D1x2", "S2x3" — the label is the base (D1/S2/etc.)
    and the optional xN suffix shows how many times that court was played.
    """
    if not lines_played:
        return "–"
    # Legacy: if it's a number, just show as plain text
    if isinstance(lines_played, (int, float)):
        return str(int(lines_played))
    if isinstance(lines_played, str):
        try:
            return str(int(lines_played))
        except ValueError:
            pass
    # It's a list like ["D1x2", "D2", "S2"]
    pills = ""
    for raw in lines_played:
        raw = str(raw).strip()
        # Parse "D1x2" or "S1" format
        m = re.match(r'^([A-Z]\d+)(x\d+)?$', raw)
        if m:
            label  = m.group(1)
            suffix = m.group(2) or ""
        else:
            label  = raw
            suffix = ""
        cls = _LINE_PILL_COLORS.get(label, "pill-d1")
        pills += f'<span class="line-pill {cls}">{_esc(label + suffix)}</span>'
    return pills or "–"


def _fmt_rating(v) -> str:
    if v is None:
        return "–"
    try:
        return f"{float(v):.2f}"
    except Exception:
        return str(v)


def _rating_span(curr, baseline, ntrp_str: str) -> str:
    """Return color-coded span for current rating."""
    s = _fmt_rating(curr)
    if curr is None:
        return f'<span class="rn">{s}</span>'
    try:
        c = float(curr)
        b = float(baseline) if baseline is not None else c
        if c > b + 0.005:
            return f'<span class="ru">{s}</span>'
        elif c < b - 0.005:
            return f'<span class="rd">{s}</span>'
        return f'<span class="rn">{s}</span>'
    except Exception:
        return f'<span class="rn">{s}</span>'


def _global_diff_span(glob, div_rating) -> str:
    """Show global-vs-division diff as a muted ±0.XX value. Blank if same or missing."""
    if glob is None or div_rating is None:
        return ""
    try:
        g, d = float(glob), float(div_rating)
        diff = g - d
        if abs(diff) < 0.005:
            return ""
        sign = "+" if diff > 0 else ""
        cls = "gdiff-up" if diff > 0 else "gdiff-dn"
        return f'<span class="{cls}">{sign}{diff:.2f}</span>'
    except Exception:
        return ""


def _baseline_diff_span(curr, baseline) -> tuple[str, str]:
    """
    Returns (html, sort_key) for the New-vs-Baseline diff column.
    html: coloured ±0.XX span (or '–' if missing).
    sort_key: numeric string for data-sort attribute.
    """
    if curr is None or baseline is None:
        return "–", "-999"
    try:
        c, b = float(curr), float(baseline)
        diff = c - b
        sort_key = f"{diff:.4f}"
        if abs(diff) < 0.005:
            return "<span class='gdiff-zero'>0</span>", sort_key
        sign = "+" if diff > 0 else ""
        cls = "gdiff-up" if diff > 0 else "gdiff-dn"
        return f'<span class="{cls}">{sign}{diff:.2f}</span>', sort_key
    except Exception:
        return "–", "-999"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(subflights: list[dict]) -> list[str]:
    """
    Cross-check each team's W/L in the standings table against what the
    match results actually show. Returns a list of human-readable warnings.
    """
    warnings = []
    for sf in subflights:
        label = sf.get("flight_label", "?")
        wins: dict[str, int] = defaultdict(int)
        losses: dict[str, int] = defaultdict(int)
        for m in sf.get("matches", []):
            if m.get("pending"):
                continue
            hw = m.get("team_wins_home") or 0
            aw = m.get("team_wins_away") or 0
            if hw == aw:
                continue
            if hw > aw:
                wins[m["home_team"]] += 1
                losses[m["away_team"]] += 1
            else:
                wins[m["away_team"]] += 1
                losses[m["home_team"]] += 1

        for t in sf.get("teams", []):
            name = t.get("team_name", "")
            sw, sl = (t.get("team_wins") or 0), (t.get("team_losses") or 0)
            rw, rl = wins.get(name, 0), losses.get(name, 0)
            if rw != sw or rl != sl:
                warnings.append(
                    f"Subflight {label} — {name}: "
                    f"standings shows {sw}W–{sl}L but match results count {rw}W–{rl}L"
                )
    return warnings


# ---------------------------------------------------------------------------
# Badge / record helpers
# ---------------------------------------------------------------------------

def _badge_record(w, l) -> str:
    if w is None and l is None:
        return "–"
    w, l = int(w or 0), int(l or 0)
    rec = f"{w}–{l}"
    cls = "bw" if w > l else ("bl" if l > w else "bn")
    return f'<span class="badge {cls}">{rec}</span>'


def _team_result_for(matches: list[dict], team: str) -> list[dict]:
    """
    Build a per-team list of results (score/won from that team's perspective).
    """
    out = []
    for m in sorted(matches, key=lambda x: _date_sort_key(x.get("date", ""))):
        hw = m.get("team_wins_home")
        aw = m.get("team_wins_away")
        pending = m.get("pending", False)
        if m.get("home_team") == team:
            opp = m.get("away_team", "")
            if not pending and hw is not None and aw is not None:
                won = hw > aw
                score = f"{hw}–{aw}"
            else:
                won, score = None, ""
        elif m.get("away_team") == team:
            opp = m.get("home_team", "")
            if not pending and hw is not None and aw is not None:
                won = aw > hw
                score = f"{aw}–{hw}"
            else:
                won, score = None, ""
        else:
            continue
        out.append({
            "date": m.get("date", ""),
            "opponent": opp,
            "won": won,
            "score": score,
            "pending": pending,
            "lines": m.get("lines", []),
            # Whether per-court results have been verified via TL match page.
            # Without tl_match_id the court result field is unreliable (defaults
            # to "home" for every court) so we suppress per-court highlighting.
            "courts_verified": bool(m.get("tl_match_id")),
            # Official home/away team names so we can detect swapped scorecards.
            "home_team": m.get("home_team", ""),
            "away_team": m.get("away_team", ""),
        })
    return out


def _result_badge(won, score, pending) -> str:
    if pending or won is None:
        return '<span class="badge bn">Pending</span>'
    if won:
        return f'<span class="badge bw">W&nbsp;{_esc(score)}</span>'
    return f'<span class="badge bl">L&nbsp;{_esc(score)}</span>'


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------

def _wl_cell(w, l) -> str:
    """Render a W–L cell like '16–4', or '–' if both missing."""
    if w is None and l is None:
        return "–"
    return f"{int(w or 0)}–{int(l or 0)}"


def _standings_tab(subflights: list[dict], warnings: list[str]) -> str:
    warn_html = ""
    if warnings:
        items = "".join(f"<li>{_esc(w)}</li>" for w in warnings)
        warn_html = f'<div class="warn-box">⚠ Data validation warnings:<ul>{items}</ul></div>'

    # Build subflight tabs
    sf_labels = [sf.get("flight_label", str(i)) for i, sf in enumerate(subflights)]
    first_sf = sf_labels[0] if sf_labels else ""

    sf_btns = ""
    for i, lbl in enumerate(sf_labels):
        active = " on" if i == 0 else ""
        sf_btns += (
            f'<button class="rtab sf-switcher{active}" '
            f'data-sf="{_esc(lbl)}" '
            f'onclick="filterStandingsSF(\'{_esc(lbl)}\')">'
            f'{_esc(lbl)}</button>\n'
        )

    panes = ""
    for i, sf in enumerate(subflights):
        lbl    = _esc(sf.get("flight_label", "?"))
        sf_raw = sf.get("flight_label", "")
        teams  = sf.get("teams", [])
        summary = _esc(sf.get("subflight_summary", "") or "")
        visible = "" if i == 0 else ' style="display:none"'

        rows = ""
        for j, t in enumerate(teams, 1):
            name = t.get("team_name", "")
            w = t.get("team_wins")
            l = t.get("team_losses")
            iw = t.get("indiv_wins")  if t.get("indiv_wins")  is not None else "–"
            il = t.get("indiv_losses") if t.get("indiv_losses") is not None else "–"
            sw = t.get("sets_won")    if t.get("sets_won")    is not None else "–"
            sl = t.get("sets_lost")   if t.get("sets_lost")   is not None else "–"
            gw = t.get("games_won")   if t.get("games_won")   is not None else "–"
            gl = t.get("games_lost")  if t.get("games_lost")  is not None else "–"
            slug   = _slug(name)
            sf_esc = _esc(sf_raw).replace("'", "\\'")
            notes = _esc(t.get("notes", "") or "")
            rows += (
                f"<tr>"
                f"<td class='rank'>{j}</td>"
                f"<td class='tname'>"
                f"<a class='team-link' href='#' "
                f"onclick=\"goToRoster('{slug}','{sf_esc}'); return false;\">"
                f"{_esc(name)}</a></td>"
                f"<td>"
                f"<a class='team-link' href='#' "
                f"onclick=\"goToResult('{slug}','{sf_esc}'); return false;\">"
                f"{_badge_record(w, l)}</a></td>"
                f"<td class='st-w'>{iw}</td><td class='st-l'>{il}</td>"
                f"<td class='st-w'>{sw}</td><td class='st-l'>{sl}</td>"
                f"<td class='st-w'>{gw}</td><td class='st-l'>{gl}</td>"
                f"<td class='notes-cell'>{notes}</td>"
                f"</tr>\n"
            )

        summary_html = (
            f'<p class="sf-summary">{summary}</p>' if summary else ""
        )
        panes += (
            f'<div class="st-pane" data-sf="{_esc(sf_raw)}"{visible}>'
            f'<p class="sf-header">Subflight {lbl}</p>'
            f'{summary_html}'
            f'<table class="st-table"><thead><tr>'
            f'<th style="width:2rem">#</th><th>Team</th>'
            f'<th>Record</th><th colspan="2">Courts</th><th colspan="2">Sets</th><th colspan="2">Games</th><th>Notes</th>'
            f'</tr></thead><tbody>{rows}</tbody></table>'
            f'</div>\n'
        )

    return (
        warn_html
        + f'<div class="rtabs" id="st-sf-tabs">{sf_btns}</div>'
        + panes
    )


def _rosters_tab(subflights: list[dict], players: list[dict], ntrp: str = "") -> str:
    # Field suffix for per-division stats ("3.0" -> "30", "3.5" -> "35")
    _sfx = ntrp.replace(".", "") if ntrp else ""

    # Build team set from standings so we know which teams belong to this NTRP level
    _standings_teams = {
        t.get("team_name", "")
        for sf in subflights
        for t in sf.get("teams", [])
    }

    # Include a player in this division's rosters if:
    #   (a) their registered division matches this NTRP level → place in their registered team
    #   (b) they have match stats for this NTRP level AND their division-specific team
    #       (team_{sfx}) is in this division's standings → place in that actual team
    #       (handles dual-division players registered in one level but also playing in another)
    by_team: dict[str, list] = defaultdict(list)
    for p in players:
        t   = p.get("team", "")            # registered team
        div = p.get("division", "")
        in_div = not ntrp or div.startswith(ntrp)

        # The team they actually played for in this division (set during stats computation)
        div_team = (p.get(f"team_{_sfx}", "") or "") if _sfx else ""

        has_ntrp_stats = bool(
            _sfx and (p.get(f"lines_played_{_sfx}") or p.get(f"wl_record_{_sfx}"))
        )

        # Prefer the team derived from actual scorecard data (team_35 / team_30) when
        # available and valid — it is more reliable than the roster-scraped 'team' field.
        effective_team = (div_team if (div_team and div_team in _standings_teams) else t) if in_div else t

        if in_div and effective_team:
            by_team[effective_team].append(p)
        elif has_ntrp_stats and div_team and div_team in _standings_teams:
            # Dual-division player: place in the team they actually competed with
            by_team[div_team].append(p)

    # Build panes grouped by subflight
    sf_labels = [sf.get("flight_label", str(i)) for i, sf in enumerate(subflights)]
    first_sf = sf_labels[0] if sf_labels else ""

    sf_btns = ""
    for i, lbl in enumerate(sf_labels):
        active = " on" if i == 0 else ""
        sf_btns += (
            f'<button class="rtab sf-switcher{active}" '
            f'data-sf="{_esc(lbl)}" '
            f'onclick="filterSF(\'{_esc(lbl)}\',this,\'ro-sf-tabs\',\'ro-tabs\',\'ro\')">'
            f'{_esc(lbl)}</button>\n'
        )

    team_tabs, rpanes = "", ""
    first_seen = True
    for sf in subflights:
        sf_lbl = sf.get("flight_label", "")
        for t in sf.get("teams", []):
            tname = t.get("team_name", "")
            if not tname:
                continue
            tid = f"ro-{_slug(tname)}"
            # First tab of first subflight is active
            active = " on" if first_seen else ""
            visible = "" if sf_lbl == first_sf else ' style="display:none"'
            first_seen = False

            roster = sorted(
                by_team.get(tname, []),
                key=lambda p: -(p.get(f"rating_{_sfx}") or
                                p.get("current_division_rating") or
                                p.get("dynamic_rating_baseline") or 0)
            )
            rows = ""
            for p in roster:
                ntrp_r = p.get("ntrp_rating", "") or ""
                baseline = p.get("dynamic_rating_baseline")
                curr = p.get(f"rating_{_sfx}") or p.get("current_division_rating")
                glob = p.get("global_rating")
                # Use per-division stats only — legacy wl_record is a combined total across
                # all divisions and is wrong for cross-listed players.
                wl    = p.get(f"wl_record_{_sfx}") or "–"
                lines = p.get(f"lines_played_{_sfx}") or "–"
                pnotes = _esc(p.get(f"notes_{_sfx}", "") or "")
                dw = p.get(f"default_wins_{_sfx}", 0) or 0
                if dw:
                    dw_note = (f'<span class="default-win-badge">'
                               f'incl. {dw} walkover{"s" if dw > 1 else ""}</span>')
                    pnotes = (pnotes + " " if pnotes else "") + dw_note
                rows += (
                    f"<tr>"
                    f"<td>{_esc(p.get('name',''))}</td>"
                    f"<td>{_esc(ntrp_r)}</td>"
                    f"<td>{_esc(_fmt_rating(baseline))}</td>"
                    f"<td>{_rating_span(curr, baseline, ntrp_r)}</td>"
                    f"<td>{_global_diff_span(glob, curr)}</td>"
                    f"<td>{_esc(str(wl))}</td>"
                    f"<td>{_lines_pills_html(lines)}</td>"
                    f"<td class='notes-cell'>{pnotes}</td>"
                    f"</tr>\n"
                )
            if not rows:
                rows = "<tr><td colspan='8' class='muted'>No players yet.</td></tr>"

            team_tabs += (
                f'<button class="rtab{active}" data-sf="{_esc(sf_lbl)}"{visible} '
                f'onclick="sr(\'{tid}\',this,\'ro-tabs\')">'
                f'{_esc(_abbrev_team(tname))}'
                f'</button>\n'
            )
            rpanes += (
                f'<div id="{tid}" class="rpane{active}">'
                f'<p class="sec-title">{_esc(tname)} &mdash; Subflight {_esc(sf_lbl)}</p>'
                f'<table><thead><tr>'
                f'<th class="sortable" onclick="sortRoster(0)">Player ↕</th>'
                f'<th>NTRP</th>'
                f'<th class="sortable" onclick="sortRoster(2)">Base ↕</th>'
                f'<th class="sortable" onclick="sortRoster(3)">New ↕</th>'
                f'<th class="sortable" onclick="sortRoster(4)">Gbl ±</th>'
                f'<th>W–L</th><th>Lines</th><th>Notes</th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div>\n'
            )

    return (
        f'<div class="rtabs" id="ro-sf-tabs">{sf_btns}</div>'
        f'<div class="rtabs scrollable" id="ro-tabs">{team_tabs}</div>'
        + rpanes
    )


def _players_tab(players: list[dict], ntrp: str, subflights: list[dict] = None) -> str:
    _sfx = ntrp.replace(".", "") if ntrp else ""

    # Build team → subflight label lookup so dual-division players get the right SF pill
    team_to_sf: dict[str, str] = {}
    for sf_obj in (subflights or []):
        lbl = sf_obj.get("flight_label", "")
        for t in sf_obj.get("teams", []):
            team_to_sf[t.get("team_name", "")] = lbl

    def _in_ntrp(p):
        if p.get("division", "").startswith(ntrp):
            return True
        if _sfx and (p.get(f"lines_played_{_sfx}") or p.get(f"wl_record_{_sfx}")):
            return True
        return False

    div_players = [p for p in players if _in_ntrp(p)]
    div_players.sort(
        key=lambda p: -(p.get(f"rating_{_sfx}") or
                        p.get("current_division_rating") or
                        p.get("dynamic_rating_baseline") or 0)
    )
    rows = ""
    for p in div_players:
        ntrp_r = p.get("ntrp_rating", "") or ""
        baseline = p.get("dynamic_rating_baseline")
        curr = p.get(f"rating_{_sfx}") or p.get("current_division_rating")
        glob = p.get("global_rating")
        wl = p.get(f"wl_record_{_sfx}") or "–"
        division = p.get("division", "")
        # For players registered in this division, read subflight from their division string.
        # For dual-division players, look up the subflight of their actual team in this division.
        if division.startswith(ntrp):
            sf = division.split()[-1] if division else ""
        else:
            div_team = (p.get(f"team_{_sfx}", "") or "") if _sfx else ""
            sf = team_to_sf.get(div_team, "")
        # Numeric sort key for W-L: wins * 100 + total_matches
        # (sort by most wins first; more matches breaks ties at same win count)
        _wl_sort = "0"
        _wl_str = str(wl) if wl else "–"
        if "-" in _wl_str and _wl_str != "–":
            _wparts = _wl_str.split("-")
            try:
                _w, _l = int(_wparts[0]), int(_wparts[1])
                # Primary: wins (higher = better). Secondary: fewer losses (4-0 > 4-1).
                _wl_sort = str(_w * 100 - _l)
            except (ValueError, IndexError):
                pass
        _diff_html, _diff_sort = _baseline_diff_span(curr, baseline)
        rows += (
            f"<tr data-sf='{_esc(sf)}'>"
            f"<td class='pname'>{_esc(p.get('name',''))}</td>"
            f"<td>{_esc(_abbrev_team((_sfx and p.get(f'team_{_sfx}')) or p.get('team','') or ''))}</td>"
            f"<td><span class='sf-pill'>{_esc(sf)}</span></td>"
            f"<td>{_esc(ntrp_r)}</td>"
            f"<td>{_esc(_fmt_rating(baseline))}</td>"
            f"<td>{_rating_span(curr, baseline, ntrp_r)}</td>"
            f"<td data-sort='{_diff_sort}'>{_diff_html}</td>"
            f"<td data-sort='{_wl_sort}'>{_esc(_wl_str)}</td>"
            f"</tr>\n"
        )
    return f"""
<div class="ap-controls">
  <input type="text" id="player-search" placeholder="Filter by name or team…"
         oninput="filterPlayers()">
  <div class="sf-filter-btns">
    <button class="rtab on" onclick="filterPlayerSF('all',this)">All</button>
    <button class="rtab" onclick="filterPlayerSF('A',this)">A</button>
    <button class="rtab" onclick="filterPlayerSF('B',this)">B</button>
  </div>
</div>
<table id="ap-table">
  <thead><tr>
    <th class="sortable" onclick="sortAP(0)">Player ↕</th>
    <th class="sortable" onclick="sortAP(1)">Team ↕</th>
    <th>SF</th>
    <th>NTRP</th>
    <th class="sortable" onclick="sortAP(4)">Base ↕</th>
    <th class="sortable" onclick="sortAP(5)">New ↕</th>
    <th class="sortable" onclick="sortAP(6)">Diff ↕</th>
    <th class="sortable" onclick="sortAP(7)">W–L ↕</th>
  </tr></thead>
  <tbody>{rows}</tbody>
</table>"""


def _results_tab(subflights: list[dict], players: list[dict] | None = None,
                 sfx: str = "") -> str:
    # Build name → baseline and name → new (division) rating lookups
    _baseline_by_name: dict[str, str] = {}
    _new_by_name: dict[str, str] = {}
    # name → {date: pre-match rating} for point-in-time display
    _timeline_by_name: dict[str, dict[str, float]] = {}
    _team_by_name: dict[str, str] = {}
    if players:
        for p in players:
            norm = re.sub(r"\s+", " ", (p.get("name") or "").strip().lower())
            raw_base = p.get("dynamic_rating_baseline")
            if raw_base is not None:
                try:
                    _baseline_by_name[norm] = f"{float(raw_base):.2f}"
                except (ValueError, TypeError):
                    pass
            raw_new = p.get(f"rating_{sfx}") if sfx else None
            if raw_new is None:
                raw_new = p.get("current_division_rating")
            if raw_new is not None:
                try:
                    _new_by_name[norm] = f"{float(raw_new):.2f}"
                except (ValueError, TypeError):
                    pass
            # Timeline: per-date pre-match rating stored by sequential computation
            timeline = p.get(f"rating_timeline_{sfx}") if sfx else None
            if timeline and isinstance(timeline, dict):
                _timeline_by_name[norm] = {k: float(v) for k, v in timeline.items()}
            # Include all team fields so swap detection works for cross-listed players
            for _tf in ("team", "team_30", "team_35"):
                _tv = p.get(_tf)
                if _tv:
                    _team_by_name[norm] = _tv
            if p.get("team"):
                _team_by_name[norm] = p["team"]  # primary team wins

    def _pname_key(name: str) -> str:
        """Normalise a player name for data-pkey attribute."""
        return re.sub(r"[^a-z0-9]", "-", name.strip().lower())

    def _pit_rating(nkey: str, match_date: str) -> str:
        """Return the point-in-time rating for a player going into match_date.

        Priority:
        1. Exact timeline entry for match_date (player played that date → pre-match snapshot)
        2. Most recent timeline entry BEFORE match_date (last known rating)
        3. Baseline (player hadn't played yet in this division)
        4. Final season rating (no timeline at all — e.g. opponents from other divisions)
        """
        if nkey not in _timeline_by_name:
            return _new_by_name.get(nkey, "")
        tl = _timeline_by_name[nkey]
        # Exact hit
        if match_date in tl:
            return f"{tl[match_date]:.2f}"
        # Most recent entry strictly before match_date
        match_key = _date_sort_key(match_date)
        prior = [(k, v) for k, v in tl.items() if _date_sort_key(k) < match_key]
        if prior:
            # Take the latest prior entry
            latest_k, latest_v = max(prior, key=lambda kv: _date_sort_key(kv[0]))
            return f"{latest_v:.2f}"
        # No timeline entries before this date → player hadn't played yet → use baseline
        base = _baseline_by_name.get(nkey, "")
        return base if base else _new_by_name.get(nkey, "")

    def _render_players(raw: str, match_date: str = "") -> str:
        """Wrap each player name in a clickable span with rating badge (base + new).

        When match_date is provided and the player has a sequential rating timeline,
        data-new shows the player's rating going *into* that match (based on all
        prior matches in the division), not their final end-of-season rating.
        """
        parts = [p.strip() for p in raw.split("/") if p.strip()]
        rendered = []
        for name in parts:
            nkey = re.sub(r"\s+", " ", name.lower())
            base_r = _baseline_by_name.get(nkey, "")
            # Point-in-time new rating via timeline lookup (falls back to baseline
            # if player hadn't played yet — avoids showing end-of-season rating
            # on default lines or weeks before their first actual match).
            if match_date:
                new_r = _pit_rating(nkey, match_date)
            else:
                new_r = _new_by_name.get(nkey, "")
            # Embed both ratings as data attributes; text defaults to new rating.
            # JS setResultRatingMode() swaps displayed text without re-rendering.
            if base_r or new_r:
                default_txt = new_r or base_r
                rating_html = (
                    f'<em class="prating" data-base="{base_r}" data-new="{new_r}">'
                    f'({default_txt})</em>'
                )
            else:
                rating_html = ""
            key = _pname_key(name)
            rendered.append(
                f'<span class="pname" data-pkey="{key}" '
                f'onclick="highlightPlayer(this)">{_esc(name)}{rating_html}</span>'
            )
        return " / ".join(rendered) if rendered else _esc(raw)

    sf_labels = [sf.get("flight_label", str(i)) for i, sf in enumerate(subflights)]
    first_sf = sf_labels[0] if sf_labels else ""

    # Build date → week-label map (W1, W2, …) from all unique match dates
    # across every subflight, sorted chronologically.
    _all_dates: set[str] = set()
    for _sf in subflights:
        for _m in _sf.get("matches", []):
            d = _m.get("date", "")
            if d:
                _all_dates.add(d)
    _sorted_dates = sorted(_all_dates, key=_date_sort_key)
    _week_label: dict[str, str] = {d: f"W{i+1}" for i, d in enumerate(_sorted_dates)}

    sf_btns = ""
    for i, lbl in enumerate(sf_labels):
        active = " on" if i == 0 else ""
        sf_btns += (
            f'<button class="rtab sf-switcher{active}" '
            f'data-sf="{_esc(lbl)}" '
            f'onclick="filterSF(\'{_esc(lbl)}\',this,\'re-sf-tabs\',\'re-tabs\',\'re\')">'
            f'{_esc(lbl)}</button>\n'
        )

    team_tabs, rpanes = "", ""
    first_seen = True
    for sf in subflights:
        sf_lbl = sf.get("flight_label", "")
        matches = sf.get("matches", [])
        for t in sf.get("teams", []):
            tname = t.get("team_name", "")
            if not tname:
                continue
            tid = f"re-{_slug(tname)}"
            active = " on" if first_seen else ""
            visible = "" if sf_lbl == first_sf else ' style="display:none"'
            first_seen = False

            team_matches = _team_result_for(matches, tname)
            blocks = ""
            for m in team_matches:
                badge = _result_badge(m["won"], m["score"], m["pending"])
                wlabel = _week_label.get(m.get("date", ""), "")
                blocks += (
                    f'<div class="mblock">'
                    f'<div class="mhdr">'
                    f'<span class="mtitle">vs {_esc(m["opponent"])}</span>'
                    f'<span class="mweek-lbl">{_esc(wlabel)}</span>'
                    f'<span>{badge}</span>'
                    f'</div>'
                    f'<div class="mdate">{_esc(m["date"])}</div>'
                )
                lines = m.get("lines", [])
                courts_verified = m.get("courts_verified", False)
                if lines:
                    # Detect scorecard swap at match level: TennisLink sometimes lists
                    # the away team's players in the "home" column and vice versa.
                    # result="home" means the HOME TEAM won that court (TL radio label),
                    # NOT that the players_home column player won. Swap detection tells
                    # us which column actually has the home team's players.
                    _mht = m.get("home_team", "")
                    _mat = m.get("away_team", "")
                    _home_votes = _away_votes = 0
                    for _vln in lines:
                        for _pn in [x.strip() for x in _vln.get("players_home", "").split("/") if x.strip()]:
                            _pt = _team_by_name.get(re.sub(r"\s+", " ", _pn.lower().strip()), "")
                            if _pt == _mht: _home_votes += 1
                            elif _pt == _mat: _away_votes += 1
                    _is_swapped = (_away_votes > _home_votes)

                    blocks += '<div class="line-lbl">line results</div>'
                    for ln in lines:
                        # Keep original scorecard layout (home col left, away col right)
                        ph_raw = ln.get("players_home", "")
                        pa_raw = ln.get("players_away", "")
                        # Show "default" for empty/N/A player slots
                        def _default_or_render(raw, _mdate=m["date"]):
                            s = raw.strip()
                            if not s or s.upper() == "N/A":
                                return '<em class="default-marker">default</em>'
                            return _render_players(raw, _mdate)
                        left = _default_or_render(ph_raw)
                        right = _default_or_render(pa_raw)
                        sc = _esc(ln.get("score", ""))
                        lnum = _line_label_short(ln.get("line", ""))
                        # result="home"/"away" = which TEAM won (not which column).
                        # If swapped, home team players are in the right (away) column.
                        result = ln.get("result", "")
                        if result == "home":
                            hw, aw = ("", "w") if _is_swapped else ("w", "")
                        elif result == "away":
                            hw, aw = ("w", "") if _is_swapped else ("", "w")
                        else:
                            hw = aw = ""
                        blocks += (
                            f'<div class="line-row">'
                            f'<span class="lh {hw}"><span class="lr-lbl">{_esc(lnum)}</span> {left}</span>'
                            f'<span class="ls">{sc}</span>'
                            f'<span class="la {aw}">{right}</span>'
                            f'</div>'
                        )
                blocks += "</div>\n"

            if not blocks:
                blocks = '<p class="muted">No results yet.</p>'

            team_tabs += (
                f'<button class="rtab{active}" data-sf="{_esc(sf_lbl)}"{visible} '
                f'onclick="sr(\'{tid}\',this,\'re-tabs\')">'
                f'{_esc(_abbrev_team(tname))}'
                f'</button>\n'
            )
            rpanes += (
                f'<div id="{tid}" class="rpane{active}">{blocks}</div>\n'
            )

    rating_toggle = (
        '<div class="re-rating-toggle">'
        '<span class="re-rtog-label">Ratings:</span>'
        '<button class="rtab" onclick="setResultRatingMode(\'none\',this)">None</button>'
        '<button class="rtab" onclick="setResultRatingMode(\'base\',this)">Base</button>'
        '<button class="rtab on" onclick="setResultRatingMode(\'new\',this)">New</button>'
        '</div>'
    )
    return (
        rating_toggle
        + f'<div class="rtabs" id="re-sf-tabs">{sf_btns}</div>'
        + f'<div class="rtabs scrollable" id="re-tabs">{team_tabs}</div>'
        + rpanes
    )


# ---------------------------------------------------------------------------
# Summary cards
# ---------------------------------------------------------------------------

def _summary_cards(ntrp: str, year: int, subflights: list[dict]) -> str:
    total_teams = sum(len(sf.get("teams", [])) for sf in subflights)
    total_matches = sum(len(sf.get("matches", [])) for sf in subflights)
    played = sum(
        1 for sf in subflights
        for m in sf.get("matches", [])
        if not m.get("pending")
    )

    # Leader = team with most wins across all subflights
    best_team, best_w, best_l = "–", 0, 0
    for sf in subflights:
        for t in sf.get("teams", []):
            w = t.get("team_wins") or 0
            if w > best_w:
                best_w, best_l = w, (t.get("team_losses") or 0)
                best_team = t.get("team_name", "–")

    # Latest completed match date
    dates = [
        m.get("date", "")
        for sf in subflights
        for m in sf.get("matches", [])
        if not m.get("pending") and m.get("date")
    ]
    latest = sorted(dates)[-1] if dates else "–"

    return f"""<div class="cards-row">
  <div class="mcard">
    <div class="mcard-label">division</div>
    <div class="mcard-val">{_esc(ntrp)} Women</div>
    <div class="mcard-sub">NV Area F · {year}</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">teams</div>
    <div class="mcard-val">{total_teams}</div>
    <div class="mcard-sub">{played} of {total_matches} matches played</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">leader</div>
    <div class="mcard-val">{_esc(best_team)}</div>
    <div class="mcard-sub">{best_w}–{best_l} record</div>
  </div>
  <div class="mcard">
    <div class="mcard-label">last played</div>
    <div class="mcard-val">{_esc(latest)}</div>
    <div class="mcard-sub">through week of {_esc(latest)}</div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Full page
# ---------------------------------------------------------------------------

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       padding: 1rem; background: #fff; color: #222; font-size: 13px; }
/* Cards */
.cards-row { display: grid; grid-template-columns: repeat(4, minmax(0,1fr));
             gap: 10px; margin-bottom: 16px; }
.mcard { background: #f5f4f0; border-radius: 8px; padding: .75rem 1rem; }
.mcard-label { font-size: 11px; color: #888; margin-bottom: 3px; }
.mcard-val { font-size: 20px; font-weight: 600; }
.mcard-sub { font-size: 11px; color: #888; margin-top: 2px; }
/* Main tabs */
.tabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
.tab { padding: 6px 14px; border: 1px solid #ccc; border-radius: 20px;
       font-size: 13px; background: transparent; color: #666;
       cursor: pointer; font-weight: 500; }
.tab.on { background: #eee; color: #222; font-weight: 600; border-color: #999; }
.pane { display: none; } .pane.on { display: block; }
/* Sub-tabs */
.rtabs { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.rtabs.scrollable { overflow-x: auto; flex-wrap: nowrap; padding-bottom: 4px; }
.re-rating-toggle { display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }
.re-rtog-label { font-size: 11px; color: #888; }
.rtab { padding: 4px 10px; border: 1px solid #ccc; border-radius: 20px;
        font-size: 11px; background: transparent; color: #666; cursor: pointer;
        white-space: nowrap; }
.rtab.on { background: #eee; color: #222; font-weight: 600; }
.rpane { display: none; } .rpane.on { display: block; }
/* Tables */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 5px 6px; color: #888; font-weight: 600;
     border-bottom: 1px solid #eee; font-size: 11px; }
th.sortable { cursor: pointer; }
th.sortable:hover { color: #333; }
td { padding: 5px 6px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
tr:last-child td { border-bottom: none; }
.rank { color: #aaa; width: 1.5rem; }
.tname { font-weight: 500; max-width: 140px; overflow: hidden; text-overflow: ellipsis; }
.pname { font-weight: 500; }
#ap-table td { vertical-align: middle; }
.st-table td:nth-child(n+4):nth-child(-n+6) { white-space: nowrap; font-size: 11px; }
/* Courts/Sets/Games split sub-columns */
.st-w { text-align: right; padding-right: 2px; padding-left: 6px; white-space: nowrap; font-size: 11px; width: 1.6rem; }
.st-l { text-align: right; padding-left: 2px; padding-right: 10px; white-space: nowrap; font-size: 11px; color: #aaa; width: 1.6rem; }
.st-table thead th[colspan] { text-align: center; }
.rpane table td:nth-child(6), #ap-table td:nth-child(7) { white-space: nowrap; }
.muted { color: #aaa; font-size: 11px; }
/* Badges */
.badge { display: inline-block; padding: 1px 7px; border-radius: 10px;
         font-size: 11px; font-weight: 600; white-space: nowrap; }
.bw { background: #EAF3DE; color: #27500A; }
.bl { background: #FCEBEB; color: #791F1F; }
.bn { background: #F1EFE8; color: #444; }
/* Ratings */
.ru { color: #27500A; font-weight: 600; }
.rd { color: #791F1F; }
.rn { color: #888; }
/* SF pill */
.sf-pill { display: inline-block; padding: 0 5px; border-radius: 8px;
           font-size: 10px; font-weight: 600; background: #e8edf5;
           color: #3a5a8c; vertical-align: middle; }
/* Search + SF filter row */
.ap-controls { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
.ap-controls input { flex: 1; min-width: 160px; max-width: 320px; padding: 5px 10px;
  border: 1px solid #ddd; border-radius: 20px; font-size: 12px; }
.sf-filter-btns { display: flex; gap: 4px; }
/* Match blocks */
.mblock { border: 1px solid #eee; border-radius: 8px;
          padding: .75rem 1rem; margin-bottom: 10px; }
.mhdr { display: flex; justify-content: space-between;
        align-items: center; margin-bottom: 4px; }
.mtitle { font-size: 13px; font-weight: 600; flex: 1; }
.mweek-lbl { font-size: 11px; font-weight: 700; color: #888;
             letter-spacing: .04em; text-align: center; flex: 0 0 auto;
             padding: 0 8px; }
.mdate { font-size: 11px; color: #888; margin-bottom: 6px; }
.line-lbl { font-size: 10px; font-weight: 600; color: #aaa;
            text-transform: uppercase; letter-spacing: .05em; margin: 8px 0 3px; }
.line-row { display: grid; grid-template-columns: 1fr auto 1fr;
            gap: 2px 8px; font-size: 12px; align-items: center; padding: 2px 0; }
.lh { color: #aaa; } .lh.w { color: #27500A; font-weight: 600; }
.la { color: #aaa; text-align: right; } .la.w { color: #27500A; font-weight: 600; }
.ls { text-align: center; font-weight: 600; font-size: 11px; color: #aaa; }
/* Clickable player names in results tab */
.pname { cursor: pointer; border-radius: 3px; padding: 0 2px; transition: background .1s; }
.pname:hover { background: #f0f4ff; }
.pname.phi { background: #fff3b0; outline: 1px solid #e6c800; }
.prating { font-size: 10px; color: #aaa; font-style: normal; font-weight: 400; margin-left: 2px; }
.lh.w .prating, .la.w .prating { color: #5a8a2a; }
/* Section title */
.sec-title { font-size: 11px; font-weight: 600; color: #888;
             text-transform: uppercase; letter-spacing: .07em; margin: 0 0 8px; }
/* Standings subflight header */
.sf-header { font-size: 12px; font-weight: 700; color: #555;
             text-transform: uppercase; letter-spacing: .06em;
             margin: 18px 0 6px; padding-bottom: 4px;
             border-bottom: 2px solid #e8e8e8; }
.sf-header:first-child { margin-top: 0; }
.st-table { margin-bottom: 6px; }
/* Validation warning */
.warn-box { background: #fffbe6; border: 1px solid #f0c040;
            border-radius: 6px; padding: .6rem .9rem;
            font-size: 12px; margin-bottom: 10px; }
.warn-box ul { margin: 4px 0 0 1.2em; }
.warn-box li { margin: 2px 0; }
/* Line court pills */
.line-pill { display: inline-block; padding: 1px 6px; border-radius: 8px;
             font-size: 10px; font-weight: 700; margin-right: 3px;
             letter-spacing: .02em; }
.pill-s1 { background: #FDEBD0; color: #A04000; }
.pill-s2 { background: #FAD7A0; color: #784212; }
.pill-s3 { background: #F9E79F; color: #7D6608; }
.pill-d1 { background: #D6EAF8; color: #1A5276; }
.pill-d2 { background: #D5F5E3; color: #1E8449; }
.pill-d3 { background: #E8DAEF; color: #6C3483; }
/* Line label in line-row */
.lr-lbl { display: inline-block; font-size: 10px; font-weight: 700;
          color: #aaa; min-width: 20px; }
/* Standings clickable team / record links */
.team-link { color: inherit; text-decoration: none; }
.team-link:hover { color: #1a5276; text-decoration: underline; cursor: pointer; }
/* Cross-dashboard link */
.top-bar { text-align: right; margin-bottom: 8px; }
.cross-link { font-size: 12px; color: #1a5276; text-decoration: none; font-weight: 500; }
.cross-link:hover { text-decoration: underline; }
/* Row hover highlight */
.st-table tbody tr:hover, .rpane table tbody tr:hover, #ap-table tbody tr:hover { background: #f5f7fa; }
/* Global diff column */
.gdiff-up   { font-size: 10px; color: #5a8a2a; font-weight: 500; }
.gdiff-dn   { font-size: 10px; color: #a04000; font-weight: 500; }
.gdiff-zero { font-size: 10px; color: #999; }
/* Notes column */
.notes-cell { font-size: 10px; color: #555; line-height: 1.35; min-width: 180px; max-width: 320px; }
/* Walkover/default win badge in roster notes */
.default-win-badge { display: inline-block; font-size: 9px; color: #888; background: #f3f3f3;
  border: 1px solid #ddd; border-radius: 3px; padding: 1px 4px; margin-top: 2px; font-style: italic; }
/* Default marker in results tab */
.default-marker { color: #999; font-style: italic; font-size: 11px; }
/* Subflight summary in standings */
.sf-summary { font-size: 12px; color: #444; line-height: 1.55; margin: 0 0 12px 0;
              padding: 10px 12px; background: #f9f9f9; border-left: 3px solid #d0d0d0; border-radius: 4px; }
/* Analysis + Predictions tab */
.insight { padding: 8px 0; border-bottom: 1px solid #f0f0f0; font-size: 12px; line-height: 1.6; }
.insight:last-child { border-bottom: none; }
.itag { display: inline-block; font-size: 10px; font-weight: 600; padding: 1px 6px;
        border-radius: 8px; margin-right: 5px; background: #E6F1FB; color: #0C447C; }
.itag.you { background: #EAF3DE; color: #27500A; }
.itag.warn { background: #FAEEDA; color: #633806; }
.itag.opp { background: #EEEDFE; color: #3C3489; }
"""

_JS = """
function sw(id, btn) {
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('on'));
  document.querySelectorAll('.tab').forEach(b => b.classList.remove('on'));
  document.getElementById(id).classList.add('on');
  btn.classList.add('on');
}
function sr(id, btn, groupId) {
  // Deactivate all tabs in the group
  var grp = document.getElementById(groupId);
  if (grp) grp.querySelectorAll('.rtab').forEach(b => b.classList.remove('on'));
  // Hide all rpanes that share the same prefix (e.g. "ro-" or "re-")
  var prefix = id.split('-')[0] + '-';
  document.querySelectorAll('[id^="' + prefix + '"]').forEach(p => {
    if (p.classList.contains('rpane')) p.classList.remove('on');
  });
  document.getElementById(id).classList.add('on');
  if (btn) btn.classList.add('on');
}

// Switch A/B subflight in rosters or results tab
// Simple subflight switcher for the standings tab (no team sub-tabs)
function filterStandingsSF(sf) {
  var tabs = document.getElementById('st-sf-tabs');
  if (tabs) {
    tabs.querySelectorAll('.rtab').forEach(function(b) {
      b.classList.toggle('on', b.dataset.sf === sf);
    });
  }
  document.querySelectorAll('.st-pane').forEach(function(p) {
    p.style.display = (p.dataset.sf === sf) ? '' : 'none';
  });
}

function filterSF(sf, btn, sfTabsId, teamTabsId, prefix) {
  // Update SF tab highlight
  var sfTabs = document.getElementById(sfTabsId);
  if (sfTabs) sfTabs.querySelectorAll('.rtab').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');

  // Show/hide team tabs based on data-sf
  var teamTabs = document.getElementById(teamTabsId);
  var firstVisible = null;
  if (teamTabs) {
    teamTabs.querySelectorAll('.rtab').forEach(b => {
      if (b.dataset.sf === sf) {
        b.style.display = '';
        if (!firstVisible) firstVisible = b;
      } else {
        b.style.display = 'none';
        b.classList.remove('on');
      }
    });
  }

  // Hide all rpanes for this prefix, then show the first visible team's pane
  document.querySelectorAll('[id^="' + prefix + '-"]').forEach(p => {
    if (p.classList.contains('rpane')) p.classList.remove('on');
  });
  if (firstVisible) {
    firstVisible.classList.add('on');
    var targetId = firstVisible.getAttribute('onclick').match(/'([^']+)'/)[1];
    var pane = document.getElementById(targetId);
    if (pane) pane.classList.add('on');
  }
}

// Highlight all occurrences of a player name in the results tab
function setResultRatingMode(mode, btn) {
  // Update toggle button state
  document.querySelectorAll('.re-rating-toggle .rtab').forEach(function(b) {
    b.classList.remove('on');
  });
  btn.classList.add('on');
  // Update every rating badge in the results pane
  document.querySelectorAll('.rpane .prating').forEach(function(em) {
    if (mode === 'none') {
      em.style.display = 'none';
    } else {
      em.style.display = '';
      var val = mode === 'base' ? em.dataset.base : em.dataset.new;
      em.textContent = val ? '(' + val + ')' : '';
    }
  });
}

function highlightPlayer(el) {
  var key = el.dataset.pkey;
  if (!key) return;
  var already = el.classList.contains('phi');
  // Clear all highlights first
  document.querySelectorAll('.pname.phi').forEach(function(n) { n.classList.remove('phi'); });
  if (!already) {
    document.querySelectorAll('.pname[data-pkey="' + key + '"]').forEach(function(n) {
      n.classList.add('phi');
    });
  }
}

var _apSF = 'all';
function filterPlayerSF(sf, btn) {
  _apSF = sf;
  document.querySelectorAll('.sf-filter-btns .rtab').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  applyPlayerFilters();
}
function filterPlayers() {
  applyPlayerFilters();
}
function applyPlayerFilters() {
  var q = (document.getElementById('player-search') || {value:''}).value.toLowerCase();
  document.querySelectorAll('#ap-table tbody tr').forEach(tr => {
    var sfMatch = _apSF === 'all' || tr.dataset.sf === _apSF;
    var textMatch = !q || tr.innerText.toLowerCase().includes(q);
    tr.style.display = (sfMatch && textMatch) ? '' : 'none';
  });
}
var _sortDir = {};
function _sortTable(tbodyOrSelector, col, dirKey) {
  var tbody = typeof tbodyOrSelector === 'string'
    ? document.querySelector(tbodyOrSelector)
    : tbodyOrSelector;
  if (!tbody) return;
  var rows = Array.from(tbody.querySelectorAll('tr'));
  var dir = (_sortDir[dirKey] = !_sortDir[dirKey]);
  rows.sort(function(a, b) {
    var ac = a.cells[col], bc = b.cells[col];
    // Prefer data-sort numeric attribute when present (e.g. W-L cells)
    var ads = ac ? ac.dataset.sort : undefined;
    var bds = bc ? bc.dataset.sort : undefined;
    if (ads !== undefined && bds !== undefined) {
      var an2 = parseFloat(ads), bn2 = parseFloat(bds);
      if (!isNaN(an2) && !isNaN(bn2)) return dir ? an2 - bn2 : bn2 - an2;
    }
    var av = ac ? ac.innerText.trim() : '';
    var bv = bc ? bc.innerText.trim() : '';
    var an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return dir ? an - bn : bn - an;
    return dir ? av.localeCompare(bv) : bv.localeCompare(av);
  });
  rows.forEach(r => tbody.appendChild(r));
}
function sortAP(col) {
  _sortTable('#ap-table tbody', col, 'ap-' + col);
}
function sortRoster(col) {
  // Sort the currently visible roster pane's table
  var pane = document.querySelector('.rpane.on[id^="ro-"]');
  if (!pane) return;
  var tbody = pane.querySelector('tbody');
  _sortTable(tbody, col, 'ro-' + col + '-' + (pane ? pane.id : ''));
}

// Navigate from standings → team rosters tab for a specific team
function goToRoster(slug, sf) {
  // Switch to Team Rosters main tab
  document.querySelectorAll('.pane').forEach(function(p) { p.classList.remove('on'); });
  document.querySelectorAll('.tab').forEach(function(b) {
    b.classList.remove('on');
    if (b.textContent.trim().toLowerCase() === 'team rosters') b.classList.add('on');
  });
  var rPane = document.getElementById('rosters');
  if (rPane) rPane.classList.add('on');
  // Switch subflight
  if (sf) {
    var sfBtn = document.querySelector('#ro-sf-tabs .rtab[data-sf="' + sf + '"]');
    if (sfBtn) filterSF(sf, sfBtn, 'ro-sf-tabs', 'ro-tabs', 'ro');
  }
  // Activate the team pane
  setTimeout(function() {
    var targetId = 'ro-' + slug;
    var pane = document.getElementById(targetId);
    if (!pane) return;
    document.querySelectorAll('[id^="ro-"].rpane').forEach(function(p) { p.classList.remove('on'); });
    pane.classList.add('on');
    document.querySelectorAll('#ro-tabs .rtab').forEach(function(b) {
      b.classList.remove('on');
      var oc = b.getAttribute('onclick') || '';
      if (oc.indexOf(targetId) >= 0) b.classList.add('on');
    });
  }, 40);
}

// Navigate from standings → all results tab for a specific team
function goToResult(slug, sf) {
  // Switch to All Results main tab
  document.querySelectorAll('.pane').forEach(function(p) { p.classList.remove('on'); });
  document.querySelectorAll('.tab').forEach(function(b) {
    b.classList.remove('on');
    if (b.textContent.trim().toLowerCase() === 'all results') b.classList.add('on');
  });
  var rPane = document.getElementById('allresults');
  if (rPane) rPane.classList.add('on');
  // Switch subflight
  if (sf) {
    var sfBtn = document.querySelector('#re-sf-tabs .rtab[data-sf="' + sf + '"]');
    if (sfBtn) filterSF(sf, sfBtn, 're-sf-tabs', 're-tabs', 're');
  }
  // Activate the team pane
  setTimeout(function() {
    var targetId = 're-' + slug;
    var pane = document.getElementById(targetId);
    if (!pane) return;
    document.querySelectorAll('[id^="re-"].rpane').forEach(function(p) { p.classList.remove('on'); });
    pane.classList.add('on');
    document.querySelectorAll('#re-tabs .rtab').forEach(function(b) {
      b.classList.remove('on');
      var oc = b.getAttribute('onclick') || '';
      if (oc.indexOf(targetId) >= 0) b.classList.add('on');
    });
  }, 40);
}
"""


ANALYSIS_30 = DATA_DIR / "analysis_30.json"
ANALYSIS_35 = DATA_DIR / "analysis_35.json"


# ---------------------------------------------------------------------------
# Score descriptor + win probability  (matchup page helpers)
# ---------------------------------------------------------------------------

def _score_desc_short(score: str) -> str:
    """Short descriptive label for a score string (used in matchup detail rows)."""
    if not score:
        return ""
    sets = re.findall(r"(\d+)-(\d+)", score)
    if not sets:
        return ""
    has_tb = any((a, b) in [("1", "0"), ("0", "1")] for a, b in sets)
    if len(sets) >= 3 and has_tb:
        return "3-set TB"
    regular = [(int(a), int(b)) for a, b in sets
               if not (int(a) <= 1 and int(b) <= 1 and (int(a) + int(b)) <= 1)]
    if not regular:
        return ""
    mc = min(min(a, b) for a, b in regular)
    if mc == 0:
        return "lopsided"
    if mc <= 1:
        return "dominant"
    if mc <= 2:
        return "dominant"
    if mc == 3:
        return "clear"
    return "tight"


_MX_WIN_PROB_STEPS = [
    (0.40, 0.82), (0.30, 0.75), (0.20, 0.68), (0.10, 0.58), (0.00, 0.50),
    (-0.10, 0.42), (-0.20, 0.32), (-0.30, 0.25), (-0.40, 0.18),
]
_MX_WIN_PROB_FLOOR = 0.12


def _win_prob_gap(gap: float) -> float:
    """Interpolated win probability from rating gap (player − opponent)."""
    gap = round(gap, 4)
    if gap >= _MX_WIN_PROB_STEPS[0][0]:
        return _MX_WIN_PROB_STEPS[0][1]
    if gap < _MX_WIN_PROB_STEPS[-1][0]:
        return _MX_WIN_PROB_FLOOR
    for i in range(len(_MX_WIN_PROB_STEPS) - 1):
        hi_gap, hi_prob = _MX_WIN_PROB_STEPS[i]
        lo_gap, lo_prob = _MX_WIN_PROB_STEPS[i + 1]
        if gap >= lo_gap:
            frac = (gap - lo_gap) / (hi_gap - lo_gap) if hi_gap != lo_gap else 0
            return lo_prob + frac * (hi_prob - lo_prob)
    return _MX_WIN_PROB_FLOOR


def _analysis_tab(ntrp: str) -> str:
    """
    Build the Analysis + Predictions tab from a JSON file.

    Expected JSON structure:
    {
      "sections": [
        {
          "title": "key findings — updated after W4",
          "insights": [
            {"tag": "your team", "tag_class": "you", "content": "Shi + Darian ..."},
            {"tag": "warning", "tag_class": "warn", "content": "LTF next week ..."},
            {"tag": "opponent", "tag_class": "opp", "content": "Red Rock CC #2 ..."}
          ]
        },
        {
          "title": "04/18 — Life Time Fitness/GV",
          "insights": [
            {"tag": "lineup", "tag_class": "", "content": "Predicted lineup: ..."}
          ]
        }
      ]
    }
    """
    path = ANALYSIS_35 if "3.5" in ntrp else ANALYSIS_30
    data = _load(path, {})
    sections = data.get("sections", [])

    if not sections:
        return (
            '<p class="muted" style="padding:1rem">'
            'No analysis content yet. Create '
            f'<code>{path.name}</code> to add insights and predictions.</p>'
        )

    html = ""
    for sec in sections:
        title = sec.get("title", "")
        if title:
            html += f'<p class="sec-title">{_esc(title)}</p>'
        for ins in sec.get("insights", []):
            tag = _esc(ins.get("tag", ""))
            tag_cls = _esc(ins.get("tag_class", ""))
            content = ins.get("content", "")   # allow HTML in content
            html += (
                f'<div class="insight">'
                f'<span class="itag {tag_cls}">{tag}</span>'
                f'{content}'
                f'</div>'
            )

    return html


def _generate_html(ntrp: str, standings: dict, players: list[dict]) -> str:
    year = standings.get("year", "")
    subflights = standings.get("subflights", [])

    warnings = _validate(subflights)
    if warnings:
        for w in warnings:
            print(f"  [VALIDATION] {w}")

    cards_html = _summary_cards(ntrp, year, subflights)
    standings_html = _standings_tab(subflights, warnings)
    rosters_html = _rosters_tab(subflights, players, ntrp)
    players_html = _players_tab(players, ntrp, subflights)
    results_html = _results_tab(subflights, players, sfx=ntrp.replace(".", ""))
    analysis_html = _analysis_tab(ntrp)

    tab_defs = [
        ("standings",  "standings",    standings_html),
        ("rosters",    "team rosters", rosters_html),
        ("allplayers", "all players",  players_html),
        ("allresults", "all results",  results_html),
        ("analysis",   "analysis + predictions", analysis_html),
    ]

    tab_btns = "".join(
        f'<button class="tab{" on" if i==0 else ""}" '
        f'onclick="sw(\'{tid}\',this)">{_esc(lbl)}</button>\n'
        for i, (tid, lbl, _) in enumerate(tab_defs)
    )
    tab_panes = "".join(
        f'<div id="{tid}" class="pane{" on" if i==0 else ""}">{html}</div>\n'
        for i, (tid, _, html) in enumerate(tab_defs)
    )

    # Cross-dashboard link + matchups link
    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"
    other_file = f"women_{other_ntrp.replace('.', '')}.html"
    mx_file = f"matchups_{ntrp.replace('.', '')}.html"
    cross_link = (
        f'<a href="{other_file}" class="cross-link">'
        f'Switch to {_esc(other_ntrp)} Women →</a>'
        f' &nbsp;|&nbsp; '
        f'<a href="{mx_file}" class="cross-link">Singles &amp; Doubles Explorer →</a>'
    )

    n_matches = sum(len(sf.get("matches", [])) for sf in subflights)
    n_pending = sum(1 for sf in subflights for m in sf.get("matches", []) if m.get("pending"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>USTA {_esc(ntrp)} Women {year} – NV Area F</title>
<style>{_CSS}</style>
</head>
<body>

<div class="top-bar">{cross_link}</div>

{cards_html}

<div class="tabs">{tab_btns}</div>

{tab_panes}

<script>{_JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Matchup page builder
# ---------------------------------------------------------------------------

_MATCHUP_CSS = _CSS + """
/* ---- Matchup-page additions ---- */
body { max-width: 1100px; }
.mx-top-bar { display: flex; align-items: center; gap: 14px; margin-bottom: 14px;
              border-bottom: 1px solid #eee; padding-bottom: 10px; }
.mx-page-title { font-size: 14px; font-weight: 600; color: #333; }
.mx-section { margin-bottom: 32px; }
.mx-section-hdr { font-size: 13px; font-weight: 700; color: #333;
                  text-transform: uppercase; letter-spacing: .07em;
                  margin: 0 0 10px 0; padding-bottom: 5px;
                  border-bottom: 2px solid #e8e8e8; }
.mx-controls { display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
               flex-wrap: wrap; }
.mx-search { flex: 1; min-width: 160px; max-width: 300px; padding: 5px 10px;
             border: 1px solid #ddd; border-radius: 20px; font-size: 12px; }
.mc-chip { display: inline-block; width: 18px; height: 18px; border-radius: 50%;
           font-size: 9px; font-weight: 700; line-height: 18px; text-align: center;
           cursor: default; margin-right: 2px; }
.mc-w { background: #EAF3DE; color: #27500A; }
.mc-l { background: #FCEBEB; color: #791F1F; }
.mc-p { background: #f1efe8; color: #888; }
.mx-row { cursor: pointer; }
.mx-row td { vertical-align: middle; white-space: nowrap; }
.mx-row td:first-child { white-space: normal; }   /* player names may wrap */
.mx-row:hover td { background: #f5f7fa; }
/* Clickable team cell */
.mx-team-cell { cursor: pointer; }
.mx-team-cell:hover { color: #1a5276; text-decoration: underline; }
/* Main tabs for singles / doubles */
.mx-tabs { display: flex; gap: 6px; margin-bottom: 14px; }
.mx-tab { padding: 6px 16px; border: 1px solid #ccc; border-radius: 20px;
          font-size: 13px; background: transparent; color: #666;
          cursor: pointer; font-weight: 500; }
.mx-tab.on { background: #eee; color: #222; font-weight: 600; border-color: #999; }
.mx-pane { display: none; }
.mx-pane.on { display: block; }
.mx-exp { font-size: 9px; color: #bbb; margin-left: 4px; transition: transform .15s; }
.mx-exp.open { display: inline-block; transform: rotate(90deg); }
.mx-detail td { background: #fafafa; padding: 6px 10px 10px 10px; }
.mx-matches { display: flex; flex-direction: column; gap: 3px; }
.mx-match-row { display: grid;
  grid-template-columns: 28px 36px 22px 1fr 90px 68px 68px;
  gap: 4px 8px; align-items: center; font-size: 11px;
  padding: 3px 4px; border-radius: 4px; }
.mx-match-row.mx-hi { background: #fffbe6; outline: 1px solid #e6c800; }
.mx-wk { font-size: 10px; font-weight: 700; color: #aaa; }
.mx-win { font-size: 12px; font-weight: 700; }
.mx-win.w { color: #27500A; }
.mx-win.l { color: #791F1F; }
.mx-score { font-size: 11px; font-weight: 600; color: #555; }
.mx-desc { font-size: 10px; color: #888; font-style: italic; }
.mx-odds { font-size: 10px; color: #888; }
.mx-odds.upset { color: #a04000; font-weight: 600; }
.mx-odds.solid { color: #27500A; font-weight: 600; }
.opp-link { color: #1a5276; cursor: pointer; border-radius: 3px; padding: 0 2px; }
.opp-link:hover { background: #e8f0f8; text-decoration: underline; }
.opp-team-label { color: #aaa; font-size: 10px; }
.opp-banner { display: none; align-items: center; gap: 8px; padding: 5px 10px;
              background: #fffbe6; border: 1px solid #e6c800; border-radius: 6px;
              font-size: 11px; margin-bottom: 8px; }
.opp-banner.on { display: flex; }
.opp-banner-clear { cursor: pointer; color: #791F1F; font-weight: 700; }
.min-matches-btns { display: flex; gap: 4px; margin-left: 8px; }
"""

_MATCHUP_JS = """
// ---- expand / collapse detail rows ----
function toggleDetail(id, triggerEl) {
  var det = document.getElementById(id);
  if (!det) return;
  var isOpen = det.style.display !== 'none';
  det.style.display = isOpen ? 'none' : '';
  var exp = triggerEl ? triggerEl.querySelector('.mx-exp') : null;
  if (!exp) {
    // find the corresponding main row
    var mainRow = document.querySelector('[data-det="' + id + '"]');
    if (mainRow) exp = mainRow.querySelector('.mx-exp');
  }
  if (exp) exp.classList.toggle('open', !isOpen);
}

// ---- SF filter (singles and doubles share the same pattern) ----
var _mxSF = { singles: 'all', doubles: 'all' };
function filterMxSF(sf, btn, section) {
  _mxSF[section] = sf;
  var grp = btn.parentElement;
  grp.querySelectorAll('.rtab').forEach(function(b) { b.classList.remove('on'); });
  btn.classList.add('on');
  _applyMxFilters(section);
}

// ---- search filter ----
function filterMxSearch(section) {
  _applyMxFilters(section);
}

// ---- opponent filter ----
var _oppKey = { singles: '', doubles: '' };
// Track which detail rows were auto-expanded by the opp filter (so we can collapse them on clear)
var _autoExpanded = { singles: [], doubles: [] };

function filterByOpp(key, display, section) {
  // Clicking the same opp again → clear the filter
  if (_oppKey[section] === key) {
    clearOppFilter(section);
    return;
  }
  // If switching from a different opp filter, collapse the old auto-expanded rows first
  _collapseAutoExpanded(section);

  _oppKey[section] = key;
  _autoExpanded[section] = [];

  var banner = document.getElementById(section + '-opp-banner');
  if (banner) {
    banner.classList.add('on');
    var lbl = banner.querySelector('.opp-banner-lbl');
    if (lbl) lbl.textContent = 'Filtered: players who faced ' + display;
  }
  _applyMxFilters(section);
  // auto-expand matching rows and highlight the specific match
  var tbody = document.getElementById(section + '-tbody');
  if (!tbody) return;
  tbody.querySelectorAll('tr.mx-row').forEach(function(row) {
    var keys = (row.dataset.oppKeys || '').split(' ');
    if (keys.indexOf(key) >= 0) {
      var detId = row.dataset.det;
      var det = document.getElementById(detId);
      if (det && det.style.display === 'none') {
        det.style.display = '';
        var exp = row.querySelector('.mx-exp');
        if (exp) exp.classList.add('open');
        _autoExpanded[section].push(detId);  // remember this was auto-expanded
      }
      // highlight matching match rows
      if (det) {
        det.querySelectorAll('.mx-match-row').forEach(function(mr) {
          mr.classList.toggle('mx-hi', mr.dataset.oppKey === key);
        });
      }
    }
  });
}

function _collapseAutoExpanded(section) {
  (_autoExpanded[section] || []).forEach(function(detId) {
    var det = document.getElementById(detId);
    if (det) {
      det.style.display = 'none';
      var mainRow = document.querySelector('[data-det="' + detId + '"]');
      if (mainRow) {
        var exp = mainRow.querySelector('.mx-exp');
        if (exp) exp.classList.remove('open');
      }
    }
  });
  _autoExpanded[section] = [];
}

function clearOppFilter(section) {
  _oppKey[section] = '';
  var banner = document.getElementById(section + '-opp-banner');
  if (banner) banner.classList.remove('on');
  var tbody = document.getElementById(section + '-tbody');
  if (tbody) {
    tbody.querySelectorAll('.mx-match-row').forEach(function(mr) {
      mr.classList.remove('mx-hi');
    });
  }
  _collapseAutoExpanded(section);
  _applyMxFilters(section);
}

// ---- main tab switcher (Singles / Doubles) ----
function switchMxTab(section, btn) {
  document.querySelectorAll('.mx-tab').forEach(function(b) { b.classList.remove('on'); });
  document.querySelectorAll('.mx-pane').forEach(function(p) { p.classList.remove('on'); });
  btn.classList.add('on');
  var pane = document.getElementById('mx-pane-' + section);
  if (pane) pane.classList.add('on');
}

// ---- team cell click → filter by team ----
function filterByTeam(abbrevTeam, section, event) {
  event.stopPropagation();  // don't also expand the row
  var searchEl = document.getElementById(section + '-search');
  if (!searchEl) return;
  // Toggle: clicking same team again clears the filter
  if (searchEl.value.toLowerCase() === abbrevTeam.toLowerCase()) {
    searchEl.value = '';
  } else {
    searchEl.value = abbrevTeam;
  }
  filterMxSearch(section);
}

// ---- default descending for quality columns (first click shows best first) ----
// _sortDir state is true → next click will be descending (dir = !true = false → bn-an)
['s3','s4','s5','s6','s7','d3','d4','d5'].forEach(function(k) { _sortDir[k] = true; });

function _applyMxFilters(section) {
  var sf = _mxSF[section];
  var oppK = _oppKey[section];
  var searchEl = document.getElementById(section + '-search');
  var q = searchEl ? searchEl.value.toLowerCase() : '';
  var tbody = document.getElementById(section + '-tbody');
  if (!tbody) return;
  var rows = tbody.querySelectorAll('tr.mx-row');
  rows.forEach(function(row) {
    var sfOk = sf === 'all' || row.dataset.sf === sf;
    var oppOk = !oppK || (row.dataset.oppKeys || '').split(' ').indexOf(oppK) >= 0;
    var textOk = !q || (row.dataset.searchText || '').indexOf(q) >= 0;
    var mmOk = section !== 'doubles' || parseInt(row.dataset.matchCount || '0') >= _dblMinMatches;
    var vis = sfOk && oppOk && textOk && mmOk;
    row.style.display = vis ? '' : 'none';
    // also hide detail row when main row is hidden
    var det = document.getElementById(row.dataset.det);
    if (det && !vis) det.style.display = 'none';
  });
}

// ---- sorting ----
var _mxSortDir = {};
function sortMx(tbodyId, col, dirKey) {
  _sortTable('#' + tbodyId, col, dirKey);
  // after sorting, re-pair each main row with its detail row
  var tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  var rows = Array.from(tbody.querySelectorAll('tr'));
  // rebuild order: each mx-row followed immediately by its det row
  var mxRows = rows.filter(function(r) { return r.classList.contains('mx-row'); });
  mxRows.forEach(function(mr) {
    tbody.appendChild(mr);
    var det = document.getElementById(mr.dataset.det);
    if (det) tbody.appendChild(det);
  });
}

// ---- doubles min-matches filter ----
var _dblMinMatches = 2;
function setMinMatches(n, btn) {
  _dblMinMatches = n;
  btn.parentElement.querySelectorAll('.rtab').forEach(function(b) { b.classList.remove('on'); });
  btn.classList.add('on');
  _applyMxFilters('doubles');
}
"""


def _build_matchup_page(ntrp: str, standings: dict, players: list[dict]) -> str:
    """Generate matchups_{sfx}.html — singles + doubles explorer."""
    sfx = ntrp.replace(".", "")
    year = standings.get("year", "")
    subflights = standings.get("subflights", [])

    # ── lookup tables from players ──────────────────────────────────────────
    _baseline_by_name: dict[str, float] = {}
    _new_by_name: dict[str, float] = {}
    _timeline_by_name: dict[str, dict[str, float]] = {}
    _team_by_name: dict[str, str] = {}

    team_to_sf: dict[str, str] = {}
    for sf_obj in subflights:
        lbl = sf_obj.get("flight_label", "")
        for t in sf_obj.get("teams", []):
            team_to_sf[t.get("team_name", "")] = lbl

    for p in players:
        norm = re.sub(r"\s+", " ", (p.get("name") or "").strip().lower())
        if not norm:
            continue
        baseline = p.get("dynamic_rating_baseline")
        if baseline is not None:
            try:
                _baseline_by_name[norm] = float(baseline)
            except (ValueError, TypeError):
                pass
        curr = p.get(f"rating_{sfx}")
        if curr is not None:
            try:
                _new_by_name[norm] = float(curr)
            except (ValueError, TypeError):
                pass
        tl = p.get(f"rating_timeline_{sfx}")
        if tl and isinstance(tl, dict):
            _timeline_by_name[norm] = {k: float(v) for k, v in tl.items()}
        team_val = (p.get(f"team_{sfx}") or p.get("team") or "")
        if team_val:
            _team_by_name[norm] = team_val

    def _pit_r(nkey: str, date: str) -> Optional[float]:
        nkey = re.sub(r"\s+", " ", nkey.strip().lower())
        if nkey in _timeline_by_name:
            tl = _timeline_by_name[nkey]
            if date in tl:
                return tl[date]
            mk = _date_sort_key(date)
            prior = [(k, v) for k, v in tl.items() if _date_sort_key(k) < mk]
            if prior:
                return max(prior, key=lambda kv: _date_sort_key(kv[0]))[1]
            return _baseline_by_name.get(nkey)
        return _new_by_name.get(nkey) or _baseline_by_name.get(nkey)

    # ── week label map ───────────────────────────────────────────────────────
    all_dates: set[str] = set()
    for sf in subflights:
        for m in sf.get("matches", []):
            d = m.get("date", "")
            if d:
                all_dates.add(d)
    sorted_dates = sorted(all_dates, key=_date_sort_key)
    week_label: dict[str, str] = {d: f"W{i+1}" for i, d in enumerate(sorted_dates)}

    # ── walk all lines ───────────────────────────────────────────────────────
    singles_by_player: dict[str, dict] = {}      # norm_name → player data
    doubles_by_pair: dict[frozenset, dict] = {}  # frozenset(norm1, norm2) → pair data

    for sf in subflights:
        sf_label = sf.get("flight_label", "")
        for m in sf.get("matches", []):
            if m.get("pending"):
                continue
            home_team = m.get("home_team", "")
            away_team = m.get("away_team", "")
            date = m.get("date", "")
            wk = week_label.get(date, "")

            for ln in m.get("lines", []):
                line_str = ln.get("line", "")
                line_label = _line_label_short(line_str)
                is_singles = "Singles" in line_str
                score = ln.get("score", "")
                winner_team = (ln.get("winner_team") or "").upper()

                ph = (ln.get("players_home") or "").strip()
                pa = (ln.get("players_away") or "").strip()
                if ph.upper() in ("", "N/A"):
                    ph = ""
                if pa.upper() in ("", "N/A"):
                    pa = ""

                sides = [
                    (ph, home_team, pa, away_team),
                    (pa, away_team, ph, home_team),
                ]

                if is_singles:
                    for side_raw, side_team, opp_raw, opp_team in sides:
                        if not side_raw:
                            continue
                        # Normalise all-caps names from scorecards to title case
                        raw_name = side_raw.strip()
                        player_name = raw_name.title() if raw_name.isupper() else raw_name
                        norm = re.sub(r"\s+", " ", player_name.lower())
                        opp_norm = re.sub(r"\s+", " ", (opp_raw or "").lower())

                        # Use _team_by_name for win detection — handles scorecard swaps
                        # where players_home/away columns may be swapped but winner_team
                        # always correctly names the winning team.
                        actual_team = (_team_by_name.get(norm) or side_team).upper()
                        won = (winner_team == actual_team) if winner_team else None
                        if won is None:
                            continue
                        # Opponent's actual team (also handles swapped scorecards)
                        actual_opp_team = _team_by_name.get(opp_norm) or opp_team

                        pit = _pit_r(norm, date)
                        opp_pit = _pit_r(opp_norm, date)
                        desc = _score_desc_short(score)
                        exp_prob: Optional[float] = None
                        if pit is not None and opp_pit is not None:
                            exp_prob = _win_prob_gap(pit - opp_pit)

                        if norm not in singles_by_player:
                            team = _team_by_name.get(norm) or side_team
                            psf = team_to_sf.get(team, sf_label)
                            singles_by_player[norm] = {
                                "name": player_name,
                                "team": team,
                                "sf": psf,
                                "baseline": _baseline_by_name.get(norm),
                                "rating": _new_by_name.get(norm),
                                "matches": [],
                            }

                        opp_key = re.sub(r"[^a-z0-9]+", "-",
                                         opp_raw.strip().lower()).strip("-")
                        singles_by_player[norm]["matches"].append({
                            "date": date,
                            "week": wk,
                            "line": line_label,
                            "won": won,
                            "score": score,
                            "desc": desc,
                            "opp_name": opp_raw,
                            "opp_key": opp_key,
                            "opp_team": actual_opp_team,
                            "opp_rating": opp_pit,
                            "exp_prob": exp_prob,
                        })

                else:  # doubles
                    for side_raw, side_team, opp_raw, opp_team in sides:
                        if not side_raw:
                            continue
                        parts = [x.strip() for x in side_raw.split("/") if x.strip()]
                        if len(parts) != 2:
                            continue
                        # Normalise display names: title-case all-caps names from scorecards
                        p1_name = parts[0].title() if parts[0].isupper() else parts[0]
                        p2_name = parts[1].title() if parts[1].isupper() else parts[1]
                        n1 = re.sub(r"\s+", " ", p1_name.lower())
                        n2 = re.sub(r"\s+", " ", p2_name.lower())
                        # Use _team_by_name for win detection (handles swapped scorecards)
                        actual_d_team = (
                            _team_by_name.get(n1) or _team_by_name.get(n2) or side_team
                        ).upper()
                        won = (winner_team == actual_d_team) if winner_team else None
                        if won is None:
                            continue

                        pair_key = frozenset([n1, n2])

                        opp_parts = [x.strip() for x in (opp_raw or "").split("/")
                                     if x.strip()]
                        opp_normed = [re.sub(r"\s+", " ", x.lower()) for x in opp_parts]
                        opp_ratings = [_pit_r(on, date) for on in opp_normed]
                        opp_ratings_clean = [r for r in opp_ratings if r is not None]
                        opp_avg = (sum(opp_ratings_clean) / len(opp_ratings_clean)
                                   if opp_ratings_clean else None)
                        opp_key = re.sub(r"[^a-z0-9]+", "-",
                                         (opp_raw or "").strip().lower()).strip("-")
                        # Opponent's actual team (resolves scorecard swap)
                        actual_opp_d_team = (
                            _team_by_name.get(opp_normed[0]) if opp_normed else None
                        ) or opp_team

                        desc = _score_desc_short(score)
                        r1 = _pit_r(n1, date)
                        r2 = _pit_r(n2, date)

                        if pair_key not in doubles_by_pair:
                            team = _team_by_name.get(n1) or _team_by_name.get(n2) or side_team
                            psf = team_to_sf.get(team, sf_label)
                            doubles_by_pair[pair_key] = {
                                "p1": p1_name, "p2": p2_name,
                                "team": team, "sf": psf,
                                "r1": _new_by_name.get(n1),
                                "r2": _new_by_name.get(n2),
                                "bl1": _baseline_by_name.get(n1),
                                "bl2": _baseline_by_name.get(n2),
                                "matches": [],
                            }

                        doubles_by_pair[pair_key]["matches"].append({
                            "date": date,
                            "week": wk,
                            "line": line_label,
                            "won": won,
                            "score": score,
                            "desc": desc,
                            "opp_names": opp_raw,
                            "opp_key": opp_key,
                            "opp_team": actual_opp_d_team,
                            "opp_avg_rating": opp_avg,
                        })

    # ── compute aggregates ───────────────────────────────────────────────────
    singles_list = sorted(
        singles_by_player.values(),
        key=lambda p: -(p.get("rating") or p.get("baseline") or 0)
    )

    doubles_list = []
    for pdata in doubles_by_pair.values():
        if len(pdata["matches"]) < 2:
            continue
        r1 = pdata.get("r1") or pdata.get("bl1") or 0.0
        r2 = pdata.get("r2") or pdata.get("bl2") or 0.0
        bl1 = pdata.get("bl1") or r1
        bl2 = pdata.get("bl2") or r2
        pdata["avg_rating"] = (r1 + r2) / 2 if r1 and r2 else (r1 or r2)
        pdata["avg_baseline"] = (bl1 + bl2) / 2 if bl1 and bl2 else (bl1 or bl2)
        doubles_list.append(pdata)
    doubles_list.sort(key=lambda p: -p.get("avg_rating", 0))

    # ── HTML generators ──────────────────────────────────────────────────────
    def _odds_html(exp_prob: Optional[float], won: bool) -> str:
        if exp_prob is None:
            return ""
        pct = int(round(exp_prob * 100))
        if won:
            if exp_prob < 0.40:
                return f'<span class="mx-odds upset">{pct}% odds — upset!</span>'
            elif exp_prob >= 0.65:
                return f'<span class="mx-odds solid">{pct}% fav</span>'
            return f'<span class="mx-odds">{pct}% fav</span>'
        else:
            if exp_prob >= 0.65:
                return f'<span class="mx-odds upset">lost as {pct}% fav</span>'
            return f'<span class="mx-odds">{pct}% chances</span>'

    def _singles_rows(plist) -> str:
        rows = ""
        for i, p in enumerate(plist):
            matches = sorted(p["matches"], key=lambda x: _date_sort_key(x["date"]))
            wins = sum(1 for mx in matches if mx["won"])
            losses = len(matches) - wins
            win_pct = int(round(wins / len(matches) * 100)) if matches else 0
            avg_opp = None
            opp_rs = [mx["opp_rating"] for mx in matches if mx["opp_rating"] is not None]
            if opp_rs:
                avg_opp = sum(opp_rs) / len(opp_rs)

            opp_keys_set = " ".join(
                mx["opp_key"] for mx in matches if mx.get("opp_key"))
            abbrev_team = _abbrev_team(p["team"])
            search_text = (
                p["name"] + " " + p["team"] + " " + abbrev_team
            ).lower()
            det_id = f"sdet-{i}"

            # Mini chips
            chips = ""
            for mx in matches:
                cls = "mc-w" if mx["won"] else "mc-l"
                lbl = "W" if mx["won"] else "L"
                tip = (f"{mx['week']} {mx['line']}: vs {mx['opp_name']} {mx['score']}"
                       .replace('"', "&quot;"))
                chips += f'<span class="mc-chip {cls}" title="{_esc(tip)}">{lbl}</span>'

            # Rating cell
            base = p.get("baseline")
            curr = p.get("rating")
            rat_html = _rating_span(curr, base, ntrp)

            wl_sort = wins * 100 - losses
            main_row = (
                f'<tr class="mx-row" data-sf="{_esc(p["sf"])}" '
                f'data-opp-keys="{_esc(opp_keys_set)}" '
                f'data-search-text="{_esc(search_text)}" '
                f'data-det="{det_id}" '
                f'onclick="toggleDetail(\'{det_id}\',this)">'
                f'<td class="pname">{_esc(p["name"])} '
                f'<span class="mx-exp">▸</span></td>'
                f'<td class="mx-team-cell" '
                f'onclick="filterByTeam(\'{_esc(abbrev_team)}\',\'singles\',event)">'
                f'{_esc(abbrev_team)}</td>'
                f'<td><span class="sf-pill">{_esc(p["sf"])}</span></td>'
                f'<td data-sort="{_fmt_rating(base)}">{_esc(_fmt_rating(base))}</td>'
                f'<td data-sort="{_fmt_rating(curr)}">{rat_html}</td>'
                f'<td data-sort="{wl_sort}" style="white-space:nowrap">{wins}–{losses}</td>'
                f'<td data-sort="{win_pct}">{win_pct}%</td>'
                f'<td data-sort="{f"{avg_opp:.4f}" if avg_opp else "0"}">'
                f'{_fmt_rating(avg_opp)}</td>'
                f'<td>{chips}</td>'
                f'</tr>\n'
            )

            # Detail row
            match_rows = ""
            for mx in matches:
                win_cls = "w" if mx["won"] else "l"
                win_sym = "✓" if mx["won"] else "✗"
                line_cls = _LINE_PILL_COLORS.get(mx["line"], "pill-d1")
                opp_r_str = (f"{mx['opp_rating']:.2f}" if mx["opp_rating"] is not None
                             else "")
                opp_info = f'({opp_r_str}, {_abbrev_team(mx["opp_team"])})' if opp_r_str else ""
                odds_h = _odds_html(mx.get("exp_prob"), mx["won"])
                opp_key = mx.get("opp_key", "")
                match_rows += (
                    f'<div class="mx-match-row" data-opp-key="{_esc(opp_key)}">'
                    f'<span class="mx-wk">{_esc(mx["week"])}</span>'
                    f'<span class="line-pill {line_cls}">{_esc(mx["line"])}</span>'
                    f'<span class="mx-win {win_cls}">{win_sym}</span>'
                    f'<span>vs <span class="opp-link" '
                    f'onclick="filterByOpp(\'{_esc(opp_key)}\',\'{_esc(mx["opp_name"])}\',\'singles\');event.stopPropagation()">'
                    f'{_esc(mx["opp_name"])}</span> '
                    f'<span class="opp-team-label">{_esc(opp_info)}</span></span>'
                    f'<span class="mx-score">{_esc(mx["score"])}</span>'
                    f'<span class="mx-desc">{_esc(mx["desc"])}</span>'
                    f'{odds_h}'
                    f'</div>\n'
                )

            det_row = (
                f'<tr id="{det_id}" class="mx-detail" style="display:none">'
                f'<td colspan="9" class="mx-det-cell">'
                f'<div class="mx-matches">{match_rows}</div>'
                f'</td></tr>\n'
            )
            rows += main_row + det_row
        return rows

    def _doubles_rows(dlist) -> str:
        rows = ""
        for i, p in enumerate(dlist):
            matches = sorted(p["matches"], key=lambda x: _date_sort_key(x["date"]))
            wins = sum(1 for mx in matches if mx["won"])
            losses = len(matches) - wins
            win_pct = int(round(wins / len(matches) * 100)) if matches else 0
            nm = len(matches)

            # Lines pills
            from collections import Counter
            line_counts = Counter(mx["line"] for mx in matches)
            lines_html = "".join(
                f'<span class="line-pill {_LINE_PILL_COLORS.get(l, "pill-d1")}">'
                f'{_esc(l)}{"x" + str(c) if c > 1 else ""}</span>'
                for l, c in sorted(line_counts.items())
            )

            opp_keys_set = " ".join(
                mx["opp_key"] for mx in matches if mx.get("opp_key"))
            abbrev_team_d = _abbrev_team(p["team"])
            search_text = (
                p["p1"] + " " + p["p2"] + " " + p["team"] + " " + abbrev_team_d
            ).lower()
            det_id = f"ddet-{i}"

            chips = ""
            for mx in matches:
                cls = "mc-w" if mx["won"] else "mc-l"
                lbl = "W" if mx["won"] else "L"
                tip = (f"{mx['week']} {mx['line']}: vs {mx['opp_names']} {mx['score']}"
                       .replace('"', "&quot;"))
                chips += f'<span class="mc-chip {cls}" title="{_esc(tip)}">{lbl}</span>'

            avg_r = p.get("avg_rating", 0)
            avg_bl = p.get("avg_baseline", 0)
            r1 = p.get("r1")
            r2 = p.get("r2")
            bl1 = p.get("bl1")
            bl2 = p.get("bl2")

            if avg_r and avg_bl:
                if avg_r > avg_bl + 0.005:
                    avg_cls = "ru"
                elif avg_r < avg_bl - 0.005:
                    avg_cls = "rd"
                else:
                    avg_cls = "rn"
                avg_html = f'<span class="{avg_cls}">{avg_r:.2f}</span>'
            else:
                avg_html = _fmt_rating(avg_r)

            wl_sort = wins * 100 - losses

            main_row = (
                f'<tr class="mx-row" data-sf="{_esc(p["sf"])}" '
                f'data-opp-keys="{_esc(opp_keys_set)}" '
                f'data-search-text="{_esc(search_text)}" '
                f'data-match-count="{nm}" '
                f'data-det="{det_id}" '
                f'onclick="toggleDetail(\'{det_id}\',this)">'
                f'<td class="pname">{_esc(p["p1"])} / {_esc(p["p2"])} '
                f'<span class="mx-exp">▸</span></td>'
                f'<td class="mx-team-cell" '
                f'onclick="filterByTeam(\'{_esc(abbrev_team_d)}\',\'doubles\',event)">'
                f'{_esc(abbrev_team_d)}</td>'
                f'<td><span class="sf-pill">{_esc(p["sf"])}</span></td>'
                f'<td data-sort="{avg_r:.4f}">{avg_html}</td>'
                f'<td data-sort="{wl_sort}" style="white-space:nowrap">{wins}–{losses}</td>'
                f'<td data-sort="{win_pct}">{win_pct}%</td>'
                f'<td>{lines_html}</td>'
                f'<td>{chips}</td>'
                f'</tr>\n'
            )

            match_rows = ""
            for mx in matches:
                win_cls = "w" if mx["won"] else "l"
                win_sym = "✓" if mx["won"] else "✗"
                line_cls = _LINE_PILL_COLORS.get(mx["line"], "pill-d1")
                opp_avg = mx.get("opp_avg_rating")
                opp_r_str = f"{opp_avg:.2f}" if opp_avg is not None else ""
                opp_info = f'({opp_r_str} avg, {_abbrev_team(mx["opp_team"])})' if opp_r_str else ""
                opp_key = mx.get("opp_key", "")
                match_rows += (
                    f'<div class="mx-match-row" data-opp-key="{_esc(opp_key)}">'
                    f'<span class="mx-wk">{_esc(mx["week"])}</span>'
                    f'<span class="line-pill {line_cls}">{_esc(mx["line"])}</span>'
                    f'<span class="mx-win {win_cls}">{win_sym}</span>'
                    f'<span>vs <span class="opp-link" '
                    f'onclick="filterByOpp(\'{_esc(opp_key)}\',\'{_esc(mx["opp_names"])}\',\'doubles\');event.stopPropagation()">'
                    f'{_esc(mx["opp_names"])}</span> '
                    f'<span class="opp-team-label">{_esc(opp_info)}</span></span>'
                    f'<span class="mx-score">{_esc(mx["score"])}</span>'
                    f'<span class="mx-desc">{_esc(mx["desc"])}</span>'
                    f'</div>\n'
                )

            det_row = (
                f'<tr id="{det_id}" class="mx-detail" style="display:none">'
                f'<td colspan="8" class="mx-det-cell">'
                f'<div class="mx-matches">{match_rows}</div>'
                f'</td></tr>\n'
            )
            rows += main_row + det_row
        return rows

    s_rows = _singles_rows(singles_list)
    d_rows = _doubles_rows(doubles_list)

    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"
    other_mx = f"matchups_{other_ntrp.replace('.', '')}.html"
    main_dash = f"women_{sfx}.html"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Singles &amp; Doubles Explorer — {_esc(ntrp)} Women {year}</title>
<style>{_MATCHUP_CSS}</style>
</head>
<body>

<div class="mx-top-bar">
  <a href="{main_dash}" class="cross-link">← Division Dashboard</a>
  <span class="mx-page-title">Singles &amp; Doubles Explorer — {_esc(ntrp)} Women {year}</span>
  <a href="{other_mx}" class="cross-link" style="margin-left:auto">{_esc(other_ntrp)} Explorer →</a>
</div>

<div class="mx-tabs">
  <button class="mx-tab on" onclick="switchMxTab('singles',this)">Singles</button>
  <button class="mx-tab" onclick="switchMxTab('doubles',this)">Doubles Pairs</button>
</div>

<!-- ========= SINGLES ========= -->
<div id="mx-pane-singles" class="mx-pane on">
<section class="mx-section" id="singles-section">
  <div id="singles-opp-banner" class="opp-banner">
    <span class="opp-banner-lbl"></span>
    <span class="opp-banner-clear" onclick="clearOppFilter('singles')">✕ clear</span>
  </div>
  <div class="mx-controls">
    <div class="sf-filter-btns">
      <button class="rtab on" onclick="filterMxSF('all',this,'singles')">All</button>
      <button class="rtab" onclick="filterMxSF('A',this,'singles')">A</button>
      <button class="rtab" onclick="filterMxSF('B',this,'singles')">B</button>
    </div>
    <input class="mx-search" id="singles-search" placeholder="Search player or team…"
           oninput="filterMxSearch('singles')">
  </div>
  <table>
    <thead><tr>
      <th class="sortable" onclick="sortMx('singles-tbody',0,'s0')">Player ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',1,'s1')">Team ↕</th>
      <th>SF</th>
      <th class="sortable" onclick="sortMx('singles-tbody',3,'s3')">Base ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',4,'s4')">Rating ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',5,'s5')">W–L ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',6,'s6')">Win% ↕</th>
      <th class="sortable" onclick="sortMx('singles-tbody',7,'s7')">Avg Opp ↕</th>
      <th>Matches</th>
    </tr></thead>
    <tbody id="singles-tbody">{s_rows}</tbody>
  </table>
</section>
</div>

<!-- ========= DOUBLES ========= -->
<div id="mx-pane-doubles" class="mx-pane">
<section class="mx-section" id="doubles-section">
  <div id="doubles-opp-banner" class="opp-banner">
    <span class="opp-banner-lbl"></span>
    <span class="opp-banner-clear" onclick="clearOppFilter('doubles')">✕ clear</span>
  </div>
  <div class="mx-controls">
    <div class="sf-filter-btns">
      <button class="rtab on" onclick="filterMxSF('all',this,'doubles')">All</button>
      <button class="rtab" onclick="filterMxSF('A',this,'doubles')">A</button>
      <button class="rtab" onclick="filterMxSF('B',this,'doubles')">B</button>
    </div>
    <input class="mx-search" id="doubles-search" placeholder="Search players or team…"
           oninput="filterMxSearch('doubles')">
    <div class="min-matches-btns">
      <span style="font-size:11px;color:#888">min matches:</span>
      <button class="rtab on" onclick="setMinMatches(2,this)">2+</button>
      <button class="rtab" onclick="setMinMatches(3,this)">3+</button>
      <button class="rtab" onclick="setMinMatches(4,this)">4+</button>
    </div>
  </div>
  <table>
    <thead><tr>
      <th class="sortable" onclick="sortMx('doubles-tbody',0,'d0')">Players ↕</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',1,'d1')">Team ↕</th>
      <th>SF</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',3,'d3')">Avg Rating ↕</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',4,'d4')">W–L ↕</th>
      <th class="sortable" onclick="sortMx('doubles-tbody',5,'d5')">Win% ↕</th>
      <th>Lines</th>
      <th>Matches</th>
    </tr></thead>
    <tbody id="doubles-tbody">{d_rows}</tbody>
  </table>
</section>
</div>

<script>{_JS}{_MATCHUP_JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_dashboards() -> dict:
    players = _load(PLAYERS_JSON, [])
    results = {}

    for ntrp, standings_path, out_path, mx_path in [
        ("3.0", STANDINGS_30, Path("women_30.html"), Path("matchups_30.html")),
        ("3.5", STANDINGS_35, Path("women_35.html"), Path("matchups_35.html")),
    ]:
        standings = _load(standings_path, {"ntrp": ntrp, "year": 2026, "subflights": []})
        html = _generate_html(ntrp, standings, players)
        out_path.write_text(html, encoding="utf-8")
        mx_html = _build_matchup_page(ntrp, standings, players)
        mx_path.write_text(mx_html, encoding="utf-8")
        n = len([p for p in players if p.get("division", "").startswith(ntrp)])
        n_matches = sum(
            len(sf.get("matches", []))
            for sf in standings.get("subflights", [])
        )
        print(f"  [html] {out_path}  ({n} players, {n_matches} matches)")
        print(f"  [html] {mx_path}  (matchups explorer)")
        results[str(out_path)] = n

    return results


if __name__ == "__main__":
    build_dashboards()
