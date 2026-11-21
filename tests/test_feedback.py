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


def test_tapping_the_same_button_twice_is_fine(db, tg, monkeypatch, capsys):
    feedback.send_jobs(db, [add_job(db)])
    def call(token, method, payload=None):
        if method == "editMessageReplyMarkup":
            raise RuntimeError("Telegram said: Bad Request: message is not modified: ...")
        return tg(token, method, payload)
    monkeypatch.setattr(feedback.notify, "call", call)
    tg.updates = [tap(1, "a", 1), tap(2, "a", 1)]
    assert feedback.collect(db) == 2
    assert "not handled" not in capsys.readouterr().out


def test_the_quick_check_no_longer_reads_taps():
    """One reader only (the every-minute job), so two never race for the same taps."""
    import inspect
    from email_organiser import watch
    assert "feedback" not in inspect.getsource(watch)


# --- 22 Nov: 'may need experience' jobs grouped in one message; taps reach the apply queue ---

def tap_at(update_id, data, message_id, user=42):
    return {"update_id": update_id, "callback_query": {"id": f"q{update_id}", "from": {"id": user}, "data": data,
                                                       "message": {"message_id": message_id}}}


def test_group_message_lists_jobs_with_numbered_buttons(db, tg):
    jobs = [add_job(db, key=f"c{n}|t", title=f"Analyst <{n}>", company=f"Co{n}") for n in (1, 2, 3)]
    assert feedback.send_group(db, jobs) == 3
    assert feedback.send_group(db, jobs) == 0                     # never sent twice
    sends = [p for m, p in tg.calls if m == "sendMessage"]
    assert len(sends) == 1
    msg = sends[0]
    assert msg["parse_mode"] == "HTML" and msg["disable_notification"] and msg["link_preview_options"] == {"is_disabled": True}
    assert "Analyst &lt;2&gt;" in msg["text"] and "<2>" not in msg["text"]   # escaped
    assert msg["text"].index("1. ") < msg["text"].index("2. ") < msg["text"].index("3. ")
    rows = msg["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in r] for r in rows][1] == ["2 Apply", "2 Not qualified", "2 Not interested"]
    ids = [r[0]["callback_data"].split(":")[2] for r in rows]
    assert len(set(ids)) == 3 and all(len(b["callback_data"]) <= 64 for r in rows for b in r)
    stored = db.execute("SELECT message_id, msg_ids FROM job_group_messages").fetchall()
    assert len(stored) == 1 and stored[0]["message_id"] == 101 and json.loads(stored[0]["msg_ids"])[0] == 1


def test_a_tap_in_the_group_is_stored_and_the_whole_list_redrawn(db, tg):
    jobs = [add_job(db, key=f"c{n}|t", title=f"Analyst {n}", company=f"Co{n}") for n in (1, 2, 3)]
    feedback.send_group(db, jobs)
    second = db.execute("SELECT id FROM job_messages WHERE job_key = 'c2|t'").fetchone()["id"]
    tg.updates = [tap_at(1, f"j:q:{second}", 101)]
    assert feedback.collect(db) == 1
    assert db.execute("SELECT verdict FROM job_feedback WHERE job_key = 'c2|t'").fetchone()["verdict"] == "not_qualified"
    edit = [p for m, p in tg.calls if m == "editMessageReplyMarkup"][-1]
    texts = [[b["text"] for b in r] for r in edit["reply_markup"]["inline_keyboard"]]
    assert len(texts) == 3                                     # all three jobs keep their buttons
    assert texts[1] == ["2 Apply", "✓ 2 Not qualified", "2 Not interested"] and texts[0][0] == "1 Apply"


def test_long_lists_are_split_and_numbering_continues(db, tg):
    jobs = [add_job(db, key=f"c{n}|t", title=f"Analyst {n}", company=f"Co{n}") for n in range(20)]
    assert feedback.send_group(db, jobs) == 20
    sends = [p for m, p in tg.calls if m == "sendMessage"]
    assert len(sends) == 3                                     # 8 + 8 + 4
    assert sends[1]["text"].split("\n\n", 1)[1].startswith("9. ") and "16. " in sends[1]["text"]
    assert sends[2]["reply_markup"]["inline_keyboard"][0][0]["text"] == "17 Apply"
    assert all(len(p["text"]) <= feedback.GROUP_MAX_CHARS for p in sends)
    # a tap on job 17 redraws that message with its own numbering
    j17 = db.execute("SELECT id FROM job_messages WHERE job_key = 'c16|t'").fetchone()["id"]
    tg.updates = [tap_at(1, f"j:a:{j17}", 103)]
    feedback.collect(db)
    edit = [p for m, p in tg.calls if m == "editMessageReplyMarkup"][-1]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["text"] == "✓ 17 Apply"


def test_group_send_failure_is_retried_next_time(db, tg, monkeypatch):
    jobs = [add_job(db, key=f"c{n}|t", title=f"Analyst {n}", company=f"Co{n}") for n in (1, 2)]
    def down(*a, **k):
        raise RuntimeError("Telegram down")
    monkeypatch.setattr(feedback.notify, "call", down)
    with pytest.raises(RuntimeError):
        feedback.send_group(db, jobs)
    assert db.execute("SELECT COUNT(*) n FROM job_messages").fetchone()["n"] == 0
    monkeypatch.setattr(feedback.notify, "call", tg)
    assert feedback.send_group(db, jobs) == 2


def test_would_apply_taps_reach_the_apply_queue_and_a_no_takes_them_out(db, tg):
    from datetime import date
    from email_organiser import apply_queue
    job = add_job(db, key="acme|data analyst", title="Data Analyst", company="Acme Ltd")
    other = add_job(db, key="beta|engineer", title="Engineer", company="Beta Ltd")
    feedback.send_jobs(db, [job])
    feedback.send_group(db, [other])
    ids = {k: db.execute("SELECT id FROM job_messages WHERE job_key = ?", (k,)).fetchone()["id"]
           for k in ("acme|data analyst", "beta|engineer")}
    tg.updates = [tap_at(1, f"j:a:{ids['acme|data analyst']}", 101), tap_at(2, f"j:a:{ids['beta|engineer']}", 102)]
    feedback.collect(db)
    top, total, _ = apply_queue.queue(db, date.today())
    assert total == 2 and {r["job_key"] for r in top} == {"acme|data analyst", "beta|engineer"}
    # changed his mind: 'Not interested' takes it out of the queue; the other stays
    tg.updates.append(tap_at(3, f"j:i:{ids['beta|engineer']}", 102))
    feedback.collect(db)
    top, total, _ = apply_queue.queue(db, date.today())
    assert total == 1 and top[0]["job_key"] == "acme|data analyst"
    stage = db.execute("SELECT stage FROM applications WHERE job_key = 'beta|engineer'").fetchone()["stage"]
    assert stage == "withdrawn"
    # and back again: a new would-apply
    tg.updates.append(tap_at(4, f"j:a:{ids['beta|engineer']}", 102))
    feedback.collect(db)
    assert apply_queue.queue(db, date.today())[1] == 2


def test_applied_tap_files_the_application_once(db, tg, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APPLIED", tmp_path / "applied.tsv")
    job = add_job(db)
    feedback.send_jobs(db, [job])
    msg = db.execute("SELECT id FROM job_messages").fetchone()["id"]
    tg.updates = [tap_at(1, f"j:a:{msg}", 101), tap_at(2, f"j:d:{msg}", 101), tap_at(3, f"j:d:{msg}", 101)]
    feedback.collect(db)
    apps = db.execute("SELECT stage FROM applications").fetchall()
    assert [a["stage"] for a in apps] == ["applied"]           # one application, moved forward
    # a later 'no' tap never undoes an application that went further than 'would apply'
    tg.updates.append(tap_at(4, f"j:q:{msg}", 101))
    feedback.collect(db)
    assert db.execute("SELECT stage FROM applications").fetchone()["stage"] == "applied"


def test_an_applied_job_in_a_group_shows_one_inert_button(db, tg):
    jobs = [add_job(db, key=f"c{n}|t", title=f"Analyst {n}", company=f"Co{n}") for n in (1, 2)]
    feedback.send_group(db, jobs)
    db.execute("INSERT INTO job_feedback (job_key, verdict, fit_then) VALUES ('c1|t', 'applied', 'high')")
    ids = json.loads(db.execute("SELECT msg_ids FROM job_group_messages").fetchone()["msg_ids"])[1:]
    rows = feedback.group_keyboard(db, ids)["inline_keyboard"]
    assert rows[0] == [{"text": "1 ✓ Applied", "callback_data": "jx"}]
    tg.updates = [tap_at(1, "jx", 101)]
    assert feedback.collect(db) == 0
    assert db.execute("SELECT verdict FROM job_feedback WHERE job_key = 'c1|t'").fetchone()["verdict"] == "applied"


def test_sync_taps_files_missed_taps_once_and_never_doubles(db):
    from email_organiser import pipeline
    for key, title, verdict in (("a|x", "Analyst X", "apply"), ("b|y", "Engineer Y", "applied"),
                                ("c|z", "Officer Z", "not_qualified"), ("d|w", "Analyst W", "apply")):
        add_job(db, key=key, title=title, company=key[0].upper() + " Co")
        db.execute("INSERT INTO job_feedback (job_key, verdict, fit_then) VALUES (?, ?, 'high')", (key, verdict))
    pipeline.upsert(db, "D Co", "Analyst W", "interview", source="email")   # already further along, no job_key
    db.commit()
    filed = feedback.sync_taps(db)
    assert {r["job_key"] for r in filed} == {"a|x", "b|y", "d|w"}
    stages = {r["company"]: r["stage"] for r in db.execute("SELECT company, stage FROM applications")}
    assert stages == {"A Co": "would_apply", "B Co": "applied", "D Co": "interview"}   # no 'no' tap filed, nothing moved back
    assert db.execute("SELECT COUNT(*) n FROM applications").fetchone()["n"] == 3
    assert feedback.sync_taps(db) == []                        # safe to re-run


def test_a_tap_that_meets_a_busy_database_is_kept_for_next_time(db, tg, monkeypatch, tmp_path):
    monkeypatch.setattr(feedback, "OFFSET", tmp_path / "offset")
    job = add_job(db)
    feedback.send_jobs(db, [job])
    msg = db.execute("SELECT id FROM job_messages").fetchone()["id"]
    tg.updates = [tap_at(7, f"j:a:{msg}", 101)]
    real = feedback.record
    def busy(*a, **k):
        raise store.sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(feedback, "record", busy)
    assert feedback.collect(db) == 0
    assert not (tmp_path / "offset").exists() or int((tmp_path / "offset").read_text()) <= 7   # not marked read
    monkeypatch.setattr(feedback, "record", real)
    assert feedback.collect(db) == 1                       # Telegram sends it again: stored this time
    assert db.execute("SELECT verdict FROM job_feedback").fetchone()["verdict"] == "apply"
    assert int((tmp_path / "offset").read_text()) == 8


def test_database_waits_for_a_busy_writer():
    assert store.BUSY_TIMEOUT >= 30
