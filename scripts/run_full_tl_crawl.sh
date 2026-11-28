#!/bin/zsh
# Full TennisLink team crawl for everyone on the NV NTRP list, then the
# identity pass. Phases default to women 2026, women 2025, men 2026, men 2025;
# pass others as arguments, e.g.  run_full_tl_crawl.sh "F 2024"
# An optional third word names another district's list:  "F 2026 data/ntrp_ut_2026.json"
# Each phase is resumable; a failed phase (logout, block) is retried with a
# fresh login. Headed Chromium: TennisLink's login fails headless.
# Run it detached (nohup ... &) so it outlives the Claude session.
cd "$(dirname "$0")/.."
PY=${PY:-$HOME/.venvs/tennis-dashboard/bin/python}
phases=("$@")
(( ${#phases} )) || phases=("F 2026" "F 2025" "M 2026" "M 2025")
years=()
for phase in "${phases[@]}"; do
  parts=(${=phase}); g=$parts[1]; y=$parts[2]; lst=$parts[3]
  listarg=(); [[ -n $lst ]] && listarg=(--list $lst)
  years+=($y)
  for attempt in 1 2 3 4 5 6; do
    echo "=== phase $g $y attempt $attempt $(date '+%F %T')"
    $PY -u scrapers/scrape_tl_teams.py --ntrp-list --gender $g --year $y $listarg
    rc=$?
    if (( rc == 0 )); then
      echo "=== phase $g $y done $(date '+%F %T')"; break
    elif (( rc == 3 )); then
      # USTA wants a CAPTCHA: stop (never retry logins into it) and ask for a hand sign-in
      echo "=== CAPTCHA: sign in with scrapers/tl_signin.py, then rerun $(date '+%F %T')"
      osascript -e 'display notification "USTA wants a sign-in with a code. Run scrapers/tl_signin.py." with title "TennisLink crawl paused"' 2>/dev/null
      exit 3
    fi
    sleep $((attempt * 120))
  done
done
for y in ${(u)years}; do
  echo "=== resolve $y $(date '+%F %T')"
  $PY -u scrapers/resolve_tl_players.py --year $y
done
echo "=== ALL DONE $(date '+%F %T')"
osascript -e "display notification \"Phases: ${phases[*]}\" with title \"TennisLink crawl finished\"" 2>/dev/null
