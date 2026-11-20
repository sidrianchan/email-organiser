"""Milestone 3: the twice-daily summary.

One run = fetch new mail -> Claude labels it -> job alerts screened -> reply drafts (milestone 4)
          -> junk to Assistant-Review (milestone 5) -> your day (08:00 only: calendar, Apple Reminders due,
          what needs preparing this week, Hong Kong weather warnings) -> build a summary -> put it in
          your inbox -> Telegram ping (in the morning it carries the day's brief too).
The summary also lists what happened to emails you dragged into an Unsubscribe folder (unsub_folder.py).

Dry run by default (design rule 0): prints the summary and changes nothing.
    python3 -m uv run python -m email_organiser.digest
For real:
    python3 -m uv run python -m email_organiser.digest --live
Preview the design with recent mail, without marking anything as summarised:
    python3 -m uv run python -m email_organiser.digest --sample            (opens in browser)
    python3 -m uv run python -m email_organiser.digest --sample --live     (puts it in your inbox)
    python3 -m uv run python -m email_organiser.digest --sample --morning  (the 08:00 view, at any hour)

"Put it in your inbox" is IMAP APPEND: the summary is placed straight into your own
mailbox. It is NOT sent. There is still no sending code anywhere (design rule 2).
"""
import argparse
import html
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from . import (agenda, apply_queue, classify, cleanup, drafts, feedback, health, learn, notify, paths,
               pipeline, reminders, screener, slots, store, unsub_folder, weather)
from .runlock import run_lock
from .classify import classify_pending
from .fetch import OWN_HEADER, fetch_new
from .mail import connect, load_accounts
from .store import arrived

DELIVER_TO = "yahoo"   # which account's inbox gets the summary
LOOKBACK_HOURS = 26    # each run looks back a bit over 12h x 2, so a late or missed run loses nothing
MAX_AGE_HOURS = 48     # older unsummarised mail is marked done quietly (e.g. the backlog on the first run)
ROUTINE = ["fyi", "newsletter", "promo", "spam"]
GROUP_AT = 3           # in "worth reading", 3+ emails from one sender collapse into one line


def pending(db) -> tuple[list, list]:
    """(recent, too_old): emails that haven't been in a summary yet. Unlabelled ones are
    included (label None), so a Claude failure shows up instead of hiding mail (20 Nov)."""
    rows = db.execute("""
        SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.date, m.first_seen,
               c.label, c.needs_reply, c.reason, dr.uid IS NOT NULL AS drafted,
               mv.uid IS NOT NULL AS moved,
               jr.stage AS jr_stage, jr.company AS jr_company, jr.action_needed AS jr_action,
               jr.action AS jr_what, jr.deadline AS jr_deadline, jr.posting_closed AS jr_closed
        FROM messages m
        LEFT JOIN claude_labels c USING (account, uidvalidity, uid)
        LEFT JOIN job_reads jr USING (account, uidvalidity, uid)
        LEFT JOIN digested d USING (account, uidvalidity, uid)
        LEFT JOIN drafts dr USING (account, uidvalidity, uid)
        LEFT JOIN moved mv USING (account, uidvalidity, uid)
        WHERE d.uid IS NULL
    """).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    recent = sorted((r for r in rows if arrived(r) >= cutoff), key=arrived, reverse=True)
    too_old = [r for r in rows if arrived(r) < cutoff]
    return recent, too_old


def jr(r, field: str):
    """The job pipeline's reading of this email (pipeline.py), or None if it has none."""
    return r[field] if field in r.keys() else None


def is_update(r) -> bool:
    """A job email the pipeline read as status news ('viewed', 'ad closed'): nothing to do, no
    reply needed. It gets one short line under Application updates, not a full row (21 Nov)."""
    return r["label"] == "job" and not r["needs_reply"] and jr(r, "jr_stage") is not None and not jr(r, "jr_action")


def update_lines(rows: list) -> list[str]:
    """One line per application: 'Acme Bank · viewed', 'Beta Ltd · ad closed' (repeats dropped)."""
    return list(dict.fromkeys(pipeline.update_line(
        {"company": jr(r, "jr_company"), "stage": jr(r, "jr_stage"), "posting_closed": jr(r, "jr_closed")})
        for r in rows))


def due_txt(iso: str) -> str:
    return f"due {datetime.fromisoformat(iso):%a %-d %b}"


def sections(rows: list) -> dict:
    """Split emails into the summary's groups. Each email appears in one group only."""
    return {
        "unlabelled": [r for r in rows if r["label"] is None],
        "job": [r for r in rows if r["label"] == "job" and not is_update(r)],
        "updates": [r for r in rows if is_update(r)],
        "reply": [r for r in rows if r["needs_reply"] and r["label"] != "job"],
        "read": [r for r in rows if r["label"] == "read" and not r["needs_reply"]],
        "counts": {name: sum(r["label"] == name for r in rows) for name in ROUTINE},
    }


def ping_counts(rows: list, jobs: list = ()) -> dict:
    """Just the numbers for the Telegram ping: no subjects or senders leave the Mac."""
    g = sections(rows)
    return {k: len(g[k]) for k in ("unlabelled", "job", "updates", "reply", "read")} | {"jobs": len(jobs)}


