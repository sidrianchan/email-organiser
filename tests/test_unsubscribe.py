"""Milestone 5b offline tests: which senders are offered, when one-click is allowed,
and that the POST is exactly what RFC 8058 describes (no cookies, no redirects)."""
import email
import http.server
import threading
from datetime import datetime
from email import policy

import pytest

from email_organiser import store, unsubscribe as u

URL = "https://news.shop.test/unsub?id=Xk29"


def headers(*, sig_domain="shop.test", verifier="mx.google.com", passed="shop.test",
            signed="from:to:subject:list-unsubscribe:list-unsubscribe-post",
            lu=f"<{URL}>, <mailto:unsub@shop.test?subject=stop>", post="List-Unsubscribe=One-Click",
            extra=""):
    raw = (f"Authentication-Results: {verifier}; dkim=pass header.i=@{passed} header.s=s1;"
           f" spf=pass smtp.mailfrom={passed}\r\n"
           f"DKIM-Signature: v=1; a=rsa-sha256; d={sig_domain}; s=s1;\r\n"
           f"\th={signed};\r\n\tbh=abc=; b=xyz=\r\n"
           f"{extra}"
           f"From: Shop <deals@shop.test>\r\nList-Unsubscribe: {lu}\r\n"
           + (f"List-Unsubscribe-Post: {post}\r\n" if post else "") + "Subject: Sale\r\n\r\nbody")
    return email.message_from_bytes(raw.encode(), policy=policy.default)


# --- when one-click is allowed ----------------------------------------------------------

def test_signed_one_click_is_offered():
    assert u.header_info(headers()) == {"url": URL, "mailto": "mailto:unsub@shop.test?subject=stop",
                                        "one_click": 1, "signed": 1}


def test_bare_links_without_angle_brackets_are_read():
    # seen in real Google mail: the link isn't wrapped in <...>
    assert u.header_info(headers(lu=URL))["url"] == URL
    assert u.header_info(headers(lu=f"{URL}, mailto:x@shop.test"))["mailto"] == "mailto:x@shop.test"


def test_signature_must_cover_both_unsubscribe_headers():
    assert u.header_info(headers(signed="from:to:subject:list-unsubscribe"))["signed"] == 0


def test_dkim_pass_from_an_untrusted_verifier_is_ignored():
    # a sender can write its own "dkim=pass" header; only Yahoo/Google/iCloud's count
    assert u.header_info(headers(verifier="mx.evil.test"))["signed"] == 0
    assert u.header_info(headers(verifier="notgoogle.com"))["signed"] == 0
    assert u.header_info(headers(verifier="mta.yahoo.com"))["signed"] == 1
    assert u.header_info(headers(verifier="dkim-verifier.icloud.com"))["signed"] == 1


def test_signature_domain_must_be_the_one_that_passed():
    assert u.header_info(headers(sig_domain="other.test"))["signed"] == 0


def test_no_post_header_means_no_one_click():
    info = u.header_info(headers(post=None))
    assert info["one_click"] == 0 and info["signed"] == 0 and info["url"] == URL


def test_mailto_only_and_http_links_are_not_one_click():
    assert u.header_info(headers(lu="<mailto:x@shop.test>"))["one_click"] == 0
    assert u.header_info(headers(lu="<http://shop.test/u>"))["one_click"] == 0


def test_unsafe_links_are_refused():
    assert u.unsafe(URL) is None
    for bad in ("http://shop.test/u", "https://192.168.1.1/u", "https://[::1]/u",
                "https://localhost/u", "https://printer.local/u", "https://me:pw@shop.test/u"):
        assert u.unsafe(bad), bad
    assert u.one_click("https://127.0.0.1/u")[0] is False   # refused before any network call


# --- which senders are offered ----------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def add(db, uid, sender, label, *, reply=0, job_alert=0, unsub=None, account="yahoo",
        seen="2026-11-16 01:00:00"):
    info = dict(message_id=f"<{uid}>", sender=sender, subject="s", date="", snippet="")
    store.save_if_new(db, account, 1, uid, info)
    db.execute("UPDATE messages SET first_seen = ? WHERE uid = ?", (seen, uid))
    db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply, job_alert) "
               "VALUES (?, 1, ?, ?, ?, ?)", (account, uid, label, reply, job_alert))
    if unsub is not None:
        store.save_unsub(db, account, 1, uid, unsub)
    db.commit()


SIGNED = {"url": URL, "mailto": None, "one_click": 1, "signed": 1}


def test_only_promo_and_newsletter_are_offered_never_spam_job_or_reply(db):
    add(db, 1, "Shop <deals@shop.test>", "promo", unsub=SIGNED)
    add(db, 2, "Spammer <x@spam.test>", "spam", unsub=SIGNED)
    add(db, 3, "Acme HR <hr@acme.test>", "job", unsub=SIGNED)
    add(db, 4, "Club <club@club.test>", "newsletter", reply=1, unsub=SIGNED)
    review, _ = u.candidates(db)
    assert [g["address"] for g in review] == ["deals@shop.test"]


