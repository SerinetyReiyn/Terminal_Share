from __future__ import annotations

import re
import threading

import pytest

from terminal_share import pty_session as ps_mod
from terminal_share.config import Participant
from terminal_share.pty_session import PtySession


class _FakeProc:
    """Stand-in for winpty.PtyProcess that records bytes written so tests
    can inspect provenance and chat rendering."""

    pid = 12345

    def __init__(self) -> None:
        self._alive = True
        self.writes: list[str] = []
        self.write_lock = threading.Lock()

    def isalive(self) -> bool:
        return self._alive

    def write(self, data) -> int:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        with self.write_lock:
            self.writes.append(data)
        return len(data)

    def read(self, n=1024):
        return ""

    def terminate(self, force: bool = False) -> None:
        self._alive = False


class _FakeProcFactory:
    last: _FakeProc | None = None

    @classmethod
    def spawn(cls, _command: str, dimensions=None) -> _FakeProc:
        cls.last = _FakeProc()
        return cls.last


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> PtySession:
    monkeypatch.setattr(ps_mod, "PtyProcess", _FakeProcFactory)
    s = PtySession(command="fake.exe", buffer_bytes_cap=1000)
    yield s
    s.close()


@pytest.fixture
def session_with_participants(monkeypatch: pytest.MonkeyPatch) -> PtySession:
    monkeypatch.setattr(ps_mod, "PtyProcess", _FakeProcFactory)
    participants = {
        "serinety": Participant(name="serinety", role="human", display="Serinety", color="cyan"),
        "claudia": Participant(name="claudia", role="claude_desktop", display="Claudia", color="magenta"),
        "code": Participant(name="code", role="claude_code", display="Claude Code", color="green"),
    }
    s = PtySession(command="fake.exe", buffer_bytes_cap=1000, participants=participants)
    yield s
    s.close()


@pytest.fixture
def session_with_stdout(monkeypatch: pytest.MonkeyPatch):
    """Session with a captured BytesIO stdout — exercises the 1.2.2
    colored stdout-bypass path."""
    import io
    monkeypatch.setattr(ps_mod, "PtyProcess", _FakeProcFactory)
    participants = {
        "serinety": Participant(name="serinety", role="human", display="Serinety", color="cyan"),
        "claudia": Participant(name="claudia", role="claude_desktop", display="Claudia", color="bright_magenta"),
        "code": Participant(name="code", role="claude_code", display="Claude Code", color="bright_blue"),
    }
    stdout = io.BytesIO()
    s = PtySession(
        command="fake.exe",
        buffer_bytes_cap=10000,
        participants=participants,
        stdout=stdout,
        system_color="bright_black",
    )
    yield s, stdout
    s.close()


# --- 1.0 buffer behavior preserved -----------------------------------------

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
    session.append_output(b"x" * 600)
    session.append_output(b"y" * 600)
    st = session.status()
    assert st["buffer_bytes"] == 600
    assert st["buffer_head_seq"] == 2
    r = session.read_since(since_seq=0)
    assert "x" not in r["data"]
    assert r["data"] == "y" * 600


def test_max_bytes_truncates_and_signals(session: PtySession) -> None:
    session.append_output(b"a" * 100)
    session.append_output(b"b" * 100)
    session.append_output(b"c" * 100)
    r = session.read_since(since_seq=0, max_bytes=150)
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
    r3 = session.read_since(since_seq=r2["last_seq"], max_bytes=150)
    assert r3["data"] == "c" * 100


def test_send_returns_byte_count(session: PtySession) -> None:
    assert session.send("hello") == 5
    assert session.send(b"\x03") == 1


def test_send_handles_unicode(session: PtySession) -> None:
    assert session.send("café") == 5


def test_signal_ctrl_c_sends_x03(session: PtySession) -> None:
    assert session.signal_ctrl_c() == 1


# --- 1.1 ANSI strip --------------------------------------------------------

def test_read_since_strip_ansi(session: PtySession) -> None:
    session.append_output(b"\x1b[7;63H\x1b[93mGet-Date\x1b[0m\r\n")
    r_raw = session.read_since(since_seq=0)
    r_clean = session.read_since(since_seq=0, strip_ansi=True)
    assert "\x1b[" in r_raw["data"]
    assert "\x1b[" not in r_clean["data"]
    assert "Get-Date" in r_clean["data"]


def test_read_since_strip_ansi_default_false(session: PtySession) -> None:
    session.append_output(b"\x1b[31mred\x1b[0m")
    r = session.read_since(since_seq=0)
    assert "\x1b[31m" in r["data"]


# --- 1.1 chat rendering ----------------------------------------------------

def test_render_chat_line_direct(session_with_participants: PtySession) -> None:
    session_with_participants.render_chat_line("claudia", "code", "hi from claudia")
    full = "".join(_FakeProcFactory.last.writes)
    assert "Claudia -> Claude Code" in full
    assert "hi from claudia" in full
    assert full.endswith("\r")
    # Plain text, no ANSI escapes — PSReadLine would strip them anyway.
    assert "\x1b" not in full
    assert full.count("\r") == 1


def test_render_chat_line_broadcast(session_with_participants: PtySession) -> None:
    session_with_participants.render_chat_line("code", "all", "team check")
    full = "".join(_FakeProcFactory.last.writes)
    assert "Claude Code -> all" in full
    assert "\x1b" not in full


def test_render_chat_line_collapses_newlines(session_with_participants: PtySession) -> None:
    session_with_participants.render_chat_line("claudia", "all", "line1\nline2\r\nline3")
    full = "".join(_FakeProcFactory.last.writes)
    # Only the trailing \r should be a real CR — internal newlines collapsed
    assert full.count("\r") == 1
    assert "line1\\nline2\\nline3" in full