def collapse(rows: list) -> tuple[list, list]:
    """(singles, groups). Senders with GROUP_AT+ emails become one group: (name, [rows]).

    Only used for "worth reading". Job and reply emails are never collapsed:
    the point is that nothing important gets buried, and grouping could hide one.
    Singles come first, so a lone important email isn't pushed below a noisy sender.
    """
    by_sender: dict[str, list] = {}
    for r in rows:
        by_sender.setdefault(sender_name(r), []).append(r)
    singles = [r for r in rows if len(by_sender[sender_name(r)]) < GROUP_AT]
    groups = [(name, rs) for name, rs in by_sender.items() if len(rs) >= GROUP_AT]
    return singles, groups


def group_subjects(rs: list, limit: int = 4) -> str:
    seen = list(dict.fromkeys(r["subject"] or "(no subject)" for r in rs))  # unique, in order
    more = f" +{len(seen) - limit} more" if len(seen) > limit else ""
    return " / ".join(seen[:limit]) + more


def moved_line(rows: list) -> str:
    """'Moved to Assistant-Review: 3 newsletter, 1 promo.' or '' if nothing moved."""
    moved = [r for r in rows if "moved" in r.keys() and r["moved"]]
    if not moved:
        return ""
    counts = {name: sum(r["label"] == name for r in moved) for name in cleanup.MOVE_LABELS}
    return f"Moved to {cleanup.REVIEW}: " + ", ".join(f"{n} {k}" for k, n in counts.items() if n) + "."


def has_draft(r) -> bool:
    """True if milestone 4 put a reply draft in Drafts for this email."""
    return "drafted" in r.keys() and bool(r["drafted"])  # works for database rows and test dicts


def sender_name(r) -> str:
    return parseaddr(r["sender"])[0] or parseaddr(r["sender"])[1]  # "Jobsdb <x@y>" -> "Jobsdb"


def when(r) -> str:
    t = arrived(r).astimezone()  # your local time
    return t.strftime("%H:%M") if t.date() == datetime.now().date() else t.strftime("%a %H:%M")


def cal_bit(cal) -> str:
    """'1 event today, 1 to do, 1 coming up', or '' when there's nothing (or it couldn't be read)."""
    if not cal:
        return ""
    n, t, u = len(cal["events"] or []), len(cal.get("todo") or []), len(cal["upcoming"])
    return ", ".join(b for b in (f"{n} event{'s' if n > 1 else ''} today" if n else "",
                                 f"{t} to do" if t else "",
                                 f"{u} coming up" if u else "") if b)


def warnings_of(cal) -> list:
    """Hong Kong weather warnings in force this morning (weather.py), e.g. a No. 8 signal."""
    return ((cal or {}).get("weather") or {}).get("warnings") or []


def has_cal(cal) -> bool:
    """Worth a morning summary even with no new mail: events, to-dos or a warning signal."""
    return bool(cal and (cal["events"] or cal["upcoming"] or cal.get("todo") or warnings_of(cal)))


def show_today(cal) -> bool:
    """Whether the summary gets a Today section at all."""
    return bool(cal and (cal["events"] is not None or cal.get("todo") or warnings_of(cal)
                         or weather.line(cal.get("weather"))))


def cal_heading(cal) -> str:
    return f"{cal['label']}, {cal['day']:%a %-d %b}"   # 'Today, Wed 18 Nov'


def cal_note(cal) -> list:
    """A failed calendar read is said out loud, so an empty section is never mistaken for a free day."""
    if not cal:
        return []
    notes = []
    if cal["events"] is None:
        notes.append("Calendar couldn't be read this time, so events are missing from this summary.")
    elif not cal["judged"]:
        notes.append("Couldn't check which upcoming events need preparing this time; retrying tomorrow.")
    if "todo" in cal and cal["todo"] is None:
        notes.append("Reminders couldn't be read this time, so to-dos are missing from this summary.")
    return notes


def failed_bit(g: dict) -> str:
    n = len(g.get("unlabelled", []))
    return f"⚠️ {n} not labelled, Claude failed" if n else ""


def todo_bit(todos) -> str:
    n = len(todos or [])
    return f"{n} job to-do{'s' if n > 1 else ''}" if n else ""


def subject_line(g: dict, total: int, jobs: list = (), cal=None, todos=()) -> str:
    stamp = datetime.now().strftime("%a %H:%M")
    if failed_bit(g):   # never "nothing needs you" when Claude didn't look
        bits = [failed_bit(g), cal_bit(cal), todo_bit(todos),
                f"{len(jobs)} good job{'s' if len(jobs) > 1 else ''}" if jobs else "",
                f"{len(g['job'])} job" if g["job"] else "",
                f"{len(g['updates'])} job update{'s' if len(g['updates']) > 1 else ''}" if g["updates"] else "",
                f"{len(g['reply'])} to reply" if g["reply"] else "",
                f"{len(g['read'])} to read" if g["read"] else ""]
        return f"Mail summary {stamp}: " + ", ".join(b for b in bits if b)
    lead = ", ".join(b for b in (cal_bit(cal), todo_bit(todos)) if b)
    lead += ", " if lead else ""
    if jobs or g["job"] or g["updates"] or g["reply"] or g["read"]:
        bits = [cal_bit(cal), todo_bit(todos),
                f"{len(jobs)} good job{'s' if len(jobs) > 1 else ''}" if jobs else "",
                f"{len(g['job'])} job" if g["job"] else "",
                f"{len(g['updates'])} job update{'s' if len(g['updates']) > 1 else ''}" if g["updates"] else "",
                f"{len(g['reply'])} to reply" if g["reply"] else "",
                f"{len(g['read'])} to read" if g["read"] else ""]
        return f"Mail summary {stamp}: " + ", ".join(b for b in bits if b)
    return f"Mail summary {stamp}: {lead}nothing needs you ({total} routine)"


