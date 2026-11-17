"""Milestone 3: the twice-daily summary.

One run = fetch new mail -> Claude labels it -> job alerts screened -> reply drafts (milestone 4)
          -> junk to Assistant-Review (milestone 5) -> build a summary -> put it in your inbox -> Telegram ping.

Dry run by default (design rule 0): prints the summary and changes nothing.
    python3 -m uv run python -m email_organiser.digest
For real:
    python3 -m uv run python -m email_organiser.digest --live
Preview the design with recent mail, without marking anything as summarised:
    python3 -m uv run python -m email_organiser.digest --sample            (opens in browser)
    python3 -m uv run python -m email_organiser.digest --sample --live     (puts it in your inbox)

"Put it in your inbox" is IMAP APPEND: the summary is placed straight into your own
mailbox. It is NOT sent. There is still no sending code anywhere (design rule 2).
"""
import argparse
import html
import subprocess
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from . import cleanup, drafts, notify, paths, screener, store
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
    """(recent, too_old): labelled emails that haven't been in a summary yet."""
    rows = db.execute("""
        SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.date, m.first_seen,
               c.label, c.needs_reply, c.reason, dr.uid IS NOT NULL AS drafted,
               mv.uid IS NOT NULL AS moved
        FROM messages m
        JOIN claude_labels c USING (account, uidvalidity, uid)
        LEFT JOIN digested d USING (account, uidvalidity, uid)
        LEFT JOIN drafts dr USING (account, uidvalidity, uid)
        LEFT JOIN moved mv USING (account, uidvalidity, uid)
        WHERE d.uid IS NULL
    """).fetchall()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    recent = sorted((r for r in rows if arrived(r) >= cutoff), key=arrived, reverse=True)
    too_old = [r for r in rows if arrived(r) < cutoff]
    return recent, too_old


def sections(rows: list) -> dict:
    """Split emails into the summary's groups. Each email appears in one group only."""
    return {
        "job": [r for r in rows if r["label"] == "job"],
        "reply": [r for r in rows if r["needs_reply"] and r["label"] != "job"],
        "read": [r for r in rows if r["label"] == "read" and not r["needs_reply"]],
        "counts": {name: sum(r["label"] == name for r in rows) for name in ROUTINE},
    }


def ping_counts(rows: list, jobs: list = ()) -> dict:
    """Just the numbers for the Telegram ping: no subjects or senders leave the Mac."""
    g = sections(rows)
    return {k: len(g[k]) for k in ("job", "reply", "read")} | {"jobs": len(jobs)}


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


def subject_line(g: dict, total: int, jobs: list = ()) -> str:
    stamp = datetime.now().strftime("%a %H:%M")
    if jobs or g["job"] or g["reply"] or g["read"]:
        bits = [f"{len(jobs)} good job{'s' if len(jobs) > 1 else ''}" if jobs else "",
                f"{len(g['job'])} job" if g["job"] else "",
                f"{len(g['reply'])} to reply" if g["reply"] else "",
                f"{len(g['read'])} to read" if g["read"] else ""]
        return f"Mail summary {stamp}: " + ", ".join(b for b in bits if b)
    return f"Mail summary {stamp}: nothing needs you ({total} routine)"


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


def build_text(rows: list, jobs: list = ()) -> str:
    """Plain-text version, for mail apps that don't show HTML."""
    g = sections(rows)
    out = [f"{len(rows)} new emails since the last summary", ""]
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
                out += [f"* {r['subject']}{tag}",
                        f"  {sender_name(r)} | {r['account']} | {when(r)}",
                        f"  {r['reason']}", ""]
            for name, rs in groups:
                accounts = ", ".join(sorted({r["account"] for r in rs}))
                out += [f"* {name}: {len(rs)} emails",
                        f"  {accounts} | latest {when(rs[0])}",
                        f"  {group_subjects(rs)}", ""]
    routine = ", ".join(f"{n} {name}" for name, n in g["counts"].items() if n)
    if routine:
        out.append(f"Everything else: {routine}.")
    if moved_line(rows):
        out.append(moved_line(rows))
    out.append("\nLabelled by Claude. Nothing was deleted or sent.")
    return "\n".join(out)


