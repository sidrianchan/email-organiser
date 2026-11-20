"""Milestone 5a: junk out of the inbox, into an Assistant-Review folder.

Emails Claude labelled spam, promo or newsletter move from INBOX to Assistant-Review
in the same account. Nothing is deleted (design rule 3): you look through the folder
and empty it yourself. Drag an email back to the inbox to undo a move.

Dry run by default (design rule 0): prints what would move and changes nothing.
    python3 -m uv run python -m email_organiser.cleanup
For real:
    python3 -m uv run python -m email_organiser.cleanup --live

The model labels; the code acts (design rule 4). Plain rules below decide what moves:
  * job emails and anything that needs a reply never move, whatever the label says
  * job alerts stay in the inbox until the screener has read them (screener.py)
  * only mail from the last 48h (the backlog is left alone for now)
  * at most MAX_MOVES per run, so a bug can't empty an inbox
"""
import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from imapclient.imapclient import DELETED

from . import store
from .mail import connect, load_accounts
from .store import arrived

REVIEW = "Assistant-Review"
MOVE_LABELS = ("spam", "promo", "newsletter")
MAX_AGE_HOURS = 48
MAX_MOVES = 30

# Job-board senders: their mail is screened for jobs before it may move (screener.py).
# A domain matches itself and its subdomains (e.jobsdb.com matches jobsdb.com).
JOB_BOARD_DOMAINS = (
    "jobsdb.com", "ctgoodjobs.hk", "recruit.com.hk", "indeed.com", "glassdoor.com",
    "efinancialcareers.com", "gradcracker.com", "prospects.ac.uk",
    "brightnetwork.co.uk", "targetjobs.co.uk", "reed.co.uk", "totaljobs.com",
)
# LinkedIn sends social notifications too (updates-, invitations@), so only the
# addresses seen sending job alerts count. messages-noreply also sends "X is hiring".
JOB_BOARD_ADDRESSES = ("jobs-noreply@linkedin.com", "jobalerts-noreply@linkedin.com",
                       "messages-noreply@linkedin.com")


def is_job_board(sender: str) -> bool:
    address = parseaddr(sender or "")[1].lower()
    domain = address.rpartition("@")[2]
    return (address in JOB_BOARD_ADDRESSES
            or any(domain == d or domain.endswith("." + d) for d in JOB_BOARD_DOMAINS))


def pending(db) -> tuple[list, list]:
    """(to_move, held_job_alerts). Recent junk that hasn't been moved yet.
    A job alert (Claude flagged it, or it's from a job board) is held until it's screened."""
    marks = ",".join("?" * len(MOVE_LABELS))
    rows = db.execute(f"""
        SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.date, m.first_seen,
               c.label, c.needs_reply, c.job_alert, s.uid IS NOT NULL AS screened
        FROM messages m
        JOIN claude_labels c USING (account, uidvalidity, uid)
        LEFT JOIN moved mv USING (account, uidvalidity, uid)
        LEFT JOIN screened s USING (account, uidvalidity, uid)
        WHERE mv.uid IS NULL AND c.label IN ({marks}) AND COALESCE(c.needs_reply, 0) = 0
    """, MOVE_LABELS).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    recent = [r for r in rows if arrived(r) >= cutoff]
    def waiting(r) -> bool:
        return bool(r["job_alert"] or is_job_board(r["sender"])) and not r["screened"]
    # Milestone 7: a sender you rescued from Review is never moved again.
    keep = store.never_move(db)
    recent = [r for r in recent if store.sender_address(r["sender"]) not in keep]
    held = [r for r in recent if waiting(r)]
    return [r for r in recent if not waiting(r)], held


def move(client, uids: list[int]) -> None:
    """Move these INBOX emails to REVIEW. INBOX must already be selected (read-write).

    Yahoo and Gmail have a MOVE command. iCloud doesn't, so there it is:
      1. COPY to Review       (if this fails, an error is raised and nothing is removed)
      2. flag \\Deleted        (only these emails)
      3. UID EXPUNGE           (removes ONLY these UIDs from the inbox)
    A plain EXPUNGE would also wipe anything else marked \\Deleted (say, by Mail.app),
    so the code refuses to run on a server without UID EXPUNGE (the UIDPLUS extension).
    """
    if client.has_capability("MOVE"):
        client.move(uids, REVIEW)
        return
    if not client.has_capability("UIDPLUS"):
        raise RuntimeError("server has neither MOVE nor UID EXPUNGE; refusing to move")
    client.copy(uids, REVIEW)
    client.add_flags(uids, [DELETED], silent=True)
    client.uid_expunge(uids)


def move_all(db, rows: list) -> int:
    """Move rows (already capped) account by account; record each batch after it moved."""
    moved = 0
    accounts = {a["name"]: a for a in load_accounts()}
    for name in sorted({r["account"] for r in rows}):
        mine = [r for r in rows if r["account"] == name]
        with connect(accounts[name]) as client:
            if not client.folder_exists(REVIEW):
                client.create_folder(REVIEW)
                print(f"  [{name}] created folder {REVIEW}")
            info = client.select_folder("INBOX")  # read-write: moving changes the inbox
            mine = [r for r in mine if r["uidvalidity"] == info[b"UIDVALIDITY"]]
            # Only emails still in the inbox (you may have moved or deleted some yourself).
            present = set(client.fetch([r["uid"] for r in mine], ["FLAGS"])) if mine else set()
            gone = [r for r in mine if r["uid"] not in present]
            if gone:
                print(f"  [{name}] {len(gone)} already gone from the inbox (moved or deleted "
                      f"outside this program); left alone")
            mine = [r for r in mine if r["uid"] in present]
            if not mine:
                continue
            move(client, [r["uid"] for r in mine])
        db.executemany("INSERT OR IGNORE INTO moved (account, uidvalidity, uid, folder) "
                       "VALUES (?, ?, ?, ?)",
                       [(r["account"], r["uidvalidity"], r["uid"], REVIEW) for r in mine])
        db.commit()
        moved += len(mine)
        print(f"  [{name}] moved {len(mine)} to {REVIEW}")
    return moved


def run(db, live: bool) -> int:
    """The whole step. Returns how many emails were moved (0 on a dry run)."""
    rows, held = pending(db)
    if len(rows) > MAX_MOVES:
        print(f"{len(rows)} to move; capped at {MAX_MOVES} this run, the rest next run")
        rows = rows[:MAX_MOVES]
    print(f"{len(rows)} emails to move to {REVIEW}; {len(held)} job alerts kept until screened")
    if not live:
        for r in rows:
            print(f"  would move [{r['account']}] {r['label']:<10} {r['subject'][:60]}")
        if rows:
            print("(dry run: nothing moved)")
        return 0
    return move_all(db, rows) if rows else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really move the emails")
    run(store.open_db(), live=parser.parse_args().live)


if __name__ == "__main__":
    main()
