"""Your verdict on each screened job, from buttons in Telegram (added 20 Nov).

Why: the screener had shown 67 "high fit" jobs and nothing recorded what you made of them,
so there was no way to tell a better screener from a worse one. Each high job now arrives in
Telegram as its own message with four buttons:

    Would apply | Applied | Not qualified | Not interested

"Not qualified" and "Not interested" are kept apart on purpose: the first means the screener
misjudged the requirements, the second that it misjudged what you want. "Applied" also adds the
job to applied.tsv, so it's never suggested again. You can change your mind: the latest tap wins.

What goes to Telegram (Sid agreed, 20 Nov): the job's title, company, location, closing date and
link. These are public job ads, not your email. Nothing from any other email goes there.
Link previews are switched off, so Telegram's servers never open the link (job-board links can
carry tracking).

The 15-minute check collects taps (getUpdates). Only taps from your own chat count.

    python3 -m uv run python -m email_organiser.feedback backlog          (dry run: what would be sent)
    python3 -m uv run python -m email_organiser.feedback backlog --live   (send past jobs to mark)
    python3 -m uv run python -m email_organiser.feedback collect          (read taps now)
    python3 -m uv run python -m email_organiser.feedback report           (what you've said so far)
"""
import argparse
import random
import sys
import time
from datetime import date

import keyring

from . import notify, paths, store

VERDICTS = {  # callback code -> (stored verdict, button text)
    "a": ("apply", "Would apply"),
    "d": ("applied", "Applied"),
    "q": ("not_qualified", "Not qualified"),
    "i": ("not_interested", "Not interested"),
}
OFFSET = paths.HOME / "telegram_offset"   # the last Telegram update already read
BACKLOG_MEDIUM = 15                       # medium jobs mixed into the backlog: did it miss good ones?
SEND_GAP = 1.1                            # seconds between messages (Telegram: ~1 per second per chat)


def job_text(j, fit_note: str = "") -> str:
    where = ", ".join(x for x in (j["company"], j["location"]) if x)
    lines = [j["title"], where]
    if j["closing"]:
        lines.append(f"Closes {j['closing']}")
    if "commission" in j.keys() and j["commission"]:
        lines.append("Likely commission-based")
    if fit_note:
        lines.append(fit_note)
    if j["url"]:
        lines.append(j["url"])
    return "\n".join(lines)


def keyboard(msg_id: int, chosen: str | None = None) -> dict:
    """Two rows of two buttons. The chosen one gets a tick, and all stay tappable."""
    def button(code):
        verdict, text = VERDICTS[code]
        return {"text": ("✓ " if verdict == chosen else "") + text, "callback_data": f"j:{code}:{msg_id}"}
    return {"inline_keyboard": [[button("a"), button("d")], [button("q"), button("i")]]}


def chat() -> tuple[str, int]:
    token = keyring.get_password(notify.SERVICE, notify.TOKEN_KEY)
    if not token or not paths.TELEGRAM_CHAT.exists():
        raise RuntimeError("Telegram not set up")
    return token, int(paths.TELEGRAM_CHAT.read_text().strip())


def send_jobs(db, jobs: list, fit_note=lambda j: "") -> int:
    """Send each job not sent before, silently. Returns how many went out. Raises on a
    Telegram failure; jobs already sent stay recorded, so a retry only sends the rest."""
    token, chat_id = chat()
    sent = 0
    for j in jobs:
        if db.execute("SELECT 1 FROM job_messages WHERE job_key = ?", (j["job_key"],)).fetchone():
            continue
        msg_id = db.execute("INSERT INTO job_messages (job_key) VALUES (?)", (j["job_key"],)).lastrowid
        try:
            result = notify.call(token, "sendMessage", {
                "chat_id": chat_id, "text": job_text(j, fit_note(j)),
                "disable_notification": True,                    # the summary ping already rang
                "link_preview_options": {"is_disabled": True},   # Telegram never opens the link
                "reply_markup": keyboard(msg_id)})
        except Exception:
            db.rollback()   # not sent: forget the row, so the next run tries again
            raise
        db.execute("UPDATE job_messages SET message_id = ? WHERE id = ?", (result["message_id"], msg_id))
        db.commit()
        sent += 1
        time.sleep(SEND_GAP)
    return sent


def add_to_applied(j) -> None:
    """Same format as the rest of applied.tsv: company, title, status, date."""
    line = "\t".join([j["company"] or "", j["title"] or "", "Marked applied in Telegram",
                      date.today().isoformat()])
    existing = paths.APPLIED.read_text() if paths.APPLIED.exists() else ""
    with paths.APPLIED.open("a") as f:
        f.write(("\n" if existing and not existing.endswith("\n") else "") + line + "\n")


