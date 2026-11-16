"""Remembers which emails we've already seen, in a small SQLite database.

SQLite = a whole database in one file (data/mail.db). No server to install.
data/ is git-ignored, so your mail never reaches GitHub.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/mail.db")

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


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(SCHEMA)
    return db


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
