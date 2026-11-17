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
