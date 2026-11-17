"""Milestone 7b: grow the hand-labelled test set from your Sep-Nov job-hunt mail.

Why: the weekly check had only 6 job emails. 6/6 right sounds perfect, but the honest
95% range for the true job accuracy is about 54-100%. More job emails narrow that.

Who chooses the emails matters. If Claude picked them, the test set would contain the job
emails Claude already recognises, and the job score would flatter it ("selection bias").
So plain rules pick them, nothing else:
  * RULE_PICKS emails from application systems (Workday, Greenhouse, tal.net...) or with job
    words in the subject; job-alert digests excluded; at most PER_SENDER per sender
  * RANDOM_PICKS at random from everything else, so the set also has ordinary mail
    (rule-picked job emails are the obvious ones; the random ones keep the test honest)
Each pick records why it was picked, so scores can be split by rule vs random later.

Dry run by default (design rule 0): shows what it would pick.
    python3 -m uv run python -m email_organiser.grow_testset
    python3 -m uv run python -m email_organiser.grow_testset --live   (saves them for labelling)
Then label them (blind, shuffled) in Terminal:
    python3 -m uv run python -m email_organiser.label_by_hand --pool
Read-only on the mail servers (BODY.PEEK: nothing is marked read).
"""
import argparse
import random
import re
from collections import Counter
from datetime import date

from . import store
from .fetch import parse_message
from .mail import connect, load_accounts
from .store import sender_address

START, END = date(2026, 9, 1), date(2026, 11, 16)   # job season, before the organiser existed
RULE_PICKS, RANDOM_PICKS, PER_SENDER = 35, 15, 3
SEED = 7   # fixed, so the same picks come out if it is re-run

ATS_DOMAINS = ("myworkday.com", "workday.com", "greenhouse.io", "greenhouse-mail.io", "lever.co",
               "smartrecruiters.com", "successfactors.com", "successfactors.eu", "taleo.net",
               "icims.com", "workablemail.com", "workable.com", "hirevue.com", "oraclecloud.com",
               "avature.net", "tal.net", "cappfinity.com", "eightfold.ai", "hackajob.com",
               "mail-hackajob.com", "makipeople.com", "pymetrics.ai", "amazon.jobs", "beamery.com")
JOB_WORDS = re.compile(r"(?i)\b(application|applying|applied|interview|assessment|candidate|recruit\w*|"
                       r"graduate (programme|program|scheme)|offer|vacanc\w+|talent|shortlist\w*)\b|"
                       r"申請|面試|職位|招聘|應徵")
ALERT_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-listings@linkedin.com")
ALERT_NAMES = re.compile(r"(?i)recommendation|job alert|jobs for you")


def is_rule_pick(sender: str, name: str, subject: str) -> bool:
    addr = sender.lower()
    if addr in ALERT_SENDERS or ALERT_NAMES.search(name or "") or ALERT_NAMES.search(subject or ""):
        return False   # job-alert digests: plenty already, and they aren't employers acting
    domain = addr.rpartition("@")[2]
    return any(domain == d or domain.endswith("." + d) for d in ATS_DOMAINS) or bool(JOB_WORDS.search(subject or ""))


def choose(candidates: list[dict], rng: random.Random) -> list[dict]:
    """Pure: [{...candidate, 'why': 'rule'|'random'}]. Candidates need sender, name, subject."""
    rule = [c for c in candidates if is_rule_pick(c["sender"], c["name"], c["subject"])]
    rng.shuffle(rule)
    per = Counter()
    picked = []
    for c in rule:
        if len(picked) < RULE_PICKS and per[c["sender"]] < PER_SENDER:
            picked.append(c | {"why": "rule"})
            per[c["sender"]] += 1
    taken = {id(c) for c in rule}
    rest = [c for c in candidates if id(c) not in taken]
    picked += [c | {"why": "random"} for c in rng.sample(rest, min(RANDOM_PICKS, len(rest)))]
    return picked


def decode(value) -> str:
    if isinstance(value, bytes):
        from email.header import decode_header, make_header
        try:
            return str(make_header(decode_header(value.decode(errors="replace"))))
        except Exception:
            return value.decode(errors="replace")
    return value or ""


def candidates(db) -> list[dict]:
    """Every inbox email in the window that the database doesn't already have (envelopes only)."""
    out = []
    for account in load_accounts():
        with connect(account) as client:
            info = client.select_folder("INBOX", readonly=True)
            uidvalidity = info[b"UIDVALIDITY"]
            known = {r[0] for r in db.execute("SELECT uid FROM messages WHERE account=? AND uidvalidity=?",
                                              (account["name"], uidvalidity))}
            uids = [u for u in client.search(["SINCE", START]) if u not in known]
            for uid, d in (client.fetch(uids, ["ENVELOPE", "INTERNALDATE"]) if uids else {}).items():
                if not (START <= d[b"INTERNALDATE"].date() < END):   # Yahoo's BEFORE is unreliable
                    continue
                env = d[b"ENVELOPE"]
                frm = env.from_[0] if env.from_ else None
                addr = f"{decode(frm.mailbox)}@{decode(frm.host)}".lower() if frm else ""
                out.append(dict(account=account["name"], uidvalidity=uidvalidity, uid=uid,
                                sender=addr, name=decode(frm.name) if frm else "",
                                subject=decode(env.subject)))
    return out


def save(db, picks: list[dict]) -> int:
    """Download just the picked emails (read-only) into the database and the labelling pool."""
    saved = 0
    for account in load_accounts():
        mine = [p for p in picks if p["account"] == account["name"]]
        if not mine:
            continue
        with connect(account) as client:
            client.select_folder("INBOX", readonly=True)
            data = client.fetch([p["uid"] for p in mine], ["BODY.PEEK[]"])
        for p in mine:
            raw = data.get(p["uid"], {}).get(b"BODY[]")
            if raw is None:
                continue
            info = parse_message(raw)
            store.save_if_new(db, p["account"], p["uidvalidity"], p["uid"], info)
            store.save_unsub(db, p["account"], p["uidvalidity"], p["uid"], info["unsub"])
            db.execute("INSERT OR IGNORE INTO testset_pool (account, uidvalidity, uid, why) VALUES (?, ?, ?, ?)",
                       (p["account"], p["uidvalidity"], p["uid"], p["why"]))
            saved += 1
    db.commit()
    return saved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="save the picks for labelling")
    live = parser.parse_args().live
    db = store.open_db()
    have = db.execute("SELECT COUNT(*) FROM testset_pool").fetchone()[0]
    if have:
        print(f"The pool already has {have} emails; nothing new picked. Label them with label_by_hand --pool.")
        return
    found = candidates(db)
    picks = choose(found, random.Random(SEED))
    print(f"{len(found)} inbox emails from {START} to {END}; "
          f"{sum(is_rule_pick(c['sender'], c['name'], c['subject']) for c in found)} match the job rules.")
    print(f"Picked {sum(p['why'] == 'rule' for p in picks)} by rule, {sum(p['why'] == 'random' for p in picks)} at random:")
    for p in picks:
        print(f"  {p['why']:<6} [{p['account']}] {p['sender'][:38]:<38} {p['subject'][:55]}")
    if not live:
        print("(dry run: nothing saved. Run with --live to save them for labelling.)")
        return
    print(f"Saved {save(db, picks)} emails. Now label them in Terminal:\n"
          "  python3 -m uv run python -m email_organiser.label_by_hand --pool")


if __name__ == "__main__":
    main()
