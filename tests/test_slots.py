"""Catch-up (slots.py): a missed 08:00 / 18:00 summary is run by the 15-minute check, once,
without duplicating it, without giving up on a mere lack of network, and with one ping
(counts and error type only) if it really keeps failing.

The real case behind this (2026-11-19): the Mac on battery woke for ~20 s at 08:06, launchd
started the missed 08:00 summary, the Mac slept again, and the run died on a DNS error.
launchd never retries, so no summary and no Telegram ping that morning."""
import socket
from datetime import datetime, timedelta

import pytest

from email_organiser import digest, runlock, slots, store, watch

DAY = datetime(2026, 11, 19)


def at(hour, minute=0, day=DAY):
    return day.replace(hour=hour, minute=minute)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runlock.paths, "HOME", tmp_path)
    return store.open_db()


@pytest.fixture
def runs(monkeypatch):
    """Fake summary: no mail server, no Claude. Set `runs.error` to make it fail."""
    calls = []
    def fake_summarise(live, sample, slot=None):
        calls.append(slot)
        if fake_summarise.error:
            raise fake_summarise.error
    fake_summarise.error = None
    fake_summarise.calls = calls
    monkeypatch.setattr(digest, "summarise", fake_summarise)
    return fake_summarise


@pytest.fixture
def pings(monkeypatch):
    sent = []
    monkeypatch.setattr(watch.notify, "send", lambda text, loud: sent.append((text, loud)))
    return sent


def test_slot_is_latest_0800_or_1800():
    assert slots.current_slot(at(7, 59)) == at(18, day=DAY - timedelta(days=1))
    assert slots.current_slot(at(8, 0)) == at(8)
    assert slots.current_slot(at(17, 59)) == at(8)
    assert slots.current_slot(at(18, 0)) == at(18)
    assert slots.current_slot(at(23, 30)) == at(18)


def test_fresh_install_has_nothing_to_catch_up(db):
    assert slots.done(db, at(8))
    assert slots.catch_up_due(db, at(9)) is None


def test_done_needs_a_success_that_started_after_the_slot(db):
    slots.record(db, at(18, day=DAY - timedelta(days=1)), at(18, day=DAY - timedelta(days=1)), "digest")
    assert not slots.done(db, at(8))                       # yesterday's evening run doesn't count for 08:00
    slots.record(db, at(8), at(8, 6), "digest", error=socket.gaierror(8, "nodename nor servname"))
    assert not slots.done(db, at(8))                       # a failure isn't delivery
    slots.record(db, at(8), at(9, 30), "catch-up")
    assert slots.done(db, at(8))


def test_grace_lets_the_scheduled_run_go_first(db):
    slots.record(db, at(18, day=DAY - timedelta(days=1)), at(18, day=DAY - timedelta(days=1)), "digest")
    assert slots.catch_up_due(db, at(8, 9)) is None
    assert slots.catch_up_due(db, at(8, 10)) == at(8)


def test_the_19_november_morning(db, runs, pings):
    """Scheduled run dies on DNS at 08:06 -> the 09:30 check delivers it -> later checks and
    a late-starting scheduled run don't make a second summary."""
    slots.record(db, at(18, day=DAY - timedelta(days=1)), at(18, day=DAY - timedelta(days=1)), "digest")
    runs.error = socket.gaierror(8, "nodename nor servname provided, or not known")
    with pytest.raises(socket.gaierror):
        digest.summarise_recorded("digest", now=at(8, 6))
    assert slots.failures(db, at(8)) == 0                  # network errors don't count towards giving up

    runs.error = None
    assert watch.catch_up(db, at(9, 30)) is True
    assert runs.calls[-1] == at(8)                          # run as the 08:00 summary (calendar included)
    assert watch.catch_up(db, at(9, 45)) is False           # once only
    assert digest.summarise_recorded("digest", now=at(9, 50)) is False   # no duplicate
    assert len(runs.calls) == 2 and pings == []
    assert digest.summarise_recorded("digest", now=at(9, 50), force=True) is True   # --force still works


def test_no_network_keeps_retrying_and_never_pings(db, runs, pings):
    slots.record(db, at(8, day=DAY - timedelta(days=1)), at(8, day=DAY - timedelta(days=1)), "digest")
    runs.error = TimeoutError("_ssl.c:994: The handshake operation timed out")
    for minute in (15, 30, 45):
        assert watch.catch_up(db, at(8, minute)) is False   # never raises
    for hour in (9, 10):
        assert watch.catch_up(db, at(hour)) is False
    assert len(runs.calls) == 5 and pings == []


def test_real_failure_gives_up_after_three_with_one_ping(db, runs, pings):
    slots.record(db, at(8, day=DAY - timedelta(days=1)), at(8, day=DAY - timedelta(days=1)), "digest")
    runs.error = RuntimeError("claude -p failed: Secret Bank interview")
    for minute in (15, 30, 45, 50, 55):
        assert watch.catch_up(db, at(8, minute)) is False
    assert len(runs.calls) == 3                              # stopped after 3
    assert len(pings) == 1
    text, loud = pings[0]
    assert text.startswith("The 08:00 summary failed 3 times (RuntimeError).") and loud
    assert "Secret" not in text                              # error type only, never the message
    runs.error = None
    assert watch.catch_up(db, at(18, 15)) is True            # the next slot is tried afresh


def test_failure_record_is_short_and_typed(db, runs):
    slots.record(db, at(8), at(8), "digest")                 # not a fresh install
    runs.error = RuntimeError("x" * 500)
    with pytest.raises(RuntimeError):
        digest.summarise_recorded("digest", now=at(18, 1))
    row = db.execute("SELECT * FROM summary_runs WHERE ok = 0").fetchone()
    assert row["slot"] == at(18).isoformat() and len(row["error"]) <= 200 and row["network"] == 0


def test_dry_run_check_never_catches_up(db, runs, monkeypatch, tmp_path):
    slots.record(db, at(8, day=DAY - timedelta(days=1)), at(8, day=DAY - timedelta(days=1)), "digest")
    monkeypatch.setattr(watch, "check", lambda db, live: 0)
    monkeypatch.setattr("sys.argv", ["watch"])               # no --live
    watch.main()
    assert runs.calls == []
