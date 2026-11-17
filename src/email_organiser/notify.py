"""Phone ping after each summary, through a Telegram bot.

The ping carries COUNTS ONLY, never anything from an email (no senders, no subjects).
Telegram can read bot chats (they are not end-to-end encrypted), so nothing personal
goes there. The full summary stays in your inbox; the ping just tells you to look.

    "18:00 mail: 1 job, 1 to reply. Check Yahoo."      -> arrives with sound
    "18:00 mail: nothing needs you (7 routine)."        -> arrives without sound
    "18:00 mail: no new email."                          -> without sound; proves the schedule ran

A ping every run, even when nothing came in: if no ping arrives, the schedule broke.

One-time setup (after creating the bot with @BotFather and sending it "hi"):
    python3 -m uv run python -m email_organiser.notify setup
Send a test ping:
    python3 -m uv run python -m email_organiser.notify test

The bot token is a password: it lives in the macOS Keychain, never in a file or a log.
If Telegram is down or not set up, the summary still goes to your inbox (ping() never raises).
Still no email-sending code anywhere: this talks to Telegram, not to a mail server.
"""
import getpass
import json
import sys
import urllib.request
from datetime import datetime

import keyring

from . import paths

SERVICE = "email-organiser"   # same Keychain label as the app passwords
TOKEN_KEY = "telegram"
API = "https://api.telegram.org/bot{token}/{method}"


def message(counts: dict, total: int, account: str) -> tuple[str, bool]:
    """(text, loud). counts has 'job', 'reply', 'read' as numbers. No email content goes in."""
    stamp = datetime.now().strftime("%H:%M")
    if total == 0:
        return f"{stamp} mail: no new email.", False
    bits = [f"{counts['job']} job" if counts["job"] else "",
            f"{counts['reply']} to reply" if counts["reply"] else "",
            f"{counts['read']} to read" if counts["read"] else ""]
    bits = [b for b in bits if b]
    if not bits:
        return f"{stamp} mail: nothing needs you ({total} routine).", False
    loud = bool(counts["job"] or counts["reply"])  # only these are worth a sound
    return f"{stamp} mail: {', '.join(bits)}. Check {account.capitalize()}.", loud


def call(token: str, method: str, payload: dict | None = None) -> dict:
    """One Telegram Bot API request. Errors never contain the token."""
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            reply = json.load(resp)
    except Exception as e:  # scrub the token in case a library ever puts the URL in the message
        raise RuntimeError(str(e).replace(token, "<token>")) from None
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram said: {reply.get('description', 'unknown error')}")
    return reply["result"]


def send(text: str, loud: bool) -> None:
    token = keyring.get_password(SERVICE, TOKEN_KEY)
    if not token or not paths.TELEGRAM_CHAT.exists():
        raise RuntimeError("Telegram not set up (run: python -m email_organiser.notify setup)")
    chat_id = int(paths.TELEGRAM_CHAT.read_text().strip())
    # disable_notification: the notification still shows, just without sound (Telegram docs).
    call(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_notification": not loud})


def ping(counts: dict, total: int, account: str) -> bool:
    """Send the ping; on any failure print why and carry on. Returns True if it was sent."""
    text, loud = message(counts, total, account)
    try:
        send(text, loud)
        print(f"Telegram ping sent{'' if loud else ' (no sound)'}: {text}")
        return True
    except Exception as e:
        print(f"Telegram ping FAILED (summary unaffected): {e}")
        return False


def setup() -> None:
    """Save the token, find your chat from the "hi" you sent the bot, send a test."""
    token = getpass.getpass("Bot token from @BotFather (hidden): ").strip()
    if not token:
        sys.exit("Nothing entered; nothing saved.")
    me = call(token, "getMe")  # proves the token works before saving it
    updates = call(token, "getUpdates")  # messages people sent the bot (kept up to 24h)
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"]
             for u in updates if "message" in u and u["message"]["chat"]["type"] == "private"}
    if not chats:
        sys.exit(f"Token works (bot @{me['username']}), but nobody has messaged it in the last 24h. "
                 "Send it 'hi' in Telegram, then run setup again.")
    if len(chats) > 1:
        names = ", ".join(c.get("username") or c.get("first_name", "?") for c in chats.values())
        sys.exit(f"More than one person has messaged this bot ({names}). Make a fresh bot "
                 "that only you have messaged.")
    chat_id, chat = next(iter(chats.items()))
    keyring.set_password(SERVICE, TOKEN_KEY, token)
    paths.HOME.mkdir(parents=True, exist_ok=True)
    paths.TELEGRAM_CHAT.write_text(f"{chat_id}\n")
    print(f"Saved. Bot @{me['username']} will message {chat.get('first_name', 'you')}.")
    send("Email organiser connected. You'll get a ping after each summary.", loud=True)
    print("Test ping sent: check Telegram.")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        setup()
    elif cmd == "test":
        try:
            send("TEST ping from the email organiser: not real mail. If you see this, pings work.", loud=True)
            print("Test ping sent: check Telegram.")
        except Exception as e:
            sys.exit(f"Test ping FAILED: {e}")
    else:
        sys.exit("usage: python -m email_organiser.notify setup | test")


if __name__ == "__main__":
    main()
