"""Milestone 5b: unsubscribe from junk senders you pick, about once a week.

Run it on the Mac (nothing about your picks leaves the Mac):
    python3 -m uv run python -m email_organiser.unsubscribe           (preview: changes nothing)
    python3 -m uv run python -m email_organiser.unsubscribe --live    (really unsubscribe)
or simply  scripts/unsubscribe.sh [--live]

What it does:
  1. Lists senders whose mail Claude labelled promo or newsletter. Never spam (unsubscribing
     tells a spammer your address is live) and never anything labelled job or needing a reply.
  2. You type which to unsubscribe from, and which to keep for good.
  3. For each pick that supports it, it sends the standard one-click unsubscribe (RFC 8058):
     an HTTPS POST of "List-Unsubscribe=One-Click" to the link in the email's
     List-Unsubscribe header. Anything else is listed for you to do by hand.

Why not just click the link in the email body? (the explain-back question) Body links are
whatever the sender chose: often a tracking page, a login, or a "manage preferences" maze,
and a lookalike in a phishing email can lead anywhere. The header link is machine-readable,
offered only when the sender's DKIM signature vouches for it, and the POST carries no
cookies or login, so it can unsubscribe you and do nothing else.

Rules the code enforces (design rule 4: the model labels, the code acts):
  * one-click only when the email has List-Unsubscribe-Post AND a DKIM signature that your
    own mail provider verified ("dkim=pass") AND that signature covers both unsubscribe
    headers. RFC 8058: without that, "SHOULD NOT offer a one-click unsubscribe".
  * HTTPS only; no cookies or login sent; redirects are NOT followed (RFC 8058 forbids the
    sender from redirecting, so a redirect is suspicious): you get told to do it by hand.
  * no POST without your typed "yes" (RFC 8058: "MUST NOT perform a POST ... without user consent").
  * mailto-only unsubscribes are never done by code: there is no email-sending code (rule 2).
Senders are grouped by name AND address: Jobsdb uses one address for both your application
updates and its recommendations, and only the junk one should ever be offered.
"""
import argparse
import email
import ipaddress
import re
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email import policy
from email.utils import parseaddr
from urllib.parse import urlsplit

from . import notify, store
from .cleanup import is_job_board
from .mail import connect, load_accounts

OFFER_LABELS = ("promo", "newsletter")    # spam is deliberately not here
TRUSTED_VERIFIERS = ("yahoo.com", "google.com", "icloud.com")  # who adds Authentication-Results we believe
STILL_SENDING_AFTER = timedelta(days=3)   # mail this long after unsubscribing = it didn't work
TIMEOUT = 20


# --- reading the unsubscribe headers (called by fetch.py for every new email) ------------

def _flat(value) -> str:
    return " ".join(str(value or "").split())


def _domain(value: str) -> str:
    return value.strip().strip(";").rpartition("@")[2].lower()


def verified_domains(msg) -> set[str]:
    """Domains whose DKIM signature your own mail provider checked and passed.

    Only Authentication-Results headers written by Yahoo, Google or iCloud count. A sender
    can put a fake "dkim=pass" header in its own email, but it can't make it come from
    mx.google.com (and providers strip ones claiming to)."""
    passed = set()
    for header in msg.get_all("Authentication-Results") or []:
        header = _flat(header)
        verifier = header.split(";", 1)[0].strip().lower()
        if not any(verifier == t or verifier.endswith("." + t) for t in TRUSTED_VERIFIERS):
            continue
        for clause in header.split(";")[1:]:
            clause = clause.strip()
            if not clause.startswith("dkim=pass"):
                continue
            found = re.search(r"header\.d=([^\s;]+)", clause) or re.search(r"header\.i=([^\s;]+)", clause)
            if found:
                passed.add(_domain(found.group(1)))
    return passed


