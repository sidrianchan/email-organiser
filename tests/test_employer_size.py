"""Employer size orders the high list, big employers first (Sid, 20 Nov). Never changes fit. Offline."""
import sqlite3

import pytest

from email_organiser import screener, store


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def row(uid):
    return {"account": "yahoo", "uidvalidity": 1, "uid": uid}


def job(title, size, fit="high", uid=1):
    return dict(title=title, company=title + " Ltd", location="HK", closing="", fit=fit,
                reason="r", url="", employer_size=size, row=row(uid))


def test_high_list_is_big_first_then_oldest_first(db):
    jobs = [job("Small One", "small"), job("Old Unknown", "unknown"), job("Big One", "large"),
            job("Not Judged", None), job("Big Two", "large"), job("Medium Big", "large", fit="medium")]
    for n, j in enumerate(jobs):   # stored one by one, so found_at gives their age order
        screener.save(db, [{"row": j["row"]}], [j])
        db.execute("UPDATE jobs SET found_at = datetime('now', ?) WHERE title = ?", (f"+{n} seconds", j["title"]))
    db.commit()
    assert [r["title"] for r in screener.unshown_high(db)] == [
        "Big One", "Big Two", "Old Unknown", "Not Judged", "Small One"]   # medium never listed


def test_size_is_stored(db):
    screener.save(db, [{"row": row(1)}], [job("Bank Analyst", "large")])
    assert db.execute("SELECT employer_size FROM jobs").fetchone()[0] == "large"


def test_old_database_gets_the_column(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE jobs (job_key TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, "
                "closing TEXT, fit TEXT, reason TEXT, url TEXT, account TEXT, uidvalidity INTEGER, "
                "uid INTEGER, found_at TEXT DEFAULT (datetime('now')), shown_at TEXT)")
    old.execute("INSERT INTO jobs (job_key, title, fit) VALUES ('k', 'Old Job', 'high')")
    old.commit()
    old.close()
    monkeypatch.setattr(store, "DB_PATH", path)
    db = store.open_db()
    assert [r["title"] for r in screener.unshown_high(db)] == ["Old Job"]   # NULL size still listed


def test_schema_asks_for_size_after_fit():
    props = screener.SCHEMA["properties"]["emails"]["items"]["properties"]["jobs"]["items"]
    assert props["properties"]["employer_size"]["enum"] == ["large", "small", "unknown"]
    assert "employer_size" in props["required"]
    order = list(props["properties"])
    assert order.index("employer_size") > order.index("fit")   # judged after fit: can't feed it
    assert "never" in screener.SYSTEM_PROMPT.split("employer_size")[1][:200]   # told not to change fit
