"""Shared mail helpers: read the account list and log in. Used by every script."""
import tomllib

import keyring
from imapclient import IMAPClient

from . import paths

SERVICE = "email-organiser"  # the Keychain label store_password.py saves under

# Seconds of silence from the server before a read gives up. Without it, a
# connection broken by sleep or a network change waits forever: on 18 Nov a
# quick check hung for 2 hours, holding the run lock, so the 18:00 summary was
# 23 minutes late and no quick check ran. A slow SEARCH on the big Yahoo inbox
# can take tens of seconds, hence 120 rather than less.
TIMEOUT = 120


def load_accounts() -> list[dict]:
    """accounts.toml holds names, servers and addresses. No passwords."""
    with open(paths.ACCOUNTS, "rb") as f:
        return tomllib.load(f)["account"]


def connect(account: dict) -> IMAPClient:
    """Log in to one account and return the connection.

    Use it with `with connect(account) as client:` so it always logs out.
    """
    # Google shows app passwords with spaces only for readability: remove them.
    password = keyring.get_password(SERVICE, account["name"]).replace(" ", "")
    client = IMAPClient(account["host"], ssl=True, timeout=TIMEOUT)  # encrypted, port 993
    client.login(account["username"], password)
    return client
