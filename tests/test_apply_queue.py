"""The apply queue (milestone 9): offline, fake Telegram, temp database. All jobs are made up."""
import json
from datetime import date, timedelta

import pytest

from email_organiser import apply_queue, feedback, notify, paths, pipeline, store

TODAY = date(2026, 11, 21)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(paths, "APPLIED", tmp_path / "applied.tsv")
    return store.open_db()


def want(db, key, title, company, size="unknown", days_ago=1, url="https://www.linkedin.com/jobs/view/1/"):
    db.execute("INSERT INTO jobs (job_key, title, company, fit, url, employer_size) VALUES (?, ?, ?, 'high', ?, ?)",
               (key, title, company, url, size))
    db.execute("INSERT INTO job_feedback (job_key, verdict, fit_then) VALUES (?, 'apply', 'high')", (key,))
    since = f"{TODAY - timedelta(days=days_ago):%Y-%m-%d} 09:00:00"
    return pipeline.upsert(db, company, title, "would_apply", source="telegram", job_key=key, since=since)


def test_big_employers_first_then_longest_waiting(db):
    want(db, "k1", "Analyst", "Small Co", size="small", days_ago=5)
    want(db, "k2", "Trainee", "Big Bank", size="large", days_ago=1)
    want(db, "k3", "Engineer", "Mid Co", size="unknown", days_ago=3)
    want(db, "k4", "Intern", "Big Insurer", size="large", days_ago=2)
    old = want(db, "k5", "Old Job", "Gone Co", size="large", days_ago=20)
    snoozed = want(db, "k6", "Later", "Big Later", size="large", days_ago=4)
    db.execute("UPDATE applications SET snoozed_until = ? WHERE id = ?", (f"{TODAY + timedelta(days=3)}", snoozed))
    top, total, left = apply_queue.queue(db, TODAY)
    assert [r["company"] for r in top] == ["Big Insurer", "Big Bank", "Mid Co", "Small Co"]
    assert (total, left) == (4, 1)
    assert old not in [r["id"] for r in top]


def test_message_escapes_and_links_only_https(db):
    want(db, "k1", "Analyst <b>now</b>", "A&B Ltd", url="http://tracker.example/x")
    want(db, "k2", "Trainee", "Big Bank", size="large")
    top, total, left = apply_queue.queue(db, TODAY)
    text = apply_queue.message(top, total, left, TODAY)
    assert text.startswith("<b>🗂 Apply queue</b>\n2 jobs you said you'd apply to. Do these first:")
    assert '<a href="https://www.linkedin.com/jobs/view/1/">Trainee</a> · Big Bank · waiting 1 day' in text
    assert "Analyst &lt;b&gt;now&lt;/b&gt; · A&amp;B Ltd" in text and "http://tracker" not in text


def test_applied_tap_moves_it_on_and_records_it_once(db):
    a = want(db, "k1", "Analyst", "Acme Bank", size="large")
    db.execute("INSERT INTO queue_messages (day, message_id, app_ids) VALUES (?, 77, ?)", (TODAY.isoformat(), json.dumps([a])))
    said, markup = apply_queue.tap(db, "a", a, 77)
    assert said == "Saved: applied"
    assert db.execute("SELECT stage FROM applications WHERE id = ?", (a,)).fetchone()[0] == "applied"
    assert db.execute("SELECT verdict FROM job_feedback WHERE job_key = 'k1'").fetchone()[0] == "applied"
    assert paths.APPLIED.read_text().count("Acme Bank\tAnalyst") == 1
    assert markup == {"inline_keyboard": [[{"text": "1 ✓ Applied", "callback_data": "q:x:0"}]]}
    apply_queue.tap(db, "a", a, 77)                                        # tapped twice
    assert paths.APPLIED.read_text().count("Acme Bank\tAnalyst") == 1
    assert apply_queue.queue(db, TODAY)[1] == 0


def test_not_now_hides_it_for_a_week(db):
    a = want(db, "k1", "Analyst", "Acme Bank")
    said, _ = apply_queue.tap(db, "n", a, None)
    assert "7 days" in said
    assert apply_queue.queue(db, date.today())[1] == 0
    assert apply_queue.queue(db, date.today() + timedelta(days=8))[1] == 1
    assert db.execute("SELECT stage FROM applications WHERE id = ?", (a,)).fetchone()[0] == "would_apply"


def test_sent_once_a_day_silently(db, monkeypatch):
    want(db, "k1", "Analyst", "Acme Bank")
    calls = []
    monkeypatch.setattr(feedback, "chat", lambda: ("TOKEN", 42))
    def call(token, method, payload):
        calls.append(payload)
        return {"message_id": 501}
    monkeypatch.setattr(notify, "call", call)
    assert apply_queue.send(db, TODAY) == 1
    assert apply_queue.send(db, TODAY) == 0                                # once a day
    p = calls[0]
    assert p["parse_mode"] == "HTML" and p["disable_notification"] and p["link_preview_options"]["is_disabled"]
    assert p["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("q:a:")


def test_nothing_waiting_sends_nothing(db, monkeypatch):
    monkeypatch.setattr(notify, "call", lambda *a, **k: pytest.fail("nothing to send"))
    assert apply_queue.send(db, TODAY) == 0


def test_queue_taps_arrive_through_the_tap_collector(db, monkeypatch):
    a = want(db, "k1", "Analyst", "Acme Bank")
    db.execute("INSERT INTO queue_messages (day, message_id, app_ids) VALUES (?, 77, ?)", (TODAY.isoformat(), json.dumps([a])))
    db.commit()
    update = {"update_id": 5, "callback_query": {"id": "cb1", "from": {"id": 42}, "data": f"q:a:{a}",
                                                 "message": {"message_id": 77}}}
    got = feedback.handle(db, update, 42)
    assert got[1]["text"] == "Saved: applied" and got[1]["markup"]["inline_keyboard"][0][0]["text"] == "1 ✓ Applied"
    stranger = {"update_id": 6, "callback_query": {"id": "cb2", "from": {"id": 99}, "data": f"q:n:{a}", "message": {}}}
    assert feedback.handle(db, stranger, 42)[1] == {"text": "Not yours to mark."}


def test_an_older_applications_table_is_upgraded(tmp_path, monkeypatch):
    """21 Nov: dry runs had created an empty applications table in the real database before the
    snoozed_until column existed. Opening the database must add the column, not fail later."""
    import sqlite3
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, company TEXT NOT NULL, title TEXT, "
                "stage TEXT NOT NULL, posting_closed INTEGER DEFAULT 0, deadline TEXT, action TEXT, source TEXT, "
                "job_key TEXT, created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))")
    con.commit()
    con.close()
    monkeypatch.setattr(store, "DB_PATH", old)
    db = store.open_db()
    assert "snoozed_until" in [r[1] for r in db.execute("PRAGMA table_info(applications)")]
    assert apply_queue.queue(db, TODAY) == ([], 0, 0)
