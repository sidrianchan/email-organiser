"""Job buttons in Telegram: your verdict on each screened job (feedback.py). Offline."""
import json

import pytest

from email_organiser import feedback, paths, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def add_job(db, key="acme|data analyst", fit="high", title="Data Analyst", company="Acme Ltd"):
    db.execute("INSERT INTO jobs (job_key, title, company, location, closing, fit, reason, url, "
               "commission, account, uidvalidity, uid) VALUES (?, ?, ?, 'Hong Kong', '30 Nov', ?, 'r', "
               "'https://www.linkedin.com/jobs/view/1/', 0, 'yahoo', 1, 1)", (key, title, company, fit))
    db.commit()
    return db.execute("SELECT * FROM jobs WHERE job_key = ?", (key,)).fetchone()


class FakeTelegram:
    def __init__(self, updates=()):
        self.calls, self.updates, self.next_id = [], list(updates), 100
    def __call__(self, token, method, payload=None):
        self.calls.append((method, payload))
        if method == "sendMessage":
            self.next_id += 1
            return {"message_id": self.next_id}
        if method == "getUpdates":
            return [u for u in self.updates if u["update_id"] >= payload["offset"]]
        return True


@pytest.fixture
def tg(monkeypatch):
    fake = FakeTelegram()
    monkeypatch.setattr(feedback, "chat", lambda: ("TOKEN", 42))
    monkeypatch.setattr(feedback.notify, "call", fake)
    return fake


def tap(update_id, code, msg_id, user=42, message_id=101):
    return {"update_id": update_id, "callback_query": {
        "id": f"q{update_id}", "from": {"id": user}, "data": f"j:{code}:{msg_id}",
        "message": {"message_id": message_id}}}


def test_buttons_fit_telegrams_limit_and_tick_the_choice():
    kb = feedback.keyboard(123456789, chosen="not_qualified")
    buttons = [b for row in kb["inline_keyboard"] for b in row]
    assert len(buttons) == 4 and all(len(b["callback_data"].encode()) <= 64 for b in buttons)
    assert [b["text"] for b in buttons if b["text"].startswith("✓")] == ["✓ Not qualified"]


def test_send_once_silently_without_link_preview(db, tg):
    job = add_job(db)
    assert feedback.send_jobs(db, [job]) == 1
    assert feedback.send_jobs(db, [job]) == 0          # never sent twice
    method, payload = tg.calls[0]
    assert payload["disable_notification"] and payload["link_preview_options"] == {"is_disabled": True}
    assert "Data Analyst" in payload["text"] and "Closes 30 Nov" in payload["text"]
    assert db.execute("SELECT message_id FROM job_messages").fetchone()[0] == 101


def test_failed_send_is_retried_next_time(db, tg, monkeypatch):
    job = add_job(db)
    def down(*a, **k):
        raise RuntimeError("Telegram down")
    monkeypatch.setattr(feedback.notify, "call", down)
    with pytest.raises(RuntimeError):
        feedback.send_jobs(db, [job])
    assert db.execute("SELECT COUNT(*) FROM job_messages").fetchone()[0] == 0
    monkeypatch.setattr(feedback.notify, "call", tg)
    assert feedback.send_jobs(db, [job]) == 1


def test_taps_are_stored_latest_wins_and_ticked(db, tg):
    job = add_job(db)
    feedback.send_jobs(db, [job])
    tg.updates = [tap(1, "a", 1), tap(2, "q", 1)]
    assert feedback.collect(db) == 2
    row = db.execute("SELECT verdict, fit_then FROM job_feedback").fetchone()
    assert tuple(row) == ("not_qualified", "high")
    edits = [p for m, p in tg.calls if m == "editMessageReplyMarkup"]
    assert "✓ Not qualified" in json.dumps(edits[-1], ensure_ascii=False)
    assert feedback.collect(db) == 0                    # offset saved: the same taps aren't read twice


def test_applied_adds_to_applied_tsv_once(db, tg):
    job = add_job(db)
    feedback.send_jobs(db, [job])
    paths.APPLIED.write_text("# header\nOther Co\tOther job\tApplied\t2026-11-01")   # no final newline
    tg.updates = [tap(1, "d", 1), tap(2, "d", 1)]
    feedback.collect(db)
    lines = paths.APPLIED.read_text().splitlines()
    assert lines[1] == "Other Co\tOther job\tApplied\t2026-11-01"
    assert sum("Data Analyst" in l for l in lines) == 1
    from email_organiser.screener import already_applied, load_applied
    assert already_applied(dict(job), load_applied())   # never suggested again


def test_other_people_and_junk_taps_are_ignored(db, tg):
    job = add_job(db)
    feedback.send_jobs(db, [job])
    tg.updates = [tap(1, "a", 1, user=999), tap(2, "z", 1), tap(3, "a", 77),
                  {"update_id": 4, "callback_query": {"id": "x", "from": {"id": 42}, "data": "rm -rf"}}]
    assert feedback.collect(db) == 0
    assert db.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 0


def test_collect_never_raises_without_telegram(db, capsys):
    assert feedback.collect(db) == 0          # conftest: Telegram not set up
    assert "couldn't read Telegram" in capsys.readouterr().out


def test_backlog_is_high_plus_a_fixed_medium_sample_minus_applied(db):
    for n in range(3):
        add_job(db, key=f"h{n}", title=f"High {n}", company=f"H{n} Ltd")
    for n in range(30):
        add_job(db, key=f"m{n:02}", fit="medium", title=f"Medium {n}", company=f"M{n} Ltd")
    add_job(db, key="low", fit="low")
    paths.APPLIED.write_text("H0 Ltd\tHigh 0\tApplied\t2026-11-01\n")
    jobs = feedback.backlog(db)
    assert len(jobs) == 2 + feedback.BACKLOG_MEDIUM and "low" not in {j["job_key"] for j in jobs}
    assert [j["job_key"] for j in jobs] == [j["job_key"] for j in feedback.backlog(db)]   # same sample


def test_report(db, tg):
    feedback.send_jobs(db, [add_job(db)])
    tg.updates = [tap(1, "i", 1)]
    feedback.collect(db)
    assert feedback.report(db) == "high: 1 not interested"
