"""Unsubscribe by dragging: move an email into a folder called "Unsubscribe" (any account,
Mac or iPhone) and the next 15-minute check unsubscribes you from that list.

Replaces the weekly Terminal step (scripts/unsubscribe.sh still works, as a fallback).

Each check, for every account (no Claude call):
  1. Create the Unsubscribe folder if it's missing (first live run only).
  2. Read the headers of each email in it (BODY.PEEK: stays unread).
  3. Decide, with plain rules (design rule 4):
       refused   Claude labelled it spam (unsubscribing tells a spammer your address works)
       refused   that address has also sent you job or needs-reply mail: one wrong unsubscribe
                 there could silently stop an interview email, so do it on their website
       by_hand   no one-click link, or the link isn't vouched for by a DKIM signature that
                 the receiving provider checked (unsubscribe.header_info)
       unsubscribed / failed   the RFC 8058 one-click POST (unsubscribe.one_click):
                 HTTPS only, no cookies or login, redirects not followed
  4. Record the result, then move the email to Assistant-Review. Nothing is deleted
     (design rule 3), and the Unsubscribe folder ends up empty again.
  5. The 15-minute check pings the counts; the next summary lists the names and results.

Consent (RFC 8058: "MUST NOT perform a POST ... without user consent"): you moving the
email into the folder is the consent. Nothing else ever triggers a POST.

The account doesn't matter: the link and the receiving provider's DKIM results travel
inside the email, so an email dragged from Gmail into Yahoo's Unsubscribe folder works.

Dry run by default (design rule 0):
    python3 -m uv run python -m email_organiser.unsub_folder
    python3 -m uv run python -m email_organiser.unsub_folder --live
"""
import argparse
import email
from collections import Counter
from email import policy
from email.utils import parseaddr

from . import cleanup, store, unsubscribe
from .mail import connect, load_accounts

FOLDER = "Unsubscribe"
MAX_PER_RUN = 20   # a bug (or a mass drag by mistake) can't fire off hundreds of requests

LABELS = {"unsubscribed": "Unsubscribed", "by_hand": "Do by hand", "refused": "Not unsubscribed",
          "failed": "Unsubscribe failed, do by hand"}


def important_addresses(db) -> set[str]:
    """Addresses that have ever sent you mail labelled job, or needing a reply."""
    rows = db.execute("""SELECT m.sender FROM messages m JOIN claude_labels c
                         USING (account, uidvalidity, uid)
                         WHERE c.label = 'job' OR c.needs_reply = 1""")
    return {store.sender_address(r["sender"]) for r in rows}


def known_label(db, message_id: str):
    """Claude's label for this email, found by Message-ID (the email has a new UID in the
    Unsubscribe folder). None for old mail the organiser never downloaded."""
    if not message_id:
        return None
    return db.execute("""SELECT c.label, c.job_alert FROM messages m JOIN claude_labels c
                         USING (account, uidvalidity, uid) WHERE m.message_id = ?
                         ORDER BY c.labelled_at DESC LIMIT 1""", (message_id,)).fetchone()


def decide(db, msg, important: set[str]) -> dict:
    """What to do with one dragged email. Pure rules; sends nothing."""
    sender = str(msg.get("From") or "")
    address = store.sender_address(sender)
    label = known_label(db, str(msg.get("Message-ID") or "").strip())
    job_board = bool(cleanup.is_job_board(sender) or (label and label["job_alert"]))
    out = dict(sender=sender, address=address, subject=str(msg.get("Subject") or ""),
               job_board=job_board, url=None)
    if label and label["label"] == "spam":
        return out | dict(result="refused", detail="Claude labelled it spam: unsubscribing tells "
                          "a spammer your address works. Mark it as junk instead")
    if address in important:
        return out | dict(result="refused", detail="this address also sends you job or reply "
                          "mail: unsubscribe on their website so only the right list stops")
    info = unsubscribe.header_info(msg)
    if info["url"] and info["signed"]:
        return out | dict(result="post", url=info["url"], detail="")
    if info["one_click"]:
        why = "its link isn't vouched for by a DKIM signature your provider checked"
    elif info["mailto"]:
        why = f"it only offers unsubscribe by email ({info['mailto'][7:].split('?')[0]}); this code can't send email"
    elif info["url"]:
        why = "its link isn't one-click: open it from the email footer"
    else:
        why = "no unsubscribe header: use a Mail rule or block the sender"
    return out | dict(result="by_hand", detail=why)


def save(db, account: str, d: dict) -> None:
    db.execute("INSERT INTO unsub_requests (account, sender, subject, result, detail, job_board) "
               "VALUES (?, ?, ?, ?, ?, ?)",
               (account, d["sender"], d["subject"], d["result"], d["detail"], int(d["job_board"])))
    if d["result"] == "unsubscribed":   # the Terminal command then won't offer it again
        name = parseaddr(d["sender"])[0].strip()
        unsubscribe.record(db, f"{d['address']}|{name}", "unsubscribed", d["detail"])


