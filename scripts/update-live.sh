#!/bin/bash
# Move the stable copy (what the schedule runs) to the latest main.
# Run this after you merge a finished milestone into main.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIVE="${LIVE_DIR:-$(dirname "$REPO")/email-organiser-live}"
git -C "$LIVE" merge --ff-only main   # fast-forward only: refuses if anything odd happened
echo "Stable copy now at: $(git -C "$LIVE" log --oneline -1)"
