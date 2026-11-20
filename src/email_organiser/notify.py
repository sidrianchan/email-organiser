"""Phone ping after each summary, through a Telegram bot.

The mail part carries COUNTS ONLY, never anything from an email (no senders, no subjects).
Telegram can read bot chats (they are not end-to-end encrypted), so email text never goes
there. The full summary stays in your inbox; the ping just tells you to look.

Since 21 Nov the 08:00 ping also carries the day's brief (digest.today_brief): event and
reminder titles, times, what to prepare, Hong Kong weather. Sid's rule from 20 Nov allows
names and dates in Telegram; email text is still never sent.

    "18:00 mail: 1 job, 1 to reply. Check Yahoo."      -> arrives with sound
    "18:00 mail: nothing needs you (7 routine)."        -> arrives without sound
    "18:00 mail: no new email."                          -> without sound; proves the schedule ran
    "08:00 mail: ⚠️ 5 not labelled (Claude failed: run /login). Check Yahoo."  -> with sound

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
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

import keyring

from . import paths

SERVICE = "email-organiser"   # same Keychain label as the app passwords
TOKEN_KEY = "telegram"
API = "https://api.telegram.org/bot{token}/{method}"


def message(counts: dict, total: int, account: str, today: dict | None = None) -> tuple[str, bool]:
    """(text, loud). counts has 'job', 'reply', 'read' (and optionally 'jobs': good jobs
    found by the screener) as numbers. No email content goes in.
    today: the morning brief {'sections': [...], 'loud': bool} (digest.today_brief). Then the
    message is Telegram HTML: the brief's sections, then a Mail section, a blank line between."""
    text, loud = mail_line(counts, total, account)
    if today and today.get("sections"):
        body = text.split(" mail: ", 1)[-1]               # '08:00 mail: 1 job. Check Yahoo.' -> '1 job. ...'
        mail = f"<b>📬 Mail</b>\n{html.escape(body[:1].upper() + body[1:])}"
        return "\n\n".join(today["sections"] + [mail]), loud or bool(today.get("loud"))
    return text, loud


def is_html(today: dict | None) -> bool:
    """The morning message is formatted (bold headings); every other ping is plain text."""
    return bool(today and today.get("sections"))


def plain(text: str) -> str:
    """The formatted message without its formatting, for logs and as a fallback."""
    return html.unescape(re.sub(r"</?b>", "", text))


def mail_line(counts: dict, total: int, account: str) -> tuple[str, bool]:
    """The counts-only mail line, e.g. '08:00 mail: 1 job, 3 to read. Check Yahoo.'"""
    stamp = datetime.now().strftime("%H:%M")
    if total == 0:
        return f"{stamp} mail: no new email.", False
    good = counts.get("jobs", 0)
    failed = counts.get("unlabelled", 0)
    bits = [f"⚠️ {failed} not labelled (Claude failed: run /login)" if failed else "",
            f"{good} good job{'s' if good > 1 else ''}" if good else "",
            f"{counts['job']} job" if counts["job"] else "",
            f"{counts.get('updates', 0)} job update{'s' if counts.get('updates', 0) > 1 else ''}"
            if counts.get("updates") else "",   # status news (milestone 9): listed, never a sound
            f"{counts['reply']} to reply" if counts["reply"] else "",
            f"{counts['read']} to read" if counts["read"] else ""]
    bits = [b for b in bits if b]
    if not bits:
        return f"{stamp} mail: nothing needs you ({total} routine).", False
    loud = bool(counts["job"] or counts["reply"] or good or failed)  # only these are worth a sound
    return f"{stamp} mail: {', '.join(bits)}. Check {account.capitalize()}.", loud


def call(token: str, method: str, payload: dict | None = None) -> dict:
    """One Telegram Bot API request. Errors never contain the token."""
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            reply = json.load(resp)
    except urllib.error.HTTPError as e:   # Telegram explains a 400 in the body: keep that
        try:
            why = json.load(e).get("description", "")
        except Exception:
            why = ""
        raise RuntimeError(f"Telegram said: {why or e}".replace(token, "<token>")) from None
    except Exception as e:  # scrub the token in case a library ever puts the URL in the message
        raise RuntimeError(str(e).replace(token, "<token>")) from None
    if not reply.get("ok"):
        raise RuntimeError(f"Telegram said: {reply.get('description', 'unknown error')}")
    return reply["result"]


def send(text: str, loud: bool, html_mode: bool = False, markup: dict | None = None) -> int | None:
    """html_mode: text uses Telegram's HTML formatting (only <b> here). If Telegram can't read
    the formatting, the same message goes again as plain text rather than not at all.
    markup: buttons under the message (the job to-dos' Done buttons). Returns Telegram's message id."""
    token = keyring.get_password(SERVICE, TOKEN_KEY)
    if not token or not paths.TELEGRAM_CHAT.exists():
        raise RuntimeError("Telegram not set up (run: python -m email_organiser.notify setup)")
    chat_id = int(paths.TELEGRAM_CHAT.read_text().strip())
    # disable_notification: the notification still shows, just without sound (Telegram docs).
    payload = {"chat_id": chat_id, "text": text, "disable_notification": not loud}
    if markup:
        payload["reply_markup"] = markup
    if not html_mode:
        return (call(token, "sendMessage", payload) or {}).get("message_id")
    try:
        result = call(token, "sendMessage", payload | {"parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
    except RuntimeError as e:
        if "parse" not in str(e).lower():   # e.g. "can't parse entities"; anything else is a real failure
            raise
        print(f"Telegram couldn't read the formatting ({e}); sending it plain")
        result = call(token, "sendMessage", payload | {"text": plain(text)})
    return (result or {}).get("message_id")


def ping(counts: dict, total: int, account: str, today: dict | None = None):
    """Send the ping; on any failure print why and carry on. Returns Telegram's message id (or
    True if it gave none) when sent, False when not."""
    text, loud = message(counts, total, account, today)
    try:
        if is_html(today):
            sent = send(text, loud, html_mode=True, markup=today.get("markup"))
        else:
            sent = send(text, loud)
        print(f"Telegram ping sent{'' if loud else ' (no sound)'}: {plain(text)}")
        return sent or True
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