GROUPS = [  # (key, heading, colour)
    ("job", "Job", "#2563eb"),
    ("reply", "Needs a reply", "#d97706"),
    ("read", "Worth reading", "#059669"),
]


def is_commission(j) -> bool:
    return "commission" in j.keys() and bool(j["commission"])


def job_line(j) -> str:
    """'Data Analyst, Acme, Kwun Tong, closes 30 Nov' (empty parts left out)."""
    closing = f"closes {j['closing']}" if j["closing"] else ""
    return ", ".join(x for x in (j["title"], j["company"], j["location"], closing) if x)


FAILED_HEADING = "Not labelled: Claude failed"


def failed_note(n: int) -> str:
    return (f"Claude couldn't label {n} email{'s' if n > 1 else ''} (listed at the top), so nothing "
            "in them was checked. If Claude is logged out, run /login in Claude Code. They'll be "
            "labelled and shown again in the next summary.")


def footer_line(rows: list) -> str:
    if any(r["label"] is None for r in rows):
        return "Some emails were NOT labelled (see top). Nothing was deleted or sent."
    return "Labelled by Claude. Nothing was deleted or sent."


def build_text(rows: list, jobs: list = (), notes: list = (), cal=None, todos=()) -> str:
    """Plain-text version, for mail apps that don't show HTML."""
    g = sections(rows)
    out = [f"{len(rows)} new emails since the last summary", ""]
    if g["unlabelled"]:   # first: the summary can't vouch for these
        out += [f"⚠️ {FAILED_HEADING.upper()} ({len(g['unlabelled'])})", "-" * 30]
        for r in g["unlabelled"]:
            out += [f"* {r['subject'] or '(no subject)'}", f"  {sender_name(r)} | {r['account']} | {when(r)}", ""]
    if show_today(cal):  # first: the shape of the day
        events = cal["events"] or []
        if events:
            out += [f"{cal_heading(cal).upper()} ({len(events)})", "-" * 30]
            for ev in events:
                out += [f"* {agenda.when(ev, cal['day'])}  {title_of(ev)}"] + \
                       ([f"  Prepare: {ev['prep']}"] if ev["important"] and ev["prep"] else [])
        elif cal["events"] is not None:
            out += [f"{cal_heading(cal).upper()}: nothing on the calendar"]
        else:   # calendar unreadable (said in the footer), but there are to-dos or weather
            out += [cal_heading(cal).upper(), "-" * 30]
        for t in cal.get("todo") or []:
            out += [f"* To do: {title_of(t)} ({reminders.when(t, cal['day'])})"]
        if weather.line(cal.get("weather")):
            out += [f"* Weather: {weather.line(cal.get('weather'))}"]
        out += [f"* ⚠️ {w}" for w in warnings_of(cal)] + [""]
        if cal["upcoming"]:
            out += [f"COMING UP, NEEDS PREPARING ({len(cal['upcoming'])})", "-" * 30]
            for ev in cal["upcoming"]:
                out += [f"* {ev['start']:%a %-d %b} ({agenda.days_away(ev, cal['day'])}), "
                        f"{agenda.when(ev, ev['start'].date())}  {title_of(ev)}"] + \
                       ([f"  Prepare: {ev['prep']}"] if ev["prep"] else []) + [""]
    if todos:   # what your applications are waiting for you to do (21 Nov)
        today = (cal or {}).get("day") or datetime.now().date()
        out += [f"JOB TO-DOS ({len(todos)})", "-" * 30]
        out += [f"* {t['company']}: {t['action']} ({pipeline.due_words(t['due'], today)})" for t in todos] + [""]
    if jobs:
        out += [f"JOBS WORTH APPLYING TO ({len(jobs)})", "-" * 30]
        for j in jobs:
            tag = "  [likely commission-based]" if is_commission(j) else ""
            out += [f"* {job_line(j)}{tag}", f"  {j['reason']}"] + ([f"  {j['url']}"] if j["url"] else []) + [""]
    for key, heading, _ in GROUPS:
        if g[key]:
            out += [f"{heading.upper()} ({len(g[key])})", "-" * 30]
            singles, groups = collapse(g[key]) if key == "read" else (g[key], [])
            for r in singles:
                tag = "  [needs reply]" if r["needs_reply"] and key == "job" else ""
                tag += "  [draft in Drafts]" if has_draft(r) else ""
                todo = (f"  To do: {jr(r, 'jr_what')}" + (f" ({due_txt(jr(r, 'jr_deadline'))})" if jr(r, "jr_deadline") else "")
                        if jr(r, "jr_action") and jr(r, "jr_what") else "")
                out += [f"* {r['subject']}{tag}",
                        f"  {sender_name(r)} | {r['account']} | {when(r)}"] + ([todo] if todo else []) + \
                       [f"  {r['reason']}", ""]
            for name, rs in groups:
                accounts = ", ".join(sorted({r["account"] for r in rs}))
                out += [f"* {name}: {len(rs)} emails",
                        f"  {accounts} | latest {when(rs[0])}",
                        f"  {group_subjects(rs)}", ""]
        if key == "job" and g["updates"]:   # status news: one short line per application
            lines = update_lines(g["updates"])
            out += [f"APPLICATION UPDATES ({len(lines)})", "-" * 30] + [f"* {l}" for l in lines] + [""]
    routine = ", ".join(f"{n} {name}" for name, n in g["counts"].items() if n)
    if routine:
        out.append(f"Everything else: {routine}.")
    if moved_line(rows):
        out.append(moved_line(rows))
    if g["unlabelled"]:
        out.append(failed_note(len(g["unlabelled"])))
    out += list(notes) + cal_note(cal)   # e.g. the weekly labelling check (milestone 7)
    out.append("\n" + footer_line(rows))
    return "\n".join(out)


