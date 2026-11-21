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

A small launchd job collects taps every minute (`feedback collect`, getUpdates); it is the only
reader, so two readers never race. Only taps from your own chat count.

    python3 -m uv run python -m email_organiser.feedback backlog          (dry run: what would be sent)
    python3 -m uv run python -m email_organiser.feedback backlog --live   (send past jobs to mark)
    python3 -m uv run python -m email_organiser.feedback collect          (read taps now)
    python3 -m uv run python -m email_organiser.feedback report           (what you've said so far)
"""
import argparse
import html
import json
import random
import sqlite3
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
GROUP_SIZE = 8                            # 'may need experience' jobs per grouped message (22 Nov)
GROUP_MAX_CHARS = 3500                    # Telegram's limit is 4,096; links make job lines long
GROUP_CODES = [("a", "Apply"), ("q", "Not qualified"), ("i", "Not interested")]   # 'Applied': the apply queue


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


def group_line(n: int, j) -> str:
    """'3. Data Analyst · Acme · Hong Kong · closes 30 Nov', the title linked if it has a link."""
    e = html.escape
    title = e(j["title"] or "(no title)")
    if (j["url"] or "").startswith("https://"):
        title = f'<a href="{e(j["url"])}">{title}</a>'
    bits = [e(x) for x in (j["company"], j["location"]) if x]
    if j["closing"]:
        bits.append(f"closes {e(j['closing'])}")
    if "commission" in j.keys() and j["commission"]:
        bits.append("likely commission-based")
    return f"{n}. " + " · ".join([title] + bits)


def group_text(jobs: list, first: int = 1) -> str:
    head = ("<b>🔎 May need experience</b>\nJunior roles and plain titles: the alert doesn't say whether "
            "they take graduates.")
    return head + "\n\n" + "\n".join(group_line(first + i, j) for i, j in enumerate(jobs))


def group_keyboard(db, msg_ids: list[int], first: int = 1) -> dict:
    """One row per job: 'n Apply' | 'n Not qualified' | 'n Not interested', the chosen one ticked.
    A job marked Applied since (the apply queue) shows one inert '✓ Applied' button instead, so a
    stray tap can't move it back to 'would apply'."""
    rows = []
    for n, msg_id in enumerate(msg_ids, first):
        got = db.execute("SELECT f.verdict FROM job_messages m LEFT JOIN job_feedback f USING (job_key) "
                         "WHERE m.id = ?", (msg_id,)).fetchone()
        chosen = got["verdict"] if got else None
        if chosen == "applied":
            rows.append([{"text": f"{n} ✓ Applied", "callback_data": "jx"}])
            continue
        rows.append([{"text": ("✓ " if VERDICTS[code][0] == chosen else "") + f"{n} {text}",
                      "callback_data": f"j:{code}:{msg_id}"} for code, text in GROUP_CODES])
    return {"inline_keyboard": rows}


def chunks(jobs: list) -> list[list]:
    """At most GROUP_SIZE jobs per message, and short enough for Telegram."""
    out, cur = [], []
    for j in jobs:
        if cur and (len(cur) == GROUP_SIZE or len(group_text(cur + [j])) > GROUP_MAX_CHARS):
            out.append(cur)
            cur = []
        cur.append(j)
    return out + ([cur] if cur else [])


def send_group(db, jobs: list) -> int:
    """The 'may need experience' jobs (22 Nov): a few messages listing several jobs each, instead
    of one message per job. Silent, like send_jobs. Returns how many jobs went out. Raises on a
    Telegram failure; jobs already sent stay recorded, so a retry only sends the rest."""
    todo = [j for j in jobs
            if not db.execute("SELECT 1 FROM job_messages WHERE job_key = ?", (j["job_key"],)).fetchone()]
    if not todo:
        return 0
    token, chat_id = chat()
    sent, first = 0, 1
    for part in chunks(todo):
        ids = [db.execute("INSERT INTO job_messages (job_key) VALUES (?)", (j["job_key"],)).lastrowid
               for j in part]
        try:
            result = notify.call(token, "sendMessage", {
                "chat_id": chat_id, "text": group_text(part, first), "parse_mode": "HTML",
                "disable_notification": True, "link_preview_options": {"is_disabled": True},
                "reply_markup": group_keyboard(db, ids, first)})
        except Exception:
            db.rollback()   # not sent: forget these rows, so the next run tries again
            raise
        db.executemany("UPDATE job_messages SET message_id = ? WHERE id = ?",
                       [(result["message_id"], i) for i in ids])
        db.execute("INSERT INTO job_group_messages (message_id, msg_ids) VALUES (?, ?)",
                   (result["message_id"], json.dumps([first] + ids)))   # numbering start, then the ids
        db.commit()
        sent += len(part)
        first += len(part)
        time.sleep(SEND_GAP)
    return sent


def redraw(db, msg_id: int, verdict: str, message_id: int | None) -> dict:
    """The buttons to show after a tap: the whole list for a grouped message, else the job's own four."""
    row = db.execute("SELECT msg_ids FROM job_group_messages WHERE message_id = ?", (message_id,)).fetchone() \
        if message_id else None
    if row:
        first, *ids = json.loads(row["msg_ids"])
        return group_keyboard(db, ids, first)
    return keyboard(msg_id, verdict)


def sync_taps(db) -> list:
    """One-off repair (22 Nov): 'Would apply' / 'Applied' taps that never reached the applications
    table (taps between 21 Nov 02:28 and the fix). Only taps with no application at all; safe to
    re-run. Returns the jobs it filed."""
    rows = db.execute("""SELECT j.*, f.verdict FROM job_feedback f JOIN jobs j USING (job_key)
                         WHERE f.verdict IN ('apply', 'applied')
                         AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_key = f.job_key)
                         ORDER BY f.at""").fetchall()
    for r in rows:
        to_pipeline(db, r, r["verdict"])
    db.commit()
    return rows


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
    to_pipeline(db, row, verdict)
    db.commit()
    if verdict == "applied" and (before is None or before["verdict"] != "applied"):
        add_to_applied(row)
    return verdict


def to_pipeline(db, job, verdict: str) -> None:
    """A tap also files the job in the applications table (fixed 22 Nov: from 21 Nov only the
    one-off seed had done this, so new 'Would apply' taps never reached the apply queue).
    Would apply -> 'would_apply' (the queue); Applied -> 'applied'. A 'no' tap on a job still only
    at 'would_apply' takes it out of the queue ('withdrawn'); anything further along is kept."""
    from . import pipeline
    if verdict in ("apply", "applied"):
        pipeline.upsert(db, job["company"] or "", job["title"] or "",
                        "would_apply" if verdict == "apply" else "applied",
                        source="telegram", job_key=job["job_key"])
        return
    app = db.execute("SELECT * FROM applications WHERE job_key = ? AND stage = 'would_apply'",
                     (job["job_key"],)).fetchone()
    if app is not None:
        db.execute("UPDATE applications SET stage = 'withdrawn', updated_at = datetime('now') WHERE id = ?",
                   (app["id"],))
        pipeline.log(db, app["id"], None, "would_apply", "withdrawn", False, None,
                     f"update, from telegram: you tapped {verdict.replace('_', ' ')}")


def handle(db, update: dict, chat_id: int) -> tuple[str, dict] | None:
    """One Telegram update -> (callback id, what to answer) if it was our button; else None."""
    q = update.get("callback_query")
    if not q:
        return None
    if q.get("from", {}).get("id") != chat_id:   # someone else pressing a forwarded button
        return q["id"], {"text": "Not yours to mark."}
    parts = (q.get("data") or "").split(":")
    if len(parts) == 2 and parts[0] == "d" and parts[1].isdigit():   # Done on a job to-do (21 Nov)
        return q["id"], done_tap(db, int(parts[1]), q.get("message", {}))
    if len(parts) == 3 and parts[0] == "q" and parts[2].isdigit():   # the apply queue (milestone 9)
        from . import apply_queue
        said, markup = apply_queue.tap(db, parts[1], int(parts[2]), q.get("message", {}).get("message_id"))
        return q["id"], {"text": said, "markup": markup, "message": q.get("message", {})}
    if len(parts) != 3 or parts[0] != "j" or not parts[2].isdigit():
        return q["id"], {}
    verdict = record(db, parts[1], int(parts[2]))
    if verdict is None:
        return q["id"], {"text": "Couldn't find that job."}
    return q["id"], {"text": f"Saved: {VERDICTS[parts[1]][1]}", "verdict": verdict, "msg_id": int(parts[2]),
                     "message": q.get("message", {})}


def done_tap(db, app_id: int, message: dict) -> dict:
    """A to-do's Done button: stop reminding, and redraw the buttons on that message."""
    import json
    from . import pipeline
    if app_id == 0:   # an already-done button
        return {}
    marked = pipeline.mark_done(db, app_id)
    row = db.execute("SELECT app_ids FROM todo_messages WHERE message_id = ?",
                     (message.get("message_id"),)).fetchone()
    markup = pipeline.todo_keyboard(db, json.loads(row["app_ids"])) if row else None
    return {"text": "Marked done: no more reminders" if marked else "Already done", "markup": markup,
            "message": message}


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
                if ans.get("markup") and ans["message"].get("message_id"):   # an apply-queue tap
                    stored += 1
                    try:
                        notify.call(token, "editMessageReplyMarkup", {
                            "chat_id": chat_id, "message_id": ans["message"]["message_id"],
                            "reply_markup": ans["markup"]})
                    except RuntimeError as e:
                        if "not modified" not in str(e):
                            raise
                elif "verdict" in ans:
                    stored += 1
                    m = ans["message"]
                    if m.get("message_id"):   # tick the chosen button
                        try:
                            notify.call(token, "editMessageReplyMarkup", {
                                "chat_id": chat_id, "message_id": m["message_id"],
                                "reply_markup": redraw(db, ans["msg_id"], ans["verdict"], m["message_id"])})
                        except RuntimeError as e:   # the same button tapped twice: already ticked
                            if "not modified" not in str(e):
                                raise
        except sqlite3.OperationalError as e:   # database busy (a summary writing): keep this tap
            db.rollback()                            # unread, so Telegram sends it again next minute
            print(f"Job feedback: database busy, tap kept for the next run ({e})")
            break
        except Exception as e:   # one bad update must not block the rest
            print(f"Job feedback: one tap not handled ({e})")
        OFFSET.write_text(str(u["update_id"] + 1))   # read: Telegram won't send it again
    if stored:
        print(f"Job feedback: {stored} tap(s) stored")
    return stored


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
    parser.add_argument("command", choices=["backlog", "collect", "report", "sync-taps"])
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    db = store.open_db()
    if args.command == "sync-taps":
        from .pipeline import scratch_copy
        work = db if args.live else scratch_copy(db)
        for r in sync_taps(work):
            print(f"  {r['verdict']:<8} {r['company'][:30]:<30} {r['title'][:50]}")
        print("(filed)" if args.live else "(dry run on a copy: nothing kept)")
        return
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
