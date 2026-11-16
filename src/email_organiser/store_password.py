"""Save one account's app password into the macOS Keychain.

Run:  uv run python -m email_organiser.store_password yahoo

getpass() hides what you type, so the password never appears on screen,
in your shell history, or in any file. Keychain encrypts it and only
releases it to programs running as you, on this Mac.
"""
import getpass
import sys

import keyring

SERVICE = "email-organiser"  # the label the Keychain stores it under


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python -m email_organiser.store_password <account name>")
    name = sys.argv[1]
    password = getpass.getpass(f"App password for {name} (hidden): ").strip()
    if not password:
        sys.exit("Nothing entered; nothing saved.")
    keyring.set_password(SERVICE, name, password)
    print(f"Saved. Check it in Keychain Access under '{SERVICE}' / '{name}'.")


if __name__ == "__main__":
    main()
