from __future__ import annotations

import asyncio
import time
from typing import Mapping

import pytest

from terminal_share.chat_store import ChatStore
from terminal_share.chat_tools import _compute_status, make_chat_tools
from terminal_share.config import Heartbeat, Participant


def _run(coro):
    """Run an async tool callable; sync tools pass through unchanged."""
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _participants() -> dict[str, Participant]:
    return {
        "serinety": Participant(name="serinety", role="human", display="Serinety", color="cyan"),
        "claudia": Participant(name="claudia", role="claude_desktop", display="Claudia", color="magenta"),
        "code": Participant(name="code", role="claude_code", display="Claude Code", color="green"),
    }


class _FakeSession:
    """Records render calls without spawning a real PTY."""

    def __init__(self, participants: Mapping[str, Participant]) -> None:
        self.participants = participants
        self.chat_renders: list[tuple[str, str, str]] = []
        self.system_renders: list[list[str]] = []

    def render_chat_line(self, sender_key: str, recipient_key: str, text: str) -> None:
        self.chat_renders.append((sender_key, recipient_key, text))

    def render_system_comment(self, lines: list[str]) -> None:
        self.system_renders.append(lines)


@pytest.fixture
def setup(tmp_path):
    store = ChatStore(tmp_path / "chat.db")
    parts = _participants()
    session = _FakeSession(parts)
    heartbeat = Heartbeat(online_seconds=90, stale_seconds=300)
    tools = make_chat_tools(session, store, heartbeat)
    yield session, store, heartbeat, tools
    store.close()


# --- _compute_status helper ---------------------------------------------

def test_compute_status_offline_when_none() -> None:
    hb = Heartbeat(online_seconds=90, stale_seconds=300)
    assert _compute_status(None, hb) == "offline"


def test_compute_status_online_recent() -> None:
    hb = Heartbeat(online_seconds=90, stale_seconds=300)
    assert _compute_status(time.time() - 10, hb) == "online"


def test_compute_status_stale_mid_range() -> None:
    hb = Heartbeat(online_seconds=90, stale_seconds=300)
    assert _compute_status(time.time() - 120, hb) == "stale"


def test_compute_status_offline_old() -> None:
    hb = Heartbeat(online_seconds=90, stale_seconds=300)
    assert _compute_status(time.time() - 600, hb) == "offline"


# --- chat_send offline warning -----------------------------------------

def test_chat_send_no_warning_when_recipient_online(setup) -> None:
    session, store, _, tools = setup
    # Bring code "online" by inboxing
    _run(tools["chat_inbox"](reader="code"))
    r = tools["chat_send"](sender="claudia", text="hi", to="code")
    assert r["ok"]
    assert session.system_renders == []  # no warning


def test_chat_send_warns_when_recipient_offline(setup) -> None:
    session, _, _, tools = setup
    r = tools["chat_send"](sender="claudia", text="hi", to="code")
    assert r["ok"]
    assert len(session.system_renders) == 1
    warning = "\n".join(session.system_renders[0])
    assert "@code" in warning
    assert "not currently listening" in warning


def test_chat_send_to_all_warns_for_all_offline_excluding_sender(setup) -> None:
    session, _, _, tools = setup
    # Mark serinety online; claudia, code offline
    _run(tools["chat_inbox"](reader="serinety"))
    r = tools["chat_send"](sender="claudia", text="hi", to="all")
    assert r["ok"]
    assert len(session.system_renders) == 1
    warning = "\n".join(session.system_renders[0])
    # Should warn about code (offline), should NOT warn about claudia (sender)
    assert "@code" in warning
    assert "@claudia" not in warning


def test_chat_send_to_all_no_warning_when_all_online(setup) -> None:
    session, _, _, tools = setup
    _run(tools["chat_inbox"](reader="serinety"))
    _run(tools["chat_inbox"](reader="claudia"))
    _run(tools["chat_inbox"](reader="code"))
    session.system_renders.clear()
    r = tools["chat_send"](sender="claudia", text="hi", to="all")
    assert r["ok"]
    assert session.system_renders == []


