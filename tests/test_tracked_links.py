"""QM Careers Hub job links go through a mail tracker (screener.resolve_tracked). Offline."""
import io
import urllib.error

import pytest

from email_organiser import screener

QM = "QM Careers Hub <qmul@email.targetconnect.com>"
TRACK = "https://mxm.mxmfb.com/rest/v1/abc"


def redirect_to(monkeypatch, location, code=302):
    opened = []
    class Opener:
        def open(self, url, timeout):
            opened.append(url)
            raise urllib.error.HTTPError(url, code, "Found", {"Location": location}, io.BytesIO())
    monkeypatch.setattr(screener.urllib.request, "build_opener", lambda *h: Opener())
    return opened


def test_job_link_behind_the_tracker_is_kept_without_utm(monkeypatch):
    redirect_to(monkeypatch, "http://qmul.targetconnect.net/leap/jobs.html?id=214267&utm_source=x&utm_medium=email")
    assert screener.resolve_tracked(TRACK, QM) == "https://qmul.targetconnect.net/leap/jobs.html?id=214267"


@pytest.mark.parametrize("location", [
    "https://evil.example/leap/jobs.html?id=1",                    # lands somewhere else
    "https://qmul.targetconnect.net/leap/jobs.html?id=",           # the "all jobs" page, not a job
    "https://qmul.targetconnect.net/student/profile.html",         # not a job
    "javascript:alert(1)",
])
def test_anything_but_a_job_on_the_hub_is_dropped(monkeypatch, location):
    redirect_to(monkeypatch, location)
    assert screener.resolve_tracked(TRACK, QM) == ""


def test_only_known_senders_and_their_tracker(monkeypatch):
    opened = redirect_to(monkeypatch, "https://qmul.targetconnect.net/leap/jobs.html?id=1")
    assert screener.resolve_tracked(TRACK, "Spoof <a@other.example>") == ""
    assert screener.resolve_tracked("https://other-tracker.example/x", QM) == ""
    assert opened == []          # no request made for either


def test_network_error_means_no_link(monkeypatch):
    class Opener:
        def open(self, url, timeout):
            raise OSError("offline")
    monkeypatch.setattr(screener.urllib.request, "build_opener", lambda *h: Opener())
    assert screener.resolve_tracked(TRACK, QM) == ""


def test_rubric_fields_are_stored(tmp_path, monkeypatch):
    from email_organiser import store
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    db = store.open_db()
    row = {"account": "yahoo", "uidvalidity": 1, "uid": 1}
    job = {"title": "Data Analyst", "company": "Acme", "location": "HK", "closing": "", "fit": "medium",
           "reason": "Needs Tableau", "url": "", "commission": False, "level": "junior",
           "gap": "\"Tableau required\"", "row": row}
    screener.save(db, [{"row": row}], [job])
    got = db.execute("SELECT level, gap FROM jobs").fetchone()
    assert tuple(got) == ("junior", "\"Tableau required\"")


def test_rubric_asks_for_level_and_gap_before_fit():
    props = list(screener.SCHEMA["properties"]["emails"]["items"]["properties"]["jobs"]["items"]["properties"])
    assert props.index("level") < props.index("gap") < props.index("fit")
    assert "never guess a gap" in screener.SYSTEM_PROMPT
