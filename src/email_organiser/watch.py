"""Milestone 6: every 15 minutes, a quick check for job and needs-reply mail, with an instant ping.

One check = fetch new mail -> Claude labels it (only if there is new mail) -> for new job or
needs-reply emails: write the reply draft now -> Telegram ping, counts only.
Junk moving and job-alert screening stay in the 08:00 / 18:00 summary.

Dry run by default (design rule 0):
    python3 -m uv run python -m email_organiser.watch
For real (this is what the schedule runs every 15 minutes):
    python3 -m uv run python -m email_organiser.watch --live

Rules:
  * a check with no new mail makes no Claude call, so it costs none of your Pro usage
  * only mail from the last LOOKBACK_HOURS; a longer gap (the Mac asleep) is covered by the summary
  * each email is pinged at most once (`alerted` table), and never if a summary already showed it
  * 23:00-08:00 the ping is silent (still sent, no sound)
  * the ping says how many and which account, never who or what (Telegram isn't end-to-end encrypted)
  * if the summary is running, this check skips its turn (runlock.py)
"""
import argparse
from datetime import datetime, timedelta, timezone

from . import drafts, notify, store
from .classify import classify_pending
from .fetch import fetch_new
from .runlock import run_lock
from .store import arrived

LOOKBACK_HOURS = 3   # covers a few missed checks; the summary looks back 26h anyway
QUIET_FROM, QUIET_UNTIL = 23, 8
NAMES = {"icloud": "iCloud", "yahoo": "Yahoo", "gmail": "Gmail"}


def to_alert(db) -> list:
    """Job or needs-reply emails, recent, not yet pinged, not already in a summary."""
    rows = db.execute("""
        SELECT m.account, m.uidvalidity, m.uid, m.date, m.first_seen, c.label, c.needs_reply,
               dr.uid IS NOT NULL AS drafted
        FROM messages m
        JOIN claude_labels c USING (account, uidvalidity, uid)
        LEFT JOIN alerted a USING (account, uidvalidity, uid)
        LEFT JOIN digested d USING (account, uidvalidity, uid)
        LEFT JOIN drafts dr USING (account, uidvalidity, uid)
        WHERE (c.label = 'job' OR c.needs_reply = 1) AND a.uid IS NULL AND d.uid IS NULL
    """).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    return [r for r in rows if arrived(r) >= cutoff]


def quiet(now: datetime) -> bool:
    return now.hour >= QUIET_FROM or now.hour < QUIET_UNTIL


def message(rows: list, now: datetime) -> tuple[str, bool]:
    """(text, loud). Counts and account names only."""
    jobs = sum(r["label"] == "job" for r in rows)
    replies = sum(bool(r["needs_reply"]) and r["label"] != "job" for r in rows)
    drafted = sum(bool(r["drafted"]) for r in rows)
    bits = [f"{jobs} job email{'s' if jobs > 1 else ''}" if jobs else "",
            f"{replies} to reply" if replies else ""]
    accounts = ", ".join(sorted({NAMES.get(r["account"], r["account"]) for r in rows}, key=str.lower))
    text = f"{now:%H:%M} new: {', '.join(b for b in bits if b)} ({accounts})."
    if drafted:
        text += f" {drafted} draft{'s' if drafted > 1 else ''} ready."
    return text, not quiet(now)


def check(db, live: bool, now: datetime | None = None) -> int:
    """One quick check. Returns how many emails were pinged about."""
    fetch_new(db, LOOKBACK_HOURS, show=False)
    classify_pending(db, since_hours=LOOKBACK_HOURS)
    rows = to_alert(db)
    if not rows:
        print("nothing urgent")
        return 0
    keys = {(r["account"], r["uidvalidity"], r["uid"]) for r in rows}
    try:
        drafts.make_drafts(db, live=live, only=keys)
    except Exception as e:  # a drafting problem must never stop the ping
        print(f"Drafts FAILED (ping unaffected): {e}")
    rows = to_alert(db)   # re-read, so the ping knows which now have a draft
    text, loud = message(rows, now or datetime.now())
    if not live:
        print(f"(dry run: ping not sent; it would say: {text}{'' if loud else '  [no sound]'})")
        return 0
    try:
        notify.send(text, loud)
    except Exception as e:  # not recorded, so the next check tries again
        print(f"Telegram ping FAILED (will retry next check): {e}")
        return 0
    db.executemany("INSERT OR IGNORE INTO alerted (account, uidvalidity, uid) VALUES (?, ?, ?)",
                   [(r["account"], r["uidvalidity"], r["uid"]) for r in rows])
    db.commit()
    print(f"Telegram ping sent{'' if loud else ' (no sound)'}: {text}")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really write drafts and ping")
    live = parser.parse_args().live
    print(f"--- {datetime.now():%Y-%m-%d %H:%M} quick check ({'LIVE' if live else 'dry run'})")
    with run_lock(wait_seconds=0) as got:
        if not got:
            print("summary is running: skipping this check")
            return
        check(store.open_db(), live)


if __name__ == "__main__":
    main()
