"""Offline tests: no mail server, no Claude. They check the parts that must never break."""
from email.message import EmailMessage

import pytest

from email_organiser import digest, store
from email_organiser.fetch import body_text, parse_message


def make_email(subject="Hello", sender="Alice <alice@example.com>", body="Hi there", html=None) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, "me@example.com", subject
    msg["Date"], msg["Message-ID"] = "Mon, 16 Nov 2026 09:00:00 +0800", "<1@example.com>"
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


def row(**over) -> dict:
    """A fake summary row, shaped like the database rows digest.py uses."""
    base = dict(account="yahoo", uidvalidity=1, uid=1, sender="Acme <hr@acme.example>",
                subject="Your application", date="Mon, 16 Nov 2026 09:00:00 +0800",
                first_seen="2026-11-16 01:00:00", label="read", needs_reply=0, reason="r")
    return base | over


# --- reading emails -------------------------------------------------------------

def test_parse_message_reads_headers_and_body():
    info = parse_message(make_email(subject="面試邀請 Interview", body="Please reply by Friday"))
    assert info["subject"] == "面試邀請 Interview"  # encoded Chinese subject decoded
    assert info["sender"] == "Alice <alice@example.com>"
    assert "reply by Friday" in info["snippet"]


def test_html_only_email_is_reduced_to_text():
    msg = EmailMessage()
    msg["Subject"] = "html only"
    msg.set_content("<style>p{color:red}</style><p>Hello <b>you</b></p><script>x()</script>",
                    subtype="html")
    assert body_text(msg) == "Hello you"  # tags, styling and scripts removed


# --- remembering what we've seen ------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def test_same_email_is_only_saved_once(db):
    info = dict(message_id="<1>", sender="a", subject="s", date="d", snippet="x")
    assert store.save_if_new(db, "yahoo", 1, 42, info) is True
    assert store.save_if_new(db, "yahoo", 1, 42, info) is False   # primary key blocks it
    assert store.save_if_new(db, "yahoo", 2, 42, info) is True    # folder renumbered: new


def test_sql_injection_in_subject_is_stored_as_text(db):
    nasty = "x'); DROP TABLE messages; --"
    info = dict(message_id="<2>", sender="a", subject=nasty, date="d", snippet="x")
    store.save_if_new(db, "yahoo", 1, 7, info)
    assert db.execute("SELECT subject FROM messages").fetchone()[0] == nasty


# --- the summary ------------------------------------------------------------------

def test_html_summary_escapes_email_text():
    html = digest.build_html([row(subject="<script>alert(1)</script>")])
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_noisy_sender_is_grouped_but_others_are_not():
    rows = [row(uid=i, sender="Yahoo <n@yahoo.example>", subject=f"alert {i}") for i in range(3)]
    rows.append(row(uid=9, sender="Prize team <p@example.com>", subject="tax answer"))
    singles, groups = digest.collapse(rows)
    assert [r["subject"] for r in singles] == ["tax answer"]
    assert groups[0][0] == "Yahoo" and len(groups[0][1]) == 3


def test_job_emails_are_never_grouped():
    rows = [row(uid=i, label="job", sender="Jobsdb <j@jobsdb.example>") for i in range(5)]
    text = digest.build_text(rows)
    assert text.count("Jobsdb") == 5   # every job email listed on its own


def test_subject_line_when_nothing_matters():
    rows = [row(label="fyi"), row(uid=2, label="promo")]
    assert "nothing needs you (2 routine)" in digest.subject_line(digest.sections(rows), 2)


# --- Telegram ping ----------------------------------------------------------------

from email_organiser import notify


def test_ping_carries_counts_but_no_email_content():
    rows = [row(subject="Offer from Acme", label="job", needs_reply=1),
            row(uid=2, subject="Secret thing", sender="Bob <bob@x.example>", needs_reply=1, label="fyi"),
            row(uid=3, label="newsletter")]
    text, loud = notify.message(digest.ping_counts(rows), len(rows), "yahoo")
    assert "1 job, 1 to reply" in text and loud
    for private in ("Offer", "Acme", "Secret", "Bob", "bob@"):
        assert private not in text


