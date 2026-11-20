"""20 Nov: Claude was logged out, every call failed, and the summary said "nothing needs you".
These tests replay that day offline: a failure must be said, never hidden."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from email_organiser import classify, digest, health, notify, store, watch


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def add(db, uid, subject, label=None, sender="Broker <noreply@broker.example>"):
    sent = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    store.save_if_new(db, "yahoo", 1, uid, dict(message_id=f"<{uid}@x>", sender=sender,
                                                 subject=subject, date=sent, snippet="b"))
    if label:
        db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply, reason) "
                   "VALUES ('yahoo', 1, ?, ?, 0, 'r')", (uid, label))
    db.commit()


# What `claude -p --output-format json` printed on 20 Nov, cut down: the reason was past char 300.
LOGGED_OUT = json.dumps({"duration_api_ms": 0, "stop_reason": "stop_sequence", "total_cost_usd": 0,
                         "usage": {"input_tokens": 0, "output_tokens": 0, "server_tool_use": {"web_search_requests": 0}},
                         "padding": "x" * 300, "is_error": True, "result": "Not logged in · Please run /login"})


def failing_claude(monkeypatch):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=LOGGED_OUT, stderr="")
    monkeypatch.setattr(classify.subprocess, "run", run)


def test_error_message_keeps_the_reason(monkeypatch):
    failing_claude(monkeypatch)
    with pytest.raises(RuntimeError, match="Please run /login"):
        classify.run_claude("p", "s", {})


def test_model_is_pinned(monkeypatch):
    seen = {}
    def run(command, **kwargs):
        seen["cmd"] = command
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(
            {"structured_output": {"ok": True}, "modelUsage": {classify.MODEL: {}}}), stderr="")
    monkeypatch.setattr(classify.subprocess, "run", run)
    classify.run_claude("p", "s", {})
    assert seen["cmd"][seen["cmd"].index("--model") + 1] == classify.MODEL


def test_unlabelled_mail_is_in_the_summary_not_hidden(db, monkeypatch):
    add(db, 1, "Event Notification: Trading Activity")
    add(db, 2, "Weekly deals", label="promo")
    failing_claude(monkeypatch)
    assert classify.classify_pending(db) == 0
    assert "Please run /login" in classify.last_error

    recent, _ = digest.pending(db)
    g = digest.sections(recent)
    assert len(recent) == 2 and len(g["unlabelled"]) == 1
    subject = digest.subject_line(g, len(recent))
    assert "nothing needs you" not in subject and "1 not labelled, Claude failed" in subject
    text = digest.build_text(recent)
    assert text.index("NOT LABELLED") < text.index("Everything else")      # at the top
    assert "Event Notification: Trading Activity" in text
    assert "Labelled by Claude." not in text and "run /login" in text
    html = digest.build_html(recent)
    assert "Not labelled: Claude failed" in html and "Labelled by Claude." not in html


def test_unlabelled_mail_comes_back_in_the_next_summary(db):
    add(db, 1, "Trading Activity")
    add(db, 2, "Weekly deals", label="promo")
    recent, _ = digest.pending(db)
    digest.mark_done(db, [r for r in recent if r["label"] is not None])
    db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply, reason) "
               "VALUES ('yahoo', 1, 1, 'read', 0, 'trades')")   # Claude works again
    recent, _ = digest.pending(db)
    assert [r["uid"] for r in recent] == [1] and recent[0]["label"] == "read"


def test_all_labelled_summary_is_unchanged(db):
    add(db, 2, "Weekly deals", label="promo")
    recent, _ = digest.pending(db)
    assert digest.subject_line(digest.sections(recent), 1).endswith("nothing needs you (1 routine)")
    assert "Labelled by Claude. Nothing was deleted or sent." in digest.build_text(recent)


def test_ping_is_loud_and_says_what_to_do():
    text, loud = notify.message({"unlabelled": 5, "job": 0, "reply": 0, "read": 0}, 5, "yahoo")
    assert loud and "5 not labelled" in text and "/login" in text and "nothing needs you" not in text


def test_claude_down_pings_once_then_waits(monkeypatch):
    sent = []
    monkeypatch.setattr(health.notify, "send", lambda text, loud: sent.append((text, loud)))
    t = datetime(2026, 11, 20, 8, 0)
    assert health.claude_down("x", t) is True
    assert health.claude_down("x", t + timedelta(minutes=15)) is False   # the next quick check
    assert health.claude_down("x", t + timedelta(hours=6)) is True
    health.claude_ok()
    assert health.claude_down("x", t + timedelta(hours=6, minutes=15)) is True   # a new outage
    assert len(sent) == 3 and all(loud and "/login" in text for text, loud in sent)


def test_quick_check_pings_when_claude_fails(db, monkeypatch):
    add(db, 1, "Interview invitation")
    failing_claude(monkeypatch)
    pinged = []
    monkeypatch.setattr(watch, "fetch_new", lambda *a, **k: 0)
    monkeypatch.setattr(health.notify, "send", lambda text, loud: pinged.append(text))
    watch.check(db, live=True, now=datetime(2026, 11, 20, 14, 28))
    assert len(pinged) == 1 and "Claude isn't answering" in pinged[0]


def test_dry_run_check_never_pings(db, monkeypatch):
    add(db, 1, "Interview invitation")
    failing_claude(monkeypatch)
    monkeypatch.setattr(watch, "fetch_new", lambda *a, **k: 0)
    monkeypatch.setattr(health.notify, "send", lambda *a: pytest.fail("pinged on a dry run"))
    watch.check(db, live=False, now=datetime(2026, 11, 20, 14, 28))


def test_summary_run_replays_20_nov(db, monkeypatch, capsys, tmp_path):
    """The whole summary with Claude down: health check fails, labels fail, screener fails.
    It must still deliver, say so at the top, ping loudly, and leave the mail for next time."""
    add(db, 1, "Event Notification: Trading Activity")
    failing_claude(monkeypatch)
    monkeypatch.setattr(classify, "health_check", lambda: "claude failed: Not logged in")
    monkeypatch.setattr(digest, "fetch_new", lambda *a, **k: 0)
    def screener_down(*a, **k):
        raise RuntimeError("claude failed: Not logged in")
    monkeypatch.setattr(digest.screener, "run", screener_down)
    monkeypatch.setattr(digest.cleanup, "run", lambda *a, **k: 0)
    monkeypatch.setattr(digest.store, "open_db", lambda: db)
    monkeypatch.setattr(digest.paths, "PREVIEW", tmp_path / "preview.html")
    delivered, pings = [], []
    monkeypatch.setattr(digest, "deliver", lambda s, t, h: delivered.append((s, t)))
    monkeypatch.setattr(notify, "send", lambda text, loud: pings.append((text, loud)))
    digest.summarise(live=True, sample=False)

    subject, text = delivered[0]
    assert "1 not labelled, Claude failed" in subject
    assert "Trading Activity" in text and "Job alerts couldn't be screened" in text
    assert pings[0][1] and "Claude isn't answering" in pings[0][0]     # health ping, loud
    assert pings[-1][1] and "1 not labelled" in pings[-1][0]          # summary ping, loud
    recent, _ = digest.pending(db)
    assert len(recent) == 1   # not marked as summarised: comes back once labelled


def test_each_step_uses_its_model(monkeypatch):
    """Labelling and calendar on Sonnet (measured 20 Nov); screener and drafts on Opus (not measured)."""
    used = []
    def run(command, **kwargs):
        used.append(command[command.index("--model") + 1])
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(
            {"structured_output": {"labels": [], "ok": True}, "modelUsage": {}}), stderr="")
    monkeypatch.setattr(classify.subprocess, "run", run)
    classify.ask_claude([])
    assert used == [classify.LABEL_MODEL] and "sonnet" in classify.LABEL_MODEL
    assert "opus" in classify.MODEL
    import inspect
    from email_organiser import agenda, drafts, screener
    assert "model=LABEL_MODEL" in inspect.getsource(agenda.judge if hasattr(agenda, "judge") else agenda)
    for mod in (drafts, screener):   # no override: they get the default, Opus
        assert "model=" not in inspect.getsource(mod)