def record(db, code: str, msg_id: int) -> str | None:
    """Store one tap. Returns the stored verdict, or None if the tap doesn't match a sent job."""
    if code not in VERDICTS:
        return None
    row = db.execute("""SELECT j.* FROM job_messages m JOIN jobs j USING (job_key)
                        WHERE m.id = ?""", (msg_id,)).fetchone()
    if row is None:
        return None
    verdict = VERDICTS[code][0]
    before = db.execute("SELECT verdict FROM job_feedback WHERE job_key = ?", (row["job_key"],)).fetchone()
    db.execute("INSERT OR REPLACE INTO job_feedback (job_key, verdict, fit_then) VALUES (?, ?, ?)",
               (row["job_key"], verdict, row["fit"]))
    db.commit()
    if verdict == "applied" and (before is None or before["verdict"] != "applied"):
        add_to_applied(row)
    return verdict


def handle(db, update: dict, chat_id: int) -> tuple[str, dict] | None:
    """One Telegram update -> (callback id, what to answer) if it was our button; else None."""
    q = update.get("callback_query")
    if not q:
        return None
    if q.get("from", {}).get("id") != chat_id:   # someone else pressing a forwarded button
        return q["id"], {"text": "Not yours to mark."}
    parts = (q.get("data") or "").split(":")
    if len(parts) != 3 or parts[0] != "j" or not parts[2].isdigit():
        return q["id"], {}
    verdict = record(db, parts[1], int(parts[2]))
    if verdict is None:
        return q["id"], {"text": "Couldn't find that job."}
    return q["id"], {"text": f"Saved: {VERDICTS[parts[1]][1]}", "verdict": verdict, "msg_id": int(parts[2]),
                     "message": q.get("message", {})}


def collect(db) -> int:
    """Read new button taps and store them. Never raises. Returns how many were stored."""
    try:
        token, chat_id = chat()
        offset = int(OFFSET.read_text()) if OFFSET.exists() else 0
        updates = notify.call(token, "getUpdates", {"offset": offset, "timeout": 0,
                                                    "allowed_updates": ["callback_query"]})
    except Exception as e:
        print(f"Job feedback: couldn't read Telegram ({e})")
        return 0
    stored = 0
    for u in updates:
        try:
            got = handle(db, u, chat_id)
            if got:
                qid, ans = got
                try:   # stops the button's spinner; fails harmlessly if the tap is old
                    notify.call(token, "answerCallbackQuery", {"callback_query_id": qid, "text": ans.get("text", "")})
                except Exception:
                    pass
                if "verdict" in ans:
                    stored += 1
                    m = ans["message"]
                    if m.get("message_id"):   # tick the chosen button
                        notify.call(token, "editMessageReplyMarkup", {
                            "chat_id": chat_id, "message_id": m["message_id"],
                            "reply_markup": keyboard(ans["msg_id"], ans["verdict"])})
        except Exception as e:   # one bad update must not block the rest
            print(f"Job feedback: one tap not handled ({e})")
        OFFSET.write_text(str(u["update_id"] + 1))   # read: Telegram won't send it again
    if stored:
        print(f"Job feedback: {stored} tap(s) stored")
    return stored


def step(db, live: bool) -> None:
    """For the 15-minute check. Never raises."""
    if live:
        collect(db)


def backlog(db) -> list:
    """Every high job, plus a fixed random sample of medium ones (to see what the screener missed).
    Jobs already applied to are left out."""
    from .screener import already_applied, load_applied
    applied = load_applied()
    high = db.execute("SELECT * FROM jobs WHERE fit = 'high' ORDER BY found_at").fetchall()
    medium = db.execute("SELECT * FROM jobs WHERE fit = 'medium' ORDER BY job_key").fetchall()
    sample = random.Random(20).sample(list(medium), min(BACKLOG_MEDIUM, len(medium)))
    return [j for j in high + sample if not already_applied(dict(j), applied)]


def report(db) -> str:
    rows = db.execute("SELECT fit_then, verdict, COUNT(*) n FROM job_feedback GROUP BY 1, 2").fetchall()
    if not rows:
        return "No job feedback yet."
    out = []
    for fit in ("high", "medium", "low"):
        mine = {r["verdict"]: r["n"] for r in rows if r["fit_then"] == fit}
        if mine:
            out.append(f"{fit}: " + ", ".join(f"{n} {v.replace('_', ' ')}" for v, n in mine.items()))
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["backlog", "collect", "report"])
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    db = store.open_db()
    if args.command == "collect":
        collect(db)
    elif args.command == "report":
        print(report(db))
    else:
        jobs = backlog(db)
        note = lambda j: "(The screener rated this medium.)" if j["fit"] == "medium" else ""
        if not args.live:
            for j in jobs:
                print("---\n" + job_text(j, note(j)))
            print(f"\n(dry run: {len(jobs)} jobs would be sent; nothing sent)")
            return
        print(f"Sending {len(jobs)} jobs (about {len(jobs) * SEND_GAP / 60:.0f} min)...")
        print(f"Sent {send_jobs(db, jobs, note)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    main()