def test_render_chat_line_truncates_long_text(session_with_participants: PtySession) -> None:
    huge = "x" * 8000
    session_with_participants.render_chat_line("claudia", "all", huge)
    full = "".join(_FakeProcFactory.last.writes)
    assert "... (truncated)" in full
    assert "x" * 8000 not in full


def test_render_chat_line_unknown_sender_raises(session_with_participants: PtySession) -> None:
    with pytest.raises(KeyError):
        session_with_participants.render_chat_line("bob", "all", "hi")


# --- 1.1 ps_send transactional atomicity -----------------------------------

def test_send_with_provenance_writes_pair(session_with_participants: PtySession) -> None:
    session_with_participants.send_with_provenance("claudia", "Get-Date")
    proc = _FakeProcFactory.last
    # Two writes inside one lock acquire: provenance then command
    assert len(proc.writes) == 2
    provenance, command = proc.writes
    assert "running:" in provenance
    assert "Claudia" in provenance
    assert "\x1b" not in provenance
    assert command == "Get-Date\r"


def test_send_with_provenance_preserves_trailing_cr(session_with_participants: PtySession) -> None:
    session_with_participants.send_with_provenance("code", "Get-Date\r")
    _, command = _FakeProcFactory.last.writes
    assert command == "Get-Date\r"  # not "Get-Date\r\r"


def test_concurrent_send_with_provenance_no_interleave(
    session_with_participants: PtySession,
) -> None:
    """AC#5: two concurrent ps_send transactions must not interleave."""
    threads = [
        threading.Thread(
            target=session_with_participants.send_with_provenance,
            args=("claudia", "Get-Date"),
        ),
        threading.Thread(
            target=session_with_participants.send_with_provenance,
            args=("code", "Get-Process"),
        ),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    writes = _FakeProcFactory.last.writes
    assert len(writes) == 4  # two pairs of (provenance, command)
    # Each pair must be contiguous: provenance(N) then command(N), then
    # provenance(M) then command(M). Never interleaved.
    pair_a = writes[0:2]
    pair_b = writes[2:4]
    for prov, cmd in (pair_a, pair_b):
        assert "running:" in prov
        assert cmd in ("Get-Date\r", "Get-Process\r")


def test_chat_line_format_shape(session_with_participants: PtySession) -> None:
    session_with_participants.render_chat_line("claudia", "code", "msg")
    full = "".join(_FakeProcFactory.last.writes)
    # Expect: # [Claudia -> Claude Code HH:MM:SS] msg\r
    assert re.match(
        r"^# \[Claudia -> Claude Code \d{2}:\d{2}:\d{2}\] msg\r$",
        full,
    )


# --- 1.2.2 colored stdout-bypass for chat lines ---------------------------

def test_render_chat_line_bypasses_pty_when_stdout_present(session_with_stdout) -> None:
    session, stdout = session_with_stdout
    session.render_chat_line("claudia", "code", "hi")
    # FakeProc shouldn't have been written to — no PTY path
    assert _FakeProcFactory.last.writes == []
    # Stdout has the colored line
    out = stdout.getvalue().decode("utf-8")
    assert "# [Claudia -> Claude Code" in out
    assert "hi" in out


def test_render_chat_line_uses_sender_color(session_with_stdout) -> None:
    session, stdout = session_with_stdout
    session.render_chat_line("claudia", "code", "hello")
    out = stdout.getvalue().decode("utf-8")
    # claudia's color is bright_magenta = ANSI 95
    assert out.startswith("\x1b[95m")
    assert out.rstrip("\r\n").endswith("\x1b[0m")


def test_render_chat_line_code_color_blue(session_with_stdout) -> None:
    session, stdout = session_with_stdout
    session.render_chat_line("code", "claudia", "ping")
    out = stdout.getvalue().decode("utf-8")
    # code's color is bright_blue = ANSI 94
    assert out.startswith("\x1b[94m")


def test_render_chat_line_broadcast_uses_sender_color(session_with_stdout) -> None:
    session, stdout = session_with_stdout
    session.render_chat_line("code", "all", "broadcast")
    out = stdout.getvalue().decode("utf-8")
    assert out.startswith("\x1b[94m")
    assert "Claude Code -> all" in out


def test_render_chat_line_appends_to_buffer_too(session_with_stdout) -> None:
    """Stdout-bypass also appends to the PTY buffer so ps_read sees it."""
    session, stdout = session_with_stdout
    session.render_chat_line("claudia", "code", "buffered")
    r = session.read_since(since_seq=0)
    assert "buffered" in r["data"]
    assert "Claudia -> Claude Code" in r["data"]


def test_render_system_comment_uses_system_color(session_with_stdout) -> None:
    session, stdout = session_with_stdout
    session.render_system_comment(["@code is offline", "  message queued"])
    out = stdout.getvalue().decode("utf-8")
    # system_color set to bright_black = ANSI 90 in the fixture
    assert "\x1b[90m" in out
    assert "@code is offline" in out
    assert "message queued" in out


def test_render_chat_line_falls_back_to_pty_when_no_stdout(
    session_with_participants: PtySession,
) -> None:
    """Without a stdout (non-interactive / tests), fall back to plain
    PTY write so pwsh still sees the comment."""
    session_with_participants.render_chat_line("claudia", "code", "fallback")
    full = "".join(_FakeProcFactory.last.writes)
    assert "fallback" in full
    assert "\x1b" not in full  # no color in fallback path
