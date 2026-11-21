"""Remembers which emails we've already seen, in a small SQLite database.

SQLite = a whole database in one file (~/.email-organiser/mail.db, see paths.py).
No server to install, and it lives outside the code folder, so it never reaches GitHub.
"""
import sqlite3
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import paths

DB_PATH = paths.DB

# One row per email we've seen.
# PRIMARY KEY (account, uidvalidity, uid) = "this combination can only exist once".
# That rule is what makes "seen before?" a one-line question for the database.
#
# uid         = the number the server gives each email in a folder.
# uidvalidity = a number the server changes if it ever renumbers the folder.
#               Storing it means an old uid can never be confused with a new one.
SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    account     TEXT    NOT NULL,
    uidvalidity INTEGER NOT NULL,
    uid         INTEGER NOT NULL,
    message_id  TEXT,
    sender      TEXT,
    subject     TEXT,
    date        TEXT,
    snippet     TEXT,
    first_seen  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
)
"""


# Milestone 3: digested = emails already shown in a summary, so none appears twice.
# Milestone 4: drafts    = emails that already have a reply draft, so none gets two.
# Milestone 5: moved     = emails moved to Assistant-Review (later: what you drag back = a wrong label).
# Screener:    screened  = job alerts already read; jobs = one row per job found (deduplicated).
# 5b:          unsub_headers  = each email's unsubscribe link, saved at fetch (you may delete the email)
#              sender_choices = per sender: unsubscribed / by_hand / keep / just shown (NULL)
#              nudges         = weeks the Telegram "senders to review" nudge went out (retired)
#              unsub_requests = emails you dragged into an Unsubscribe folder, and what happened
# 6:           alerted        = emails the 15-minute check already pinged about
# 7:           corrections    = what you corrected (rescued / marked_junk / replied_unflagged)
#              sender_rules   = never_move (after 1 rescue) / junk (after 3 junk corrections)
#              sent_replies   = what you sent in reply to a drafted email (style, later)
#              scores         = the weekly labelling check; score_answers = each email's answer that week
# 7b:          testset_pool   = old job-season emails picked by rule/random for you to label
# Milestone 2. Two label tables, kept apart on purpose:
#   claude_labels = what Claude thinks
#   my_labels     = what YOU say (the "ground truth" we measure Claude against)
# Keeping them separate means Claude's answer can never overwrite yours.
LABEL_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_labels (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    label TEXT NOT NULL, needs_reply INTEGER, reason TEXT, model TEXT,
    labelled_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS digested (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    digest_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS drafts (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    folder TEXT, draft_message_id TEXT,
    drafted_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS moved (
    account TEXT, uidvalidity INTEGER, uid INTEGER, folder TEXT,
    moved_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS screened (
    account TEXT, uidvalidity INTEGER, uid INTEGER, jobs_found INTEGER,
    screened_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS jobs (
    job_key TEXT PRIMARY KEY,
    title TEXT, company TEXT, location TEXT, closing TEXT,
    fit TEXT, reason TEXT, url TEXT, commission INTEGER,
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    found_at TEXT DEFAULT (datetime('now')),
    shown_at TEXT
);
CREATE TABLE IF NOT EXISTS unsub_headers (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    url TEXT, mailto TEXT, one_click INTEGER, signed INTEGER,
    missing INTEGER DEFAULT 0,     -- 1 = email was already gone when we looked
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS sender_choices (
    sender_key TEXT PRIMARY KEY,   -- "address|display name"
    status TEXT, decided_at TEXT, shown_at TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS nudges (
    week TEXT PRIMARY KEY, sent_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS alerted (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    alerted_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS corrections (
    account TEXT, uidvalidity INTEGER, uid INTEGER, kind TEXT,
    label_was TEXT, truth TEXT, found_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid, kind)
);
CREATE TABLE IF NOT EXISTS sender_rules (
    address TEXT PRIMARY KEY, rule TEXT NOT NULL, label TEXT, reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sent_replies (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    draft_body TEXT, sent_body TEXT, similarity REAL,
    found_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS scores (
    week TEXT PRIMARY KEY, total INTEGER, correct INTEGER, job_total INTEGER, job_correct INTEGER,
    reply_correct INTEGER, corr_total INTEGER, corr_correct INTEGER, model TEXT, prompt_hash TEXT,
    line TEXT, shown INTEGER DEFAULT 0, scored_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS score_answers (
    week TEXT, account TEXT, uidvalidity INTEGER, uid INTEGER,
    label TEXT, needs_reply INTEGER, is_right INTEGER,
    PRIMARY KEY (week, account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS testset_pool (
    account TEXT, uidvalidity INTEGER, uid INTEGER, why TEXT,   -- why = 'rule' or 'random'
    added_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS event_flags (
    event_key TEXT PRIMARY KEY,        -- title + start time: each calendar event is judged once
    important INTEGER, prep TEXT, model TEXT,
    judged_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS applications (   -- pipeline.py: one row per job application
    id INTEGER PRIMARY KEY,
    company TEXT NOT NULL, title TEXT,
    stage TEXT NOT NULL,                     -- would_apply/applied/viewed/assessment/interview/offer/rejected
    posting_closed INTEGER DEFAULT 0,        -- the ad closed; the application may still count
    deadline TEXT, action TEXT,              -- the latest thing to do, and by when (only if an email said)
    action_since TEXT, action_done_at TEXT,  -- when that step was asked for; when you tapped Done
    source TEXT, job_key TEXT,               -- where it came from; the screened job, if any
    snoozed_until TEXT,                      -- apply queue: "Not now" hides it until this date
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS queue_messages (   -- apply_queue.py: one morning message a day
    day TEXT PRIMARY KEY, message_id INTEGER, app_ids TEXT
);
CREATE TABLE IF NOT EXISTS todo_messages (   -- a summary ping with Done buttons for job to-dos
    message_id INTEGER PRIMARY KEY, app_ids TEXT,
    sent_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS job_group_messages (   -- feedback.py: several 'may need experience' jobs
    message_id INTEGER PRIMARY KEY,                -- in one Telegram message (22 Nov)
    msg_ids TEXT,                                  -- their job_messages ids, in the order listed
    sent_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS application_events (   -- every change, with the email behind it (undo)
    id INTEGER PRIMARY KEY,
    application_id INTEGER NOT NULL,
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    old_stage TEXT, new_stage TEXT, posting_closed INTEGER, deadline TEXT, detail TEXT,
    at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS job_reads (   -- pipeline.py: Claude's reading of one job email
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    company TEXT, role TEXT, stage TEXT, posting_closed INTEGER,
    action_needed INTEGER, action TEXT, deadline TEXT,
    application_id INTEGER, model TEXT,
    read_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS my_actions (   -- your answer for the pipeline gate: must you act on it?
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    action INTEGER NOT NULL,
    labelled_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS short_titles (   -- shorten.py: a long event/reminder title, said briefly
    kind TEXT NOT NULL,                      -- event / reminder / prep
    original TEXT NOT NULL,
    short TEXT NOT NULL, model TEXT,
    made_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (kind, original)
);
CREATE TABLE IF NOT EXISTS unsub_requests (
    id INTEGER PRIMARY KEY,
    account TEXT, sender TEXT, subject TEXT,
    result TEXT,                       -- unsubscribed / by_hand / refused / failed
    detail TEXT, job_board INTEGER DEFAULT 0,
    done_at TEXT DEFAULT (datetime('now')),
    shown INTEGER DEFAULT 0            -- 1 = already listed in a summary
);
CREATE TABLE IF NOT EXISTS summary_runs (   -- slots.py: was each 08:00/18:00 summary delivered?
    slot TEXT NOT NULL,                -- the scheduled time this run belongs to (local, ISO)
    started TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,                        -- error type + short message only, never email content
    network INTEGER DEFAULT 0,         -- 1 = failed for lack of network (doesn't count towards giving up)
    via TEXT                           -- digest (scheduled or by hand) / catch-up
);
CREATE TABLE IF NOT EXISTS job_messages (   -- feedback.py: a job sent to Telegram with buttons
    id INTEGER PRIMARY KEY,               -- what the buttons carry (Telegram allows 64 bytes)
    job_key TEXT UNIQUE NOT NULL,
    message_id INTEGER,                   -- Telegram's id, to update the buttons after a tap
    sent_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS job_feedback (   -- your verdict on a screened job; the latest tap wins
    job_key TEXT PRIMARY KEY,
    verdict TEXT NOT NULL,                -- apply / applied / not_qualified / not_interested
    fit_then TEXT,                        -- what the screener had said, kept for scoring
    at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS my_labels (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    label TEXT NOT NULL,
    needs_reply INTEGER,
    labelled_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
"""