def signature_covers_unsubscribe(msg) -> bool:
    """True if a DKIM signature that passed lists both unsubscribe headers in its h= tag.
    (h= = the headers the signature protects. Covered = the sender vouches for the link.)"""
    passed = verified_domains(msg)
    for sig in msg.get_all("DKIM-Signature") or []:
        tags = {}
        for part in re.sub(r"\s+", "", str(sig)).split(";"):
            name, eq, value = part.partition("=")
            if eq:
                tags[name.lower()] = value
        signed = {h.lower() for h in tags.get("h", "").split(":")}
        if (tags.get("d", "").lower() in passed
                and {"list-unsubscribe", "list-unsubscribe-post"} <= signed):
            return True
    return False


def header_info(msg) -> dict:
    """What an email offers for unsubscribing. Stored at fetch time, because by the weekly
    run you may already have deleted the email."""
    value = _flat(msg.get("List-Unsubscribe"))
    # The standard wraps each link in <...>; Google sends bare links, so accept those too.
    links = re.findall(r"<([^>]+)>", value) or [l.strip() for l in value.split(",")]
    url = next((l for l in links if l.lower().startswith("https://")), None)
    mailto = next((l for l in links if l.lower().startswith("mailto:")), None)
    post = _flat(msg.get("List-Unsubscribe-Post")).lower().replace(" ", "")
    one_click = bool(url and post == "list-unsubscribe=one-click")
    return {"url": url, "mailto": mailto, "one_click": int(one_click),
            "signed": int(one_click and signature_covers_unsubscribe(msg))}


# --- which senders to offer --------------------------------------------------------------

def sender_key(sender: str) -> tuple[str, str]:
    name, address = parseaddr(sender or "")
    return address.lower(), name.strip()


EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "example")  # RFC 2606: never real


def is_example(address: str) -> bool:
    domain = address.rpartition("@")[2]
    return any(domain == d or domain.endswith("." + d) for d in EXAMPLE_DOMAINS)


def groups(db) -> list[dict]:
    """Every junk sender, one entry per (address, name), with what can be done about it."""
    rows = db.execute("""
        SELECT m.account, m.sender, m.first_seen, c.label, c.needs_reply, c.job_alert,
               u.url, u.mailto, u.one_click, u.signed,
               u.uid IS NOT NULL AND NOT u.missing AS has_headers
        FROM messages m
        JOIN claude_labels c USING (account, uidvalidity, uid)
        LEFT JOIN unsub_headers u USING (account, uidvalidity, uid)
        ORDER BY m.first_seen
    """).fetchall()
    # Addresses that also send you job mail, or mail you need to answer (under any name).
    important = {sender_key(r["sender"])[0] for r in rows if r["label"] == "job" or r["needs_reply"]}
    rescued = store.never_move(db)   # you dragged its mail back out of Review: you want it
    out: dict[tuple, dict] = {}
    for r in rows:
        address, name = sender_key(r["sender"])
        if is_example(address) or address in rescued:
            continue   # planted test emails (example.com is reserved), or a sender you rescued
        g = out.setdefault((address, name), dict(
            address=address, name=name, key=f"{address}|{name}", count=0, accounts=set(),
            last_seen="", url=None, mailto=None, unsigned=False, unknown=0, offer=True,
            job_board=False, also_important=address in important))
        if r["label"] not in OFFER_LABELS or r["needs_reply"]:
            g["offer"] = False   # one non-junk email from this exact sender = never offered
            continue
        g["count"] += 1
        g["accounts"].add(r["account"])
        g["last_seen"] = max(g["last_seen"], r["first_seen"])
        g["job_board"] |= bool(r["job_alert"] or is_job_board(r["sender"]))
        if not r["has_headers"]:
            g["unknown"] += 1
        elif r["one_click"] and r["signed"]:
            g["url"] = r["url"]          # rows run oldest first, so the newest link wins
        elif r["one_click"]:
            g["unsigned"] = True
        if r["mailto"]:
            g["mailto"] = r["mailto"]
    return [g for g in out.values() if g["offer"] and g["count"]]


