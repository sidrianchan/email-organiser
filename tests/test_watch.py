"""Milestone 6 offline tests: the 15-minute check pings once per urgent email, counts only,
silently at night, makes no Claude call when nothing is new, and never overlaps the summary."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from email_organiser import classify, fetch, runlock, store, watch


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runlock.paths, "HOME", tmp_path)
    return store.open_db()


def add(db, uid, label, *, reply=0, account="icloud", hours_ago=0.5, sender="HR <hr@acme.test>"):
    sent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    store.save_if_new(db, account, 1, uid, dict(message_id=f"<{uid}>", sender=sender,
                                                subject="Interview", date=sent, snippet="secret"))
    if label:
        db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply) "
                   "VALUES (?, 1, ?, ?, ?)", (account, uid, label, reply))
    db.commit()


@pytest.fixture
def offline(monkeypatch):
    """No mail server, no Claude: fetch does nothing, drafts are recorded, pings captured."""
    monkeypatch.setattr(watch, "fetch_new", lambda *a, **k: 0)
    drafted, sent = [], []
    def fake_drafts(db, live, only=None):
        drafted.append(set(only))
        made = 0
        for key in only:   # like the real one: only needs-reply mail gets a draft
            if db.execute("SELECT needs_reply FROM claude_labels WHERE account=? AND uidvalidity=? "
                          "AND uid=?", key).fetchone()[0]:
                db.execute("INSERT OR IGNORE INTO drafts (account, uidvalidity, uid) VALUES (?, ?, ?)", key)
                made += 1
        return made
    monkeypatch.setattr(watch.drafts, "make_drafts", fake_drafts)
    monkeypatch.setattr(watch.notify, "send", lambda text, loud: sent.append((text, loud)))
    return drafted, sent


NOON = datetime(2026, 11, 18, 12, 0)


def test_urgent_mail_pinged_once_with_counts_only(db, offline):
    drafted, sent = offline
    add(db, 1, "job", sender="Secret Bank Recruiter <x@bank.test>")
    add(db, 2, "read", reply=1, account="yahoo")
    add(db, 3, "newsletter")
    assert watch.check(db, live=True, now=NOON) == 2
    text, loud = sent[0]
    assert text == "12:00 new: 1 job email, 1 to reply (iCloud, Yahoo). 1 draft ready." and loud
    assert "Secret" not in text and "bank" not in text and "Interview" not in text
    assert drafted == [{("icloud", 1, 1), ("yahoo", 1, 2)}]      # drafts only for these
    assert watch.check(db, live=True, now=NOON) == 0               # no second ping
    assert len(sent) == 1


def test_no_draft_no_draft_claim(db, offline):
    drafted, sent = offline
    add(db, 1, "job")    # a job email that needs no reply gets no draft
    watch.check(db, live=True, now=NOON)
    assert sent[0][0] == "12:00 new: 1 job email (iCloud)."


def test_night_pings_are_silent_but_still_sent(db, offline):
    _, sent = offline
    add(db, 1, "job")
    watch.check(db, live=True, now=datetime(2026, 11, 18, 23, 30))
    assert sent and sent[0][1] is False
    for hour, loud in ((7, False), (8, True), (22, True), (23, False)):
        assert watch.message([], datetime(2026, 11, 18, hour, 59))[1] is loud


def test_already_summarised_or_old_mail_is_not_pinged(db, offline):
    _, sent = offline
    add(db, 1, "job")
    db.execute("INSERT INTO digested (account, uidvalidity, uid) VALUES ('icloud', 1, 1)")
    add(db, 2, "job", hours_ago=5)        # older than the 3h lookback: the summary has it
    assert watch.check(db, live=True, now=NOON) == 0 and not sent


def test_failed_ping_is_retried_next_check(db, offline, monkeypatch):
    _, sent = offline
    add(db, 1, "job")
    def down(text, loud):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(watch.notify, "send", down)
    assert watch.check(db, live=True, now=NOON) == 0
    monkeypatch.setattr(watch.notify, "send", lambda text, loud: sent.append(text))
    assert watch.check(db, live=True, now=NOON) == 1


def test_dry_run_pings_and_records_nothing(db, offline):
    _, sent = offline
    add(db, 1, "job")
    watch.check(db, live=False, now=NOON)
    assert not sent and db.execute("SELECT COUNT(*) FROM alerted").fetchone()[0] == 0


def test_no_new_mail_means_no_claude_call(db, monkeypatch):
    monkeypatch.setattr(classify, "ask_claude", lambda emails: pytest.fail("Claude called with nothing new"))
    assert classify.classify_pending(db, since_hours=3) == 0


def test_quick_check_only_labels_recent_mail(db, monkeypatch):
    add(db, 1, None)
    add(db, 2, None)
    db.execute("UPDATE messages SET first_seen = datetime('now', '-5 hours') WHERE uid = 2")
    seen = []
    monkeypatch.setattr(classify, "ask_claude",
                        lambda emails: (seen.extend(emails), ({0: {"label": "job", "needs_reply": False,
                                                                    "reason": "r"}}, "m"))[1])
    classify.classify_pending(db, since_hours=3)
    assert len(seen) == 1      # the 5-hour-old one is left for the summary run


def test_fetch_downloads_only_unseen_recent_mail():
    now = datetime.now()

    class FakeClient:
        def __init__(self):
            self.bodies_asked = None
        def search(self, criteria):
            return [1, 2, 3]
        def fetch(self, uids, items):
            if items == ["INTERNALDATE"]:
                return {1: {b"INTERNALDATE": now}, 2: {b"INTERNALDATE": now},
                        3: {b"INTERNALDATE": now - timedelta(hours=10)}}
            self.bodies_asked = list(uids)
            return {u: {b"BODY[]": b"raw"} for u in uids}

    client = FakeClient()
    assert fetch.fetch_recent(client, hours=3, seen=frozenset({1})) == {2: b"raw"}
    assert client.bodies_asked == [2]     # not 1 (seen) and not 3 (too old)


def test_lock_stops_overlapping_runs(db):
    with runlock.run_lock() as first:
        assert first
        with runlock.run_lock(wait_seconds=0) as second:
            assert not second
    with runlock.run_lock() as again:
        assert again                      # released afterwards


def test_lock_works_across_processes(db, tmp_path):
    import subprocess, sys, textwrap
    code = textwrap.dedent(f"""
        from email_organiser import runlock
        from pathlib import Path
        runlock.paths.HOME = Path({str(tmp_path)!r})
        with runlock.run_lock() as got:
            print(got)
    """)
    with runlock.run_lock() as held:
        assert held
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
    assert out.strip() == "False"         # the scheduled job in another process skips its turn
