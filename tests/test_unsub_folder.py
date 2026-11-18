"""Unsubscribe-folder offline tests: which dragged emails get a one-click POST, that a dry run
changes nothing, that everything handled moves to Review (never deleted), and that only the
receiving provider's own DKIM results are trusted."""
import email
from collections import Counter
from email import policy

import pytest

from email_organiser import store, unsub_folder as uf, unsubscribe as u, watch

URL = "https://news.shop.test/unsub?id=Xk29"
SIGNED_HDRS = "from:to:subject:list-unsubscribe:list-unsubscribe-post"


def raw_email(*, sender="Shop <deals@shop.test>", top="Authentication-Results: mx.google.com; "
              "dkim=pass header.i=@shop.test header.s=s1\r\n", lu=f"<{URL}>",
              post="List-Unsubscribe=One-Click", message_id="<m1@shop.test>", extra=""):
    return ("Received: by mx.google.com\r\n" + top + "Received: from shop.test\r\n" + extra +
            f"DKIM-Signature: v=1; a=rsa-sha256; d=shop.test; s=s1;\r\n\th={SIGNED_HDRS};\r\n\tbh=a=; b=b=\r\n"
            f"From: {sender}\r\nMessage-ID: {message_id}\r\nSubject: Sale\r\n"
            + (f"List-Unsubscribe: {lu}\r\n" if lu else "")
            + (f"List-Unsubscribe-Post: {post}\r\n" if post else "") + "\r\n").encode()


def parse(raw: bytes):
    return email.message_from_bytes(raw, policy=policy.default)


# --- the DKIM trust fix (unsubscribe.receiver_results) -------------------------------------

def test_fake_stamp_from_another_provider_below_the_real_block_is_ignored():
    # Mail to Gmail; the sender wrote its own "Yahoo says dkim=pass" further down.
    raw = raw_email(top="Authentication-Results: mx.google.com; dkim=none\r\n",
                    extra="Authentication-Results: mta.yahoo.com; dkim=pass header.i=@shop.test\r\n")
    assert u.header_info(parse(raw))["signed"] == 0


def test_fake_stamp_placed_straight_after_the_block_from_another_provider_is_ignored():
    raw = raw_email(top="Authentication-Results: mx.google.com; dkim=none\r\n"
                        "Authentication-Results: mta.yahoo.com; dkim=pass header.i=@shop.test\r\n")
    assert u.header_info(parse(raw))["signed"] == 0


def test_icloud_stamps_with_other_lines_between_them_count():
    # real iCloud shape (18 Nov): bimi, arc, dmarc, dkim-verifier, spf, with X- lines between
    top = ("Authentication-Results: bimi.icloud.com; bimi=none\r\n"
           "X-ARC-Info: policy=fail; arc=none\r\n"
           "Authentication-Results: arc.icloud.com; arc=none\r\n"
           "Authentication-Results: dmarc.icloud.com; dmarc=pass header.from=shop.test\r\n"
           "X-DMARC-Info: pass=pass\r\n"
           "Authentication-Results: dkim-verifier.icloud.com; dkim=pass header.d=shop.test\r\n"
           "Authentication-Results: spf.icloud.com; spf=pass smtp.mailfrom=shop.test\r\n")
    assert u.header_info(parse(raw_email(top=top)))["signed"] == 1


def test_untrusted_first_stamp_means_nothing_counts():
    raw = raw_email(top="Authentication-Results: mx.evil.test; dkim=none\r\n"
                        "Authentication-Results: mx.google.com; dkim=pass header.i=@shop.test\r\n")
    assert u.header_info(parse(raw))["signed"] == 0


def test_a_yahoo_email_moved_into_gmail_keeps_its_yahoo_block():
    raw = raw_email(top="Authentication-Results: mta.yahoo.com; dkim=pass header.i=@shop.test\r\n")
    assert u.header_info(parse(raw))["signed"] == 1


# --- decide: the rules -------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    return store.open_db()


def add(db, uid, sender, label, *, reply=0, job_alert=0, message_id=None):
    store.save_if_new(db, "gmail", 1, uid, dict(message_id=message_id or f"<{uid}>", sender=sender,
                                                subject="s", date="", snippet=""))
    db.execute("INSERT INTO claude_labels (account, uidvalidity, uid, label, needs_reply, job_alert) "
               "VALUES ('gmail', 1, ?, ?, ?, ?)", (uid, label, reply, job_alert))
    db.commit()