def link_unknown(g: dict) -> bool:
    """No link saved yet because its emails were deleted before 5b: wait for the next one."""
    return not g["url"] and not g["unsigned"] and not g["mailto"] and g["unknown"] == g["count"]


def method(g: dict) -> str:
    if g["url"]:
        return "one-click"
    if g["unsigned"]:
        return "by hand: link not vouched for by a DKIM signature"
    if g["mailto"]:
        return f"by hand: email {g['mailto'][7:].split('?')[0]} (this code can't send email)"
    if link_unknown(g):
        return "wait: its emails were deleted before 5b, so no link yet. Pick it and it comes back with one"
    return "by hand: no unsubscribe header; use a Mail rule or block the sender"


def candidates(db) -> tuple[list, list]:
    """(to_review, still_sending).
    to_review: not decided yet, and new mail since you last saw it in the list.
    still_sending: you unsubscribed, but mail kept coming more than 3 days later."""
    choices = {r["sender_key"]: r for r in db.execute("SELECT * FROM sender_choices")}
    review, still = [], []
    for g in groups(db):
        c = choices.get(g["key"])
        if c is None or (c["status"] in (None, "waiting")
                         and (c["shown_at"] is None or g["last_seen"] > c["shown_at"])):
            review.append(g | {"picked_before": c is not None and c["status"] == "waiting"})
        elif c["status"] in ("unsubscribed", "by_hand"):
            cutoff = datetime.fromisoformat(c["decided_at"]) + STILL_SENDING_AFTER
            if g["last_seen"] > cutoff.isoformat(" "):
                still.append(g | {"decided_at": c["decided_at"], "status": c["status"]})
    key = lambda g: (-g["count"], g["name"].lower())
    return sorted(review, key=key), sorted(still, key=key)


# --- the one-click POST ------------------------------------------------------------------

