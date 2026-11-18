"""The login helper must never wait forever on a silent server."""
import socket
import threading
import time

import pytest
from imapclient import IMAPClient

from email_organiser import mail


def test_connect_sets_a_timeout(monkeypatch):
    seen = {}

    class FakeClient:
        def __init__(self, host, **kw):
            seen.update(kw, host=host)

        def login(self, user, password):
            seen["user"] = user

    monkeypatch.setattr(mail, "IMAPClient", FakeClient)
    monkeypatch.setattr(mail.keyring, "get_password", lambda *a: "app pass word")
    mail.connect({"name": "yahoo", "host": "imap.example.com", "username": "me"})
    assert seen["ssl"] is True
    assert seen["timeout"] == mail.TIMEOUT
    assert 0 < mail.TIMEOUT <= 300


def test_a_silent_server_times_out_instead_of_hanging():
    """A server that accepts the connection and then says nothing: the case that hung."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    start = time.monotonic()
    with pytest.raises((socket.timeout, TimeoutError, OSError)):
        IMAPClient("127.0.0.1", port=port, ssl=False, timeout=1)  # waits for the greeting
    assert time.monotonic() - start < 10
    server.close()