def test_signed_one_click_is_posted(db):
    assert uf.decide(db, parse(raw_email()), set())["result"] == "post"


def test_spam_is_refused(db):
    add(db, 1, "Shop <deals@shop.test>", "spam", message_id="<m1@shop.test>")
    d = uf.decide(db, parse(raw_email()), uf.important_addresses(db))
    assert d["result"] == "refused" and "spam" in d["detail"]


def test_address_that_sent_job_or_reply_mail_is_refused_even_under_another_name(db):
    add(db, 1, "Jobsdb Updates <deals@shop.test>", "job")
    d = uf.decide(db, parse(raw_email(sender="Jobsdb Picks <deals@shop.test>")), uf.important_addresses(db))
    assert d["result"] == "refused" and "website" in d["detail"]
    add(db, 2, "Someone <ask@shop.test>", "fyi", reply=1)
    assert "ask@shop.test" in uf.important_addresses(db)


def test_unsigned_mailto_and_missing_headers_are_by_hand(db):
    unsigned = raw_email(top="Authentication-Results: mx.google.com; dkim=fail\r\n")
    assert uf.decide(db, parse(unsigned), set())["result"] == "by_hand"
    mailto = uf.decide(db, parse(raw_email(lu="<mailto:stop@shop.test?subject=x>", post=None)), set())
    assert mailto["result"] == "by_hand" and "stop@shop.test" in mailto["detail"]
    assert "no unsubscribe header" in uf.decide(db, parse(raw_email(lu=None, post=None)), set())["detail"]


def test_job_board_alert_is_allowed_but_flagged(db):
    d = uf.decide(db, parse(raw_email(sender="LinkedIn <jobalerts-noreply@linkedin.com>")), set())
    assert d["result"] == "post" and d["job_board"]


# --- the folder run, against a fake mail server -----------------------------------------

class FakeServer:
    def __init__(self, emails=(), has_folder=True, move=True):
        self.folders = {"INBOX"} | ({uf.FOLDER} if has_folder else set())
        self.box = {i + 1: e for i, e in enumerate(emails)}   # uid -> raw headers
        self.moved, self.readonly, self.caps = [], None, {"MOVE"} if move else {"UIDPLUS"}

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def folder_exists(self, f): return f in self.folders
    def create_folder(self, f): self.folders.add(f)
    def select_folder(self, f, readonly=False): self.readonly = readonly
    def search(self, criteria): return list(self.box)
    def fetch(self, uids, what): return {i: {b"BODY[HEADER]": self.box[i]} for i in uids}
    def has_capability(self, c): return c in self.caps
    def move(self, uids, folder):
        assert folder in self.folders
        self.moved += [(i, folder) for i in uids]


@pytest.fixture
def servers(monkeypatch):
    """{account name: FakeServer}; POSTs are captured, never sent."""
    found, posts = {}, []
    monkeypatch.setattr(uf, "load_accounts", lambda: [{"name": n} for n in found])
    monkeypatch.setattr(uf, "connect", lambda acc: found[acc["name"]])
    monkeypatch.setattr(uf.unsubscribe, "one_click", lambda url: (posts.append(url), (True, "HTTP 200"))[1])
    return found, posts


def test_dry_run_sends_moves_records_and_creates_nothing(db, servers):
    found, posts = servers
    found["gmail"] = FakeServer([raw_email()])
    found["yahoo"] = FakeServer(has_folder=False)
    uf.run(db, live=False)
    assert posts == [] and found["gmail"].moved == [] and found["gmail"].readonly is True
    assert uf.FOLDER not in found["yahoo"].folders
    assert db.execute("SELECT COUNT(*) FROM unsub_requests").fetchone()[0] == 0


def test_live_run_posts_records_and_moves_everything_to_review(db, servers):
    found, posts = servers
    add(db, 9, "Spam <deals@spam.test>", "spam", message_id="<s@spam.test>")
    found["gmail"] = FakeServer([raw_email(), raw_email(lu=None, post=None, message_id="<m2>"),
                                 raw_email(sender="Spam <deals@spam.test>", message_id="<s@spam.test>")])
    counts = uf.run(db, live=True)
    assert posts == [URL]
    assert counts == Counter(unsubscribed=1, by_hand=1, refused=1)
    assert found["gmail"].moved == [(1, "Assistant-Review"), (2, "Assistant-Review"), (3, "Assistant-Review")]
    assert db.execute("SELECT status FROM sender_choices WHERE sender_key = 'deals@shop.test|Shop'"
                      ).fetchone()[0] == "unsubscribed"   # the Terminal command won't offer it again