def unsafe(url: str) -> str | None:
    """Why this link must not be POSTed to, or None if it's fine."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        return "not HTTPS"
    if not host or "@" in parts.netloc:
        return "no host, or a login inside the link"
    try:
        ipaddress.ip_address(host)
        return "a bare IP address, not a domain"
    except ValueError:
        pass
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return "points inside your own network"
    return None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """RFC 8058: the sender "MUST NOT return an HTTPS redirect". If one comes, stop there."""
    def redirect_request(self, *args, **kwargs):
        return None   # None = don't follow; urllib then reports the 3xx as an error we catch


# build_opener adds no cookie handler, so no cookies can ever be sent.
_opener = urllib.request.build_opener(_NoRedirects)


def build_request(url: str) -> urllib.request.Request:
    """RFC 8058 says the body SHOULD be multipart/form-data with the one key/value pair."""
    boundary = "email-organiser-" + secrets.token_hex(8)
    body = (f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="List-Unsubscribe"\r\n\r\n'
            f"One-Click\r\n--{boundary}--\r\n").encode()
    return urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "email-organiser (RFC 8058 one-click unsubscribe)",
    })


def send_request(req: urllib.request.Request) -> tuple[bool, str]:
    """(worked, what happened). Never raises."""
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            return False, f"HTTP {e.code} redirect, not followed: do it by hand"
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"failed: {type(e).__name__}: {e}"


def one_click(url: str) -> tuple[bool, str]:
    reason = unsafe(url)
    if reason:
        return False, f"refused: link is {reason}"
    return send_request(build_request(url))


# --- the command -------------------------------------------------------------------------

def parse_picks(text: str, n: int) -> tuple[set, set]:
    """'1 3 k2' -> ({1, 3}, {2}). Raises ValueError on anything else."""
    unsub, keep = set(), set()
    for token in text.replace(",", " ").split():
        target = keep if token.lower().startswith("k") else unsub
        number = int(token[1:] if token.lower().startswith("k") else token)
        if not 1 <= number <= n:
            raise ValueError(f"{number} isn't on the list")
        target.add(number)
    if unsub & keep:
        raise ValueError(f"{sorted(unsub & keep)} both picked and kept")
    return unsub, keep


def describe(i: int, g: dict) -> str:
    flags = "  [you picked this before; it has emailed again]" if g.get("picked_before") else ""
    flags += "  [feeds the job screener]" if g["job_board"] else ""
    if g["also_important"]:
        flags += "  [same address also sends you job/reply mail: only this list is affected]"
    label = f"{g['name']} <{g['address']}>" if g["name"] else g["address"]
    return (f"{i:>3}. {label}\n       {g['count']} email{'s' if g['count'] > 1 else ''}, "
            f"{', '.join(sorted(g['accounts']))}, {method(g)}{flags}")


def record(db, key: str, status: str | None, detail: str = "") -> None:
    db.execute("""INSERT INTO sender_choices (sender_key, status, decided_at, shown_at, detail)
                  VALUES (?, ?, CASE WHEN ? IS NULL THEN NULL ELSE datetime('now') END, datetime('now'), ?)
                  ON CONFLICT(sender_key) DO UPDATE SET
                    status = excluded.status, decided_at = excluded.decided_at,
                    shown_at = excluded.shown_at, detail = excluded.detail""",
               (key, status, status, detail))


def run(db, live: bool, ask=input) -> None:
    review, still = candidates(db)
    if still:
        print("Still sending more than 3 days after you unsubscribed (consider a Mail rule or block):")
        for g in still:
            print(f"     {g['name']} <{g['address']}>, unsubscribed {g['decided_at'][:10]}")
        print()
    if not review:
        print("No junk senders to review. (Only mail Claude labelled promo or newsletter is listed.)")
        return
    print(f"Junk senders to review ({len(review)}), from mail Claude labelled promo or newsletter:\n")
    for i, g in enumerate(review, 1):
        print(describe(i, g))
    print("\nType numbers to unsubscribe, k+number to keep for good (e.g. 1 3 k2). Enter = nothing.")
    while True:
        try:
            unsub, keep = parse_picks(ask("> "), len(review))
            break
        except ValueError as e:
            print(f"  {e}. Try again.")

    picked = [review[i - 1] for i in sorted(unsub)]
    clicks = [g for g in picked if g["url"]]
    waiting = [g for g in picked if link_unknown(g)]
    by_hand = [g for g in picked if not g["url"] and not link_unknown(g)]
    for g in clicks:
        print(f"  unsubscribe: {g['name'] or g['address']}  (one-click POST to {urlsplit(g['url']).hostname})")
    for g in by_hand:
        print(f"  by hand:     {g['name'] or g['address']}  ({method(g)[9:]})")
    for g in waiting:
        print(f"  waiting:     {g['name'] or g['address']}  (comes back with a one-click link when it next emails you)")
    for i in sorted(keep):
        print(f"  keep:        {review[i - 1]['name'] or review[i - 1]['address']}")
    if not live:
        print("\n(preview: nothing sent, nothing recorded. Run with --live to do it.)")
        return
    if clicks and ask(f"\nSend {len(clicks)} unsubscribe request{'s' if len(clicks) > 1 else ''}? Type yes: ").strip().lower() != "yes":
        print("Nothing sent, nothing recorded.")
        return

    worked = 0
    for g in clicks:
        ok, what = one_click(g["url"])
        print(f"  {'OK  ' if ok else 'FAIL'} {g['name'] or g['address']}: {what}")
        if ok:
            record(db, g["key"], "unsubscribed", what)
            worked += 1
        else:  # not recorded as done, so it comes back next time
            db.execute("DELETE FROM sender_choices WHERE sender_key = ?", (g["key"],))
    for g in by_hand:
        record(db, g["key"], "by_hand", method(g))
    for g in waiting:
        record(db, g["key"], "waiting", "picked; no link saved yet")
    for i in keep:
        record(db, review[i - 1]["key"], "keep")
    done = {g["key"] for g in picked} | {review[i - 1]["key"] for i in keep}
    for g in review:   # unticked: shown now, back only if they send more
        if g["key"] not in done:
            record(db, g["key"], None)
    db.commit()
    print(f"\n{worked}/{len(clicks)} one-click unsubscribes accepted. "
          f"{len(by_hand)} to do by hand, {len(waiting)} waiting for their next email, {len(keep)} kept.")
    if clicks:
        print("Accepted = the sender's server said OK. Senders may take a couple of days; "
              "if mail keeps coming, this list will say so.")


# --- missing headers for mail fetched before 5b, and the weekly nudge --------------------

def backfill(db) -> int:
    """Read the headers (read-only, BODY.PEEK) of junk mail fetched before 5b that is still
    in the inbox. Mail already gone is recorded as unknown so it isn't asked for again."""
    marks = ",".join("?" * len(OFFER_LABELS))
    missing = db.execute(f"""
        SELECT m.account, m.uidvalidity, m.uid FROM messages m
        JOIN claude_labels c USING (account, uidvalidity, uid)
        LEFT JOIN unsub_headers u USING (account, uidvalidity, uid)
        WHERE u.uid IS NULL AND c.label IN ({marks})""", OFFER_LABELS).fetchall()
    if not missing:
        return 0
    found = 0
    for account in load_accounts():
        mine = [r for r in missing if r["account"] == account["name"]]
        if not mine:
            continue
        with connect(account) as client:
            info = client.select_folder("INBOX", readonly=True)
            current = [r for r in mine if r["uidvalidity"] == info[b"UIDVALIDITY"]]
            data = client.fetch([r["uid"] for r in current], ["BODY.PEEK[HEADER]"]) if current else {}
        for r in mine:
            raw = data.get(r["uid"], {}).get(b"BODY[HEADER]")
            h = header_info(email.message_from_bytes(raw, policy=policy.default)) if raw else None
            store.save_unsub(db, r["account"], r["uidvalidity"], r["uid"], h)
            found += bool(raw)
    db.commit()
    return found


