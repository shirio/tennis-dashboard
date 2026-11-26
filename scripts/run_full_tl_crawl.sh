#!/bin/zsh
# Full TennisLink team crawl for everyone on the NV NTRP list:
# women 2026, women 2025, men 2026, men 2025, then the identity pass.
# Each phase is resumable; a failed phase (logout, block) is retried with a
# fresh login. Headed Chromium: TennisLink's login fails headless.
cd "$(dirname "$0")/.."
PY=${PY:-$HOME/.venvs/tennis-dashboard/bin/python}
for phase in "F 2026" "F 2025" "M 2026" "M 2025"; do
  g=${phase% *}; y=${phase#* }
  for attempt in 1 2 3 4 5 6; do
    echo "=== phase $g $y attempt $attempt $(date '+%F %T')"
    if $PY -u scrapers/scrape_tl_teams.py --ntrp-list --gender $g --year $y; then
      echo "=== phase $g $y done $(date '+%F %T')"; break
    fi
    sleep $((attempt * 120))
  done
done
for y in 2026 2025; do
  echo "=== resolve $y $(date '+%F %T')"
  $PY -u scrapers/resolve_tl_players.py --year $y
done
echo "=== ALL DONE $(date '+%F %T')"
