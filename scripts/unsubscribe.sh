#!/bin/sh
# Milestone 5b: pick junk senders to unsubscribe from. Preview by default; add --live to do it.
#   scripts/unsubscribe.sh          (preview: nothing sent, nothing recorded)
#   scripts/unsubscribe.sh --live
cd "$(dirname "$0")/.." && exec python3 -m uv run python -m email_organiser.unsubscribe "$@"