def test_one_job_email_from_the_same_sender_blocks_the_offer(db):
    add(db, 1, "Workday <lseg@myworkday.com>", "newsletter", unsub=SIGNED)
    add(db, 2, "Workday <lseg@myworkday.com>", "job", unsub=SIGNED)
    assert u.candidates(db)[0] == []


def test_same_address_different_name_is_a_separate_list(db):
    # Jobsdb: one address, application updates (job) vs recommendations (newsletter)
    add(db, 1, "Jobsdb Applications <noreply@e.jobsdb.com>", "job")
    add(db, 2, "Jobsdb Recommendations <noreply@e.jobsdb.com>", "newsletter", job_alert=1, unsub=SIGNED)
    review, _ = u.candidates(db)
    assert [g["name"] for g in review] == ["Jobsdb Recommendations"]
    assert review[0]["also_important"] and review[0]["job_board"]
    assert "job/reply mail" in u.describe(1, review[0]) and "job screener" in u.describe(1, review[0])


def test_method_explains_what_can_be_done(db):
    add(db, 1, "A <a@a.test>", "promo", unsub=SIGNED)
    add(db, 2, "B <b@b.test>", "promo", unsub={"url": URL, "one_click": 1, "signed": 0})
    add(db, 3, "C <c@c.test>", "promo", unsub={"mailto": "mailto:stop@c.test?subject=x"})
    add(db, 4, "D <d@d.test>", "promo", unsub=None)
    add(db, 5, "E <e@e.test>", "promo", unsub={})
    add(db, 6, "G <g@g.test>", "promo")
    store.save_unsub(db, "yahoo", 1, 6, None)
    m = {g["address"]: u.method(g) for g in u.groups(db)}
    assert m["a@a.test"] == "one-click"
    assert "DKIM" in m["b@b.test"]
    assert "stop@c.test" in m["c@c.test"] and "?" not in m["c@c.test"]
    assert m["d@d.test"].startswith("wait") and "before 5b" in m["d@d.test"]
    assert m["g@g.test"].startswith("wait")      # email already gone when headers were read          # email already gone when headers were read
    assert "no unsubscribe header" in m["e@e.test"]


def test_planted_example_senders_are_never_listed(db):
    add(db, 1, "Shop Example <deals@example.com>", "promo", unsub=SIGNED)
    add(db, 2, "News <n@mail.example.org>", "newsletter", unsub=SIGNED)
    assert u.groups(db) == []


def test_newest_signed_link_is_used(db):
    add(db, 1, "A <a@a.test>", "promo", unsub=SIGNED | {"url": "https://a.test/old"}, seen="2026-11-10 00:00:00")
    add(db, 2, "A <a@a.test>", "promo", unsub=SIGNED | {"url": "https://a.test/new"}, seen="2026-11-16 00:00:00")
    assert u.groups(db)[0]["url"] == "https://a.test/new"


# --- the command: picks, keep, reappear, still sending ----------------------------------

def test_parse_picks():
    assert u.parse_picks("1 3 k2", 3) == ({1, 3}, {2})
    assert u.parse_picks("", 3) == (set(), set())
    for bad in ("4", "x", "1 k1"):
        with pytest.raises(ValueError):
            u.parse_picks(bad, 3)


def test_preview_sends_and_records_nothing(db, monkeypatch):
    add(db, 1, "A <a@a.test>", "promo", unsub=SIGNED)
    monkeypatch.setattr(u, "one_click", lambda url: pytest.fail("no POST on a preview"))
    u.run(db, live=False, ask=lambda _: "1")
    assert db.execute("SELECT COUNT(*) FROM sender_choices").fetchone()[0] == 0


def test_no_post_without_typed_yes(db, monkeypatch):
    add(db, 1, "A <a@a.test>", "promo", unsub=SIGNED)
    monkeypatch.setattr(u, "one_click", lambda url: pytest.fail("posted without consent"))
    answers = iter(["1", "y"])   # "y" is not "yes"
    u.run(db, live=True, ask=lambda _: next(answers))
    assert db.execute("SELECT COUNT(*) FROM sender_choices").fetchone()[0] == 0


def test_live_run_records_choices_and_unticked_come_back_only_with_new_mail(db, monkeypatch):
    add(db, 1, "A <a@a.test>", "promo", unsub=SIGNED)
    add(db, 2, "B <b@b.test>", "promo", unsub=SIGNED)
    add(db, 3, "C <c@c.test>", "promo", unsub=SIGNED)
    add(db, 4, "F <f@f.test>", "promo", unsub=SIGNED | {"url": "https://f.test/u"})
    posted = []
    monkeypatch.setattr(u, "one_click", lambda url: (posted.append(url), (url != "https://f.test/u", "HTTP 200"))[1])
    # order shown: A, B, C, F (all count 1, by name). Unsubscribe A and F, keep B, leave C.
    answers = iter(["1 4 k2", "yes"])
    u.run(db, live=True, ask=lambda _: next(answers))
    assert posted == [URL, "https://f.test/u"]
    status = {r["sender_key"].split("|")[0]: r["status"] for r in db.execute("SELECT * FROM sender_choices")}
    assert status == {"a@a.test": "unsubscribed", "b@b.test": "keep", "c@c.test": None}
    # F failed, so it isn't recorded and comes back. C comes back only once it sends more.
    assert [g["address"] for g in u.candidates(db)[0]] == ["f@f.test"]
    add(db, 5, "C <c@c.test>", "promo", unsub=SIGNED, seen="2099-01-01 00:00:00")
    add(db, 6, "B <b@b.test>", "promo", unsub=SIGNED, seen="2099-01-01 00:00:00")
    review, still = u.candidates(db)
    assert {g["address"] for g in review} == {"c@c.test", "f@f.test"}   # kept B never returns
    # A sends again well after unsubscribing: flagged as still sending
    add(db, 7, "A <a@a.test>", "promo", unsub=SIGNED, seen="2099-01-01 00:00:00")
    assert [g["address"] for g in u.candidates(db)[1]] == ["a@a.test"]


