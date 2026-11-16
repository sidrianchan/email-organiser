"""Where your personal files live: OUTSIDE the code folder.

    ~/.email-organiser/
        accounts.toml   your account names, servers and addresses (no passwords)
        mail.db         the database: seen emails, labels, summaries
        logs/           what each scheduled run printed
        preview.html    the last summary preview

Keeping personal data away from the code means:
  * the code can be public on GitHub with nothing personal anywhere near it, and
  * more than one copy of the code (e.g. the stable copy the schedule runs) shares
    the same data.
Set EMAIL_ORGANISER_HOME to use a different folder (handy for tests).
"""
import os
from pathlib import Path

HOME = Path(os.environ.get("EMAIL_ORGANISER_HOME", "~/.email-organiser")).expanduser()
ACCOUNTS = HOME / "accounts.toml"
DB = HOME / "mail.db"
LOGS = HOME / "logs"
PREVIEW = HOME / "preview.html"