def handle_account(db, account: dict, live: bool, important: set[str], budget: int,
                   posted: dict, counts: Counter) -> int:
    """Process one account's Unsubscribe folder. Returns how many emails were handled."""
    name = account["name"]
    with connect(account) as client:
        if not client.folder_exists(FOLDER):
            if live:
                client.create_folder(FOLDER)
                print(f"  [{name}] created folder {FOLDER}")
            else:
                print(f"  [{name}] would create folder {FOLDER}")
            return 0   # a new folder is empty
        client.select_folder(FOLDER, readonly=not live)
        # NOT DELETED: skip copies Mail.app has already marked for removal.
        uids = sorted(client.search(["NOT", "DELETED"]))[:budget]
        if not uids:
            return 0
        data = client.fetch(uids, ["BODY.PEEK[HEADER]"])
        done = []
        for uid in uids:
            raw = data.get(uid, {}).get(b"BODY[HEADER]")
            if not raw:
                continue
            d = decide(db, email.message_from_bytes(raw, policy=policy.default), important)
            who = d["sender"] or "(no sender)"
            if d["result"] == "post":
                if not live:
                    print(f"  [{name}] would unsubscribe: {who}")
                    continue
                if d["url"] in posted:          # same list dragged twice this run: one request
                    ok, what = posted[d["url"]]
                else:
                    ok, what = unsubscribe.one_click(d["url"])
                    posted[d["url"]] = (ok, what)
                d |= dict(result="unsubscribed" if ok else "failed", detail=what)
            elif not live:
                print(f"  [{name}] would not unsubscribe ({d['result']}): {who}: {d['detail']}")
                continue
            # Recorded BEFORE the move: if the move then fails, the email is handled again
            # next check (a second unsubscribe request is harmless).
            save(db, name, d)
            db.commit()
            counts[d["result"]] += 1
            done.append(uid)
            print(f"  [{name}] {d['result']}: {who}" + (f" ({d['detail']})" if d["detail"] else ""))
        if done:
            if not client.folder_exists(cleanup.REVIEW):
                client.create_folder(cleanup.REVIEW)
            cleanup.move(client, done)   # from the selected folder (Unsubscribe) to Review
            print(f"  [{name}] moved {len(done)} to {cleanup.REVIEW}")
        return len(uids)


def run(db, live: bool) -> Counter:
    """The whole step. Never raises; one account failing doesn't stop the others."""
    counts: Counter = Counter()
    try:
        important = important_addresses(db)
        accounts = load_accounts()
    except Exception as e:
        print(f"Unsubscribe folder FAILED: {e}")
        return counts
    budget, posted = MAX_PER_RUN, {}
    for account in accounts:
        if budget <= 0:
            print(f"  {MAX_PER_RUN} handled this run; the rest next check")
            break
        try:
            budget -= handle_account(db, account, live, important, budget, posted, counts)
        except Exception as e:
            print(f"  [{account['name']}] Unsubscribe folder FAILED (other steps unaffected): {e}")
    return counts


def ping_text(counts: Counter) -> str | None:
    """Counts only: Telegram isn't end-to-end encrypted, so no names."""
    if not counts:
        return None
    parts = [f"{counts['unsubscribed']} unsubscribed" if counts["unsubscribed"] else "",
             f"{counts['by_hand'] + counts['failed']} to do by hand" if counts["by_hand"] + counts["failed"] else "",
             f"{counts['refused']} not unsubscribed" if counts["refused"] else ""]
    return f"Unsubscribe folder: {', '.join(p for p in parts if p)}. Details in the next summary."


def unshown_lines(db) -> list[str]:
    lines = []
    for r in db.execute("SELECT * FROM unsub_requests WHERE shown = 0 ORDER BY id"):
        name, address = parseaddr(r["sender"] or "")
        who = f"{name} <{address}>" if name else (address or "(no sender)")
        line = f"{LABELS.get(r['result'], r['result'])}: {who}"
        if r["result"] != "unsubscribed" and r["detail"]:
            line += f": {r['detail']}"
        if r["job_board"]:
            line += " (this was feeding the job screener)"
        lines.append(line)
    return lines


def mark_shown(db) -> None:
    db.execute("UPDATE unsub_requests SET shown = 1 WHERE shown = 0")
    db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really unsubscribe and move")
    live = parser.parse_args().live
    counts = run(store.open_db(), live)
    print(ping_text(counts) or "Unsubscribe folders: nothing to do.")


if __name__ == "__main__":
    main()