# Dark mode (21 Nov). Mail.app follows the Mac's dark appearance, and without these rules the
# summary was a bright white card in a dark inbox. The inline styles stay as the light version, so
# a mail app that drops <style> still shows a readable summary; the rules below override them only
# when the reader is in dark mode (Apple Mail on Mac and iPhone supports this).
DARK = {  # class -> dark-mode CSS
    "eo-body": "background:#16181d!important;color:#e5e7eb!important",
    "eo-text": "color:#f3f4f6!important",
    "eo-meta": "color:#b4bac3!important",
    "eo-sub": "color:#9aa1ab!important",
    "eo-faint": "color:#7d848e!important",
    "eo-row": "border-bottom-color:#2b3038!important",
    "eo-box": "background:#1f232a!important;color:#c3c8cf!important",
}
ACCENT_DARK = {  # section and badge colours -> brighter versions that read on a dark background
    "#2563eb": "#7aa7ff", "#d97706": "#f2a33a", "#059669": "#34d399", "#0d9488": "#2dd4bf",
    "#475569": "#a3b1c6", "#b45309": "#f0a95b", "#dc2626": "#f87171", "#b91c1c": "#f87171",
    "#7c3aed": "#b197fc", "#6b7280": "#a1a7b0",
}


def accent(colour: str) -> str:
    """The class that lets dark mode brighten one accent colour."""
    return f"eo-c{colour.lstrip('#')}"


def dark_css() -> str:
    rules = [f".{cls}{{{css}}}" for cls, css in DARK.items()]
    rules += [f".{accent(c)}{{color:{d}!important;border-left-color:{d}!important}}"
              for c, d in ACCENT_DARK.items()]
    return "@media (prefers-color-scheme: dark){" + "".join(rules) + "}"