def test_routine_and_empty_runs_ping_without_sound():
    text, loud = notify.message({"job": 0, "reply": 0, "read": 0}, 4, "yahoo")
    assert "nothing needs you (4 routine)" in text and not loud
    text, loud = notify.message({"job": 0, "reply": 0, "read": 0}, 0, "yahoo")
    assert "no new email" in text and not loud
    _, loud = notify.message({"job": 0, "reply": 0, "read": 3}, 3, "yahoo")
    assert not loud  # "worth reading" alone isn't worth a sound


def test_ping_failure_never_breaks_the_summary(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.paths, "TELEGRAM_CHAT", tmp_path / "missing")
    monkeypatch.setattr(notify.keyring, "get_password", lambda *a: None)
    assert notify.ping({"job": 1, "reply": 0, "read": 0}, 1, "yahoo") is False  # no exception


def test_errors_never_show_the_token(monkeypatch):
    def boom(*a, **k):
        raise OSError("failed for https://api.telegram.org/botSECRET123/sendMessage")
    monkeypatch.setattr(notify.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError) as e:
        notify.call("SECRET123", "sendMessage", {})
    assert "SECRET123" not in str(e.value)


# --- milestone 4: reply drafts --------------------------------------------------

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from email_organiser import drafts


def reply_row(**over) -> dict:
    return row(message_id="<orig@acme.example>", snippet="Are you free next week?",
               needs_reply=1, label="job") | over


def test_draft_is_threaded_under_the_original():
    msg = drafts.build_draft("me@example.com", reply_row(),
                             {"reply_to": "", "references": "<a@x> <b@x>"}, "Hi\n\nSidrian")
    assert msg["To"] == "Acme <hr@acme.example>"
    assert msg["Subject"] == "Re: Your application"
    assert msg["In-Reply-To"] == "<orig@acme.example>"
    assert msg["References"] == "<a@x> <b@x> <orig@acme.example>"
    assert msg.get_content_type() == "text/plain"  # plain text only: nothing can run


def test_draft_goes_to_reply_to_and_re_is_not_doubled():
    msg = drafts.build_draft("me@example.com", reply_row(subject="RE: Interview"),
                             {"reply_to": "Jobs <jobs@acme.example>", "references": ""}, "x")
    assert msg["To"] == "Jobs <jobs@acme.example>"
    assert msg["Subject"] == "RE: Interview"
    assert msg["References"] == "<orig@acme.example>"


def test_reply_to_and_references_are_read_from_the_full_header():
    raw = (b"From: a@x.example\r\nReply-To: Jobs <jobs@x.example>\r\n"
           b"References: <old@x>\r\nSubject: s\r\n\r\n")

    class FakeClient:
        def fetch(self, uids, parts):
            assert parts == ["BODY.PEEK[HEADER]"]  # PEEK: never marks the email read
            return {7: {b"SEQ": 1, b"BODY[HEADER]": raw}}

    assert drafts.original_headers(FakeClient(), 7) == {
        "reply_to": "Jobs <jobs@x.example>", "references": "<old@x>"}
    assert drafts.original_headers(FakeClient(), 8) is None  # email gone: no draft


def test_line_breaks_in_subject_cannot_add_headers():
    msg = drafts.build_draft("me@example.com", reply_row(subject="Hi\r\nBcc: evil@x.example"),
                             {"reply_to": "", "references": ""}, "x")
    assert msg["Bcc"] is None
    assert "\n" not in msg["Subject"]


def save_reply_email(db, uid, label, hours_ago=1):
    when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    store.save_if_new(db, "yahoo", 1, uid, dict(message_id=f"<{uid}@x>", sender="a@x.example",
                                                  subject="s", date=when, snippet="b"))
    db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply) "
               "VALUES ('yahoo', 1, ?, ?, 1)", (uid, label))


