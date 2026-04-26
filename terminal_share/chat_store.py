from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    text      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reads (
    reader    TEXT NOT NULL,
    msg_id    INTEGER NOT NULL,
    PRIMARY KEY (reader, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_recipient_ts ON messages(recipient, ts);
"""

# Server-side cap on long-poll wait. Mirrors the existing claude-chat MCP
# convention; keeps MCP request lifetimes bounded for the wrapper's HTTP
# transport.
MAX_WAIT_SECONDS = 25.0

# 1Hz fallback inside the wait window covers the race where an insert
# sets the event between our DB read and entering wait.
_POLL_FALLBACK_S = 1.0


class ChatStore:
    """SQLite-backed message store with long-poll inbox + heartbeat tracking.

    Threading model:
      _lock         serializes all DB access on the single sqlite3 connection.
      _events_lock  guards the events registry against concurrent reader
                    registration; held very briefly.
      _events       per-reader threading.Event lazily created on first
                    inbox() call. set on every insert so any waiter wakes
                    and re-checks the DB.
      _last_seen    in-memory map reader -> wall-clock timestamp of the
                    reader's most recent inbox() call. Used to compute
                    online/stale/offline status; not persisted.
    """

    def __init__(self, path: str | Path = "terminal_share.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

        self._events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

    # --- writes ------------------------------------------------------------

    def insert_message(self, sender: str, recipient: str, text: str) -> tuple[int, float]:
        ts = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (ts, sender, recipient, text) VALUES (?, ?, ?, ?)",
                (ts, sender, recipient, text),
            )
            msg_id = int(cur.lastrowid)
        self._wake_all_readers()
        return (msg_id, ts)

    # --- reads -------------------------------------------------------------

    def inbox(
        self,
        reader: str,
        wait_seconds: float = 0.0,
        max_count: int = 20,
        mark_read: bool = True,
    ) -> tuple[list[dict], int]:
        """Return (messages, remaining) for `reader`.

        If wait_seconds > 0 and the immediate read finds nothing, block up
        to wait_seconds for an insert to wake us, then return whatever the
        next read sees (possibly still empty on timeout). Any call —
        long-poll or not — refreshes the heartbeat for `reader`.
        """
        wait_seconds = min(max(0.0, wait_seconds), MAX_WAIT_SECONDS)
        deadline = time.monotonic() + wait_seconds
        event = self._get_event(reader)

        while True:
            # Clear before read so an insert between read and wait wakes us.
            event.clear()
            msgs, remaining = self._inbox_read_locked(reader, max_count, mark_read)
            self._last_seen[reader] = time.time()
            if msgs or time.monotonic() >= deadline:
                return msgs, remaining
            event.wait(timeout=min(_POLL_FALLBACK_S, deadline - time.monotonic()))

    def _inbox_read_locked(
        self,
        reader: str,
        max_count: int,
        mark_read: bool,
    ) -> tuple[list[dict], int]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                """SELECT id, ts, sender, recipient, text
                   FROM messages
                   WHERE (recipient = ? OR recipient = 'all')
                     AND id NOT IN (SELECT msg_id FROM reads WHERE reader = ?)
                   ORDER BY ts ASC, id ASC
                   LIMIT ?""",
                (reader, reader, max_count),
            ).fetchall()

            messages = [
                {"id": r[0], "ts": r[1], "sender": r[2], "recipient": r[3], "text": r[4]}
                for r in rows
            ]

            if messages and mark_read:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO reads (reader, msg_id) VALUES (?, ?)",
                    [(reader, m["id"]) for m in messages],
                )

            remaining = self._conn.execute(
                """SELECT COUNT(*) FROM messages
                   WHERE (recipient = ? OR recipient = 'all')
                     AND id NOT IN (SELECT msg_id FROM reads WHERE reader = ?)""",
                (reader, reader),
            ).fetchone()[0]

            return (messages, int(remaining))

    def history(self, limit: int = 50) -> list[dict]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, ts, sender, recipient, text FROM messages "
                "ORDER BY ts DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "ts": r[1], "sender": r[2], "recipient": r[3], "text": r[4]}
            for r in reversed(rows)
        ]

    # --- heartbeat ---------------------------------------------------------

    def last_seen_at(self, reader: str) -> float | None:
        return self._last_seen.get(reader)

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        # Wake any waiting readers so they exit cleanly when the wrapper
        # is shutting down.
        self._wake_all_readers()
        with self._lock:
            self._conn.close()

    # --- internals ---------------------------------------------------------

    def _get_event(self, reader: str) -> threading.Event:
        with self._events_lock:
            ev = self._events.get(reader)
            if ev is None:
                ev = threading.Event()
                self._events[reader] = ev
            return ev

    def _wake_all_readers(self) -> None:
        with self._events_lock:
            for ev in self._events.values():
                ev.set()
