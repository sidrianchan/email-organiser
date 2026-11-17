"""Where your personal files live: OUTSIDE the code folder.

    ~/.email-organiser/
        accounts.toml   your account names, servers and addresses (no passwords)
        mail.db         the database: seen emails, labels, summaries
        logs/           what each scheduled run printed
        preview.html    the last summary preview
        telegram_chat   which Telegram chat gets the ping (the bot token is in Keychain)
        profile.md      your CV in brief, for the job-alert screener
        applied.tsv     jobs you've applied to (company<TAB>title...), never suggested again

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
TELEGRAM_CHAT = HOME / "telegram_chat"
PROFILE = HOME / "profile.md"     # who the job screener scores against (no name or contacts)
APPLIED = HOME / "applied.tsv"    # jobs already applied to: never suggested again
