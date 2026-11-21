"""Milestone 1: read the last 24 hours of mail from every account, read-only,
and remember what we've already seen.

Run:  python3 -m uv run python -m email_organiser.fetch
Run it twice: the second run should say 0 new.

To collect a bigger pile for the milestone 2 test set, look back further:
      python3 -m uv run python -m email_organiser.fetch --hours 168   (7 days)
"""
import argparse
import email
import html
import re
from datetime import datetime, timedelta
from email import policy

from imapclient import IMAPClient

from . import store
from .unsubscribe import header_info
from .mail import connect, load_accounts

HOURS = 24            # how far back to look
SNIPPET_CHARS = 1500  # how much body text to keep (design rule 6)


def fetch_recent(client: IMAPClient, hours: int, seen: frozenset = frozenset()) -> dict[int, bytes]:
    """Return {uid: raw email bytes} for INBOX mail from the last `hours`, skipping
    uids in `seen` (already in the database), so they aren't downloaded again.

    The inbox is already open read-only.
    """
    since = datetime.now() - timedelta(hours=hours)

    # IMAP's SINCE search only understands whole days, so this can return
    # up to a day too much. We trim it precisely below.
    uids = client.search(["SINCE", since.date()])
    if not uids:
        return {}  # asking the server to fetch nothing is an error

    # First just the arrival times (tiny), then whole emails only for new, recent ones.
    # Matters for the 15-minute check: otherwise it would re-download the day's mail each time.
    dates = client.fetch(uids, ["INTERNALDATE"])
    wanted = [uid for uid, parts in dates.items()
              if parts[b"INTERNALDATE"] >= since and uid not in seen]  # exact last N hours
    if not wanted:
        return {}

    # BODY.PEEK[] = download the whole email WITHOUT marking it as read.
    # (Keys are bytes, and the reply says BODY[], not BODY.PEEK[].)
    data = client.fetch(wanted, ["BODY.PEEK[]"])
    return {uid: parts[b"BODY[]"] for uid, parts in data.items()}


def parse_message(raw: bytes) -> dict:
    """Turn raw email bytes into the fields we keep."""
    # policy.default = use Python's modern email parser, which also decodes
    # encoded headers (e.g. Chinese subject lines) into normal text.
    msg = email.message_from_bytes(raw, policy=policy.default)

    # Headers read like a dictionary. Any of them can be missing (None),
    # so "or ''" turns a missing one into an empty string.
    return {
        "message_id": str(msg["Message-ID"] or ""),
        "sender": str(msg["From"] or ""),
        "subject": str(msg["Subject"] or ""),
        "date": str(msg["Date"] or ""),
        "snippet": body_text(msg)[:SNIPPET_CHARS],  # design rule 6: keep only the start
        "unsub": header_info(msg),  # milestone 5b: the unsubscribe link, kept in case you delete the email
    }


def body_text(msg) -> str:
    """Best-effort readable text of an email, attachments skipped. (Written for you.)"""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, UnicodeDecodeError):  # unknown or broken character set
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)  # drop code/styling
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))       # drop tags
    return re.sub(r"\s+", " ", text).strip()                       # tidy whitespace


# Our own summaries carry this header, so we never fetch and label them as mail.
OWN_HEADER = "X-Email-Organiser"


def fetch_new(db, hours: int, show: bool = True) -> int:
    """Fetch the last `hours` from every account and save the new ones. Returns how many were new."""
    total_new = 0
    for account in load_accounts():
        with connect(account) as client:
            folder = client.select_folder("INBOX", readonly=True)
            uidvalidity = folder[b"UIDVALIDITY"]
            seen = frozenset(r[0] for r in db.execute(
                "SELECT uid FROM messages WHERE account = ? AND uidvalidity = ?",
                (account["name"], uidvalidity)))
            emails = fetch_recent(client, hours, seen)

        new = 0
        for uid, raw in sorted(emails.items()):
            headers = raw.split(b"\r\n\r\n", 1)[0]  # the header block ends at the first blank line
            if OWN_HEADER.encode() in headers:      # one of our own summaries: skip it
                continue
            info = parse_message(raw)
            if store.save_if_new(db, account["name"], uidvalidity, uid, info):
                store.save_unsub(db, account["name"], uidvalidity, uid, info["unsub"])
                new += 1
                if show:
                    print(f"  [{account['name']}] {info['sender'][:35]:<35} | {info['subject'][:60]}")
        db.commit()  # each account as soon as it's read (22 Nov): holding one write open across all
                     # three downloads kept the database locked for minutes, and a failure in the
                     # next account threw this one's mail away
        print(f"{account['name']}: {new} new in the last {hours}h")
        total_new += new
    return total_new


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=HOURS, help="how far back to look")
    fetch_new(store.open_db(), parser.parse_args().hours)


if __name__ == "__main__":
    main()
