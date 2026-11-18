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


def test_gate_judges_per_email_and_ignores_wobble():
    is_job = [True, True, False, False]
    old = [[True, False, True, True]] * 3
    # job email 1 fixed in every run; email 2 wrong every run (a cheap loss); email 3 wobbles
    new = [[True, True, False, True], [True, True, False, False], [True, True, False, True]]
    ok, why = learn.kept(old, new, is_job)
    assert ok and "job emails: 1 gained, 0 lost" in why and "1 gained, 1 lost" in why
    # a job email lost in every run blocks the change, whatever else improves
    assert not learn.kept(old, [[False, True, True, True]] * 3, is_job)[0]
    # a job email right in only some new runs isn't a gain
    assert not learn.kept(old, [[True, True, True, True], [True, False, True, True], [True, True, True, True]], is_job)[0]


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
    flipped = db.execute("SELECT a.uid FROM score_answers a JOIN score_answers b USING (account, uidvalidity, uid) "
                         "WHERE a.week < b.week AND a.is_right = 1 AND b.is_right = 0").fetchall()
    assert [r[0] for r in flipped] == [1]      # the job email that flipped can be named


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


# --- 7b: growing the test set -----------------------------------------------------------------

def test_rule_picks_are_job_shaped_and_skip_alert_digests():
    from email_organiser.grow_testset import is_rule_pick
    assert is_rule_pick("lseg@myworkday.com", "Workday", "Your update")          # application system
    assert is_rule_pick("hr@bank.test", "Bank", "Interview invitation")          # job word
    assert is_rule_pick("hr@bank.test", "Bank", "面試邀請")
    assert not is_rule_pick("jobalerts-noreply@linkedin.com", "LinkedIn Job Alerts", "Interview tips")
    assert not is_rule_pick("noreply@e.jobsdb.com", "Jobsdb Recommendations", "New roles")
    assert not is_rule_pick("deals@shop.test", "Shop", "Big sale")


def test_choose_caps_senders_and_adds_random_without_claude():
    import random
    from email_organiser import grow_testset as g
    cands = ([dict(sender="hr@a.test", name="A", subject=f"Interview {i}") for i in range(10)]
             + [dict(sender=f"x{i}@b.test", name="B", subject="Hello") for i in range(30)])
    picks = g.choose(cands, random.Random(1))
    rule = [p for p in picks if p["why"] == "rule"]
    assert len(rule) == g.PER_SENDER                     # one sender can't flood the set
    assert sum(p["why"] == "random" for p in picks) == g.RANDOM_PICKS
    assert all(p["subject"] == "Hello" for p in picks if p["why"] == "random")
    assert g.choose(cands, random.Random(1)) == picks    # same seed, same picks
    assert "classify" not in open(g.__file__).read()     # Claude never chooses the test set


def test_last_sunday_noon():
    from datetime import datetime
    assert learn.last_sunday_noon(datetime(2026, 11, 22, 18, 0)) == datetime(2026, 11, 22, 12, 0)  # Sun 18:00
    assert learn.last_sunday_noon(datetime(2026, 11, 22, 8, 0)) == datetime(2026, 11, 15, 12, 0)   # Sun 08:00
    assert learn.last_sunday_noon(datetime(2026, 11, 23, 8, 0)) == datetime(2026, 11, 22, 12, 0)   # Mon catch-up