def test_chat_send_unknown_sender_rejected(setup) -> None:
    session, _, _, tools = setup
    r = tools["chat_send"](sender="bob", text="hi", to="code")
    assert r["ok"] is False
    assert r["error"] == "unknown_sender"
    assert session.chat_renders == []
    assert session.system_renders == []


def test_chat_send_unknown_recipient_rejected(setup) -> None:
    session, _, _, tools = setup
    r = tools["chat_send"](sender="claudia", text="hi", to="ghost")
    assert r["ok"] is False
    assert r["error"] == "unknown_recipient"
    assert session.chat_renders == []
    assert session.system_renders == []


# --- chat_inbox -----------------------------------------------------------

def test_chat_inbox_returns_messages(setup) -> None:
    _, store, _, tools = setup
    store.insert_message("claudia", "code", "hi")
    r = _run(tools["chat_inbox"](reader="code"))
    assert r["count"] == 1
    assert r["messages"][0]["text"] == "hi"


def test_chat_inbox_unknown_reader_rejected(setup) -> None:
    _, _, _, tools = setup
    r = _run(tools["chat_inbox"](reader="ghost"))
    assert r["ok"] is False
    assert r["error"] == "unknown_reader"


def test_chat_inbox_long_poll_returns_quickly_when_message_present(setup) -> None:
    _, store, _, tools = setup
    store.insert_message("claudia", "code", "hi")
    start = time.monotonic()
    r = _run(tools["chat_inbox"](reader="code", wait_seconds=10))
    elapsed = time.monotonic() - start
    assert r["count"] == 1
    assert elapsed < 0.5


# --- chat_participants ----------------------------------------------------

def test_chat_participants_status_offline_when_never_seen(setup) -> None:
    _, _, _, tools = setup
    r = tools["chat_participants"]()
    for key in ("serinety", "claudia", "code"):
        assert r["participants"][key]["status"] == "offline"
        assert r["participants"][key]["last_seen_at"] is None


def test_chat_participants_status_online_after_inbox(setup) -> None:
    _, _, _, tools = setup
    _run(tools["chat_inbox"](reader="claudia"))
    r = tools["chat_participants"]()
    assert r["participants"]["claudia"]["status"] == "online"
    assert r["participants"]["claudia"]["last_seen_at"] is not None
    assert r["participants"]["code"]["status"] == "offline"


def test_chat_participants_includes_existing_fields(setup) -> None:
    _, _, _, tools = setup
    r = tools["chat_participants"]()
    serinety = r["participants"]["serinety"]
    assert serinety["role"] == "human"
    assert serinety["display"] == "Serinety"
    assert serinety["color"] == "cyan"
    assert r["broadcast_keyword"] == "all"


# --- agent_stop -----------------------------------------------------------

def test_agent_stop_inserts_synthetic_exit(setup) -> None:
    _, store, _, tools = setup
    r = tools["agent_stop"]("claudia")
    assert r["ok"] is True
    assert r["participant"] == "claudia"
    assert r["was_online"] is False  # never inboxed

    # The synthetic /exit should be readable from claudia's inbox
    msgs, _ = store.inbox("claudia")
    assert len(msgs) == 1
    assert msgs[0]["sender"] == "system"
    assert msgs[0]["text"] == "/exit"


def test_agent_stop_does_not_render_to_pty(setup) -> None:
    session, _, _, tools = setup
    tools["agent_stop"]("claudia")
    assert session.chat_renders == []  # no PTY render
    assert session.system_renders == []  # no offline warning


def test_agent_stop_was_online_true_when_recently_inboxed(setup) -> None:
    _, _, _, tools = setup
    _run(tools["chat_inbox"](reader="claudia"))
    r = tools["agent_stop"]("claudia")
    assert r["was_online"] is True


def test_agent_stop_unknown_participant_rejected(setup) -> None:
    _, _, _, tools = setup
    r = tools["agent_stop"]("ghost")
    assert r["ok"] is False
    assert r["error"] == "unknown_participant"


def test_agent_stop_for_offline_queues_exit(setup) -> None:
    _, store, _, tools = setup
    r = tools["agent_stop"]("code")
    assert r["was_online"] is False
    # When code next listens, the /exit is waiting
    msgs, _ = store.inbox("code")
    assert any(m["text"] == "/exit" and m["sender"] == "system" for m in msgs)