BUSY_TIMEOUT = 30   # seconds


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout: wait up to 30 s for another run's write to finish (default 5 s). The tap collector
    # runs every minute alongside the summary, whose writes can be slow (22 Nov).
    db = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT)
    db.row_factory = sqlite3.Row  # lets us read columns by name: row["subject"]
    db.execute(SCHEMA)
    db.executescript(LABEL_SCHEMA)
    _add_column_if_missing(db, "claude_labels", "needs_reply", "INTEGER")
    _add_column_if_missing(db, "my_labels", "needs_reply", "INTEGER")
    _add_column_if_missing(db, "claude_labels", "job_alert", "INTEGER")
    _add_column_if_missing(db, "jobs", "commission", "INTEGER")
    _add_column_if_missing(db, "jobs", "level", "TEXT")   # screener rubric (20 Nov): graduate/junior/...
    _add_column_if_missing(db, "jobs", "gap", "TEXT")     # the requirement he may not meet, or ""
    _add_column_if_missing(db, "jobs", "employer_size", "TEXT")   # large/small/unknown: orders the high list
    _add_column_if_missing(db, "drafts", "body", "TEXT")   # milestone 7: compare with what you sent
    _add_column_if_missing(db, "applications", "snoozed_until", "TEXT")   # apply queue "Not now" (21 Nov)
    _add_column_if_missing(db, "applications", "action_since", "TEXT")    # job to-dos (21 Nov)
    _add_column_if_missing(db, "applications", "action_done_at", "TEXT")
    return db