def test_folder_is_created_when_missing_on_a_live_run(db, servers):
    found, _ = servers
    found["icloud"] = FakeServer(has_folder=False)
    uf.run(db, live=True)
    assert uf.FOLDER in found["icloud"].folders


def test_same_list_dragged_twice_is_one_request(db, servers):
    found, posts = servers
    found["gmail"] = FakeServer([raw_email(), raw_email(message_id="<m2>")])
    found["yahoo"] = FakeServer([raw_email(message_id="<m3>")])
    uf.run(db, live=True)
    assert posts == [URL]
    assert db.execute("SELECT COUNT(*) FROM unsub_requests WHERE result='unsubscribed'").fetchone()[0] == 3


def test_failed_post_is_reported_and_still_moved(db, servers, monkeypatch):
    found, _ = servers
    monkeypatch.setattr(uf.unsubscribe, "one_click", lambda url: (False, "HTTP 302 redirect, not followed"))
    found["gmail"] = FakeServer([raw_email()])
    assert uf.run(db, live=True) == Counter(failed=1)
    assert found["gmail"].moved == [(1, "Assistant-Review")]
    assert db.execute("SELECT COUNT(*) FROM sender_choices").fetchone()[0] == 0


def test_cap_per_run(db, servers, monkeypatch):
    found, _ = servers
    monkeypatch.setattr(uf, "MAX_PER_RUN", 2)
    found["gmail"] = FakeServer([raw_email(message_id=f"<{i}>") for i in range(3)])
    found["yahoo"] = FakeServer([raw_email(message_id="<y>")])
    uf.run(db, live=True)
    assert len(found["gmail"].moved) == 2 and found["yahoo"].moved == []


def test_one_account_failing_never_stops_the_others_or_raises(db, servers, monkeypatch, capsys):
    found, posts = servers
    class Broken(FakeServer):
        def search(self, criteria): raise OSError("server went away")
    found["yahoo"] = Broken([raw_email()])
    found["gmail"] = FakeServer([raw_email(message_id="<g>")])
    assert uf.run(db, live=True) == Counter(unsubscribed=1)
    assert "FAILED" in capsys.readouterr().out


# --- what you're told --------------------------------------------------------------------

def test_ping_is_counts_only():
    text = uf.ping_text(Counter(unsubscribed=2, by_hand=1, failed=1, refused=1))
    assert text == ("Unsubscribe folder: 2 unsubscribed, 2 to do by hand, 1 not unsubscribed. "
                    "Details in the next summary.")
    assert uf.ping_text(Counter()) is None


def test_summary_lines_name_the_sender_once(db, servers):
    found, _ = servers
    found["gmail"] = FakeServer([raw_email(sender="LinkedIn <jobalerts-noreply@linkedin.com>"),
                                 raw_email(lu=None, post=None, message_id="<m2>")])
    uf.run(db, live=True)
    lines = uf.unshown_lines(db)
    assert lines[0] == ("Unsubscribed: LinkedIn <jobalerts-noreply@linkedin.com> "
                        "(this was feeding the job screener)")
    assert lines[1].startswith("Do by hand: Shop <deals@shop.test>: no unsubscribe header")
    uf.mark_shown(db)
    assert uf.unshown_lines(db) == []


def test_quick_check_pings_unsubscribe_counts_and_survives_a_crash(db, monkeypatch, tmp_path):
    from datetime import datetime
    sent = []
    monkeypatch.setattr(watch.notify, "send", lambda text, loud: sent.append((text, loud)))
    monkeypatch.setattr(watch.unsub_folder, "run", lambda db, live: Counter(unsubscribed=1))
    watch.unsubscribe_step(db, True, datetime(2026, 11, 18, 23, 30))
    assert sent == [("Unsubscribe folder: 1 unsubscribed. Details in the next summary.", False)]  # quiet hours
    def boom(db, live): raise RuntimeError("bad")
    monkeypatch.setattr(watch.unsub_folder, "run", boom)
    watch.unsubscribe_step(db, True, datetime(2026, 11, 18, 12, 0))   # must not raise
