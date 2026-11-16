"""Milestone 0 check: can we reach all 3 mailboxes and Claude?

Run:  python3 -m uv run python -m email_organiser.check

Read-only. It counts unread mail; it cannot change, move, or send anything.
"""
import subprocess

from imapclient.exceptions import LoginError

from .mail import connect, load_accounts


def count_unread(account: dict) -> int:
    with connect(account) as client:
        # readonly=True: the server refuses any change in this session,
        # not even marking mail as read.
        client.select_folder("INBOX", readonly=True)
        # Ask the server for messages without the "Seen" flag.
        return len(client.search(["UNSEEN"]))


def check_claude() -> str:
    # Runs Claude Code in "print" mode: one question in, one answer out.
    # It uses your Pro login, so there is no API key and no extra bill.
    result = subprocess.run(
        ["claude", "-p", "Reply with exactly one word: OK"],
        capture_output=True, text=True, timeout=120,
    )
    return result.stdout.strip() or f"error: {result.stderr.strip()}"


def main() -> None:
    for account in load_accounts():
        try:
            print(f"{account['name']:<7} unread: {count_unread(account)}")
        except LoginError as e:
            # Most likely cause: wrong app password, or IMAP not enabled.
            print(f"{account['name']:<7} LOGIN FAILED: {e}")
    print(f"claude  says: {check_claude()}")


if __name__ == "__main__":
    main()
