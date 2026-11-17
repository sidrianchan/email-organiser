"""Milestone 7 offline tests: corrections are noticed, rules follow the asymmetric thresholds,
rules change what the code does (never what it may do), and the weekly check scores honestly."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from email_organiser import classify, cleanup, digest, learn, store, unsubscribe


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def add(db, uid, label, *, sender="Shop <deals@shop.test>", reply=0, moved=False, mid=None,
        hours_ago=1, account="yahoo"):
    sent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    store.save_if_new(db, account, 1, uid, dict(message_id=mid or f"<{uid}@x>", sender=sender,
                                                subject=f"s{uid}", date=sent, snippet="b"))
    if label:
        db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply) "
                   "VALUES (?, 1, ?, ?, ?)", (account, uid, label, reply))
    if moved:
        db.execute("INSERT INTO moved (account, uidvalidity, uid, folder) VALUES (?, 1, ?, 'R')", (account, uid))
    db.commit()


def rows(db):
    return db.execute("""SELECT m.*, c.label, c.needs_reply, mv.uid IS NOT NULL AS moved
                         FROM messages m JOIN claude_labels c USING (account, uidvalidity, uid)
                         LEFT JOIN moved mv USING (account, uidvalidity, uid)""").fetchall()


# --- noticing corrections -------------------------------------------------------------------

def test_compare_finds_each_kind_of_correction(db):
    add(db, 1, "promo", moved=True)                      # we moved it; it's back in the inbox
    add(db, 2, "fyi")                                    # we left it; you put it in Review
    add(db, 3, "read")                                   # we left it; it's in Junk
    add(db, 4, "fyi")                                    # you replied, Claude said no reply needed
    add(db, 5, "promo", moved=True)                      # we moved it; still gone: agreement
    add(db, 6, "newsletter")                             # junk label, you binned it: agreement
    found = learn.compare({"<1@x>", "<4@x>"}, rows(db), review={"<2@x>"}, junk={"<3@x>", "<6@x>"},
                          sent={"<4@x>": 99})
    got = {(r["uid"], kind, truth) for r, kind, truth in found}
    assert got == {(1, "rescued", "not_junk"), (2, "marked_junk", "junk"),
                   (3, "marked_junk", "spam"), (4, "replied_unflagged", "needs_reply")}


def test_replying_to_a_flagged_email_is_not_a_correction(db):
    add(db, 1, "job", reply=1)
    assert learn.compare(set(), rows(db), set(), set(), {"<1@x>": 5}) == []


def test_strip_quote_keeps_only_your_words():
    sent = "Thanks, Tuesday 2pm works.\n\nOn Mon, 16 Nov 2026 at 09:00, HR <hr@a.test> wrote:\n> Are you free?"
    assert learn.strip_quote(sent) == "Thanks, Tuesday 2pm works."


# --- rules ----------------------------------------------------------------------------------

def correct(db, uid, kind, truth="junk"):
    db.execute("INSERT INTO corrections (account, uidvalidity, uid, kind, label_was, truth) "
               "VALUES ('yahoo', 1, ?, ?, 'x', ?)", (uid, kind, truth))


def test_one_rescue_means_never_move(db):
    add(db, 1, "promo", sender="Club <news@club.test>", moved=True)
    correct(db, 1, "rescued", "not_junk")
    assert learn.update_rules(db) == ["never move news@club.test"]
    assert store.never_move(db) == {"news@club.test"}
    assert learn.update_rules(db) == []                    # nothing new the second time


def test_junk_rule_needs_three_and_no_rescue(db):
    for uid in (1, 2):
        add(db, uid, "fyi", sender="Ads <ads@ads.test>")
        correct(db, uid, "marked_junk", "spam")
    learn.update_rules(db)
    assert store.junk_rules(db) == {}                      # two isn't enough
    add(db, 3, "fyi", sender="Ads <ads@ads.test>")
    correct(db, 3, "marked_junk", "spam")
    learn.update_rules(db)
    assert store.junk_rules(db)["ads@ads.test"][0] == "spam"
    # a later rescue beats the junk rule
    add(db, 4, "spam", sender="Ads <ads@ads.test>", moved=True)
    correct(db, 4, "rescued", "not_junk")
    learn.update_rules(db)
    assert store.junk_rules(db) == {} and store.never_move(db) == {"ads@ads.test"}


def test_never_move_stops_cleanup_and_unsubscribe(db):
    add(db, 1, "promo", sender="Club <news@club.test>")
    db.execute("INSERT INTO sender_rules (address, rule) VALUES ('news@club.test', 'never_move')")
    db.execute("INSERT INTO unsub_headers (account, uidvalidity, uid, url, one_click, signed) "
               "VALUES ('yahoo', 1, 1, 'https://club.test/u', 1, 1)")
    to_move, _ = cleanup.pending(db)
    assert to_move == []
    assert unsubscribe.groups(db) == []


def test_junk_rule_labels_without_claude(db, monkeypatch):
    add(db, 1, None, sender="Ads <ads@ads.test>")
    db.execute("INSERT INTO sender_rules (address, rule, label, reason) "
               "VALUES ('ads@ads.test', 'junk', 'promo', 'you marked 3 emails from this sender as junk')")
    monkeypatch.setattr(classify, "ask_claude", lambda e: pytest.fail("Claude asked about a ruled sender"))
    assert classify.classify_pending(db) == 1
    label = db.execute("SELECT label, needs_reply, model FROM claude_labels").fetchone()
    assert tuple(label) == ("promo", 0, "sender rule")


def test_rules_cannot_unlock_sending_or_deleting():
    import pathlib
    src = pathlib.Path(learn.__file__).read_text()
    import re
    for forbidden in ("smtplib", "SMTP", "client.append", "client.move", "client.copy", "add_flags",
                      "delete_messages", "expunge", "create_folder"):
        assert forbidden not in src, forbidden
    opens = re.findall(r"select_folder\([^)]*\)", src)
    assert opens and all("readonly=True" in o for o in opens)   # learning only ever looks


# --- the weekly check -----------------------------------------------------------------------

def hand(db, uid, label, reply=0):
    db.execute("INSERT INTO my_labels (account, uidvalidity, uid, label, needs_reply) VALUES "
               "('yahoo', 1, ?, ?, ?)", (uid, label, reply))


def test_test_set_has_hand_labels_and_corrections_once_each(db):
    add(db, 1, "job"); hand(db, 1, "job")
    add(db, 2, "promo", moved=True); correct(db, 2, "rescued", "not_junk")
    add(db, 3, "fyi"); hand(db, 3, "fyi"); correct(db, 3, "replied_unflagged", "needs_reply")
    items = learn.test_set(db)
    assert sorted((i["uid"], i["set"]) for i in items) == [(1, "hand"), (2, "corrections"), (3, "hand")]


def test_score_and_right():
    items = [dict(set="hand", label="job", needs_reply=1), dict(set="hand", label="fyi", needs_reply=0),
             dict(set="corrections", kind="rescued"), dict(set="corrections", kind="marked_junk"),
             dict(set="corrections", kind="replied_unflagged")]
    answers = [dict(label="job", needs_reply=True), dict(label="read", needs_reply=False),
               dict(label="fyi", needs_reply=False), dict(label="promo", needs_reply=False),
               dict(label="fyi", needs_reply=False)]
    s = learn.score(items, answers)
    assert (s["correct"], s["total"], s["job_correct"], s["job_total"], s["reply_correct"]) == (1, 2, 1, 1, 2)
    assert (s["corr_correct"], s["corr_total"]) == (2, 3)


def test_gate_keeps_only_changes_that_dont_drop():
    old = dict(correct=44, total=50, job_correct=6, job_total=6)
    assert learn.kept(dict(correct=45, total=50, job_correct=6, job_total=6), old)
    assert not learn.kept(dict(correct=43, total=50, job_correct=6, job_total=6), old)
    assert not learn.kept(dict(correct=48, total=50, job_correct=5, job_total=6), old)  # job never drops
    assert learn.kept(dict(correct=1, total=1, job_correct=0, job_total=0), None)


def test_weekly_score_runs_once_a_week_and_warns_on_job_drop(db, monkeypatch):
    for uid, label in ((1, "job"), (2, "fyi")):
        add(db, uid, label); hand(db, uid, label)
    replies = iter([{0: dict(label="job", needs_reply=False), 1: dict(label="fyi", needs_reply=False)},
                    {0: dict(label="fyi", needs_reply=False), 1: dict(label="fyi", needs_reply=False)}])
    calls = []
    monkeypatch.setattr(learn, "ask_claude", lambda emails: (calls.append(1), (next(replies), "m1"))[1])
    sunday = datetime(2026, 11, 22, 18, 0)
    first = learn.weekly_score(db, live=True, now=sunday)
    assert first.startswith("Weekly labelling check: 2/2 hand-labelled right (100%), job 1/1")
    assert learn.weekly_score(db, live=True, now=sunday + timedelta(hours=14)) is None   # same week
    second = learn.weekly_score(db, live=True, now=sunday + timedelta(days=7))
    assert "WARNING: job emails dropped" in second and len(calls) == 2


def test_dry_run_makes_no_claude_calls(db, monkeypatch):
    add(db, 1, "job"); hand(db, 1, "job")
    monkeypatch.setattr(learn, "ask_claude", lambda e: pytest.fail("Claude called on a dry run"))
    assert learn.weekly_score(db, live=False, now=datetime(2026, 11, 22, 18, 0)) is None


def test_score_line_appears_in_summary_once(db):
    db.execute("INSERT INTO scores (week, line) VALUES ('2026-11-22', 'Weekly labelling check: 44/50')")
    notes = learn.unshown_lines(db)
    assert "Weekly labelling check: 44/50" in digest.build_text([], notes=notes)
    assert "Weekly labelling check: 44/50" in digest.build_html([], notes=notes)
    learn.mark_shown(db)
    assert learn.unshown_lines(db) == []


def test_learning_failure_never_stops_the_summary(db, monkeypatch, capsys):
    def boom(db):
        raise RuntimeError("imap down")
    monkeypatch.setattr(learn, "collect", boom)
    learn.step(db, live=True)
    assert "FAILED" in capsys.readouterr().out
