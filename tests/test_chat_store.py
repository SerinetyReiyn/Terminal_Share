from __future__ import annotations

import sqlite3
import threading
import time

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


# --- 1.2 long-poll + heartbeat -------------------------------------------

def test_inbox_immediate_when_messages_present(store: ChatStore) -> None:
    store.insert_message("claudia", "code", "hi")
    start = time.monotonic()
    msgs, _ = store.inbox("code", wait_seconds=10)
    elapsed = time.monotonic() - start
    assert len(msgs) == 1
    assert elapsed < 0.5  # didn't wait, returned at once


def test_inbox_long_poll_returns_empty_at_timeout(store: ChatStore) -> None:
    start = time.monotonic()
    msgs, remaining = store.inbox("code", wait_seconds=1.0)
    elapsed = time.monotonic() - start
    assert msgs == []
    assert remaining == 0
    assert 0.9 <= elapsed <= 2.5  # ~1s timeout


def test_inbox_long_poll_wakes_on_insert(store: ChatStore) -> None:
    """Reader blocks in long-poll; another thread inserts; reader returns
    promptly without waiting the full timeout."""
    def delayed_insert():
        time.sleep(0.3)
        store.insert_message("claudia", "code", "hi")

    threading.Thread(target=delayed_insert, daemon=True).start()
    start = time.monotonic()
    msgs, _ = store.inbox("code", wait_seconds=10)
    elapsed = time.monotonic() - start
    assert len(msgs) == 1
    assert msgs[0]["text"] == "hi"
    assert elapsed < 2.0  # well under the 10s cap


def test_inbox_long_poll_wakes_on_broadcast(store: ChatStore) -> None:
    """A broadcast (recipient='all') wakes a reader waiting on a different
    name."""
    def delayed_insert():
        time.sleep(0.3)
        store.insert_message("claudia", "all", "team")

    threading.Thread(target=delayed_insert, daemon=True).start()
    start = time.monotonic()
    msgs, _ = store.inbox("code", wait_seconds=10)
    elapsed = time.monotonic() - start
    assert len(msgs) == 1
    assert msgs[0]["recipient"] == "all"
    assert elapsed < 2.0


def test_inbox_wait_seconds_capped(store: ChatStore) -> None:
    """Values > MAX_WAIT_SECONDS get clamped server-side."""
    start = time.monotonic()
    store.inbox("code", wait_seconds=100)
    elapsed = time.monotonic() - start
    assert elapsed < 30  # capped well below 100


def test_last_seen_at_none_before_first_inbox(store: ChatStore) -> None:
    assert store.last_seen_at("code") is None


def test_last_seen_at_updated_on_inbox(store: ChatStore) -> None:
    before = time.time()
    store.inbox("code")
    after = time.time()
    seen = store.last_seen_at("code")
    assert seen is not None
    assert before <= seen <= after


def test_last_seen_at_per_reader(store: ChatStore) -> None:
    store.inbox("claudia")
    assert store.last_seen_at("claudia") is not None
    assert store.last_seen_at("code") is None


def test_inbox_mark_read_false_keeps_messages_unread(store: ChatStore) -> None:
    store.insert_message("claudia", "code", "hi")
    msgs1, _ = store.inbox("code", mark_read=False)
    msgs2, _ = store.inbox("code", mark_read=False)
    assert len(msgs1) == 1
    assert len(msgs2) == 1  # still unread


def test_inbox_long_poll_does_not_block_writers(store: ChatStore) -> None:
    """Long-poll must release the connection lock during the wait so other
    threads can insert. Verify by writing while a reader is waiting."""
    insert_done = threading.Event()

    def reader():
        store.inbox("code", wait_seconds=2.0)

    def writer():
        time.sleep(0.2)
        store.insert_message("claudia", "code", "hi")
        insert_done.set()

    rt = threading.Thread(target=reader, daemon=True)
    wt = threading.Thread(target=writer, daemon=True)
    rt.start()
    wt.start()
    # Insert should complete well before the 2s reader timeout.
    assert insert_done.wait(timeout=1.0)
    rt.join(timeout=3.0)
    wt.join(timeout=1.0)