def build_html(rows: list, jobs: list = (), notes: list = (), cal=None, todos=()) -> str:
    """The nicer version: a whole HTML document. Styles are inline because many mail apps
    strip <style> blocks; the <style> block only adds dark mode on top.

    Every piece of email text goes through html.escape(): a sender could put HTML
    or script in a subject line, and escaping shows it as plain text instead of running it.
    """
    e = html.escape
    g = sections(rows)
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

    def chip(text, colour):
        return (f'<span class="{accent(colour)}" style="display:inline-block;padding:3px 10px;'
                f'margin:0 6px 6px 0;border-radius:12px;background:{colour}1a;color:{colour};'
                f'font-size:13px;font-weight:600">{e(text)}</span>')

    def badge(text, colour):
        return (f' <span class="{accent(colour)}" style="padding:1px 7px;border-radius:9px;'
                f'background:{colour}1a;color:{colour};font-size:11px;font-weight:600">{e(text)}</span>')

    def heading(text, colour, count=None):
        n = f" &middot; {count}" if count is not None else ""
        return (f'<div class="{accent(colour)}" style="margin:22px 0 4px;padding-left:10px;'
                f'border-left:4px solid {colour};font-size:13px;font-weight:700;letter-spacing:.06em;'
                f'text-transform:uppercase;color:{colour}">{e(text)}{n}</div>')

    def item(title_html, meta_html="", sub_html=""):
        """One email or job: bold title, a meta line, a muted reason. Pieces arrive escaped."""
        return ('<div class="eo-row" style="padding:10px 0 10px 14px;border-bottom:1px solid #eef0f2">'
                f'<div class="eo-text" style="font-size:15px;font-weight:600;color:#111827;line-height:1.35">'
                f'{title_html}</div>'
                + (f'<div class="eo-meta" style="font-size:13px;color:#4b5563;margin-top:2px">{meta_html}</div>'
                   if meta_html else "")
                + (f'<div class="eo-sub" style="font-size:13px;color:#6b7280;margin-top:3px">{sub_html}</div>'
                   if sub_html else "")
                + '</div>')

    def day_row(left, right, below="", below_colour="#b45309", left_note=""):
        """A Today line: time (or 'To do') in a left column, what in a right column, a note under
        it. A two-cell table (21 Nov): long titles wrap inside their own column instead of under
        the time, and a long date can't run into the text beside it. Tables are the layout that
        every mail app renders the same way."""
        note = (f'<div class="{accent(below_colour)}" style="font-size:13px;color:{below_colour};'
                f'margin-top:3px">{e(below)}</div>' if below else "")
        small = (f'<div class="eo-sub" style="font-size:12px;font-weight:400;color:#6b7280;margin-top:1px">'
                 f'{e(left_note)}</div>' if left_note else "")
        return ('<table role="presentation" class="eo-row" width="100%" cellpadding="0" cellspacing="0" '
                'style="border-collapse:collapse;border-bottom:1px solid #eef0f2"><tr>'
                '<td class="eo-text" style="width:118px;padding:8px 10px 8px 14px;vertical-align:top;'
                f'font-size:15px;font-weight:600;color:#111827">{e(left)}{small}</td>'
                '<td class="eo-text" style="padding:8px 0;vertical-align:top;font-size:15px;color:#111827">'
                f'{e(right)}{note}</td></tr></table>')

    chips = (chip(f"⚠️ {len(g['unlabelled'])} not labelled", "#dc2626") if g["unlabelled"] else "") + \
        (chip(f"{len(jobs)} good job{'s' if len(jobs) > 1 else ''}", "#0d9488") if jobs else "") + \
        "".join(chip(f"{len(g[k])} {h.lower()}", c) for k, h, c in GROUPS if g[k])
    chips = chips or chip("Nothing needs you", "#6b7280")

    body = []
    if g["unlabelled"]:   # first: the summary can't vouch for these
        body.append(heading(FAILED_HEADING, "#dc2626", len(g["unlabelled"])))
        for r in g["unlabelled"]:
            body.append(item(e(r["subject"] or "(no subject)"),
                             f'{e(sender_name(r))} &middot; {e(r["account"])} &middot; {e(when(r))}'))
    if show_today(cal):  # first: the shape of the day
        events = cal["events"] or []
        body.append(heading(cal_heading(cal), "#475569", len(events) if events else None))
        for ev in events:
            body.append(day_row(agenda.when(ev, cal["day"]), title_of(ev),
                                f"Prepare: {ev['prep']}" if ev["important"] and ev["prep"] else ""))
        if cal["events"] == []:
            body.append('<div class="eo-sub" style="padding:8px 0 8px 14px;font-size:14px;color:#6b7280">'
                        'Nothing on the calendar.</div>')
        for t in cal.get("todo") or []:
            due = reminders.when(t, cal["day"])
            body.append(day_row("To do", title_of(t), due[:1].upper() + due[1:],   # not .capitalize(): keeps 'Wed'
                                "#b91c1c" if t["overdue"] else "#b45309"))

        if weather.line(cal.get("weather")):
            body.append(day_row("Weather", weather.line(cal.get("weather"))))
        for w in warnings_of(cal):
            body.append(day_row("⚠️ Signal", w, "In force now (Hong Kong Observatory)"))
        if cal["upcoming"]:
            body.append(heading("Coming up, needs preparing", "#b45309", len(cal["upcoming"])))
            for ev in cal["upcoming"]:
                time_txt = agenda.when(ev, ev["start"].date())
                body.append(day_row(f"{ev['start']:%a %-d %b}", f"{time_txt} · {title_of(ev)}",
                                    f"Prepare: {ev['prep']}" if ev["prep"] else "",
                                    left_note=agenda.days_away(ev, cal["day"])))
    if todos:   # what your applications are waiting for you to do (21 Nov)
        today = (cal or {}).get("day") or datetime.now().date()
        body.append(heading("Job to-dos", "#dc2626", len(todos)))
        for t in todos:
            soon = t["due"] is not None and (t["due"] - today).days <= 2
            when_txt = pipeline.due_words(t["due"], today)
            body.append(day_row(pipeline.short_company(t["company"]), t["action"] or "action needed",
                                when_txt[:1].upper() + when_txt[1:], "#b91c1c" if soon or t["due"] is None else "#b45309"))
    if jobs:  # then: the point of the whole job hunt
        body.append(heading("Jobs worth applying to", "#0d9488", len(jobs)))
        for j in jobs:
            title = e(j["title"])
            if j["url"]:  # only links the screener checked point at the sending job board
                title = (f'<a class="eo-text" href="{e(j["url"])}" style="color:#111827;'
                         f'text-decoration:underline">{title}</a>')
            if is_commission(j):
                title += badge("likely commission-based", "#b45309")
            where = " &middot; ".join(e(x) for x in (j["company"], j["location"],
                                                    f"closes {j['closing']}" if j["closing"] else "") if x)
            body.append(item(title, where, e(j["reason"] or "")))
    for key, heading_text, colour in GROUPS:
        if not g[key] and not (key == "job" and g["updates"]):
            continue
        if g[key]:
            body.append(heading(heading_text, colour, len(g[key])))
        singles, groups = collapse(g[key]) if key == "read" else (g[key], [])
        for r in singles:
            badges = badge("needs reply", "#d97706") if r["needs_reply"] and key == "job" else ""
            badges += badge("draft ready", "#7c3aed") if has_draft(r) else ""
            badges += badge(due_txt(jr(r, "jr_deadline")), "#dc2626") if key == "job" and jr(r, "jr_deadline") else ""
            todo = e(f"To do: {jr(r, 'jr_what')}") + " &middot; " if jr(r, "jr_action") and jr(r, "jr_what") else ""
            body.append(item(e(r["subject"] or "(no subject)") + badges,
                             f'{e(sender_name(r))} &middot; {e(r["account"])} &middot; {e(when(r))}',
                             todo + e(r["reason"] or "")))
        for name, rs in groups:
            accounts = ", ".join(sorted({r["account"] for r in rs}))
            body.append(item(e(name) + badge(f"{len(rs)} emails", "#6b7280"),
                             f'{e(accounts)} &middot; latest {e(when(rs[0]))}', e(group_subjects(rs))))
        if key == "job" and g["updates"]:   # status news: one short line per application
            lines = update_lines(g["updates"])
            body.append(heading("Application updates", "#475569", len(lines)))
            body += [f'<div class="eo-row eo-meta" style="padding:6px 0 6px 14px;border-bottom:1px solid #eef0f2;'
                     f'font-size:14px;color:#4b5563">{e(l)}</div>' for l in lines]

    routine = " &middot; ".join(f"{n} {e(name)}" for name, n in g["counts"].items() if n)
    extra = ([moved_line(rows)] if moved_line(rows) else []) + \
        ([failed_note(len(g["unlabelled"]))] if g["unlabelled"] else []) + list(notes) + cal_note(cal)
    footer = ('<div class="eo-box" style="margin-top:22px;padding:12px 14px;border-radius:8px;'
              f'background:#f6f7f9;font-size:13px;color:#4b5563">Everything else: {routine or "none"}'
              + "".join(f'<div style="margin-top:4px">{e(n)}</div>' for n in extra) + '</div>')
    stamp = datetime.now().strftime("%a %d %b &middot; %H:%M")

    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            '<meta name="supported-color-schemes" content="light dark">'
            f'<style>:root{{color-scheme:light dark}}{dark_css()}</style></head>'
            '<body class="eo-body" style="margin:0;padding:0;background:#ffffff;color:#111827">'
            f'<div class="eo-body" style="font-family:{font};max-width:600px;margin:0 auto;padding:8px 4px;'
            'background:#ffffff;color:#111827">'
            f'<div class="eo-sub" style="font-size:13px;color:#6b7280">{stamp}</div>'
            '<div style="font-size:22px;font-weight:700;margin:2px 0 10px">Mail summary</div>'
            f'<div>{chips}</div>'
            f'<div class="eo-sub" style="font-size:13px;color:#6b7280">{len(rows)} new emails since the last summary</div>'
            + "".join(body) + footer +
            '<div class="eo-faint" style="margin-top:14px;font-size:12px;color:#9ca3af">'
            f'{e(footer_line(rows))}</div></div></body></html>')


