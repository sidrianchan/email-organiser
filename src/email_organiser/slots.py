"""Which 08:00 / 18:00 summary is due, and has it been delivered?

Why this exists (2026-11-19): on battery the Mac sleeps, and launchd fires a missed 08:00
run during a short "dark wake" with no network. The run crashes and launchd never retries,
so the summary silently never comes. Now every summary run is recorded here, and the
15-minute check (watch.py) runs a summary that is due but missing ("catch-up").

A "slot" is the scheduled time a summary belongs to: the latest 08:00 or 18:00 at or before
now. A slot is done once a summary that STARTED at or after it succeeded.
"""
import socket
import ssl
from datetime import datetime, timedelta

SLOT_HOURS = (8, 18)
GRACE = timedelta(minutes=10)   # the scheduled run gets the first 10 minutes to itself
MAX_FAILS = 3                   # real errors per slot before catch-up gives up (network errors don't count)
NETWORK_ERRORS = (socket.gaierror, TimeoutError, ConnectionError, ssl.SSLError)


def current_slot(now: datetime) -> datetime:
    """The latest 08:00 or 18:00 at or before `now` (local time)."""
    today = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in SLOT_HOURS]
    passed = [s for s in today if s <= now]
    if passed:
        return passed[-1]
    return today[-1] - timedelta(days=1)   # before 08:00: yesterday's 18:00


def is_network(error: BaseException) -> bool:
    """No network (asleep, Wi-Fi down) rather than something actually broken."""
    return isinstance(error, NETWORK_ERRORS)


def record(db, slot: datetime, started: datetime, via: str, error: BaseException | None = None) -> None:
    """Only the error's type and a short message: never email content."""
    db.execute("INSERT INTO summary_runs (slot, started, ok, error, network, via) VALUES (?, ?, ?, ?, ?, ?)",
               (slot.isoformat(), started.isoformat(), error is None,
                None if error is None else f"{type(error).__name__}: {error}"[:200],
                error is not None and is_network(error), via))
    db.commit()


def done(db, slot: datetime) -> bool:
    """Has a summary for this slot been delivered? With no runs recorded at all (a fresh
    install, or the first day of this feature) there is nothing to catch up."""
    if db.execute("SELECT 1 FROM summary_runs LIMIT 1").fetchone() is None:
        return True
    return db.execute("SELECT 1 FROM summary_runs WHERE ok = 1 AND started >= ?",
                      (slot.isoformat(),)).fetchone() is not None


def failures(db, slot: datetime) -> int:
    """Real (non-network) failures for this slot."""
    return db.execute("SELECT COUNT(*) FROM summary_runs WHERE slot = ? AND ok = 0 AND network = 0",
                      (slot.isoformat(),)).fetchone()[0]


def catch_up_due(db, now: datetime) -> datetime | None:
    """The slot to catch up now, or None."""
    slot = current_slot(now)
    if now - slot < GRACE or done(db, slot) or failures(db, slot) >= MAX_FAILS:
        return None
    return slot
