"""Milestone 7: the learning loop. The model never changes; what the system KNOWS does.

Three parts, all run inside the 08:00 / 18:00 summary:

1. collect(): notice your corrections, without you doing anything special.
     rescued            an email this program moved to Assistant-Review is back in your inbox
     marked_junk        an email it left in your inbox is now in Assistant-Review or Junk/Spam/Bulk
     replied_unflagged  you replied to an email Claude said needed no reply
   and, for later style learning, what you actually sent when you replied to a drafted email.

2. update_rules(): turn corrections into sender rules (plain code, no Claude):
     never_move  after ONE rescue: that sender is never moved again (being wrong = some junk stays)
     junk        after THREE junk corrections and no rescue: labelled junk without asking Claude
                 (being wrong = real mail hidden, so it needs more evidence)
   A rescue always beats a junk rule. Rules decide actions; they never unlock sending or
   deleting (design rule 7).

3. weekly_score(): once a week Claude re-labels the test set (your 50 hand labels + every
   correction) with today's instructions. The score goes in the Sunday summary and the
   `scores` table. kept() is the gate for any change (run it with gate.py): judged per email
   over repeated runs; no job email lost, at least one gained.
   Leakage rule: an email shown to Claude as an example is never scored (none yet).
"""
import difflib
import hashlib
import re
from collections import Counter
from datetime import date, datetime, timedelta
from email import message_from_bytes, policy

from .classify import BATCH_SIZE, SYSTEM_PROMPT, ask_claude
from .cleanup import MOVE_LABELS, REVIEW
from .fetch import body_text
from .mail import connect, load_accounts

WINDOW_DAYS = 14     # how far back to look for corrections
JUNK_AFTER = 3       # junk corrections needed before a sender is labelled junk without Claude
JUNK_NAMES = ("junk", "spam", "bulk", "bulk mail", "[gmail]/spam")
SENT_NAMES = ("sent", "sent messages", "sent items", "[gmail]/sent mail")


def last_sunday_noon(now: datetime) -> datetime:
    """Start of the scoring week (moved here from unsubscribe.py when the weekly nudge retired)."""
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    sunday = noon - timedelta(days=(now.weekday() + 1) % 7)   # weekday(): Monday 0 ... Sunday 6
    return sunday if sunday <= now else sunday - timedelta(days=7)


from .store import sender_address as address


def clean_id(value) -> str:
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return (value or "").strip()


# --- 1. noticing corrections ---------------------------------------------------------------

def find_folder(folders, flag: bytes, names) -> str | None:
    """Prefer the server's own tag (\\Junk, \\Sent); fall back to the usual names."""
    for flags, _, name in folders:
        if flag in flags:
            return name
    return next((name for _, _, name in folders if name.lower() in names), None)


def envelopes(client, folder: str, since: date) -> dict[str, tuple[int, str]]:
    """{message_id: (uid, in_reply_to)} for a folder's mail since a date. ENVELOPE is a short
    summary the server already has (works on Yahoo, unlike HEADER.FIELDS)."""
    client.select_folder(folder, readonly=True)
    uids = client.search(["SINCE", since])
    if not uids:
        return {}
    out = {}
    for uid, data in client.fetch(uids, ["ENVELOPE"]).items():
        env = data[b"ENVELOPE"]
        if env.message_id:
            out[clean_id(env.message_id)] = (uid, clean_id(env.in_reply_to))
    return out


def add_correction(db, row, kind: str, truth: str) -> bool:
    cur = db.execute("INSERT OR IGNORE INTO corrections (account, uidvalidity, uid, kind, label_was, truth) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (row["account"], row["uidvalidity"], row["uid"], kind, row["label"], truth))
    return cur.rowcount == 1


def strip_quote(text: str) -> str:
    """Your words only: drop the quoted original ("> ..." and everything after "On ... wrote:")."""
    text = re.split(r"\n?On .{5,200}wrote:", text, maxsplit=1)[0]
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">")).strip()