def title_of(item: dict) -> str:
    """An event's or reminder's short label (shorten.py) if it has one, else its own title."""
    return item.get("short") or item.get("title") or "(no title)"


def short(text: str, limit: int = 80) -> str:
    """One line, at most `limit` characters (Telegram brief)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def start_time(ev: dict) -> str:
    """'10:25 PM' or 'All day'."""
    return "All day" if ev["allday"] else f"{agenda.clock(ev['start'])} {ev['start']:%p}"


def today_brief(cal) -> dict | None:
    """The morning brief that rides on the 08:00 Telegram message.

    21 Nov, after Sid's first look ("too stacked", "a bit long"): one section per kind of thing,
    each with a bold heading and a blank line before the next; one short line per item, using
    the short labels from shorten.py; times and dates come from the calendar and Reminders
    themselves. Names and dates only (Sid's rule, 20 Nov), never anything from an email.
    Telegram HTML formatting, so every piece of text is escaped.
    {'sections': [...], 'loud': bool}, or None when there's no morning view (evening runs)."""
    if not cal:
        return None
    e = html.escape
    day, events = cal["day"], cal["events"] or []

    def label(item):
        return e(short(title_of(item), 60))

    sections = [f"<b>{e(day.strftime('%A %-d %b'))}</b>"]
    if cal["events"] is None:
        cal_lines = ["Couldn't read the calendar"]
    elif not events:
        cal_lines = ["Nothing on the calendar"]
    else:
        cal_lines = [f"{e(agenda.when(ev, day).replace(' – ', '–'))} · {label(ev)}" for ev in events[:8]]
        cal_lines += [f"+{len(events) - 8} more"] if len(events) > 8 else []
    sections.append("<b>📅 Calendar</b>\n" + "\n".join(cal_lines))
    todo = [f"{label(t)} · {e(reminders.when(t, day, compact=True))}" for t in cal.get("todo") or []]
    todo += [todo_line(t, day) for t in cal.get("job_todos") or []]
    if "todo" in cal and cal["todo"] is None:
        todo.append("Couldn't read Reminders")
    if todo:
        sections.append("<b>✅ To do</b>\n" + "\n".join(todo))
    if cal["upcoming"]:
        rows = []
        for ev in cal["upcoming"][:3]:
            date_txt = ev["start"].strftime("%a %-d %b")
            rows.append(f"{e(date_txt)} ({e(agenda.days_away(ev, day))}) · {e(start_time(ev))} · {label(ev)}")
            prep = ev.get("prep_short") or ev.get("prep")
            if prep:
                rows.append(f"↳ {e(short(prep, 60))}")
        sections.append("<b>⏳ Coming up</b>\n" + "\n".join(rows))
    if cal.get("pipeline"):
        sections.append(f"<b>💼 Jobs</b>\n{e(cal['pipeline'])}")
    sky = weather.line(cal.get("weather"), brief=True)
    if sky or warnings_of(cal):
        sections.append("<b>🌤 Weather</b>\n" + "\n".join(
            ([e(sky)] if sky else []) + [f"⚠️ {e(w)}" for w in warnings_of(cal)]))
    busy = bool(events or cal.get("todo") or cal.get("job_todos") or cal["upcoming"] or warnings_of(cal))
    return dict(sections=sections, loud=busy, todo_ids=[t["id"] for t in cal.get("job_todos") or []])


def todo_line(t: dict, today) -> str:
    """'Shell · Complete Virtual Job Tryout and video interview · due Thu 26 Nov' (escaped)."""
    e = html.escape
    return (f"{e(pipeline.short_company(t['company']))} · {e(short(t['action'] or 'action needed', 50))} · "
            f"{e(pipeline.due_words(t['due'], today))}")


def todo_brief(todos, today) -> dict | None:
    """The evening ping's reminder (21 Nov): your job to-dos above the mail line, with Done buttons.
    None when there's nothing to do."""
    if not todos:
        return None
    return dict(sections=["<b>📝 Job to-dos</b>\n" + "\n".join(todo_line(t, today) for t in todos)],
                loud=False, todo_ids=[t["id"] for t in todos])


def deliver(subject: str, text: str, html_body: str) -> None:
    """Place the summary in DELIVER_TO's inbox (IMAP APPEND). Not sending."""
    account = next(a for a in load_accounts() if a["name"] == DELIVER_TO)
    msg = EmailMessage()
    msg["From"] = f"Email Organiser <{account['username']}>"
    msg["To"] = account["username"]
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="email-organiser.local")
    msg[OWN_HEADER] = "summary"  # so fetch.py never labels our own summary as mail
    msg.set_content(text)                       # plain version
    msg.add_alternative(html_body, subtype="html")  # nicer version; the mail app picks the best
    with connect(account) as client:
        # No \Seen flag: it arrives unread, like new mail.
        client.append("INBOX", msg.as_bytes(), flags=(), msg_time=datetime.now(timezone.utc))


