"""Where your personal files live: OUTSIDE the code folder.

    ~/.email-organiser/
        accounts.toml   your account names, servers and addresses (no passwords)
        mail.db         the database: seen emails, labels, summaries
        logs/           what each scheduled run printed
        preview.html    the last summary preview
        telegram_chat   which Telegram chat gets the ping (the bot token is in Keychain)
        profile.md      your CV in brief, for the job-alert screener
        applied.tsv     jobs you've applied to (company<TAB>title...), never suggested again
        backups/        a copy of mail.db per day, newest 7

Keeping personal data away from the code means:
  * the code can be public on GitHub with nothing personal anywhere near it, and
  * more than one copy of the code (e.g. the stable copy the schedule runs) shares
    the same data.
Set EMAIL_ORGANISER_HOME to use a different folder (handy for tests).

Files this program creates are readable by you only (umask 077, 21 Nov): the database holds
email snippets and drafts, and other accounts on the Mac have no reason to read them.
"""
import os
from pathlib import Path

os.umask(0o077)   # new files: owner read/write only (600); new folders: 700

HOME = Path(os.environ.get("EMAIL_ORGANISER_HOME", "~/.email-organiser")).expanduser()
ACCOUNTS = HOME / "accounts.toml"
DB = HOME / "mail.db"
LOGS = HOME / "logs"
PREVIEW = HOME / "preview.html"
TELEGRAM_CHAT = HOME / "telegram_chat"
PROFILE = HOME / "profile.md"     # who the job screener scores against (no name or contacts)
APPLIED = HOME / "applied.tsv"    # jobs already applied to: never suggested again
BACKUPS = HOME / "backups"        # daily database copies, newest 7 (store.backup)
