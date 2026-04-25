from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from winpty import PtyProcess


def _detect_terminal_size() -> tuple[int, int]:
    """(rows, cols) of the wrapper's controlling terminal, with a 24x80
    fallback when stdout is redirected or the size can't be determined."""
    try:
        size = os.get_terminal_size()
        return (size.lines, size.columns)
    except OSError:
        return (24, 80)


@dataclass(frozen=True)
class Chunk:
    seq: int
    ts: float
    data: bytes


class PtySession:
    """A single pwsh PTY plus a shared output ring buffer.

    Two locks, held briefly:
      _stdin_lock  serializes writes to PTY stdin (ps_send, ps_signal).
      _buffer_lock serializes the output buffer (single appender: PTY-stdout pump,
                   multiple readers: MCP tools).

    Keeping them separate so MCP reads don't queue behind unrelated stdin writes.
    """

    def __init__(
        self,
        command: str = "pwsh.exe",
        buffer_bytes_cap: int = 10 * 1024 * 1024,
    ) -> None:
        self._proc = PtyProcess.spawn(command, dimensions=_detect_terminal_size())
        self._stdin_lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._chunks: deque[Chunk] = deque()
        self._buffer_bytes = 0
        self._buffer_bytes_cap = buffer_bytes_cap
        self._next_seq = 1
        self._started_at = time.monotonic()

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at

    def send(self, data: str | bytes) -> int:
        if isinstance(data, bytes):
            payload = data.decode("utf-8", errors="replace")
            measured = len(data)
        else:
            payload = data
            measured = len(data.encode("utf-8"))
        with self._stdin_lock:
            self._proc.write(payload)
        return measured

    def signal_ctrl_c(self) -> int:
        return self.send("\x03")

    def read_pty_output(self, n: int = 4096) -> bytes:
        try:
            data = self._proc.read(n)
        except EOFError:
            return b""
        if not data:
            return b""
        if isinstance(data, str):
            return data.encode("utf-8", errors="replace")
        return bytes(data)

    def append_output(self, data: bytes) -> int:
        if not data:
            with self._buffer_lock:
                return self._next_seq - 1
        with self._buffer_lock:
            seq = self._next_seq
            self._next_seq += 1
            self._chunks.append(Chunk(seq=seq, ts=time.time(), data=data))
            self._buffer_bytes += len(data)
            while self._chunks and self._buffer_bytes > self._buffer_bytes_cap:
                evicted = self._chunks.popleft()
                self._buffer_bytes -= len(evicted.data)
            return seq

    def read_since(self, since_seq: int = 0, max_bytes: int = 65536) -> dict:
        with self._buffer_lock:
            relevant = [c for c in self._chunks if c.seq > since_seq]
            head = self._chunks[0].seq if self._chunks else self._next_seq

        out = bytearray()
        last_seq = since_seq
        truncated = False
        for c in relevant:
            if out and len(out) + len(c.data) > max_bytes:
                truncated = True
                break
            out.extend(c.data)
            last_seq = c.seq

        return {
            "data": bytes(out).decode("utf-8", errors="replace"),
            "last_seq": last_seq,
            "truncated": truncated,
            "buffer_head_seq": head,
        }

    def status(self) -> dict:
        with self._buffer_lock:
            head = self._chunks[0].seq if self._chunks else self._next_seq
            tail = self._chunks[-1].seq if self._chunks else self._next_seq - 1
            buf_bytes = self._buffer_bytes
        return {
            "alive": self.alive,
            "pid": self.pid,
            "buffer_head_seq": head,
            "buffer_tail_seq": tail,
            "buffer_bytes": buf_bytes,
            "uptime_seconds": self.uptime_seconds(),
        }

    def close(self) -> None:
        try:
            self._proc.terminate(force=True)
        except Exception:
            pass