def mark_done(db, rows: list) -> None:
    """Unlabelled emails stay pending (unless past MAX_AGE_HOURS), so the next summary shows
    them again with their labels."""
    db.executemany("INSERT OR IGNORE INTO digested (account, uidvalidity, uid) VALUES (?, ?, ?)",
                   [(r["account"], r["uidvalidity"], r["uid"]) for r in rows])
    db.commit()


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)   # keep the log in order (errors aren't buffered, prints were)
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really deliver the summary")
    parser.add_argument("--sample", action="store_true",
                        help="preview with the last 48h of mail; never marks anything as summarised")
    parser.add_argument("--force", action="store_true",
                        help="run even if this 08:00/18:00 summary was already delivered")
    parser.add_argument("--morning", action="store_true",
                        help="with --sample: show the 08:00 view (calendar, reminders, weather) at any hour")
    args = parser.parse_args()
    # Wait for a running 15-minute check to finish (up to 15 min), so the two never label or
    # draft the same email. If it's somehow still running, go ahead: a late summary is worse.
    with run_lock(wait_seconds=900) as got:
        if not got:
            print("quick check still running after 15 min: summarising anyway")
        if args.live and not args.sample:
            summarise_recorded("digest", force=args.force)
        else:
            morning = datetime.now().replace(hour=8, minute=0, second=0, microsecond=0)
            summarise(args.live, args.sample, slot=morning if args.morning else None)


def with_buttons(db, brief):
    """The brief plus its Done buttons (pipeline.todo_keyboard), if it has job to-dos."""
    if not brief or not brief.get("todo_ids"):
        return brief
    return brief | {"markup": pipeline.todo_keyboard(db, brief["todo_ids"])}


def remember_buttons(db, message_id, brief) -> None:
    """Which to-dos a sent ping carries, so a Done tap can redraw its buttons. Never raises."""
    if not message_id or not brief or not brief.get("todo_ids"):
        return
    try:
        db.execute("INSERT OR REPLACE INTO todo_messages (message_id, app_ids) VALUES (?, ?)",
                   (int(message_id), json.dumps(brief["todo_ids"])))
        db.commit()
    except Exception as e:
        print(f"Couldn't record the Done buttons (taps on them won't redraw): {e}")


def send_queue(db, cal) -> None:
    """Mornings only (cal exists): the apply queue message (apply_queue.py). Never raises."""
    if not cal:
        return
    try:
        n = apply_queue.send(db, cal["day"])
        if n:
            print(f"Telegram: apply queue sent ({n} jobs)")
    except Exception as e:
        print(f"Apply queue FAILED (summary unaffected): {e}")


def summarise_recorded(via: str, now: datetime | None = None, force: bool = False) -> bool:
    """A real summary, recorded in summary_runs (slots.py) so a missing one can be caught up.
    Skips if this slot's summary was already delivered (e.g. the 15-minute check caught it up
    first). Returns True if it ran; a failure is recorded, then raised as before."""
    now = now or datetime.now()
    slot = slots.current_slot(now)
    db = store.open_db()
    if not force and slots.done(db, slot):
        print(f"=== {now:%Y-%m-%d %H:%M} digest (LIVE) === the {slot:%H:%M} summary was already "
              f"delivered: nothing to do (--force runs it anyway)")
        return False
    try:
        summarise(live=True, sample=False, slot=slot)
    except Exception as e:
        slots.record(db, slot, now, via, error=e)
        raise
    slots.record(db, slot, now, via)
    store.backup(db)   # once a day (the first real run of the day); never raises
    return True


