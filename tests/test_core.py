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
