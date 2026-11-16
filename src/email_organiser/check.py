"""Milestone 0 check: can we reach all 3 mailboxes and Claude?

Run:  python3 -m uv run python -m email_organiser.check

Read-only. It counts unread mail; it cannot change, move, or send anything.
"""
import subprocess
import tomllib

import keyring
from imapclient import IMAPClient
from imapclient.exceptions import LoginError

SERVICE = "email-organiser"  # same Keychain label store_password.py used


def load_accounts() -> list[dict]:
    # accounts.toml holds names, servers and addresses. No passwords.
    with open("accounts.toml", "rb") as f:
        return tomllib.load(f)["account"]


def count_unread(account: dict) -> int:
    # 1. Get the app password from Keychain. Google shows its passwords with
    #    spaces ("abcd efgh ...") only for readability, so remove them.
    password = keyring.get_password(SERVICE, account["name"]).replace(" ", "")

    # 2. Connect over an encrypted connection (IMAP over SSL, port 993).
    #    "with" makes sure we log out even if something fails.
    with IMAPClient(account["host"], ssl=True) as client:
        client.login(account["username"], password)

        # 3. readonly=True: the server itself refuses any change in this session,
        #    not even marking mail as read. Our first safety layer.
        client.select_folder("INBOX", readonly=True)

        # 4. Ask the server for messages without the "Seen" flag.
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
