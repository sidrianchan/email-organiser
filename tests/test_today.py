"""The morning brief (21 Nov), dark mode, no drafts to no-reply senders, daily backups.
Offline: fake Reminders output, fake Observatory JSON, temp databases."""
import json
import sqlite3
import stat
import subprocess
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from email_organiser import agenda, digest, drafts, notify, reminders, shorten, store, weather
from email_organiser.reminders import FIELD, RECORD

SAT = date(2026, 11, 21)
MORNING = datetime(2026, 11, 21, 8, 0)


def rem(*items) -> str:
    """Fake osascript output: (list, title, due ISO or '') per reminder."""
    return "".join(FIELD.join(it) + RECORD for it in items) + "\n"


OVERDUE = ("Reminders", "Renew passport", "2026-11-18T20:00:00")
CALL = ("plan", "Call the bank", "2026-11-21T17:00:00")
TODAY_ALLDAY = ("plan", "Post the form", "2026-11-21T00:00:00")
LATER = ("plan", "Renew gym", "2026-11-25T09:00:00")
UNDATED = ("plan", "Read a book", "")


# --- Apple Reminders ------------------------------------------------------------------

def test_due_today_and_overdue_only_overdue_first():
    todo = reminders.due_by(reminders.parse(rem(CALL, LATER, UNDATED, TODAY_ALLDAY, OVERDUE)), SAT)
    assert [t["title"] for t in todo] == ["Renew passport", "Post the form", "Call the bank"]
    assert [reminders.when(t, SAT) for t in todo] == ["overdue since Wed 18 Nov", "today", "by 5 PM"]
    assert reminders.when(todo[0], SAT, compact=True) == "3 days overdue"
    yesterday = reminders.due_by(reminders.parse(rem(("plan", "x", "2026-11-20T09:00:00"))), SAT)[0]
    assert reminders.when(yesterday, SAT, compact=True) == "1 day overdue"


def test_half_hour_and_cap():
    half = ("plan", "Pick up parcel", "2026-11-21T17:30:00")
    assert reminders.when(reminders.due_by(reminders.parse(rem(half)), SAT)[0], SAT) == "by 5:30 PM"
    many = [("plan", f"t{i}", "2026-11-20T09:00:00") for i in range(20)]
    assert len(reminders.due_by(reminders.parse(rem(*many)), SAT)) == reminders.MAX_SHOWN


def test_malformed_records_are_skipped():
    raw = rem(OVERDUE) + "junk without separators" + RECORD + FIELD.join(("plan", "", "2026-11-21T09:00:00")) + RECORD
    assert [t["title"] for t in reminders.parse(raw)] == ["Renew passport"]


def test_reminders_failure_never_raises(monkeypatch, capsys):
    def boom():
        raise subprocess.TimeoutExpired("osascript", 60)
    monkeypatch.setattr(reminders, "read_raw", boom)
    assert reminders.get(SAT) is None
    assert "Reminders FAILED" in capsys.readouterr().out


def test_reminders_script_only_reads():
    s = reminders.SCRIPT.lower()
    for word in ("make new", "delete", "save", "set completed", "set name", "set due date", "move"):
        assert word not in s


# --- Hong Kong weather ------------------------------------------------------------------

FND = {"weatherForecast": [
    {"forecastDate": "20261121", "forecastWeather": "Mainly cloudy. Dry with sunny periods.",
     "forecastMaxtemp": {"value": 21, "unit": "C"}, "forecastMintemp": {"value": 13, "unit": "C"}, "PSR": "Low"},
    {"forecastDate": "20261122", "forecastWeather": "Sunny.",
     "forecastMaxtemp": {"value": 23, "unit": "C"}, "forecastMintemp": {"value": 15, "unit": "C"}, "PSR": "Low"}]}


def test_only_todays_forecast_is_used():
    f = weather.forecast_for(FND, SAT)
    assert (f["low"], f["high"]) == (13, 21)
    assert weather.forecast_for(FND, date(2026, 11, 30)) is None   # never another day's weather
    assert weather.line({"forecast": f, "warnings": []}) == \
        "13–21°C, rain chance low. Mainly cloudy. Dry with sunny periods."
    assert weather.line(None) == "" and weather.line({"forecast": None, "warnings": []}) == ""


