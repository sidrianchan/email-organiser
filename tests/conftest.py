"""Shared test setup."""
import pytest

from email_organiser import agenda


@pytest.fixture(autouse=True)
def no_real_calendar(monkeypatch):
    """Tests never touch Calendar.app (slow, and personal). Default: an empty calendar.
    A test that wants events or a failure patches agenda.read_raw itself."""
    monkeypatch.setattr(agenda, "read_raw", lambda: "")
