"""Shared test setup."""
import pytest

from email_organiser import agenda


@pytest.fixture(autouse=True)
def no_real_calendar(monkeypatch):
    """Tests never touch Calendar.app (slow, and personal). Default: an empty calendar.
    A test that wants events or a failure patches agenda.read_raw itself."""
    monkeypatch.setattr(agenda, "read_raw", lambda: "")


@pytest.fixture(autouse=True)
def no_real_unsubscribe_folders(monkeypatch):
    """Tests never log in to a real mailbox through the Unsubscribe-folder step (it runs at
    the start of every quick check). A test that wants accounts patches them itself."""
    from email_organiser import unsub_folder
    monkeypatch.setattr(unsub_folder, "load_accounts", lambda: [])


@pytest.fixture(autouse=True)
def no_real_claude_health_check(monkeypatch, tmp_path):
    """The summary's Claude health check is a real `claude -p` call: tests never make it.
    Claude-down ping state goes to a temp file, never ~/.email-organiser."""
    from email_organiser import classify, health
    monkeypatch.setattr(classify, "health_check", lambda: None)
    monkeypatch.setattr(health, "STATE", tmp_path / "claude_down_pinged")


@pytest.fixture(autouse=True)
def no_real_telegram_job_buttons(monkeypatch, tmp_path):
    """Job buttons (feedback.py) never reach the real Telegram or the real applied.tsv in tests."""
    from email_organiser import feedback, paths
    def no_chat():
        raise RuntimeError("Telegram not set up (test)")
    monkeypatch.setattr(feedback, "chat", no_chat)
    monkeypatch.setattr(feedback, "OFFSET", tmp_path / "telegram_offset")
    monkeypatch.setattr(feedback, "SEND_GAP", 0)
    monkeypatch.setattr(paths, "APPLIED", tmp_path / "applied.tsv")


@pytest.fixture(autouse=True)
def no_real_reminders_or_weather(monkeypatch):
    """Tests never read Reminders.app or call the Hong Kong Observatory. Default: no reminders,
    and "not in Hong Kong" so no weather is fetched. A test that wants either patches it itself."""
    from email_organiser import reminders, weather
    monkeypatch.setattr(reminders, "read_raw", lambda: "")
    monkeypatch.setattr(weather, "in_hong_kong", lambda: False)
    def no_network(kind):
        raise AssertionError("tests must not call the Hong Kong Observatory")
    monkeypatch.setattr(weather, "fetch", no_network)


@pytest.fixture(autouse=True)
def no_real_title_shortening(monkeypatch):
    """Long calendar/reminder titles would be shortened by a real Claude call (shorten.py).
    Tests never make it; a test that wants shortening patches shorten.run_claude itself."""
    from email_organiser import shorten
    monkeypatch.setattr(shorten, "run_claude",
                        lambda *a, **k: pytest.fail("tests must not call Claude to shorten titles"))