KEEP_BACKUPS = 7   # days


def backup(db: sqlite3.Connection, folder: Path | None = None, today: date | None = None,
           keep: int = KEEP_BACKUPS) -> Path | None:
    """A daily copy of the database (21 Nov), so a corrupted file or a bad change can be undone.
    The database holds things that can't be rebuilt: your hand labels, corrections and job taps.

    One file per day (mail-YYYY-MM-DD.db); the first real summary of the day makes it, later
    runs that day find it there and skip. The newest `keep` are kept. SQLite's own backup copies
    a consistent snapshot even while another run is writing. Same disk, so it doesn't protect
    against losing the Mac (that needs Time Machine or similar). Never raises."""
    try:
        folder = folder or paths.BACKUPS
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"mail-{today or date.today():%Y-%m-%d}.db"
        if not target.exists():
            copy = sqlite3.connect(target)
            try:
                db.backup(copy)
            finally:
                copy.close()
            print(f"Database backed up to {target}")
        for old in sorted(folder.glob("mail-*.db"))[:-keep]:   # names sort by date
            old.unlink()
        return target
    except Exception as e:
        print(f"Database backup FAILED (nothing else affected): {e}")
        return None


def _add_column_if_missing(db: sqlite3.Connection, table: str, column: str, kind: str) -> None:
    """Upgrade an existing database in place ("migration") instead of starting over.
    needs_reply: 1 = yes, 0 = no, NULL = not answered yet."""
    existing = [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def save_if_new(db: sqlite3.Connection, account: str, uidvalidity: int,
                uid: int, info: dict) -> bool:
    """Save one email. Return True if it was new, False if we'd already seen it."""
    # INSERT OR IGNORE: if this (account, uidvalidity, uid) already exists,
    # the PRIMARY KEY rule makes SQLite skip the row instead of erroring.
    #
    # The "?" placeholders let the database insert the values safely. Building
    # SQL by pasting email text into the string would let a crafted subject
    # line change the query itself. That attack is called "SQL injection".
    cur = db.execute(
        "INSERT OR IGNORE INTO messages "
        "(account, uidvalidity, uid, message_id, sender, subject, date, snippet) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (account, uidvalidity, uid, info["message_id"], info["sender"],
         info["subject"], info["date"], info["snippet"]),
    )
    # rowcount = how many rows that statement changed: 1 = new, 0 = seen before.
    return cur.rowcount == 1


def save_unsub(db: sqlite3.Connection, account: str, uidvalidity: int, uid: int,
               info: dict | None) -> None:
    """Save an email's unsubscribe options. None = the email was already gone (so: unknown)."""
    missing = int(info is None)
    info = info or {}
    db.execute("INSERT OR IGNORE INTO unsub_headers "
               "(account, uidvalidity, uid, url, mailto, one_click, signed, missing) "
               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               (account, uidvalidity, uid, info.get("url"), info.get("mailto"),
                info.get("one_click", 0), info.get("signed", 0), missing))


def sender_address(sender: str) -> str:
    from email.utils import parseaddr
    return parseaddr(sender or "")[1].lower()


def never_move(db: sqlite3.Connection) -> set[str]:
    """Senders you rescued from Assistant-Review: never moved again (milestone 7)."""
    return {r[0] for r in db.execute("SELECT address FROM sender_rules WHERE rule = 'never_move'")}


def junk_rules(db: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """{address: (label, reason)} for senders labelled junk without asking Claude (milestone 7)."""
    return {r[0]: (r[1], r[2]) for r in
            db.execute("SELECT address, label, reason FROM sender_rules WHERE rule = 'junk'")}


def arrived(row) -> datetime:
    """When the email was sent, from its Date header; first-seen time if that's unreadable."""
    try:
        return parsedate_to_datetime(row["date"]).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromisoformat(row["first_seen"]).replace(tzinfo=timezone.utc)
