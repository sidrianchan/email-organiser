#!/bin/bash
# Install (or reinstall) the schedule: 08:00 / 18:00 summary + 15-minute quick check.
#
# The schedule runs a STABLE copy of the code (a git worktree on the `live` branch),
# so half-finished work on a feature branch never runs unattended.
#
#   scripts/install-schedule.sh            # set up the stable copy + schedule
#   scripts/update-live.sh                 # later: move the stable copy to the latest main
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIVE="${LIVE_DIR:-$(dirname "$REPO")/email-organiser-live}"
DATA="${EMAIL_ORGANISER_HOME:-$HOME/.email-organiser}"

UV="$(command -v uv || python3 -c 'import uv; print(uv.find_uv_bin())')"
CLAUDE_DIR="$(dirname "$(command -v claude)")"

# 1. The stable copy: a second checkout of the same repo, on its own `live` branch.
if [ ! -d "$LIVE" ]; then
    git -C "$REPO" branch -f live main
    git -C "$REPO" worktree add "$LIVE" live
fi

# 2. Fill in each template and hand it to launchd:
#    digest = the 08:00 / 18:00 summary; watch = the 15-minute check (milestone 6).
mkdir -p "$HOME/Library/LaunchAgents" "$DATA/logs"
for JOB in digest watch; do
    AGENT="$HOME/Library/LaunchAgents/com.email-organiser.$JOB.plist"
    sed -e "s|__UV__|$UV|" -e "s|__CODE_DIR__|$LIVE|" \
        -e "s|__PATH__|$CLAUDE_DIR:/usr/bin:/bin:/usr/sbin:/sbin|" \
        -e "s|__LOG__|$DATA/logs/$JOB.log|g" \
        "$REPO/launchd/$JOB.plist.template" > "$AGENT"
    plutil -lint "$AGENT" >/dev/null
    launchctl unload "$AGENT" 2>/dev/null || true
    launchctl load "$AGENT"
done

echo "Schedule installed: summary at 08:00 and 18:00, quick check every 15 min, running $LIVE"
echo "Logs: $DATA/logs/digest.log, $DATA/logs/watch.log"
