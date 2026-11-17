"""One run at a time: the 15-minute check and the 08:00/18:00 summary share this lock.

Without it, both could label the same new emails (two Claude calls) or write two drafts
for one email. The lock is a file in ~/.email-organiser; macOS releases it automatically
if a run crashes, so it can never get stuck "locked".
"""
import fcntl
import time
from contextlib import contextmanager

from . import paths


@contextmanager
def run_lock(wait_seconds: float = 0):
    """Yields True if we got the lock, False if another run still held it after waiting.
    wait_seconds=0: don't wait (the 15-minute check just skips a turn)."""
    paths.HOME.mkdir(parents=True, exist_ok=True)
    handle = open(paths.HOME / "run.lock", "w")
    deadline = time.monotonic() + wait_seconds
    got = False
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)   # NB = fail at once if taken
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(2)
        yield got
    finally:
        if got:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
