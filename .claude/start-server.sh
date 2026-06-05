#!/bin/bash
# Starts the dashboard server as a detached daemon, immune to the preview
# tool's process lifecycle. The real node process survives pane open/close.
cd "$(dirname "$0")/.."

# If the server is already up and responding, nothing to do.
if curl -sf http://localhost:8080/ > /dev/null 2>&1; then
  :  # already running — fall through to exec sleep infinity
else
  # Kill any stale process occupying the port (not responding to HTTP).
  lsof -ti:8080 | xargs kill -9 2>/dev/null || true
  sleep 0.2

  # Launch the server fully detached: nohup + disown removes it from this
  # shell's job list so it survives when the preview tool kills this script.
  nohup node .claude/server.js >> /tmp/tennis-dashboard.log 2>&1 &
  disown $!

  # Wait up to 5 seconds for the server to become ready.
  for i in $(seq 1 50); do
    sleep 0.1
    if curl -sf http://localhost:8080/ > /dev/null 2>&1; then break; fi
  done
fi

# Hand off to sleep infinity — this is the process the preview tool tracks.
# When the pane closes and the tool kills this pid, the node server is
# already fully detached and keeps running unaffected.
exec sleep infinity
