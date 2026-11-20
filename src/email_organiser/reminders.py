"""Your Apple Reminders in the 08:00 summary and the morning Telegram brief: what's due today
or already overdue (added 21 Nov: "remind me what things I need to do on that day").

Reads Reminders.app with AppleScript (osascript). Read-only: nothing here creates, ticks off,
changes or deletes a reminder (tests/test_today.py checks the script for write commands).

Only reminders with a due date of today or earlier are shown. Undated ones are left out on
purpose: the brief answers "what do I need to do today", and a long undated list would bury it.
No Claude call; a read takes about 2 seconds.
"""
import subprocess
from datetime import date, datetime, time

TIMEOUT = 60                    # seconds; a stuck Reminders must not hold up the summary
FIELD, RECORD = "\x1f", "\x1e"  # separators no reminder title will contain
MAX_SHOWN = 8                   # more than this and the brief stops being a brief

SCRIPT = """
on run argv
    set fs to (ASCII character 31)
    set rs to (ASCII character 30)
    set out to ""
    tell application "Reminders"
        repeat with l in lists
            set ln to name of l
            set {ns, ds} to {name, due date} of (reminders of l whose completed is false)
            repeat with i from 1 to count of ns
                set d to item i of ds
                if d is missing value then
                    set dtxt to ""
                else
                    set dtxt to (d as «class isot» as string)
                end if
                set t to item i of ns
                if t is missing value then set t to ""
                set out to out & ln & fs & t & fs & dtxt & rs
            end repeat
        end repeat
    end tell
    return out
end run
"""


def read_raw() -> str:
    """Every open reminder as one string. Raises if osascript fails or times out."""
    result = subprocess.run(["osascript", "-"], input=SCRIPT, capture_output=True,
                            text=True, timeout=TIMEOUT)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"osascript exit {result.returncode}")
    return result.stdout


def parse(raw: str) -> list[dict]:
    """[{list, title, due}] where due is a datetime or None (no due date)."""
    items = []
    for rec in raw.strip("\n").split(RECORD):
        parts = rec.strip("\n").split(FIELD)
        if len(parts) != 3 or not parts[1].strip():
            continue   # empty tail or a malformed record: skip it, don't fail the summary
        lst, title, due = parts
        items.append(dict(list=lst.strip(), title=title.strip(),
                          due=datetime.fromisoformat(due.strip()) if due.strip() else None))
    return items


def due_by(items: list[dict], day: date) -> list[dict]:
    """Reminders due on `day` or earlier, overdue first (oldest first), then today's by time.
    Each gets overdue (due before today) and timed (a time was set, not just a date)."""
    end = datetime.combine(day, time.max)
    out = [it | {"overdue": it["due"].date() < day, "timed": it["due"].time() != time(0, 0)}
           for it in items if it["due"] is not None and it["due"] <= end]
    return sorted(out, key=lambda it: (not it["overdue"], it["due"]))[:MAX_SHOWN]


def get(day: date) -> list | None:
    """Due today or overdue. None if Reminders couldn't be read. Never raises."""
    try:
        todo = due_by(parse(read_raw()), day)
    except Exception as e:
        print(f"Reminders FAILED (summary unaffected): {e}")
        return None
    print(f"Reminders: {len(todo)} due today or overdue")
    return todo


def when(item: dict, day: date, compact: bool = False) -> str:
    """'overdue since Wed 18 Nov', 'by 5 PM', 'today'. compact (Telegram): '3 days overdue'."""
    if item["overdue"] and compact:
        n = (day - item["due"].date()).days
        return f"{n} day{'s' if n > 1 else ''} overdue"
    if item["overdue"]:
        return f"overdue since {item['due']:%a %-d %b}"
    if item["timed"]:
        t = item["due"]
        return f"by {t:%-I} {t:%p}" if t.minute == 0 else f"by {t:%-I:%M} {t:%p}"
    return "today"
