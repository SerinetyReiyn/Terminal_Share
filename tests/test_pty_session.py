from __future__ import annotations

import pytest

from terminal_share import pty_session as ps_mod
from terminal_share.pty_session import PtySession


class _FakeProc:
    """Stand-in for winpty.PtyProcess so the buffer logic can be tested
    without spawning a real shell."""

    pid = 12345

    def __init__(self) -> None:
        self._alive = True

    def isalive(self) -> bool:
        return self._alive

    def write(self, data) -> int:
        return len(data)

    def read(self, n=1024):
        return ""

    def terminate(self, force: bool = False) -> None:
        self._alive = False


class _FakeProcFactory:
    @staticmethod
    def spawn(_command: str, dimensions=None) -> _FakeProc:
        return _FakeProc()


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> PtySession:
    monkeypatch.setattr(ps_mod, "PtyProcess", _FakeProcFactory)
    s = PtySession(command="fake.exe", buffer_bytes_cap=1000)
    yield s
    s.close()


def test_initial_status(session: PtySession) -> None:
    st = session.status()
    assert st["alive"] is True
    assert st["pid"] == 12345
    assert st["buffer_bytes"] == 0
    assert st["buffer_head_seq"] == st["buffer_tail_seq"] + 1


def test_seq_is_monotonic(session: PtySession) -> None:
    s1 = session.append_output(b"first")
    s2 = session.append_output(b"second")
    s3 = session.append_output(b"third")
    assert s1 < s2 < s3


def test_read_since_returns_only_newer(session: PtySession) -> None:
    session.append_output(b"alpha")
    s2 = session.append_output(b"beta")
    session.append_output(b"gamma")

    r = session.read_since(since_seq=s2)
    assert r["data"] == "gamma"
    assert r["last_seq"] > s2


def test_read_since_zero_returns_everything(session: PtySession) -> None:
    session.append_output(b"alpha")
    session.append_output(b"beta")
    session.append_output(b"gamma")

    r = session.read_since(since_seq=0)
    assert r["data"] == "alphabetagamma"
    assert r["truncated"] is False


def test_read_since_empty_buffer(session: PtySession) -> None:
    r = session.read_since(since_seq=0)
    assert r["data"] == ""
    assert r["last_seq"] == 0
    assert r["truncated"] is False


def test_buffer_eviction_fifo(session: PtySession) -> None:
    # Cap is 1000 bytes (set by fixture).
    session.append_output(b"x" * 600)
    session.append_output(b"y" * 600)  # would push total to 1200, evicts the 600 X's

    st = session.status()
    assert st["buffer_bytes"] == 600
    assert st["buffer_head_seq"] == 2  # the 600 Y's are now seq 2

    r = session.read_since(since_seq=0)
    assert "x" not in r["data"]
    assert r["data"] == "y" * 600
    assert r["buffer_head_seq"] == 2


def test_max_bytes_truncates_and_signals(session: PtySession) -> None:
    session.append_output(b"a" * 100)
    session.append_output(b"b" * 100)
    session.append_output(b"c" * 100)

    r = session.read_since(since_seq=0, max_bytes=150)
    # First chunk fits (100). Second would push to 200 > 150 -> stop.
    assert r["data"] == "a" * 100
    assert r["truncated"] is True
    assert r["last_seq"] == 1


def test_paginate_via_last_seq(session: PtySession) -> None:
    session.append_output(b"a" * 100)
    session.append_output(b"b" * 100)
    session.append_output(b"c" * 100)

    r1 = session.read_since(since_seq=0, max_bytes=150)
    r2 = session.read_since(since_seq=r1["last_seq"], max_bytes=150)
    assert r2["data"] == "b" * 100
    assert r2["truncated"] is True
    r3 = session.read_since(since_seq=r2["last_seq"], max_bytes=150)
    assert r3["data"] == "c" * 100
    assert r3["truncated"] is False


def test_send_returns_byte_count(session: PtySession) -> None:
    n = session.send("hello")
    assert n == 5
    n2 = session.send(b"\x03")
    assert n2 == 1


def test_send_handles_unicode(session: PtySession) -> None:
    n = session.send("café")  # 5 bytes in UTF-8
    assert n == 5


def test_signal_ctrl_c_sends_x03(session: PtySession) -> None:
    n = session.signal_ctrl_c()
    assert n == 1
