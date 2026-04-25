from __future__ import annotations

import sqlite3
import threading

import pytest

from terminal_share.chat_store import ChatStore


@pytest.fixture
def store(tmp_path) -> ChatStore:
    s = ChatStore(tmp_path / "chat.db")
    yield s
    s.close()


def test_schema_created(store: ChatStore, tmp_path) -> None:
    conn = sqlite3.connect(tmp_path / "chat.db")
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert {"messages", "reads"} <= tables


def test_wal_mode_enabled(store: ChatStore, tmp_path) -> None:
    # WAL is per-database, persists across connections
    conn = sqlite3.connect(tmp_path / "chat.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_insert_and_history(store: ChatStore) -> None:
    a = store.insert_message("claudia", "all", "first")
    b = store.insert_message("code", "claudia", "second")
    assert a[0] != b[0]
    assert a[1] <= b[1]

    history = store.history()
    assert len(history) == 2
    assert history[0]["text"] == "first"
    assert history[1]["text"] == "second"


def test_inbox_returns_only_addressed_messages(store: ChatStore) -> None:
    store.insert_message("claudia", "code", "for code only")
    store.insert_message("claudia", "all", "to everyone")
    store.insert_message("code", "serinety", "for serinety only")

    code_inbox, _ = store.inbox("code")
    code_texts = [m["text"] for m in code_inbox]
    assert "for code only" in code_texts
    assert "to everyone" in code_texts
    assert "for serinety only" not in code_texts


def test_inbox_marks_read_atomically(store: ChatStore) -> None:
    store.insert_message("claudia", "code", "hi")
    first, remaining_after_first = store.inbox("code")
    second, remaining_after_second = store.inbox("code")
    assert len(first) == 1
    assert len(second) == 0
    assert remaining_after_first == 0
    assert remaining_after_second == 0


def test_broadcast_delivered_to_each_reader_independently(store: ChatStore) -> None:
    store.insert_message("claudia", "all", "team check")
    code_inbox, _ = store.inbox("code")
    serinety_inbox, _ = store.inbox("serinety")
    assert len(code_inbox) == 1
    assert len(serinety_inbox) == 1
    assert code_inbox[0]["text"] == "team check"
    assert serinety_inbox[0]["text"] == "team check"


def test_inbox_remaining_count(store: ChatStore) -> None:
    for i in range(5):
        store.insert_message("claudia", "code", f"msg{i}")
    batch, remaining = store.inbox("code", max_count=2)
    assert len(batch) == 2
    assert remaining == 3
    batch2, remaining2 = store.inbox("code", max_count=2)
    assert len(batch2) == 2
    assert remaining2 == 1


def test_history_returns_last_n_oldest_first(store: ChatStore) -> None:
    for i in range(10):
        store.insert_message("claudia", "all", f"msg{i}")
    h = store.history(limit=3)
    assert len(h) == 3
    # Last 3 messages (msg7, msg8, msg9), oldest-first
    assert [m["text"] for m in h] == ["msg7", "msg8", "msg9"]


def test_history_does_not_mark_read(store: ChatStore) -> None:
    store.insert_message("claudia", "code", "hi")
    store.history()
    inbox, _ = store.inbox("code")
    assert len(inbox) == 1


def test_concurrent_inserts(store: ChatStore) -> None:
    """SQLite + WAL + the app-level lock should serialize cleanly under
    concurrent writers."""
    def writer(idx: int) -> None:
        for i in range(20):
            store.insert_message(f"sender{idx}", "all", f"msg{idx}_{i}")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = store.history(limit=200)
    assert len(history) == 80
