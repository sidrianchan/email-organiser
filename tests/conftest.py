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