def compare(found: dict, rows: list, review: set, junk: set, sent: dict) -> list[tuple]:
    """Pure logic, no server: [(row, kind, truth)] from what is where.
    found = message ids now in the inbox; sent = {in_reply_to: sent uid}."""
    out = []
    for r in rows:
        mid = clean_id(r["message_id"])
        if not mid:
            continue
        if r["moved"] and mid in found:
            out.append((r, "rescued", "not_junk"))
        if not r["moved"] and r["label"] not in MOVE_LABELS and (mid in review or mid in junk):
            out.append((r, "marked_junk", "spam" if mid in junk else "junk"))
        if mid in sent and not r["needs_reply"]:
            out.append((r, "replied_unflagged", "needs_reply"))
    return out


def collect(db) -> Counter:
    """Look at every account once (read-only) and record new corrections. Returns counts."""
    since = date.today() - timedelta(days=WINDOW_DAYS)
    counts = Counter()
    for account in load_accounts():
        name = account["name"]
        rows = db.execute("""
            SELECT m.account, m.uidvalidity, m.uid, m.message_id, m.sender, c.label, c.needs_reply,
                   mv.uid IS NOT NULL AS moved, d.body AS draft_body, d.uid IS NOT NULL AS drafted
            FROM messages m JOIN claude_labels c USING (account, uidvalidity, uid)
            LEFT JOIN moved mv USING (account, uidvalidity, uid)
            LEFT JOIN drafts d USING (account, uidvalidity, uid)
            WHERE m.account = ? AND m.first_seen >= ?""", (name, since.isoformat())).fetchall()
        if not rows:
            continue
        with connect(account) as client:
            folders = client.list_folders()
            inbox = envelopes(client, "INBOX", since)
            review = envelopes(client, REVIEW, since) if client.folder_exists(REVIEW) else {}
            junk_name = find_folder(folders, b"\\Junk", JUNK_NAMES)
            junk = envelopes(client, junk_name, since) if junk_name else {}
            sent_name = find_folder(folders, b"\\Sent", SENT_NAMES)
            sent_env = envelopes(client, sent_name, since) if sent_name else {}
            sent = {irt: uid for uid, irt in sent_env.values() if irt}
            for r, kind, truth in compare(inbox, rows, set(review), set(junk), sent):
                counts[kind] += add_correction(db, r, kind, truth)
            # What you actually sent in reply to a drafted email (style examples, later).
            for r in rows:
                mid = clean_id(r["message_id"])
                if not (r["drafted"] and mid in sent) or db.execute(
                        "SELECT 1 FROM sent_replies WHERE account=? AND uidvalidity=? AND uid=?",
                        (r["account"], r["uidvalidity"], r["uid"])).fetchone():
                    continue
                client.select_folder(sent_name, readonly=True)
                raw = client.fetch([sent[mid]], ["BODY.PEEK[]"])[sent[mid]][b"BODY[]"]
                mine = strip_quote(body_text(message_from_bytes(raw, policy=policy.default)))
                similar = (difflib.SequenceMatcher(None, r["draft_body"], mine).ratio()
                           if r["draft_body"] else None)
                db.execute("INSERT INTO sent_replies (account, uidvalidity, uid, draft_body, sent_body, "
                           "similarity) VALUES (?, ?, ?, ?, ?, ?)",
                           (r["account"], r["uidvalidity"], r["uid"], r["draft_body"], mine, similar))
                counts["sent_reply"] += 1
        db.commit()
    return counts


# --- 2. corrections -> sender rules --------------------------------------------------------

