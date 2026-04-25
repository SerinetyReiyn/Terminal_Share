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


class ChatStore:
    """SQLite-backed message store. WAL + synchronous=NORMAL for concurrent
    chat_inbox readers. Single connection serialized via app-level lock."""

    def __init__(self, path: str | Path = "terminal_share.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def insert_message(self, sender: str, recipient: str, text: str) -> tuple[int, float]:
        ts = time.time()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO messages (ts, sender, recipient, text) VALUES (?, ?, ?, ?)",
                (ts, sender, recipient, text),
            )
            return (int(cur.lastrowid), ts)

    def inbox(self, reader: str, max_count: int = 20) -> tuple[list[dict], int]:
        with self._lock, self._conn:
            unread_rows = self._conn.execute(
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
                for r in unread_rows
            ]

            if messages:
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
