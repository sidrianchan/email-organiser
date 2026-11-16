#!/bin/bash
# Install (or reinstall) the 08:00 / 18:00 summary schedule.
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
AGENT="$HOME/Library/LaunchAgents/com.email-organiser.digest.plist"

UV="$(command -v uv || python3 -c 'import uv; print(uv.find_uv_bin())')"
CLAUDE_DIR="$(dirname "$(command -v claude)")"

# 1. The stable copy: a second checkout of the same repo, on its own `live` branch.
if [ ! -d "$LIVE" ]; then
    git -C "$REPO" branch -f live main
    git -C "$REPO" worktree add "$LIVE" live
fi

# 2. Fill in the template and hand it to launchd.
mkdir -p "$HOME/Library/LaunchAgents" "$DATA/logs"
sed -e "s|__UV__|$UV|" -e "s|__CODE_DIR__|$LIVE|" \
    -e "s|__PATH__|$CLAUDE_DIR:/usr/bin:/bin:/usr/sbin:/sbin|" \
    -e "s|__LOG__|$DATA/logs/digest.log|g" \
    "$REPO/launchd/digest.plist.template" > "$AGENT"
plutil -lint "$AGENT" >/dev/null
launchctl unload "$AGENT" 2>/dev/null || true
launchctl load "$AGENT"

echo "Schedule installed: 08:00 and 18:00, running $LIVE"
echo "Log: $DATA/logs/digest.log"