def test_junk_old_and_already_drafted_mail_gets_no_draft(db):
    save_reply_email(db, 1, "job")
    save_reply_email(db, 2, "spam")             # Claude said "needs reply" but it's junk
    save_reply_email(db, 3, "promo")
    save_reply_email(db, 4, "read", hours_ago=72)  # too old
    save_reply_email(db, 5, "read")
    db.execute("INSERT INTO drafts (account, uidvalidity, uid) VALUES ('yahoo', 1, 5)")
    assert [r["uid"] for r in drafts.pending(db)] == [1]


def test_drafting_failure_never_stops_the_summary(db, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("claude is down")
    monkeypatch.setattr(digest, "fetch_new", lambda *a, **k: 0)
    monkeypatch.setattr(digest, "classify_pending", lambda db: 0)
    monkeypatch.setattr(digest.drafts, "make_drafts", boom)
    monkeypatch.setattr(digest.store, "open_db", lambda: db)
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()  # dry run; must not raise
    assert "Drafts FAILED (summary unaffected)" in capsys.readouterr().out


def test_summary_shows_draft_ready():
    rows = [row(needs_reply=1, label="fyi", drafted=1)]
    assert "draft ready" in digest.build_html(rows)
    assert "[draft in Drafts]" in digest.build_text(rows)


def test_no_sending_code_exists():
    """Design rule 2, enforced: the project contains no way to send email."""
    src = Path(__file__).parent.parent / "src"
    forbidden = ("smtplib", "SMTP(", "sendmail", "send_message")
    for f in src.rglob("*.py"):
        text = f.read_text()
        for word in forbidden:
            assert word not in text, f"{word} found in {f.name}"


# --- milestone 5: junk to Assistant-Review --------------------------------------

from email_organiser import cleanup


def save_labelled(db, uid, label, sender="a@shop.example", needs_reply=0, hours_ago=1):
    when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=hours_ago))
    store.save_if_new(db, "yahoo", 1, uid, dict(message_id=f"<{uid}@x>", sender=sender,
                                                  subject=f"s{uid}", date=when, snippet="b"))
    db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply) "
               "VALUES ('yahoo', 1, ?, ?, ?)", (uid, label, needs_reply))


def test_only_recent_junk_that_needs_no_reply_moves(db):
    save_labelled(db, 1, "promo")
    save_labelled(db, 2, "newsletter")
    save_labelled(db, 3, "spam")
    save_labelled(db, 4, "job")                          # never
    save_labelled(db, 5, "read")                         # not junk
    save_labelled(db, 6, "fyi")                          # not junk (Sid's choice)
    save_labelled(db, 7, "promo", needs_reply=1)         # needs a reply: never
    save_labelled(db, 8, "promo", hours_ago=72)          # backlog: left alone
    save_labelled(db, 9, "newsletter", sender="LinkedIn <jobs-noreply@linkedin.com>")  # job alert
    save_labelled(db, 10, "promo")
    db.execute("INSERT INTO moved (account, uidvalidity, uid) VALUES ('yahoo', 1, 10)")  # done
    to_move, held = cleanup.pending(db)
    assert sorted(r["uid"] for r in to_move) == [1, 2, 3]
    assert [r["uid"] for r in held] == [9]


def test_job_boards_are_recognised():
    assert cleanup.is_job_board("Jobsdb <noreply@e.jobsdb.com>")
    assert cleanup.is_job_board("LinkedIn <jobs-noreply@linkedin.com>")
    assert cleanup.is_job_board("LinkedIn <messages-noreply@linkedin.com>")      # "X is hiring"
    assert not cleanup.is_job_board("LinkedIn <updates-noreply@linkedin.com>")  # social
    assert not cleanup.is_job_board("x <a@notjobsdb.com>")  # look-alike domain