def test_warning_names():
    warnsum = {"WRAIN": {"name": "Rainstorm Warning Signal", "code": "WRAIN", "type": "WRAINB", "actionCode": "ISSUE"},
               "WTCSGNL": {"name": "Tropical Cyclone Warning Signal", "code": "WTCSGNL", "type": "TC8NE", "actionCode": "ISSUE"},
               "WCOLD": {"name": "Cold Weather Warning", "code": "WCOLD", "actionCode": "REISSUE"},
               "WHOT": {"name": "Very Hot Weather Warning", "code": "WHOT", "actionCode": "ISSUE"},
               "WTS": {"name": "Thunderstorm Warning", "code": "WTS", "actionCode": "CANCEL"}}   # just ended
    assert weather.warnings(warnsum) == ["Black Rainstorm Warning Signal", "No. 8 Gale or Storm Signal"]
    # cold weather (in force for days each winter) isn't plan-changing; the temperature says it
    assert weather.warnings({"WTCSGNL": {"name": "x", "type": "TC10", "actionCode": "ISSUE"}}) == ["No. 10 Hurricane Signal"]
    assert weather.warnings({}) == []


def test_weather_only_in_hong_kong_and_never_raises(monkeypatch, capsys):
    assert weather.get(SAT) is None            # conftest: not in Hong Kong, so nothing fetched
    monkeypatch.setattr(weather, "in_hong_kong", lambda: True)
    def down(kind):
        raise OSError("no network")
    monkeypatch.setattr(weather, "fetch", down)
    assert weather.get(SAT) is None and "Weather forecast FAILED" in capsys.readouterr().out
    monkeypatch.setattr(weather, "fetch", lambda kind: FND if kind == "fnd" else
                        {"WTS": {"name": "Thunderstorm Warning", "actionCode": "ISSUE"},
                         "WCOLD": {"name": "Cold Weather Warning", "actionCode": "ISSUE"}})
    w = weather.get(SAT)
    assert w["forecast"]["high"] == 21 and w["warnings"] == ["Thunderstorm Warning"]


# --- in the calendar read and the summary ------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def morning(monkeypatch, db, reminders_raw=rem(OVERDUE, CALL), in_hk=True):
    monkeypatch.setattr(reminders, "read_raw", lambda: reminders_raw)
    monkeypatch.setattr(weather, "in_hong_kong", lambda: in_hk)
    monkeypatch.setattr(weather, "fetch", lambda kind: FND if kind == "fnd" else
                        {"WRAIN": {"name": "Rainstorm Warning Signal", "type": "WRAINA", "actionCode": "ISSUE"}})
    return agenda.get(db, MORNING)


def test_calendar_read_brings_reminders_and_weather(db, monkeypatch):
    cal = morning(monkeypatch, db)
    assert [t["title"] for t in cal["todo"]] == ["Renew passport", "Call the bank"]
    assert cal["weather"]["warnings"] == ["Amber Rainstorm Warning Signal"]


def test_calendar_failure_keeps_reminders_and_weather(db, monkeypatch):
    def boom():
        raise RuntimeError("Calendar is stuck")
    monkeypatch.setattr(agenda, "read_raw", boom)
    cal = morning(monkeypatch, db)
    assert cal["events"] is None and cal["todo"] and cal["weather"]
    t = digest.build_text([], cal=cal)
    assert "* To do: Renew passport (overdue since Wed 18 Nov)" in t
    assert "Calendar couldn't be read" in t


def test_todos_and_weather_in_the_summary(db, monkeypatch):
    cal = morning(monkeypatch, db)
    t = digest.build_text([], cal=cal)
    assert "TODAY, SAT 21 NOV: nothing on the calendar" in t
    assert "* To do: Renew passport (overdue since Wed 18 Nov)" in t
    assert "* To do: Call the bank (by 5 PM)" in t
    assert "* Weather: 13–21°C, rain chance low." in t and "* ⚠️ Amber Rainstorm Warning Signal" in t
    assert t.index("TODAY") < t.index("To do") < t.index("Weather")
    h = digest.build_html([], cal=cal)
    assert "Overdue since Wed 18 Nov" in h and "Amber Rainstorm Warning Signal" in h
    assert digest.subject_line(digest.sections([]), 0, cal=cal).endswith(": 2 to do, nothing needs you (0 routine)")


