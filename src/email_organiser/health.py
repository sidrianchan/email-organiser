"""Say it out loud when Claude stops working (added 20 Nov).

On 20 Nov every Claude call failed (Claude Code was logged out) and the 08:00 summary still
said "nothing needs you". Now a failure sends one loud Telegram ping telling you what to do,
at most once every QUIET_HOURS, so a long outage doesn't ping every 15 minutes.
The ping says what happened, never anything from an email.
"""
from datetime import datetime, timedelta

from . import notify, paths

QUIET_HOURS = 6
STATE = paths.HOME / "claude_down_pinged"   # when we last pinged about it (ISO time)


def message(now: datetime) -> str:
    return (f"{now:%H:%M} ⚠️ Claude isn't answering, so new mail isn't being labelled or "
            "checked for jobs. Run /login in Claude Code on the Mac. Details in the logs.")


def claude_down(error: str, now: datetime) -> bool:
    """Ping about a Claude failure unless we already did in the last QUIET_HOURS.
    Never raises. Returns True if a ping went out."""
    try:
        last = datetime.fromisoformat(STATE.read_text().strip()) if STATE.exists() else None
        if last and now - last < timedelta(hours=QUIET_HOURS):
            print(f"(Claude still failing; already pinged at {last:%H:%M})")
            return False
        notify.send(message(now), True)
        STATE.write_text(now.isoformat())
        print(f"Telegram ping sent: {message(now)}")
        return True
    except Exception as e:
        print(f"Claude-down ping FAILED: {e}")
        return False


def claude_ok() -> None:
    """Claude answered again: clear the state, so the next outage pings straight away."""
    try:
        STATE.unlink(missing_ok=True)
    except OSError:
        pass