class FakeServer:
    """Records what the code asks the server to do."""
    def __init__(self, caps, fail_copy=False):
        self.caps, self.fail_copy, self.calls = caps, fail_copy, []

    def has_capability(self, cap):
        return cap in self.caps

    def __getattr__(self, name):
        def record(*args, **kwargs):
            if name == "copy" and self.fail_copy:
                raise RuntimeError("copy failed")
            self.calls.append((name, args))
        return record


def test_move_uses_move_when_the_server_has_it():
    s = FakeServer({"MOVE", "UIDPLUS"})
    cleanup.move(s, [5, 6])
    assert s.calls == [("move", ([5, 6], "Assistant-Review"))]


def test_icloud_style_move_only_removes_the_moved_emails():
    s = FakeServer({"UIDPLUS"})
    cleanup.move(s, [5, 6])
    assert [c[0] for c in s.calls] == ["copy", "add_flags", "uid_expunge"]
    assert s.calls[-1] == ("uid_expunge", ([5, 6],))       # only these UIDs
    assert not any(c[0] == "expunge" for c in s.calls)     # never a plain EXPUNGE


def test_failed_copy_removes_nothing():
    s = FakeServer({"UIDPLUS"}, fail_copy=True)
    with pytest.raises(RuntimeError):
        cleanup.move(s, [5])
    assert s.calls == []  # no \Deleted flag, no expunge


def test_server_without_uid_expunge_is_refused():
    s = FakeServer(set())
    with pytest.raises(RuntimeError):
        cleanup.move(s, [5])
    assert s.calls == []


def test_moves_are_capped_per_run(db, monkeypatch):
    for uid in range(1, cleanup.MAX_MOVES + 6):
        save_labelled(db, uid, "promo")
    seen = []
    monkeypatch.setattr(cleanup, "move_all", lambda db, rows: seen.append(len(rows)) or len(rows))
    assert cleanup.run(db, live=True) == cleanup.MAX_MOVES
    assert seen == [cleanup.MAX_MOVES]