def test_reminder_titles_are_escaped(db, monkeypatch):
    cal = morning(monkeypatch, db, rem(("plan", "<img src=x onerror=alert(1)>", "2026-11-21T09:00:00")))
    h = digest.build_html([], cal=cal)
    assert "<img src=x" not in h and "&lt;img src=x" in h


def test_unreadable_reminders_are_said(db, monkeypatch):
    def boom():
        raise RuntimeError("not allowed")
    monkeypatch.setattr(reminders, "read_raw", boom)
    cal = agenda.get(db, MORNING)
    assert cal["todo"] is None
    assert "Reminders couldn't be read" in digest.build_text([], cal=cal)
    assert "<b>✅ To do</b>\nCouldn't read Reminders" in digest.today_brief(cal)["sections"]


def test_what_makes_a_morning_summary(db, monkeypatch):
    cal = morning(monkeypatch, db, rem(UNDATED, LATER), in_hk=False)
    assert not digest.has_cal(cal)                # nothing due, no events, no warnings
    assert digest.has_cal(morning(monkeypatch, db, rem(OVERDUE), in_hk=False))   # a to-do alone
    assert digest.has_cal(morning(monkeypatch, db, rem(), in_hk=True))       # a warning signal alone


# --- the Telegram brief ------------------------------------------------------------------

def test_brief_has_sections_with_space_between(db, monkeypatch):
    """Sid, 21 Nov: the first version was 'too stacked'. One section per kind of thing, a bold
    heading each, a blank line between, one short line per item."""
    cal = morning(monkeypatch, db)
    brief = digest.today_brief(cal)
    assert brief["sections"] == [
        "<b>Saturday 21 Nov</b>",
        "<b>📅 Calendar</b>\nNothing on the calendar",
        "<b>✅ To do</b>\nRenew passport · 3 days overdue\nCall the bank · by 5 PM",
        "<b>🌤 Weather</b>\n13–21°C · Mainly cloudy · rain chance low\n⚠️ Amber Rainstorm Warning Signal",
    ]
    assert brief["loud"]
    text, loud = notify.message({"job": 0, "reply": 0, "read": 0}, 0, "yahoo", brief)
    assert text.startswith("<b>Saturday 21 Nov</b>\n\n<b>📅 Calendar</b>")
    assert text.endswith("\n\n<b>📬 Mail</b>\nNo new email.") and loud
    assert notify.is_html(brief) and not notify.is_html(None)


def test_upcoming_event_reads_on_one_line(db, monkeypatch):
    exam = ("plan", "Written exam", "2026-11-23T09:30:00", "2026-11-23T12:00:00", "", "false", "", "")
    monkeypatch.setattr(agenda, "read_raw", lambda: agenda.FIELD.join(exam) + agenda.RECORD)
    monkeypatch.setattr(agenda, "run_claude", lambda *a, **k: (
        {"events": [{"id": 0, "important": True, "prep": "ID card, pens"}]}, "test"))
    cal = morning(monkeypatch, db, rem(), in_hk=False)
    section = next(x for x in digest.today_brief(cal)["sections"] if "Coming up" in x)
    assert section == "<b>⏳ Coming up</b>\nMon 23 Nov (in 2 days) · 9:30 AM · Written exam\n↳ ID card, pens"


def test_quiet_brief_when_nothing_is_on(db, monkeypatch):
    cal = morning(monkeypatch, db, rem(), in_hk=False)
    brief = digest.today_brief(cal)
    assert brief["sections"] == ["<b>Saturday 21 Nov</b>", "<b>📅 Calendar</b>\nNothing on the calendar"]
    assert not brief["loud"]
    assert not notify.message({"job": 0, "reply": 0, "read": 0}, 0, "yahoo", brief)[1]


def test_brief_text_is_escaped_for_telegram(db, monkeypatch):
    cal = morning(monkeypatch, db, rem(("plan", "Pay <b>rent</b> & bills", "2026-11-21T09:00:00")), in_hk=False)
    text, _ = notify.message({"job": 0, "reply": 0, "read": 0}, 0, "yahoo", digest.today_brief(cal))
    assert "Pay &lt;b&gt;rent&lt;/b&gt; &amp; bills" in text
    assert notify.plain(text).count("Pay <b>rent</b> & bills") == 1   # the plain fallback reads normally


def test_no_brief_in_the_evening():
    assert digest.today_brief(None) is None
    assert notify.message({"job": 1, "reply": 0, "read": 0}, 1, "yahoo", None)[0].startswith(
        datetime.now().strftime("%H:%M"))


