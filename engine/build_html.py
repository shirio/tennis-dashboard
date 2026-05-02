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

    # Cross-dashboard link
    other_ntrp = "3.5" if ntrp == "3.0" else "3.0"
    other_file = f"women_{other_ntrp.replace('.', '')}.html"
    cross_link = (
        f'<a href="{other_file}" class="cross-link">'
        f'Switch to {_esc(other_ntrp)} Women →</a>'
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
# Entry point
# ---------------------------------------------------------------------------

def build_dashboards() -> dict:
    players = _load(PLAYERS_JSON, [])
    results = {}

    for ntrp, standings_path, out_path in [
        ("3.0", STANDINGS_30, Path("women_30.html")),
        ("3.5", STANDINGS_35, Path("women_35.html")),
    ]:
        standings = _load(standings_path, {"ntrp": ntrp, "year": 2026, "subflights": []})
        html = _generate_html(ntrp, standings, players)
        out_path.write_text(html, encoding="utf-8")
        n = len([p for p in players if p.get("division", "").startswith(ntrp)])
        n_matches = sum(
            len(sf.get("matches", []))
            for sf in standings.get("subflights", [])
        )
        print(f"  [html] {out_path}  ({n} players, {n_matches} matches)")
        results[str(out_path)] = n

    return results


if __name__ == "__main__":
    build_dashboards()
