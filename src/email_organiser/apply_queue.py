"""The apply queue (milestone 9): turning "Would apply" taps into applications.

Why: by 20 Nov Sid had tapped "Would apply" on 51 jobs and "Applied" on 8. Finding jobs had stopped
being the bottleneck; applying was. So each morning, after the summary, one silent Telegram
message lists the jobs to do first (big employers first, then the longest-waiting), each with:
    ✓ Applied  -> the application moves to "applied" and joins applied.tsv (like the job buttons)
    Not now    -> it leaves the queue for 7 days
A job waiting more than 14 days leaves the queue (its ad has probably closed); the message says how
many did. Titles, companies and links are public job ads (Sid's rule, 20 Nov); never email text.
Taps are collected by the existing 1-minute job (feedback.collect).
"""
import html
import json
from datetime import date, datetime, timedelta

from . import notify, pipeline
from .screener import size_rank

SHOW = 5            # jobs per morning: enough to act on, few enough to read
SNOOZE_DAYS = 7
MAX_AGE_DAYS = 14


def age_days(row, today: date) -> int:
    return (today - datetime.fromisoformat(row["created_at"]).date()).days


def queue(db, today: date) -> tuple[list, int, int]:
    """(the jobs to show, how many are in the queue, how many left it after MAX_AGE_DAYS)."""
    rows = db.execute("""SELECT a.*, j.url, j.location, j.employer_size FROM applications a
                         LEFT JOIN jobs j USING (job_key) WHERE a.stage = 'would_apply'""").fetchall()
    snoozed = {r["id"] for r in rows if r["snoozed_until"] and r["snoozed_until"] > f"{today:%Y-%m-%d}"}
    fresh = [r for r in rows if age_days(r, today) <= MAX_AGE_DAYS and r["id"] not in snoozed]
    old = sum(age_days(r, today) > MAX_AGE_DAYS for r in rows)
    fresh.sort(key=lambda r: (size_rank(r["employer_size"]), r["created_at"], r["id"]))
    return fresh[:SHOW], len(fresh), old


def line(n: int, r, today: date) -> str:
    """'1. Graduate Analyst · Acme Bank · waiting 3 days', the title linked if it has a link."""
    e = html.escape
    title = e(r["title"] or "(no title)")
    if (r["url"] or "").startswith("https://"):
        title = f'<a href="{e(r["url"])}">{title}</a>'
    days = age_days(r, today)
    wait = "today" if days == 0 else f"waiting {days} day{'s' if days > 1 else ''}"
    return f"{n}. {title} · {e(r['company'])} · {wait}"


def message(top: list, total: int, old: int, today: date) -> str:
    head = f"<b>🗂 Apply queue</b>\n{total} job{'s' if total != 1 else ''} you said you'd apply to. "
    head += "Do these first:" if top else ""
    tail = f"\n\n{old} waited over {MAX_AGE_DAYS} days and left the queue." if old else ""
    return head + "\n\n" + "\n".join(line(i, r, today) for i, r in enumerate(top, 1)) + tail


def keyboard(db, app_ids: list[int]) -> dict:
    """One row per job: '1 ✓ Applied' + '1 Not now', or a single 'done' button once tapped."""
    rows = []
    for n, app_id in enumerate(app_ids, 1):
        app = db.execute("SELECT stage, snoozed_until FROM applications WHERE id = ?", (app_id,)).fetchone()
        if app is None or app["stage"] != "would_apply":
            rows.append([{"text": f"{n} ✓ Applied", "callback_data": "q:x:0"}])
        elif app["snoozed_until"] and app["snoozed_until"] > f"{date.today():%Y-%m-%d}":
            rows.append([{"text": f"{n} · not now", "callback_data": "q:x:0"}])
        else:
            rows.append([{"text": f"{n} ✓ Applied", "callback_data": f"q:a:{app_id}"},
                         {"text": f"{n} Not now", "callback_data": f"q:n:{app_id}"}])
    return {"inline_keyboard": rows}


def send(db, today: date) -> int:
    """The morning queue message, once a day, silently (the summary ping already rang).
    Returns how many jobs it showed. Raises on a Telegram failure (the caller logs it)."""
    if db.execute("SELECT 1 FROM queue_messages WHERE day = ?", (today.isoformat(),)).fetchone():
        return 0
    top, total, old = queue(db, today)
    if not top:
        return 0
    from .feedback import chat
    token, chat_id = chat()
    ids = [r["id"] for r in top]
    result = notify.call(token, "sendMessage", {
        "chat_id": chat_id, "text": message(top, total, old, today), "parse_mode": "HTML",
        "disable_notification": True, "link_preview_options": {"is_disabled": True},
        "reply_markup": keyboard(db, ids)})
    db.execute("INSERT INTO queue_messages (day, message_id, app_ids) VALUES (?, ?, ?)",
               (today.isoformat(), result["message_id"], json.dumps(ids)))
    db.commit()
    return len(top)


def tap(db, code: str, app_id: int, message_id: int | None) -> tuple[str, dict | None]:
    """One queue button. Returns (what to say, the updated buttons or None)."""
    if code == "x":
        return "", None
    app = db.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    if app is None or code not in ("a", "n"):
        return "Couldn't find that job.", None
    if code == "a":
        pipeline.upsert(db, app["company"], app["title"] or "", "applied", source="telegram queue")
        mark_applied(db, app)
        said = "Saved: applied"
    else:
        db.execute("UPDATE applications SET snoozed_until = ? WHERE id = ?",
                   ((date.today() + timedelta(days=SNOOZE_DAYS)).isoformat(), app_id))
        said = f"Out of the queue for {SNOOZE_DAYS} days"
    db.commit()
    row = db.execute("SELECT app_ids FROM queue_messages WHERE message_id = ?", (message_id,)).fetchone()
    return said, (keyboard(db, json.loads(row["app_ids"])) if row else None)


def mark_applied(db, app) -> None:
    """Same records as the job buttons' Applied: job_feedback, and a line in applied.tsv (so the
    screener never suggests it again)."""
    from .feedback import add_to_applied
    job = db.execute("SELECT * FROM jobs WHERE job_key = ?", (app["job_key"],)).fetchone() if app["job_key"] else None
    if job is not None:
        before = db.execute("SELECT verdict FROM job_feedback WHERE job_key = ?", (job["job_key"],)).fetchone()
        db.execute("INSERT OR REPLACE INTO job_feedback (job_key, verdict, fit_then) VALUES (?, 'applied', ?)",
                   (job["job_key"], job["fit"]))
        if before is not None and before["verdict"] == "applied":
            return
    add_to_applied({"company": app["company"], "title": app["title"] or ""})