def test_picking_a_sender_with_no_saved_link_waits_and_comes_back_with_one(db, monkeypatch):
    add(db, 1, "Chess <streaks@chess.test>", "promo")
    store.save_unsub(db, "yahoo", 1, 1, None)               # its email was deleted before 5b
    monkeypatch.setattr(u, "one_click", lambda url: pytest.fail("nothing to POST to yet"))
    u.run(db, live=True, ask=lambda _: "1")                 # no "yes" asked: there's no request to send
    assert db.execute("SELECT status FROM sender_choices").fetchone()[0] == "waiting"
    assert u.candidates(db)[0] == []                        # quiet until it emails again
    add(db, 2, "Chess <streaks@chess.test>", "promo", unsub=SIGNED, seen="2099-01-01 00:00:00")
    review, _ = u.candidates(db)
    assert u.method(review[0]) == "one-click" and review[0]["picked_before"]
    assert "picked this before" in u.describe(1, review[0])


# --- the POST itself --------------------------------------------------------------------

def test_request_is_an_rfc8058_post_without_cookies_or_login():
    req = u.build_request(URL)
    assert req.get_method() == "POST" and req.full_url == URL
    body = req.data.decode()
    assert 'name="List-Unsubscribe"\r\n\r\nOne-Click\r\n' in body
    assert req.get_header("Content-type").startswith("multipart/form-data; boundary=")
    names = {k.lower() for k, _ in req.header_items()}
    assert not names & {"cookie", "authorization"}


class _Server(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        _Server.seen.append((self.path, self.headers.get("Cookie"),
                             self.rfile.read(int(self.headers["Content-Length"]))))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
        else:
            self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_redirects_are_not_followed_and_2xx_is_success():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Server)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # send_request skips the HTTPS check on purpose so a local test server can be used
        assert u.send_request(u.build_request(base + "/ok")) == (True, "HTTP 200")
        ok, what = u.send_request(u.build_request(base + "/redirect"))
        assert not ok and "not followed" in what
        assert [p for p, _, _ in _Server.seen] == ["/ok", "/redirect"]   # never /elsewhere
        assert all(cookie is None for _, cookie, _ in _Server.seen)
    finally:
        server.shutdown()


# --- weekly nudge -----------------------------------------------------------------------

def test_last_sunday_noon():
    assert u.last_sunday_noon(datetime(2026, 11, 22, 18, 0)) == datetime(2026, 11, 22, 12, 0)  # Sun 18:00
    assert u.last_sunday_noon(datetime(2026, 11, 22, 8, 0)) == datetime(2026, 11, 15, 12, 0)   # Sun 08:00
    assert u.last_sunday_noon(datetime(2026, 11, 23, 8, 0)) == datetime(2026, 11, 22, 12, 0)   # Mon catch-up


def test_nudge_is_counts_only_and_once_a_week(db, monkeypatch):
    add(db, 1, "Secret Shop <secret@shop.test>", "promo", unsub=SIGNED)
    sent = []
    monkeypatch.setattr(u.notify, "send", lambda text, loud: sent.append((text, loud)))
    sunday = datetime(2026, 11, 22, 18, 0)
    u.weekly_nudge(db, live=True, now=sunday)
    u.weekly_nudge(db, live=True, now=datetime(2026, 11, 23, 8, 0))   # same week: no repeat
    assert len(sent) == 1
    text, loud = sent[0]
    assert "1 junk sender to review" in text and not loud
    assert "Secret" not in text and "shop.test" not in text      # nothing from the email


def test_nudge_failure_never_raises(db, monkeypatch, capsys):
    add(db, 1, "A <a@a.test>", "promo", unsub=SIGNED)
    def boom(*a, **k):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(u.notify, "send", boom)
    u.weekly_nudge(db, live=True, now=datetime(2026, 11, 22, 18, 0))
    assert "FAILED" in capsys.readouterr().out
    assert db.execute("SELECT COUNT(*) FROM nudges").fetchone()[0] == 0   # retried next run


def test_fetch_saves_unsubscribe_headers(db):
    from email_organiser.fetch import parse_message
    raw = headers().as_bytes()
    info = parse_message(raw)
    assert info["unsub"]["signed"] == 1