def test_brief_never_carries_email_text(db, monkeypatch):
    cal = morning(monkeypatch, db)
    rows = [dict(account="yahoo", uidvalidity=1, uid=1, sender="Bob <bob@x.example>", subject="Secret offer",
                 date="Sat, 21 Nov 2026 07:00:00 +0800", first_seen="2026-11-20 23:00:00",
                 label="job", needs_reply=1, reason="r")]
    text, _ = notify.message(digest.ping_counts(rows), 1, "yahoo", digest.today_brief(cal))
    for private in ("Secret", "offer", "Bob", "bob@"):
        assert private not in text


def test_long_titles_are_cut_when_shortening_fails(db, monkeypatch):
    def down(*a, **k):
        raise RuntimeError("claude is down")
    monkeypatch.setattr(shorten, "run_claude", down)
    cal = morning(monkeypatch, db, rem(("plan", "x" * 300, "2026-11-21T09:00:00")), in_hk=False)
    section = next(x for x in digest.today_brief(cal)["sections"] if "To do" in x)
    line = section.split("\n")[1]
    assert len(line) < 100 and line.count("…") == 1


def test_morning_run_with_only_a_reminder_still_summarises(db, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(reminders, "read_raw", lambda: rem(OVERDUE))
    monkeypatch.setattr(agenda, "datetime", type("D", (datetime,), {"now": staticmethod(lambda: MORNING)}))
    for name in ("fetch_new", "classify_pending"):
        monkeypatch.setattr(digest, name, lambda *a, **k: 0)
    for mod, fn in ((digest.screener, "run"), (digest.drafts, "make_drafts"), (digest.cleanup, "run")):
        monkeypatch.setattr(mod, fn, lambda *a, **k: 0)
    monkeypatch.setattr(digest.store, "open_db", lambda: db)
    monkeypatch.setattr(digest.paths, "PREVIEW", tmp_path / "preview.html")
    monkeypatch.setattr("sys.argv", ["digest"])
    digest.main()
    out = capsys.readouterr().out
    assert "1 to do" in out and "No new emails" not in out
    assert "it would say: Saturday 21 Nov" in out and "Renew passport · 3 days overdue" in out


# --- dark mode ---------------------------------------------------------------------------

def test_summary_is_a_document_with_dark_mode():
    h = digest.build_html([dict(account="yahoo", uidvalidity=1, uid=1, sender="A <a@x.example>",
                                subject="<b>hi</b>", date="Sat, 21 Nov 2026 07:00:00 +0800",
                                first_seen="2026-11-20 23:00:00", label="read", needs_reply=0, reason="r")])
    assert h.startswith("<!doctype html>") and h.rstrip().endswith("</html>")
    assert '<meta name="color-scheme" content="light dark">' in h
    assert "@media (prefers-color-scheme: dark)" in h and 'class="eo-body"' in h
    assert ".eo-text{color:#f3f4f6!important}" in h and "<b>hi" not in h   # still escaped
    for colour in ("#2563eb", "#475569", "#059669"):    # every accent used has a dark version
        assert colour in digest.ACCENT_DARK


# --- no drafts to no-reply senders -------------------------------------------------------

@pytest.mark.parametrize("sender, expected", [
    ("WorkdaySystemDoNotReply <acme@myworkday.com>", True),   # the 20 Nov case (made-up company)
    ("noreply@mail.jobs.example", True),
    ("Jobsdb Applications <noreply@e.jobsdb.com>", True),
    ("HireVue <no-reply@hirevue.com>", True),
    ("Recruiting <do_not_reply@bank.example>", True),
    ("Grace Lam <grace.lam@example.com>", False),
    ("Bank HR <recruit@bank.example>", False),
    ("", False),
])
def test_no_reply_senders(sender, expected):
    assert drafts.is_no_reply(sender) is expected


def save_reply_needed(db, uid, sender):
    when = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))
    store.save_if_new(db, "yahoo", 1, uid, dict(message_id=f"<{uid}@x>", sender=sender,
                                                  subject=f"s{uid}", date=when, snippet="b"))
    db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply) "
               "VALUES ('yahoo', 1, ?, 'job', 1)", (uid,))