def last_sunday_noon(now: datetime) -> datetime:
    noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
    sunday = noon - timedelta(days=(now.weekday() + 1) % 7)   # weekday(): Monday 0 ... Sunday 6
    return sunday if sunday <= now else sunday - timedelta(days=7)


def weekly_nudge(db, live: bool, now: datetime | None = None) -> None:
    """From Sunday noon, the first scheduled run sends one counts-only Telegram nudge
    (once per week; a missed Sunday is caught up on Monday). Never raises."""
    try:
        week = last_sunday_noon(now or datetime.now()).date().isoformat()
        if db.execute("SELECT 1 FROM nudges WHERE week = ?", (week,)).fetchone():
            return
        review, still = candidates(db)
        if not review and not still:
            return
        bits = [f"{len(review)} junk sender{'s' if len(review) != 1 else ''} to review" if review else "",
                f"{len(still)} still sending after unsubscribe" if still else ""]
        text = f"Weekly: {', '.join(b for b in bits if b)}. On the Mac run scripts/unsubscribe.sh"
        if not live:
            print(f"(dry run: weekly nudge not sent; it would say: {text})")
            return
        notify.send(text, loud=False)
        db.execute("INSERT INTO nudges (week) VALUES (?)", (week,))
        db.commit()
        print(f"Weekly unsubscribe nudge sent: {text}")
    except Exception as e:
        print(f"Weekly nudge FAILED (summary unaffected): {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Unsubscribe from junk senders you pick.")
    parser.add_argument("--live", action="store_true", help="really send the unsubscribe requests")
    args = parser.parse_args()
    if not sys.stdin.isatty():
        sys.exit("Run this yourself in Terminal: it asks you which senders to pick.")
    db = store.open_db()
    try:
        n = backfill(db)
        if n:
            print(f"(read unsubscribe headers for {n} older emails)\n")
    except Exception as e:
        print(f"(couldn't read older emails' headers: {e}; carrying on)\n")
    run(db, live=args.live)


if __name__ == "__main__":
    main()