def build_html(rows: list, jobs: list = ()) -> str:
    """The nicer version. Styles are inline because many mail apps strip <style> blocks.

    Every piece of email text goes through html.escape(): a sender could put HTML
    or script in a subject line, and escaping shows it as plain text instead of running it.
    """
    e = html.escape
    g = sections(rows)
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"

    def chip(text, colour):
        return (f'<span style="display:inline-block;padding:3px 10px;margin:0 6px 6px 0;'
                f'border-radius:12px;background:{colour}1a;color:{colour};font-size:13px;'
                f'font-weight:600">{e(text)}</span>')

    chips = (chip(f"{len(jobs)} good job{'s' if len(jobs) > 1 else ''}", "#0d9488") if jobs else "") + \
        "".join(chip(f"{len(g[k])} {h.lower()}", c) for k, h, c in GROUPS if g[k])
    chips = chips or chip("Nothing needs you", "#6b7280")

    body = []
    if jobs:  # first: the point of the whole job hunt
        teal = "#0d9488"
        body.append(f'<div style="margin:22px 0 4px;padding-left:10px;border-left:4px solid {teal};'
                    f'font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
                    f'color:{teal}">Jobs worth applying to &middot; {len(jobs)}</div>')
        for j in jobs:
            title = e(j["title"])
            if j["url"]:  # only links the screener checked point at the sending job board
                title = f'<a href="{e(j["url"])}" style="color:#111827;text-decoration:underline">{title}</a>'
            if is_commission(j):
                title += (' <span style="padding:1px 7px;border-radius:9px;background:#b453091a;'
                          'color:#b45309;font-size:11px;font-weight:600">likely commission-based</span>')
            where = " &middot; ".join(e(x) for x in (j["company"], j["location"],
                                                    f"closes {j['closing']}" if j["closing"] else "") if x)
            body.append(
                '<div style="padding:10px 0 10px 14px;border-bottom:1px solid #eef0f2">'
                f'<div style="font-size:15px;font-weight:600;color:#111827;line-height:1.35">{title}</div>'
                f'<div style="font-size:13px;color:#4b5563;margin-top:2px">{where}</div>'
                f'<div style="font-size:13px;color:#6b7280;margin-top:3px">{e(j["reason"] or "")}</div>'
                '</div>')
    for key, heading, colour in GROUPS:
        if not g[key]:
            continue
        body.append(f'<div style="margin:22px 0 4px;padding-left:10px;border-left:4px solid {colour};'
                    f'font-size:13px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;'
                    f'color:{colour}">{e(heading)} &middot; {len(g[key])}</div>')
        singles, groups = collapse(g[key]) if key == "read" else (g[key], [])
        for r in singles:
            badge = ""
            if r["needs_reply"] and key == "job":
                badge = (' <span style="padding:1px 7px;border-radius:9px;background:#d977061a;'
                         'color:#d97706;font-size:11px;font-weight:600">needs reply</span>')
            if has_draft(r):
                badge += (' <span style="padding:1px 7px;border-radius:9px;background:#7c3aed1a;'
                          'color:#7c3aed;font-size:11px;font-weight:600">draft ready</span>')
            body.append(
                '<div style="padding:10px 0 10px 14px;border-bottom:1px solid #eef0f2">'
                f'<div style="font-size:15px;font-weight:600;color:#111827;line-height:1.35">'
                f'{e(r["subject"] or "(no subject)")}{badge}</div>'
                f'<div style="font-size:13px;color:#4b5563;margin-top:2px">{e(sender_name(r))}'
                f' &middot; {e(r["account"])} &middot; {e(when(r))}</div>'
                f'<div style="font-size:13px;color:#6b7280;margin-top:3px">{e(r["reason"] or "")}</div>'
                '</div>')
        for name, rs in groups:
            accounts = ", ".join(sorted({r["account"] for r in rs}))
            body.append(
                '<div style="padding:10px 0 10px 14px;border-bottom:1px solid #eef0f2">'
                f'<div style="font-size:15px;font-weight:600;color:#111827">{e(name)}'
                f' <span style="padding:1px 8px;border-radius:9px;background:#6b72801a;color:#4b5563;'
                f'font-size:12px;font-weight:600">{len(rs)} emails</span></div>'
                f'<div style="font-size:13px;color:#4b5563;margin-top:2px">{e(accounts)}'
                f' &middot; latest {e(when(rs[0]))}</div>'
                f'<div style="font-size:13px;color:#6b7280;margin-top:3px">{e(group_subjects(rs))}</div>'
                '</div>')

    routine = " &middot; ".join(f"{n} {e(name)}" for name, n in g["counts"].items() if n)
    footer = (f'<div style="margin-top:22px;padding:12px 14px;border-radius:8px;background:#f6f7f9;'
              f'font-size:13px;color:#4b5563">Everything else: {routine or "none"}'
              + (f'<div style="margin-top:4px">{e(moved_line(rows))}</div>' if moved_line(rows) else "")
              + '</div>')
    stamp = datetime.now().strftime("%a %d %b &middot; %H:%M")

    return (f'<div style="font-family:{font};max-width:600px;margin:0 auto;padding:8px 4px;color:#111827">'
            f'<div style="font-size:13px;color:#6b7280">{stamp}</div>'
            f'<div style="font-size:22px;font-weight:700;margin:2px 0 10px">Mail summary</div>'
            f'<div>{chips}</div>'
            f'<div style="font-size:13px;color:#6b7280">{len(rows)} new emails since the last summary</div>'
            + "".join(body) + footer +
            '<div style="margin-top:14px;font-size:12px;color:#9ca3af">'
            'Labelled by Claude. Nothing was deleted or sent.</div></div>')


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
    db.executemany("INSERT OR IGNORE INTO digested (account, uidvalidity, uid) VALUES (?, ?, ?)",
                   [(r["account"], r["uidvalidity"], r["uid"]) for r in rows])
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really deliver the summary")
    parser.add_argument("--sample", action="store_true",
                        help="preview with the last 48h of mail; never marks anything as summarised")
    args = parser.parse_args()
    mode = ("LIVE" if args.live else "dry run") + (" sample" if args.sample else "")
    print(f"=== {datetime.now():%Y-%m-%d %H:%M} digest ({mode}) ===")

    db = store.open_db()
    if args.sample:
        db.execute("CREATE TEMP TABLE digested AS SELECT * FROM main.digested WHERE 0")  # pretend nothing was summarised
    else:
        fetch_new(db, LOOKBACK_HOURS, show=False)
        classify_pending(db)
        try:
            screener.run(db, live=args.live)
        except Exception as e:  # a screening problem must never stop the summary
            print(f"Screener FAILED (summary unaffected): {e}")
        try:
            drafts.make_drafts(db, live=args.live)
        except Exception as e:  # a drafting problem must never stop the summary
            print(f"Drafts FAILED (summary unaffected): {e}")
        try:
            cleanup.run(db, live=args.live)
        except Exception as e:  # nor a moving problem
            print(f"Clean-up FAILED (summary unaffected): {e}")

    recent, too_old = pending(db)
    jobs = screener.unshown_high(db)
    if too_old and not args.sample:
        print(f"{len(too_old)} older emails skipped (over {MAX_AGE_HOURS}h old)")
    if not recent:
        print("No new emails: no summary this time.")
        if args.live and not args.sample:
            mark_done(db, too_old)
            notify.ping(ping_counts(recent), 0, DELIVER_TO)  # still ping: silence would mean "broken"
        return

    subject = subject_line(sections(recent), len(recent), jobs)
    text, html_body = build_text(recent, jobs), build_html(recent, jobs)
    print(f"\nSubject: {subject}\n\n{text}\n")

    preview = paths.PREVIEW
    preview.write_text(f"<!doctype html><meta charset='utf-8'><body style='background:#fff'>{html_body}")
    if args.live:
        deliver(subject, text, html_body)
        if not args.sample:
            mark_done(db, recent + too_old)  # only after delivery worked
            screener.mark_shown(db, jobs)
        print(f"Delivered to your {DELIVER_TO} inbox.")
        notify.ping(ping_counts(recent, jobs), len(recent), DELIVER_TO)  # after delivery; can't undo it
    else:
        print(f"(dry run: nothing delivered. HTML preview: {preview})")
        text, loud = notify.message(ping_counts(recent, jobs), len(recent), DELIVER_TO)
        print(f"(dry run: Telegram ping not sent; it would say: {text}{'' if loud else '  [no sound]'})")
        if args.sample:
            subprocess.run(["open", str(preview)])  # show it in your browser


if __name__ == "__main__":
    main()