def test_no_reply_mail_gets_no_draft(db):
    save_reply_needed(db, 1, "WorkdaySystemDoNotReply <acme@myworkday.com>")
    save_reply_needed(db, 2, "Grace Lam <grace.lam@example.com>")
    assert [r["uid"] for r in drafts.pending(db)] == [2]


def test_reply_to_a_no_reply_address_is_caught():
    row = {"sender": "Grace Lam <grace.lam@example.com>"}
    assert drafts.is_no_reply(drafts.reply_address(row, {"reply_to": "Careers <noreply@bank.example>"}))
    assert not drafts.is_no_reply(drafts.reply_address(row, {"reply_to": ""}))


# --- daily database backup ---------------------------------------------------------------

def test_backup_once_a_day_keeps_seven(db, tmp_path):
    folder = tmp_path / "backups"
    db.execute("INSERT INTO digested (account, uidvalidity, uid) VALUES ('yahoo', 1, 42)")
    db.commit()
    first = store.backup(db, folder, date(2026, 11, 1))
    copy = sqlite3.connect(first)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT uid FROM digested").fetchone()[0] == 42
    copy.close()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600          # readable by you only
    stamp = first.stat().st_mtime_ns
    assert store.backup(db, folder, date(2026, 11, 1)) == first and first.stat().st_mtime_ns == stamp
    for day in range(2, 10):
        store.backup(db, folder, date(2026, 11, day))
    kept = sorted(p.name for p in folder.glob("mail-*.db"))
    assert kept == [f"mail-2026-11-{d:02d}.db" for d in range(3, 10)]   # newest 7


def test_backup_failure_never_raises(db, tmp_path, capsys):
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    assert store.backup(db, blocker, date(2026, 11, 1)) is None
    assert "backup FAILED" in capsys.readouterr().out