def update_rules(db) -> list[str]:
    """Apply the asymmetric thresholds. Returns what changed, for the log."""
    changes = []
    rows = db.execute("""SELECT c.kind, c.truth, m.sender FROM corrections c
                         JOIN messages m USING (account, uidvalidity, uid)""").fetchall()
    rescued = {address(r["sender"]) for r in rows if r["kind"] == "rescued"}
    for addr in rescued:
        current = db.execute("SELECT rule FROM sender_rules WHERE address = ?", (addr,)).fetchone()
        if current is None or current[0] != "never_move":   # replaces a junk rule, if any
            db.execute("INSERT OR REPLACE INTO sender_rules (address, rule, label, reason) VALUES "
                       "(?, 'never_move', NULL, 'you moved an email from this sender back to the inbox')",
                       (addr,))
            changes.append(f"never move {addr}")
    junk = Counter(address(r["sender"]) for r in rows if r["kind"] == "marked_junk")
    for addr, n in junk.items():
        if n < JUNK_AFTER or addr in rescued:
            continue
        if db.execute("SELECT 1 FROM sender_rules WHERE address = ?", (addr,)).fetchone():
            continue
        truths = Counter(r["truth"] for r in rows if r["kind"] == "marked_junk" and address(r["sender"]) == addr)
        label = "spam" if truths["spam"] > n / 2 else "promo"
        db.execute("INSERT INTO sender_rules (address, rule, label, reason) VALUES (?, 'junk', ?, ?)",
                   (addr, label, f"you marked {n} emails from this sender as junk"))
        changes.append(f"junk ({label}) {addr}")
    db.commit()
    return changes


# --- 3. the weekly score -------------------------------------------------------------------

def test_set(db) -> list[dict]:
    """Your hand labels plus every correction, one entry per email. Each has what counts as right."""
    items = {}
    for r in db.execute("""SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.snippet,
                                  t.label, t.needs_reply
                           FROM my_labels t JOIN messages m USING (account, uidvalidity, uid)
                           WHERE t.needs_reply IS NOT NULL"""):
        items[(r["account"], r["uidvalidity"], r["uid"])] = dict(r) | {"set": "hand", "kind": None}
    for r in db.execute("""SELECT m.account, m.uidvalidity, m.uid, m.sender, m.subject, m.snippet,
                                  c.kind, c.truth
                           FROM corrections c JOIN messages m USING (account, uidvalidity, uid)"""):
        key = (r["account"], r["uidvalidity"], r["uid"])
        if key not in items:   # your own hand label beats an inferred correction
            items[key] = dict(r) | {"set": "corrections"}
    return list(items.values())


def right(item: dict, answer: dict) -> bool:
    """Is Claude's new answer right for this item?"""
    if item["set"] == "hand":
        return answer["label"] == item["label"]
    if item["kind"] == "rescued":
        return answer["label"] not in MOVE_LABELS
    if item["kind"] == "marked_junk":
        return answer["label"] in MOVE_LABELS
    return bool(answer["needs_reply"])   # replied_unflagged


def score(items: list, answers: list) -> dict:
    hand = [(i, a) for i, a in zip(items, answers) if a and i["set"] == "hand"]
    corr = [(i, a) for i, a in zip(items, answers) if a and i["set"] == "corrections"]
    jobs = [(i, a) for i, a in hand if i["label"] == "job"]
    return {"total": len(hand), "correct": sum(right(i, a) for i, a in hand),
            "job_total": len(jobs), "job_correct": sum(a["label"] == "job" for _, a in jobs),
            "reply_correct": sum(bool(a["needs_reply"]) == bool(i["needs_reply"]) for i, a in hand),
            "corr_total": len(corr), "corr_correct": sum(right(i, a) for i, a in corr)}


def kept(old_runs: list[list[bool]], new_runs: list[list[bool]], is_job: list[bool]) -> tuple[bool, str]:
    """The gate for any change (Sid, 2026-11-18). Judged per email over repeated runs, because
    totals wobble by 2-3 emails run to run (batch context). A change is kept only if:
      * no job email is lost consistently (right in every old run, wrong in every new run), and
      * at least one job email is gained consistently (wrong in every old run, right in every new).
    Emails that flip back and forth don't count either way: that's noise.
    (Written for job fixes. A change aimed at another label needs this rule revisited.)"""
    n = len(is_job)
    always = lambda runs, k, v: all(r[k] == v for r in runs)
    lost = [k for k in range(n) if always(old_runs, k, True) and always(new_runs, k, False)]
    gained = [k for k in range(n) if always(old_runs, k, False) and always(new_runs, k, True)]
    job_lost = [k for k in lost if is_job[k]]
    job_gained = [k for k in gained if is_job[k]]
    ok = not job_lost and bool(job_gained)
    why = (f"job emails: {len(job_gained)} gained, {len(job_lost)} lost; "
           f"all emails: {len(gained)} gained, {len(lost)} lost (consistently, every run)")
    return ok, why


