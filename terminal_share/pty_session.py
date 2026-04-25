from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Mapping

from winpty import PtyProcess

from .config import Participant


# Strip CSI sequences only (cursor moves, color changes, mode toggles —
# essentially everything PSReadLine emits). We deliberately don't try to
# handle every obscure ANSI case; this covers the realistic surface and
# anything past it stays in the raw stream for callers who want it.
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

# Cap rendered chat-comment text at 4096 chars (pwsh max line is ~8190).
# Full text always persists to the DB; this only bounds the rendered comment.
_CHAT_TEXT_RENDER_CAP = 4096
_TRUNCATION_MARKER = "... (truncated)"


def _detect_terminal_size() -> tuple[int, int]:
    """(rows, cols) of the wrapper's controlling terminal, with a 24x80
    fallback when stdout is redirected or the size can't be determined."""
    try:
        size = os.get_terminal_size()
        return (size.lines, size.columns)
    except OSError:
        return (24, 80)


def _flatten_for_comment(text: str) -> str:
    """Collapse newlines to literal '\\n' so the comment stays a single
    pwsh-parseable line, then truncate at the render cap."""
    flat = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    if len(flat) > _CHAT_TEXT_RENDER_CAP:
        keep = _CHAT_TEXT_RENDER_CAP - len(_TRUNCATION_MARKER)
        flat = flat[:keep] + _TRUNCATION_MARKER
    return flat


@dataclass(frozen=True)
class Chunk:
    seq: int
    ts: float
    data: bytes


class PtySession:
    """A single pwsh PTY plus a shared output ring buffer.

    Two locks, held briefly:
      _stdin_lock  serializes writes to PTY stdin. Held by send / signal_ctrl_c
                   for single byte writes; held across the entire provenance +
                   command sequence in send_with_provenance (see spec
                   §"Provenance + atomicity for ps_send"). Do NOT release
                   between provenance and command — that re-introduces the
                   keystroke-interleaving bug AC#5 catches.
      _buffer_lock serializes the output buffer (single appender: PTY-stdout
                   pump, multiple readers: MCP tools).
    """

    def __init__(
        self,
        command: str = "pwsh.exe",
        buffer_bytes_cap: int = 10 * 1024 * 1024,
        participants: Mapping[str, Participant] | None = None,
    ) -> None:
        self._proc = PtyProcess.spawn(command, dimensions=_detect_terminal_size())
        self._stdin_lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._chunks: deque[Chunk] = deque()
        self._buffer_bytes = 0
        self._buffer_bytes_cap = buffer_bytes_cap
        self._next_seq = 1
        self._started_at = time.monotonic()
        self._participants: Mapping[str, Participant] = participants or {}

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def alive(self) -> bool:
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    @property
    def participants(self) -> Mapping[str, Participant]:
        return self._participants

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

    def render_chat_line(
        self,
        sender_key: str,
        recipient_key: str,
        text: str,
    ) -> None:
        """Render a plain `# [...] text` comment into the wrapped pane.
        Holds _stdin_lock once for the whole write so the line cannot be
        interleaved with another writer's bytes.

        Plain text (no ANSI color) is intentional: PSReadLine intercepts
        ESC bytes from PTY stdin as keybinding prefixes and strips them,
        leaving the bare CSI text (e.g. `[35m`) visible to pwsh's parser
        as malformed array syntax. pwsh's built-in comment syntax
        highlighting still renders chat lines distinct from regular output.
        Per-sender color survives in structured chat_inbox / chat_history
        returns; restoring it in-pane is a 1.2 follow-up.
        """
        sender = self._participants[sender_key]
        if recipient_key == "all":
            recipient_display = "all"
        else:
            recipient_display = self._participants[recipient_key].display
        ts = time.strftime("%H:%M:%S")
        line = (
            f"# [{sender.display} -> {recipient_display} {ts}] "
            f"{_flatten_for_comment(text)}"
        )
        with self._stdin_lock:
            self._proc.write(line + "\r")

    def send_with_provenance(self, sender_key: str, command_text: str) -> int:
        """Inject a command preceded by a plain provenance comment.

        Holds _stdin_lock once across BOTH writes (provenance + command).
        Releasing between them allows the human stdin pump to slip a
        keystroke in and break atomicity; the spec is explicit about this.

        Plain text (no ANSI color) for the same PSReadLine reason as
        render_chat_line.
        """
        sender = self._participants[sender_key]
        ts = time.strftime("%H:%M:%S")
        provenance = f"# [{sender.display} {ts}] running:"
        if not command_text.endswith("\r"):
            command_text = command_text + "\r"
        with self._stdin_lock:
            self._proc.write(provenance + "\r")
            self._proc.write(command_text)
        return len(command_text.encode("utf-8"))

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

    def read_since(
        self,
        since_seq: int = 0,
        max_bytes: int = 65536,
        strip_ansi: bool = False,
    ) -> dict:
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

        text = bytes(out).decode("utf-8", errors="replace")
        if strip_ansi:
            text = _CSI_RE.sub("", text)

        return {
            "data": text,
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