def test_new_files_are_private(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("private")
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_save_skips_a_human_email_whose_reply_to_is_no_reply(db, monkeypatch):
    """The Reply-To is only known once the header is re-read from the server, at save time."""
    save_reply_needed(db, 1, "Grace Lam <grace.lam@bank.example>")    # Reply-To: noreply@
    save_reply_needed(db, 2, "Ken Wong <ken.wong@example.com>")        # no Reply-To
    headers = {1: b"From: grace.lam@bank.example\r\nReply-To: Careers <noreply@bank.example>\r\n\r\n",
               2: b"From: ken.wong@example.com\r\n\r\n"}
    appended = []

    class FakeClient:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def find_special_folder(self, kind):
            return "Draft"
        def select_folder(self, name, readonly=False):
            assert readonly                       # the inbox is only ever read here
            return {b"UIDVALIDITY": 1}
        def fetch(self, uids, parts):
            return {u: {b"BODY[HEADER]": headers[u]} for u in uids if u in headers}
        def append(self, folder, msg, flags=(), msg_time=None):
            appended.append(msg)

    monkeypatch.setattr(drafts, "load_accounts", lambda: [{"name": "yahoo", "username": "me@yahoo.example"}])
    monkeypatch.setattr(drafts, "connect", lambda account: FakeClient())
    rows = drafts.pending(db)
    assert [r["uid"] for r in rows] == [1, 2]     # both senders look human
    saved = drafts.save_drafts(db, rows, {("yahoo", 1, 1): "Hi", ("yahoo", 1, 2): "Hi"})
    assert saved == 1 and len(appended) == 1 and b"ken.wong@example.com" in appended[0]
    assert [r[0] for r in db.execute("SELECT uid FROM drafts")] == [2]


# --- short labels for long titles (shorten.py) -------------------------------------------

FLIGHT = "航班 | 香港國際機場 T1－東京成田機場 T2 | 日本航空 JL026 | 09:10出發"   # made up
LONG_TODO = "Renew passport (needed for the Tokyo trip, Fri 4 Dec 09:10)"


def fake_shortener(calls, answers):
    def run(prompt, system, schema, model=None):
        items = json.loads(prompt.split("\n", 1)[1])
        calls.append([(it["kind"], it["text"]) for it in items])
        return {"items": [dict(id=it["id"], short=answers.get(it["text"], it["text"])) for it in items]}, "test-model"
    return run


def test_long_titles_get_short_labels_once(db, monkeypatch):
    calls = []
    monkeypatch.setattr(shorten, "run_claude", fake_shortener(calls, {
        FLIGHT: "Flight JL026 to Tokyo", LONG_TODO: "Renew passport for Tokyo trip (Fri 4 Dec 09:10)"}))
    items = [("event", FLIGHT), ("reminder", LONG_TODO), ("event", "Dinner")]
    labels = shorten.get(db, items)
    assert labels == {("event", FLIGHT): "Flight JL026 to Tokyo",
                      ("reminder", LONG_TODO): "Renew passport for Tokyo trip (Fri 4 Dec 09:10)",
                      ("event", "Dinner"): "Dinner"}
    assert calls == [[("event", FLIGHT), ("reminder", LONG_TODO)]]   # sent with their kind; short ones never
    assert shorten.get(db, items[:2]) == {k: labels[k] for k in items[:2]}
    assert len(calls) == 1                          # remembered: no second call


def test_event_labels_never_repeat_the_time(db, monkeypatch):
    calls = []
    monkeypatch.setattr(shorten, "run_claude", fake_shortener(calls, {FLIGHT: "Flight JL026 to Tokyo, 09:10"}))
    assert shorten.get(db, [("event", FLIGHT)])[("event", FLIGHT)] == "Flight JL026 to Tokyo"
    assert shorten.tidy("reminder", "Renew passport (Fri 4 Dec 09:10)") == "Renew passport (Fri 4 Dec 09:10)"


def test_a_label_with_a_changed_number_is_refused(db, monkeypatch):
    calls = []
    monkeypatch.setattr(shorten, "run_claude", fake_shortener(calls, {
        LONG_TODO: "Renew passport for Tokyo trip (Fri 5 Dec)"}))   # 5 isn't in the original
    assert shorten.get(db, [("reminder", LONG_TODO)])[("reminder", LONG_TODO)] == LONG_TODO   # fits in 60
    assert db.execute("SELECT COUNT(*) FROM short_titles").fetchone()[0] == 0   # not remembered
    assert shorten.safe(FLIGHT, "Flight JL026 to Tokyo") and not shorten.safe(FLIGHT, "Flight JL027")
    assert not shorten.safe(FLIGHT, "") and not shorten.safe(FLIGHT, "x" * 61)


def test_short_labels_reach_the_brief_and_the_summary(db, monkeypatch):
    calls = []
    monkeypatch.setattr(shorten, "run_claude", fake_shortener(calls, {LONG_TODO: "Passport for Tokyo trip"}))
    cal = morning(monkeypatch, db, rem(("plan", LONG_TODO, "2026-11-18T20:00:00")), in_hk=False)
    assert "Passport for Tokyo trip · 3 days overdue" in "\n".join(digest.today_brief(cal)["sections"])
    assert "* To do: Passport for Tokyo trip (overdue since Wed 18 Nov)" in digest.build_text([], cal=cal)


def test_formatting_trouble_falls_back_to_plain_text(monkeypatch, tmp_path):
    chat = tmp_path / "chat"
    chat.write_text("42")
    monkeypatch.setattr(notify.paths, "TELEGRAM_CHAT", chat)
    monkeypatch.setattr(notify.keyring, "get_password", lambda *a: "TOKEN")
    sent = []
    def call(token, method, payload):
        sent.append(payload)
        if payload.get("parse_mode"):
            raise RuntimeError("Telegram said: Bad Request: can't parse entities: unclosed tag")
        return {}
    monkeypatch.setattr(notify, "call", call)
    notify.send("<b>Saturday</b>\nPay &amp; go", True, html_mode=True)
    assert sent[0]["parse_mode"] == "HTML" and "parse_mode" not in sent[1]
    assert sent[1]["text"] == "Saturday\nPay & go"
    def offline(token, method, payload):
        raise RuntimeError("Telegram said: Unauthorized")
    monkeypatch.setattr(notify, "call", offline)
    with pytest.raises(RuntimeError):               # a real failure is still a failure
        notify.send("<b>x</b>", True, html_mode=True)


def test_brief_weather_is_one_short_line():
    w = {"forecast": weather.forecast_for(FND, SAT), "warnings": []}
    assert weather.line(w, brief=True) == "13–21°C · Mainly cloudy · rain chance low"


def test_tests_never_touch_the_real_data_folder():
    from email_organiser import paths
    assert "email-organiser-tests-" in str(paths.HOME)
    assert paths.BACKUPS.parent == paths.HOME and str(store.DB_PATH).startswith(str(paths.HOME))
