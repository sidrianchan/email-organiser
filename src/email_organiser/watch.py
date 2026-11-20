"""Milestone 6: every 15 minutes, a quick check for job and needs-reply mail, with an instant ping.

One check = emails you dragged into an Unsubscribe folder (unsub_folder.py, no Claude call)
-> fetch new mail -> Claude labels it (only if there is new mail) -> for new job or
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
  * the ping says how many and which account, never who or what (Telegram isn't end-to-end encrypted);
    since 21 Nov (milestone 9) a job email that needs you to act is named, e.g. "Acme: complete
    online assessment, due Fri 27 Nov" (Sid allowed names and dates, never email text), and a
    job email that's only status news ("viewed", "ad closed") doesn't ping: it waits for the
    summary. If the pipeline can't read it, it pings as before.
  * if the summary is running, this check skips its turn (runlock.py)
  * catch-up (slots.py): if the 08:00 / 18:00 summary is 10+ minutes overdue and was never
    delivered (e.g. the Mac woke briefly with no network and the run crashed), run it first.
    No network = try again next check; 3 real failures = one ping, then stop until the next one.
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import classify, digest, drafts, health, notify, pipeline, slots, store, unsub_folder
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


def key(r) -> tuple:
    return (r["account"], r["uidvalidity"], r["uid"])


def is_update(r, reads: dict) -> bool:
    """A job email the pipeline read as status news only: no ping (milestone 9)."""
    rd = reads.get(key(r))
    return r["label"] == "job" and not r["needs_reply"] and rd is not None and not rd["action_needed"]


def action_text(r, rd: dict) -> str:
    """'Acme: complete online assessment, due Fri 27 Nov (Yahoo)'. Names and dates only."""
    due = f", due {datetime.fromisoformat(rd['deadline']):%a %-d %b}" if rd.get("deadline") else ""
    return f"{rd['company']}: {rd['action'] or 'action needed'}{due} ({NAMES.get(r['account'], r['account'])})"


def message(rows: list, now: datetime, reads: dict | None = None) -> tuple[str, bool]:
    """(text, loud). Job emails that need action are named (company, what, by when); everything
    else is counts and account names only."""
    reads = reads or {}
    named = [r for r in rows if r["label"] == "job" and (reads.get(key(r)) or {}).get("action_needed")
             and reads[key(r)].get("company")]
    named_keys = {key(r) for r in named}
    rest = [r for r in rows if key(r) not in named_keys]
    jobs = sum(r["label"] == "job" for r in rest)
    replies = sum(bool(r["needs_reply"]) and r["label"] != "job" for r in rest)
    drafted = sum(bool(r["drafted"]) for r in rows)
    parts = [action_text(r, reads[key(r)]) for r in named]
    bits = [f"{jobs} job email{'s' if jobs > 1 else ''}" if jobs else "",
            f"{replies} to reply" if replies else ""]
    if any(bits):
        accounts = ", ".join(sorted({NAMES.get(r["account"], r["account"]) for r in rest}, key=str.lower))
        parts.append(f"{', '.join(b for b in bits if b)} ({accounts})")
    text = f"{now:%H:%M} new: {'; '.join(parts)}."
    if drafted:
        text += f" {drafted} draft{'s' if drafted > 1 else ''} ready."
    return text, not quiet(now)


def unsubscribe_step(db, live: bool, now: datetime) -> None:
    """Emails you dragged into an Unsubscribe folder (unsub_folder.py). Never raises, so
    a problem here can't stop the job alerts below."""
    try:
        text = unsub_folder.ping_text(unsub_folder.run(db, live))
        if text and live:
            notify.send(text, not quiet(now))
            print(f"Telegram ping sent: {text}")
        elif text:
            print(f"(dry run: ping not sent; it would say: {text})")
    except Exception as e:  # results are in the DB; the summary still lists them
        print(f"Unsubscribe step FAILED (alerts unaffected): {e}")


def check(db, live: bool, now: datetime | None = None) -> int:
    """One quick check. Returns how many emails were pinged about."""
    unsubscribe_step(db, live, now or datetime.now())
    fetch_new(db, LOOKBACK_HOURS, show=False)
    labelled = classify_pending(db, since_hours=LOOKBACK_HOURS)
    if classify.last_error and live:   # Claude failing: say so now, not at the next summary
        health.claude_down(classify.last_error, now or datetime.now())
    elif labelled and not classify.last_error:
        health.claude_ok()
    rows = to_alert(db)
    if not rows:
        print("nothing urgent")
        return 0
    reads = {}
    if any(r["label"] == "job" for r in rows):   # milestone 9: what each job email asks of you
        try:
            reads = pipeline.step(db, live=live, since_hours=LOOKBACK_HOURS)
        except Exception as e:  # can't tell action from news: ping every job email, as before
            print(f"Job pipeline FAILED (every job email pings, as before): {e}")
        reads = pipeline.readings(db, [key(r) for r in rows]) | reads
    updates = [r for r in rows if is_update(r, reads)]
    if updates:
        print(f"{len(updates)} application update(s), nothing to do: no ping, they're in the next summary")
        if live:
            db.executemany("INSERT OR IGNORE INTO alerted (account, uidvalidity, uid) VALUES (?, ?, ?)",
                           [key(r) for r in updates])
            db.commit()
    keys = {key(r) for r in rows if not is_update(r, reads)}
    if not keys:
        return 0
    try:
        drafts.make_drafts(db, live=live, only=keys)
    except Exception as e:  # a drafting problem must never stop the ping
        print(f"Drafts FAILED (ping unaffected): {e}")
    rows = [r for r in to_alert(db) if key(r) in keys]   # re-read, so the ping knows which now have a draft
    text, loud = message(rows, now or datetime.now(), reads)
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
    sys.stdout.reconfigure(line_buffering=True)   # keep the log in order (errors aren't buffered, prints were)
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really write drafts and ping")
    live = parser.parse_args().live
    print(f"--- {datetime.now():%Y-%m-%d %H:%M} quick check ({'LIVE' if live else 'dry run'})")
    with run_lock(wait_seconds=0) as got:
        if not got:
            print("summary is running: skipping this check")
            return
        db = store.open_db()
        if live:
            catch_up(db, datetime.now())
        check(db, live)


def catch_up(db, now: datetime) -> bool:
    """If the 08:00 or 18:00 summary is due but wasn't delivered (the Mac asleep, no network,
    a crash), run it now. Never raises, so the quick check below still runs. Returns True
    if a summary was delivered."""
    slot = slots.catch_up_due(db, now)
    if slot is None:
        return False
    print(f"The {slot:%H:%M} summary hasn't been delivered: running it now")
    try:
        return digest.summarise_recorded("catch-up", now=now)
    except Exception as e:   # already recorded in summary_runs by summarise_recorded
        print(f"Catch-up summary FAILED: {type(e).__name__}: {e}")
        if slots.is_network(e):
            print("(no network: will try again at the next check)")
        elif slots.failures(db, slot) >= slots.MAX_FAILS:
            give_up = (f"The {slot:%H:%M} summary failed {slots.MAX_FAILS} times ({type(e).__name__}). "
                       f"No more retries until the next one; details in watch.log.")
            try:
                notify.send(give_up, True)
                print(f"Telegram ping sent: {give_up}")
            except Exception as ping_error:
                print(f"Telegram ping FAILED: {ping_error}")
        return False


if __name__ == "__main__":
    main()
