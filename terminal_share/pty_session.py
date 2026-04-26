from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import IO, Mapping

from winpty import PtyProcess

from .chat_store import ChatStore
from .config import Participant
from .modal import ModalChatInput, ModalResult


# Strip CSI sequences only (cursor moves, color changes, mode toggles —
# essentially everything PSReadLine emits). We deliberately don't try to
# handle every obscure ANSI case; this covers the realistic surface and
# anything past it stays in the raw stream for callers who want it.
_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


# ANSI foreground codes for the participant colors validated by config.py.
# Used by stdout-bypass renders (modal prompt, chat lines per 1.2.2,
# system warnings) where ANSI escapes survive because they never go
# through PSReadLine. Provenance comments still go through the PTY
# plain-text path — see send_with_provenance.
ANSI_FG: dict[str, int] = {
    "white": 37, "cyan": 36, "magenta": 35, "green": 32,
    "yellow": 33, "blue": 34, "red": 31,
    "bright_black": 90,
    "bright_white": 97, "bright_cyan": 96, "bright_magenta": 95,
    "bright_green": 92, "bright_yellow": 93, "bright_blue": 94,
    "bright_red": 91,
}


def apply_color(text: str, color_name: str) -> str:
    """Wrap text in ANSI fg color escape + reset. Falls back to white if
    color_name isn't in ANSI_FG (config validation guarantees it will be)."""
    code = ANSI_FG.get(color_name, ANSI_FG["white"])
    return f"\x1b[{code}m{text}\x1b[0m"

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
    """A single pwsh PTY plus a shared output ring buffer and modal-input
    state.

    Three locks. Required acquisition order to avoid deadlock:
        _render_lock  ->  _stdin_lock  ->  _buffer_lock

      _render_lock  serializes ALL writes to the user's stdout — modal
                    redraws, the PTY-output pump's pass-through, and the
                    modal-active branch of render_chat_line. Per the
                    1.1.1 spec patch, the pump explicitly DOES take this
                    lock; without it modal's `\\r\\x1b[K` wipe and pwsh
                    output bytes interleave at the OS level.
      _stdin_lock   serializes writes to PTY stdin (send, signal_ctrl_c,
                    render_chat_line PTY path, send_with_provenance).
                    Held across the entire provenance + command sequence
                    in send_with_provenance — do NOT release between.
      _buffer_lock  serializes the output buffer (single appender from
                    pump or the modal-active chat path, multiple readers
                    from MCP tools).
    """

    def __init__(
        self,
        command: str = "pwsh.exe",
        buffer_bytes_cap: int = 10 * 1024 * 1024,
        participants: Mapping[str, Participant] | None = None,
        chat_store: ChatStore | None = None,
        sender_self: Participant | None = None,
        stdout: IO[bytes] | None = None,
        system_color: str = "bright_black",
    ) -> None:
        self._proc = PtyProcess.spawn(command, dimensions=_detect_terminal_size())
        self._render_lock = threading.Lock()
        self._stdin_lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._chunks: deque[Chunk] = deque()
        self._buffer_bytes = 0
        self._buffer_bytes_cap = buffer_bytes_cap
        self._next_seq = 1
        self._started_at = time.monotonic()
        self._participants: Mapping[str, Participant] = participants or {}
        self._chat_store = chat_store
        self._sender_self = sender_self
        self._stdout = stdout
        self._system_color = system_color
        self._chars_since_enter = 0
        # CSI input parser: 0=normal, 1=after \x1b, 2=inside \x1b[<params><final>
        self._esc_state = 0
        self._modal: ModalChatInput | None = None

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

    @property
    def modal_active(self) -> bool:
        return self._modal is not None

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_at

    # --- raw PTY I/O ------------------------------------------------------

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

    # --- pump pass-throughs ----------------------------------------------

    def emit_pty_output(self, data: bytes) -> int:
        """Called by the PTY pump for every chunk read from PTY stdout.
        Holds _render_lock around the stdout write so modal redraws and
        pwsh output cannot interleave; appends to the buffer afterwards
        (separate lock — order respected).

        Also strips two classes of pwsh-emitted control bytes that
        produce reply sequences PSReadLine then leaks into the prompt
        as literal text:

        - CSI 9001 mode toggles. Win32-input mode wraps every keystroke
          (including the literal `@` the modal trigger needs) in a CSI
          params sequence we can't see through.
        - `\\x1b[c` DA1 (Device Attributes) query. The terminal replies
          with `\\x1b[?<caps>c`; that reply re-enters our stdin and ends
          up in pwsh's input buffer as `[?...c` because PSReadLine
          consumes the leading ESC but not the rest. Pwsh continues
          fine without the reply.
        """
        if b"\x1b[" in data:
            data = (data
                .replace(b"\x1b[?9001h", b"")
                .replace(b"\x1b[?9001l", b"")
                .replace(b"\x1b[?1004h", b"")
                .replace(b"\x1b[?1004l", b"")
                .replace(b"\x1b[c", b""))
        if not data:
            return self._next_seq - 1
        with self._render_lock:
            if self._stdout is not None:
                try:
                    self._stdout.write(data)
                    self._stdout.flush()
                except Exception:
                    pass
        return self.append_output(data)

    def handle_user_input(self, data: bytes) -> None:
        """Called by the stdin pump for every chunk of bytes the user
        typed. Routes byte-by-byte: if a modal is active, the modal owns
        every byte; if not active, we forward to the PTY and watch for
        the `@`-at-column-0 modal trigger.

        Focus-in / focus-out events (`\\x1b[I` / `\\x1b[O`) are dropped
        before forwarding — PSReadLine consumes the leading ESC but
        leaves the bracket-text as literal input, which produces a
        pwsh ParserError on the next Enter.

        After dispatching every byte we let the modal observe end-of-
        chunk so it can disambiguate a bare-ESC keypress (lone Esc =
        abort) from a CSI-introducer (\\x1b followed by `[` and a final
        byte = swallow)."""
        if b"\x1b[I" in data or b"\x1b[O" in data:
            data = data.replace(b"\x1b[I", b"").replace(b"\x1b[O", b"")
        for byte in data:
            self._handle_one_byte(byte)
        if self._modal is not None and self._modal.end_of_chunk() == ModalResult.ABORT:
            self._abort_modal()

    def _handle_one_byte(self, byte: int) -> None:
        if self._modal is not None:
            self._dispatch_modal_byte(byte)
            return

        if (
            self._esc_state == 0
            and byte == ord("@")
            and self._chars_since_enter == 0
            and self._sender_self is not None
            and self._stdout is not None
        ):
            self._enter_modal()
            return

        with self._stdin_lock:
            self._proc.write(chr(byte))
        self._update_input_counter(byte)

    def _update_input_counter(self, byte: int) -> None:
        """Track 'characters typed since last Enter' robustly enough to
        detect 'fresh prompt' for modal triggering. CSI sequences (focus
        events, arrow keys, function keys) and control bytes do not
        count.

        Three-state CSI parser:
          0 — normal
          1 — saw \\x1b, expecting an introducer (most likely `[`)
          2 — inside \\x1b[ params, awaiting final byte (0x40-0x7e)

        The previous heuristic exited escape mode the moment any byte
        in 0x40-0x7e arrived, which incorrectly fired on the `[`
        introducer (0x5b). That left every CSI param digit getting
        counted as a typed character, and `_chars_since_enter` never
        actually went back to 0 after a real Enter."""
        state = self._esc_state
        if state == 1:
            if byte == 0x5b:  # [
                self._esc_state = 2
            else:
                # Single-char escape (\x1bO… SS3, \x1bP… DCS) or bare ESC.
                # Most resolve quickly; bail back to normal so we don't
                # silently consume the rest of the input stream.
                self._esc_state = 0
            return
        if state == 2:
            if 0x40 <= byte <= 0x7e:
                self._esc_state = 0
            return
        if byte == 0x1b:
            self._esc_state = 1
            return
        if byte in (0x0d, 0x0a):
            self._chars_since_enter = 0
            return
        if 0x20 <= byte <= 0x7e:
            self._chars_since_enter += 1

    def _dispatch_modal_byte(self, byte: int) -> None:
        modal = self._modal
        assert modal is not None
        result = modal.process_byte(byte)
        if result is ModalResult.COMMIT:
            self._commit_modal()
        elif result is ModalResult.ABORT:
            self._abort_modal()
        else:
            modal.render()

    def _enter_modal(self) -> None:
        assert self._sender_self is not None
        assert self._stdout is not None
        self._modal = ModalChatInput(
            self._stdout,
            self._render_lock,
            self._sender_self,
            self._participants,
        )
        self._modal.render()

    def _commit_modal(self) -> None:
        modal = self._modal
        if modal is None or self._sender_self is None:
            return
        target = modal.target
        body = modal.body
        if target is None:
            self._abort_modal()
            return

        sender_key = self._sender_self.name
        self._modal = None
        if self._chat_store is not None:
            self._chat_store.insert_message(sender_key, target, body)
            # Modal commits are the human's natural "I'm here" signal —
            # she doesn't call chat_inbox, so without this her status
            # would stay 'offline' forever and every chat_send addressed
            # to her (or @all) would render an offline warning.
            self._chat_store.mark_active(sender_key)
        with self._render_lock:
            self._stdout.write(b"\r\x1b[K")
            self._stdout.flush()
            self._render_chat_line_unlocked(sender_key, target, body)
        self._chars_since_enter = 0

    def _abort_modal(self) -> None:
        if self._modal is None:
            return
        self._modal = None
        with self._render_lock:
            if self._stdout is not None:
                try:
                    self._stdout.write(b"\r\x1b[K")
                    self._stdout.flush()
                except Exception:
                    pass
            with self._stdin_lock:
                self._proc.write("\r")
        self._chars_since_enter = 0

    # --- chat / provenance rendering -------------------------------------

    def render_chat_line(
        self,
        sender_key: str,
        recipient_key: str,
        text: str,
    ) -> None:
        """Render a sender-colored `# [...] text` comment into the wrapped
        pane via stdout-bypass.

        Per 1.2.2: chat lines always go to stdout directly, NOT through
        the PTY. Color escapes survive because PSReadLine never sees
        them. Modal-aware: when modal is open we wipe its line first
        and re-render it below.

        Stdout fallback path (when self._stdout is None — non-interactive
        or tests without a captured stdout) writes to PTY plain-text. No
        color in that case but pwsh still sees the comment.
        """
        with self._render_lock:
            self._render_chat_line_unlocked(sender_key, recipient_key, text)

    def _render_chat_line_unlocked(
        self,
        sender_key: str,
        recipient_key: str,
        text: str,
    ) -> None:
        sender = self._participants[sender_key]
        if recipient_key == "all":
            recipient_display = "all"
        else:
            recipient_display = self._participants[recipient_key].display
        ts = time.strftime("%H:%M:%S")
        body = (
            f"# [{sender.display} -> {recipient_display} {ts}] "
            f"{_flatten_for_comment(text)}"
        )
        colored = apply_color(body, sender.color)

        if self._stdout is None:
            # Non-interactive fallback (tests, piped stdout). Plain text
            # via PTY so pwsh still sees the comment in its history.
            with self._stdin_lock:
                self._proc.write(body + "\r")
            return

        payload = (colored + "\r\n").encode("utf-8")
        try:
            if self._modal is not None:
                self._stdout.write(b"\r\x1b[K")
            self._stdout.write(payload)
            self._stdout.flush()
        except Exception:
            pass
        self.append_output(payload)
        if self._modal is not None:
            self._modal.render_locked()

    def render_system_comment(self, lines: list[str]) -> None:
        """Render one or more `# [system HH:MM:SS] <line>` comments to the
        wrapped pane via stdout-bypass with the configured system color
        (default bright_black / dim). Modal-aware: wipes modal line if
        open, re-renders modal below.

        Per 1.2.2: always stdout-bypass (no PTY path) so the system
        color survives. Falls back to plain-text PTY only when stdout
        is None (non-interactive / tests).
        """
        if not lines:
            return
        ts = time.strftime("%H:%M:%S")
        plain_lines = [
            f"# [system {ts}] {_flatten_for_comment(line)}" for line in lines
        ]

        with self._render_lock:
            if self._stdout is None:
                # Non-interactive fallback: PTY plain text.
                with self._stdin_lock:
                    self._proc.write("".join(f"{line}\r" for line in plain_lines))
                return

            colored = "".join(
                f"{apply_color(line, self._system_color)}\r\n"
                for line in plain_lines
            )
            payload = colored.encode("utf-8")
            try:
                if self._modal is not None:
                    self._stdout.write(b"\r\x1b[K")
                self._stdout.write(payload)
                self._stdout.flush()
            except Exception:
                pass
            self.append_output(payload)
            if self._modal is not None:
                self._modal.render_locked()

    def send_with_provenance(self, sender_key: str, command_text: str) -> int:
        """Inject a command preceded by a plain provenance comment.

        Holds _stdin_lock once across BOTH writes (provenance + command).
        Releasing between them allows the human stdin pump to slip a
        keystroke in and break atomicity; the spec is explicit about this.
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

    # --- buffer -----------------------------------------------------------

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