def summarise(live: bool, sample: bool, slot: datetime | None = None):
    """The summary run. Returns (database, live); database is None for a --sample preview.
    `slot` is the 08:00/18:00 it belongs to: it decides whether the calendar is included."""
    mode =("LIVE" if live else "dry run") + (" sample" if sample else "")
    print(f"=== {datetime.now():%Y-%m-%d %H:%M} digest ({mode}) ===")

    db = store.open_db()
    step_notes = []   # steps that failed this run, said in the summary instead of only in the log
    if sample:
        db.execute("CREATE TEMP TABLE digested AS SELECT * FROM main.digested WHERE 0")  # pretend nothing was summarised
    else:
        claude_error = classify.health_check()   # one tiny call: is Claude reachable at all?
        if claude_error:
            print(f"Claude health check FAILED: {claude_error}")
            if live:
                health.claude_down(claude_error, datetime.now())
        elif live:
            health.claude_ok()
        fetch_new(db, LOOKBACK_HOURS, show=False)
        learn.step(db, live=live)   # corrections -> rules BEFORE labelling and moving; never raises
        classify_pending(db)
        try:   # milestone 9: what each new job email asks of you; never stops the summary
            pipeline.step(db, live=live, since_hours=LOOKBACK_HOURS)
        except Exception as e:
            print(f"Job pipeline FAILED (summary unaffected; job emails shown in full): {e}")
        try:
            screener.run(db, live=live)
        except Exception as e:  # a screening problem must never stop the summary
            print(f"Screener FAILED (summary unaffected): {e}")
            step_notes.append("Job alerts couldn't be screened this time (Claude failed); retrying next run.")
        try:
            drafts.make_drafts(db, live=live)
        except Exception as e:  # a drafting problem must never stop the summary
            print(f"Drafts FAILED (summary unaffected): {e}")
            step_notes.append("Reply drafts couldn't be written this time; retrying next run.")
        try:
            cleanup.run(db, live=live)
        except Exception as e:  # nor a moving problem
            print(f"Clean-up FAILED (summary unaffected): {e}")

    cal = agenda.get(db, now=slot)   # Calendar.app, read-only; mornings only (by slot, so a late 08:00 still has it); never raises
    today = cal["day"] if cal else (slot or datetime.now()).date()
    todos = []
    try:   # what your applications are waiting for you to do, in both summaries (21 Nov)
        todos = pipeline.todos(db, today)
        if cal:
            cal["job_todos"] = todos
            cal["pipeline"] = pipeline.pipeline_line(db, today)
    except Exception as e:
        print(f"Job pipeline view FAILED (summary unaffected): {e}")
    brief = today_brief(cal) if cal else todo_brief(todos, today)
    recent, too_old = pending(db)
    jobs = screener.unshown_high(db)
    if too_old and not sample:
        print(f"{len(too_old)} older emails skipped (over {MAX_AGE_HOURS}h old)")
    if not recent and not jobs and not has_cal(cal) and not step_notes:   # (to-dos alone ride on the ping)
        print("No new emails: no summary this time.")
        if live and not sample:
            mark_done(db, too_old)
            remember_buttons(db, notify.ping(ping_counts(recent), 0, DELIVER_TO, with_buttons(db, brief)), brief)
            send_queue(db, cal)
        return (None if sample else db), live

    subject = subject_line(sections(recent), len(recent), jobs, cal, todos)
    notes = step_notes + learn.unshown_lines(db) + unsub_folder.unshown_lines(db)
    text, html_body = build_text(recent, jobs, notes, cal, todos), build_html(recent, jobs, notes, cal, todos)
    print(f"\nSubject: {subject}\n\n{text}\n")

    preview = paths.PREVIEW
    preview.write_text(html_body)   # already a whole HTML document
    if live:
        deliver(subject, text, html_body)
        if not sample:
            mark_done(db, [r for r in recent if r["label"] is not None] + too_old)  # only after delivery worked
            screener.mark_shown(db, jobs)
            learn.mark_shown(db)
            unsub_folder.mark_shown(db)
        print(f"Delivered to your {DELIVER_TO} inbox.")
        remember_buttons(db, notify.ping(ping_counts(recent, jobs), len(recent), DELIVER_TO,
                                         with_buttons(db, brief)), brief)   # after delivery
        if jobs and not sample:
            try:   # each good job with feedback buttons (feedback.py); never stops the summary
                print(f"Telegram: {feedback.send_jobs(db, jobs)} job(s) sent with buttons")
            except Exception as e:
                print(f"Job buttons FAILED (summary unaffected; retried with the next summary's jobs only): {e}")
        if not sample:
            send_queue(db, cal)
    else:
        print(f"(dry run: nothing delivered. HTML preview: {preview})")
        text, loud = notify.message(ping_counts(recent, jobs), len(recent), DELIVER_TO, brief)
        print(f"(dry run: Telegram ping not sent; it would say: {notify.plain(text)}{'' if loud else '  [no sound]'})")
        if sample:
            subprocess.run(["open", str(preview)])  # show it in your browser
    return (None if sample else db), live


if __name__ == "__main__":
    main()