def prompt_hash() -> str:
    """A fingerprint of Claude's instructions: tells you whether a score change came from us."""
    return hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:8]


def run_test(items: list) -> tuple[list, str]:
    answers, model = [], "unknown"
    for start in range(0, len(items), BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        got, model = ask_claude([{"id": n, "from": i["sender"], "subject": i["subject"], "body": i["snippet"]}
                                 for n, i in enumerate(batch)])
        answers += [got.get(n) for n in range(len(batch))]
    return answers, model


def line(s: dict, last: dict | None, model: str, changed: bool) -> str:
    text = (f"Weekly labelling check: {s['correct']}/{s['total']} hand-labelled right "
            f"({s['correct'] / s['total']:.0%}), job {s['job_correct']}/{s['job_total']}, "
            f"reply flag {s['reply_correct']}/{s['total']}")
    text += f"; your corrections {s['corr_correct']}/{s['corr_total']} now right" if s["corr_total"] else "; no corrections yet"
    if last:
        text += f". Last week {last['correct']}/{last['total']}, job {last['job_correct']}/{last['job_total']}"
        if s["job_total"] and last["job_total"] and \
                s["job_correct"] / s["job_total"] < last["job_correct"] / last["job_total"]:
            text += ". WARNING: job emails dropped"
        text += " (Claude's instructions changed since)" if changed else ""
        text += f" (model now {model})" if model != last["model"] else ""
    return text + "."


def weekly_score(db, live: bool, now: datetime | None = None) -> str | None:
    """Score once a week (from Sunday noon). Returns the summary line if one was made."""
    week = last_sunday_noon(now or datetime.now()).date().isoformat()
    if db.execute("SELECT 1 FROM scores WHERE week = ?", (week,)).fetchone():
        return None
    items = test_set(db)
    if not items:
        return None
    if not live:
        print(f"(dry run: weekly labelling check due: {len(items)} emails, "
              f"{-(-len(items) // BATCH_SIZE)} Claude calls; not run)")
        return None
    answers, model = run_test(items)
    s = score(items, answers)
    last = db.execute("SELECT * FROM scores ORDER BY week DESC LIMIT 1").fetchone()
    text = line(s, dict(last) if last else None, model, bool(last) and last["prompt_hash"] != prompt_hash())
    # Each email's answer too: the total wobbles run to run (batch context), so a drop must be
    # traced to the emails that flipped before anyone believes it.
    db.executemany("INSERT OR REPLACE INTO score_answers VALUES (?, ?, ?, ?, ?, ?, ?)",
                   [(week, i["account"], i["uidvalidity"], i["uid"], a["label"], int(a["needs_reply"]),
                     int(right(i, a))) for i, a in zip(items, answers) if a])
    db.execute("""INSERT INTO scores (week, total, correct, job_total, job_correct, reply_correct,
                  corr_total, corr_correct, model, prompt_hash, line) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (week, s["total"], s["correct"], s["job_total"], s["job_correct"], s["reply_correct"],
                s["corr_total"], s["corr_correct"], model, prompt_hash(), text))
    db.commit()
    return text


def unshown_lines(db) -> list[str]:
    return [r[0] for r in db.execute("SELECT line FROM scores WHERE shown = 0 ORDER BY week")]


def mark_shown(db) -> None:
    db.execute("UPDATE scores SET shown = 1 WHERE shown = 0")
    db.commit()


def step(db, live: bool) -> None:
    """The whole learning step for one summary run. Never raises (the summary comes first)."""
    try:
        found = collect(db)
        changes = update_rules(db)
        print(f"Learning: corrections found {dict(found) or 'none'}; rules {changes or 'unchanged'}")
    except Exception as e:
        print(f"Learning: collecting corrections FAILED (summary unaffected): {e}")
    try:
        text = weekly_score(db, live)
        if text:
            print(text)
    except Exception as e:
        print(f"Learning: weekly check FAILED (summary unaffected; retried next run): {e}")
