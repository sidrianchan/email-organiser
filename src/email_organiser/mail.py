"""Shared mail helpers: read the account list and log in. Used by every script."""
import tomllib

import keyring
from imapclient import IMAPClient

from . import paths

SERVICE = "email-organiser"  # the Keychain label store_password.py saves under


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
    client = IMAPClient(account["host"], ssl=True)  # encrypted, port 993
    client.login(account["username"], password)
    return client