def test_cleanup_failure_never_stops_the_summary(db, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("server said no")
    monkeypatch.setattr(digest, "fetch_new", lambda *a, **k: 0)
    monkeypatch.setattr(digest, "classify_pending", lambda db: 0)
    monkeypatch.setattr(digest.drafts, "make_drafts", lambda *a, **k: 0)
    monkeypatch.setattr(digest.cleanup, "run", boom)
    monkeypatch.setattr(digest.store, "open_db", lambda: db)
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()
    assert "Clean-up FAILED (summary unaffected)" in capsys.readouterr().out


def test_summary_says_what_was_moved():
    rows = [row(label="newsletter", moved=1), row(uid=2, label="newsletter", moved=1),
            row(uid=3, label="promo", moved=1), row(uid=4, label="fyi")]
    assert "Moved to Assistant-Review: 1 promo, 2 newsletter." in digest.build_text(rows)
    assert "Moved to Assistant-Review" in digest.build_html(rows)


# --- job-alert screener ---------------------------------------------------------

from email_organiser import notify, screener


def alert_email(html_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "Jobsdb <noreply@e.jobsdb.com>", "New jobs"
    msg.set_content("plain version")
    msg.add_alternative(html_body, subtype="html")
    return msg


def test_links_become_tags_and_keep_their_text():
    msg = alert_email('<p>Hi</p><a href="https://url.jobsdb.com/a?x=1&amp;y=2">Data Analyst</a>'
                      '<a href="https://url.jobsdb.com/b">Graduate Trainee</a>'
                      '<a href="https://url.jobsdb.com/a?x=1&amp;y=2">again</a>')
    text, links = screener.tag_links(msg)
    assert "Data Analyst [L1]" in text and "Graduate Trainee [L2]" in text
    assert "again [L1]" in text                         # same URL, same tag
    assert "http" not in text                           # Claude never sees a URL
    assert links == {"L1": "https://url.jobsdb.com/a?x=1&y=2", "L2": "https://url.jobsdb.com/b"}


def test_only_links_to_the_sending_site_are_kept():
    jobsdb = "Jobsdb <noreply@e.jobsdb.com>"
    assert screener.safe_link("https://url.jobsdb.com/x", jobsdb)
    assert screener.safe_link("https://www.linkedin.com/comm/jobs/view/1", "LinkedIn <jobs-noreply@linkedin.com>")
    assert not screener.safe_link("https://evil.example/x", jobsdb)           # other site
    assert not screener.safe_link("https://jobsdb.com.evil.example/x", jobsdb)  # look-alike
    assert not screener.safe_link("http://url.jobsdb.com/x", jobsdb)          # not https
    assert screener.site("mail.clp.com.hk") == "clp.com.hk"


def test_same_job_in_two_alerts_is_stored_once(db):
    r = row(uid=1)
    a = {"row": r}
    job = dict(title="Data Analyst", company="Acme Ltd", location="HK", closing="",
               fit="high", reason="fits", url="", row=r)
    again = job | {"title": "Data  analyst!", "company": "ACME LTD"}
    assert screener.save(db, [a], [job, again]) == 1
    assert len(screener.unshown_high(db)) == 1


def test_screener_candidates(db):
    save_labelled(db, 1, "newsletter", sender="Jobsdb <noreply@e.jobsdb.com>")   # job board
    save_labelled(db, 2, "newsletter", sender="Careers <a@uni.example>")         # flagged below
    db.execute("UPDATE claude_labels SET job_alert = 1 WHERE uid = 2")
    save_labelled(db, 3, "newsletter")                                           # ordinary
    save_labelled(db, 4, "job", sender="Jobsdb <noreply@e.jobsdb.com>")         # application news
    assert sorted(r["uid"] for r in screener.pending(db)) == [1, 2]


def test_job_alert_moves_only_after_screening(db):
    save_labelled(db, 1, "newsletter", sender="LinkedIn <jobalerts-noreply@linkedin.com>")
    to_move, held = cleanup.pending(db)
    assert [r["uid"] for r in held] == [1] and not to_move
    db.execute("INSERT INTO screened (account, uidvalidity, uid, jobs_found) VALUES ('yahoo', 1, 1, 3)")
    to_move, held = cleanup.pending(db)
    assert [r["uid"] for r in to_move] == [1] and not held


def test_no_profile_means_no_screening(db, monkeypatch, tmp_path, capsys):
    save_labelled(db, 1, "newsletter", sender="Jobsdb <noreply@e.jobsdb.com>")
    monkeypatch.setattr(screener.paths, "PROFILE", tmp_path / "missing.md")
    monkeypatch.setattr(screener, "read_alerts", lambda rows: pytest.fail("should not read mail"))
    assert screener.run(db, live=True) == 0
    assert "No profile" in capsys.readouterr().out


def test_good_jobs_lead_the_summary_and_ping():
    jobs = [dict(title="Graduate <b>Analyst</b>", company="Acme", location="HK", closing="30 Nov",
                 reason="graduate role", url="https://url.jobsdb.com/x")]
    html_body = digest.build_html([row()], jobs)
    assert "Jobs worth applying to" in html_body and "<b>Analyst" not in html_body  # escaped
    assert html_body.index("Jobs worth applying to") < html_body.index("Everything else")
    assert "JOBS WORTH APPLYING TO (1)" in digest.build_text([row()], jobs)
    assert "1 good job" in digest.subject_line(digest.sections([row()]), 1, jobs)
    text, loud = notify.message(digest.ping_counts([row()], jobs), 1, "yahoo")
    assert "1 good job" in text and loud and "Acme" not in text


def test_screener_failure_never_stops_the_summary(db, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("claude is down")
    monkeypatch.setattr(digest, "fetch_new", lambda *a, **k: 0)
    monkeypatch.setattr(digest, "classify_pending", lambda db: 0)
    monkeypatch.setattr(digest.screener, "run", boom)
    monkeypatch.setattr(digest.drafts, "make_drafts", lambda *a, **k: 0)
    monkeypatch.setattr(digest.cleanup, "run", lambda *a, **k: 0)
    monkeypatch.setattr(digest.store, "open_db", lambda: db)
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()
    assert "Screener FAILED (summary unaffected)" in capsys.readouterr().out


def test_jobs_already_applied_to_are_skipped(tmp_path, monkeypatch):
    f = tmp_path / "applied.tsv"
    f.write_text("# header\nAIA International Limited\tAIA | Business Consultant Trainee\tApplied\t2026-11-12\n")
    monkeypatch.setattr(screener.paths, "APPLIED", f)
    applied = screener.load_applied()
    assert screener.already_applied({"company": "AIA", "title": "AIA | Business Consultant Trainee"}, applied)
    assert not screener.already_applied({"company": "AIA", "title": "Data Analyst"}, applied)
    assert not screener.already_applied({"company": "Manulife", "title": "AIA | Business Consultant Trainee"}, applied)
    monkeypatch.setattr(screener.paths, "APPLIED", tmp_path / "missing.tsv")
    assert screener.load_applied() == set()


def test_cut_or_reworded_titles_still_match():
    applied = {("king", screener.norm("Finance and investment trainee (Fresh Graduate/IANG/Mandarin Speaking Welcome)"))}
    assert screener.already_applied({"company": "King View Human Resources",
                                     "title": "Finance and investment trainee (Fresh Graduate/IANG)"}, applied)
    a = {"company": "Morgan Mckinley Limited", "title": "Technology Analyst (AI Project) / 6 months extendable"}
    b = {"company": "Morgan McKinley", "title": "Technology Analyst (AI Project) / 6 months extendable / Banking"}
    assert screener.job_key(a) == screener.job_key(b)
    assert not screener.same_title("Data Analyst", "Data Engineer")


def test_commission_roles_are_tagged_in_the_summary():
    jobs = [dict(title="Wealth Management Trainee", company="AIA", location="HK", closing="",
                 reason="fresh grads", url="", commission=1),
            dict(title="Data Analyst", company="Acme", location="HK", closing="",
                 reason="fits", url="", commission=0)]
    text = digest.build_text([row()], jobs)
    assert "Wealth Management Trainee, AIA, HK  [likely commission-based]" in text
    assert "Data Analyst, Acme, HK  [likely" not in text
    assert digest.build_html([row()], jobs).count("likely commission-based") == 1


def test_prompt_rates_uk_outside_london_medium():
    assert "outside Hong Kong and London (including the rest of the UK)" in screener.SYSTEM_PROMPT


def test_linkedin_links_lose_their_tokens():
    url = ("https://www.linkedin.com/comm/jobs/view/4417134919/?trackingId=x&otpToken=SECRET&midToken=Y")
    clean = screener.safe_link(url, "LinkedIn <jobs-noreply@linkedin.com>")
    assert clean == "https://www.linkedin.com/jobs/view/4417134919/"
    assert screener.clean_link("https://url.jobsdb.com/ss/c/abc") == "https://url.jobsdb.com/ss/c/abc"


def test_jobs_alone_still_produce_a_summary(db, monkeypatch, capsys, tmp_path):
    db.execute("INSERT INTO jobs (job_key, title, company, location, closing, fit, reason, url) "
               "VALUES ('k', 'Graduate Analyst', 'Acme', 'HK', '', 'high', 'fits', '')")
    monkeypatch.setattr(digest, "fetch_new", lambda *a, **k: 0)
    monkeypatch.setattr(digest, "classify_pending", lambda db: 0)
    monkeypatch.setattr(digest.screener, "run", lambda *a, **k: 0)
    monkeypatch.setattr(digest.drafts, "make_drafts", lambda *a, **k: 0)
    monkeypatch.setattr(digest.cleanup, "run", lambda *a, **k: 0)
    monkeypatch.setattr(digest.store, "open_db", lambda: db)
    monkeypatch.setattr(digest.paths, "PREVIEW", tmp_path / "preview.html")
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()
    out = capsys.readouterr().out
    assert "1 good job" in out and "No new emails" not in out
