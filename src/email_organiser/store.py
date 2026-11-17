"""Remembers which emails we've already seen, in a small SQLite database.

SQLite = a whole database in one file (~/.email-organiser/mail.db, see paths.py).
No server to install, and it lives outside the code folder, so it never reaches GitHub.
"""
import sqlite3
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

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
CREATE TABLE IF NOT EXISTS my_labels (
    account TEXT, uidvalidity INTEGER, uid INTEGER,
    label TEXT NOT NULL,
    needs_reply INTEGER,
    labelled_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (account, uidvalidity, uid)
);
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row  # lets us read columns by name: row["subject"]
    db.execute(SCHEMA)
    db.executescript(LABEL_SCHEMA)
    _add_column_if_missing(db, "claude_labels", "needs_reply", "INTEGER")
    _add_column_if_missing(db, "my_labels", "needs_reply", "INTEGER")
    return db


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


def arrived(row) -> datetime:
    """When the email was sent, from its Date header; first-seen time if that's unreadable."""
    try:
        return parsedate_to_datetime(row["date"]).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromisoformat(row["first_seen"]).replace(tzinfo=timezone.utc)
