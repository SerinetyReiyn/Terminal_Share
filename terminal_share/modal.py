from __future__ import annotations

import enum
import threading
from typing import IO, Mapping

from .config import Participant


# Foreground ANSI codes for the participant colors validated by config.py.
# Used only for the modal prompt rendered to stdout (not through PTY), so
# PSReadLine doesn't intercept the ESC bytes here.
_ANSI_FG: dict[str, int] = {
    "white": 37, "cyan": 36, "magenta": 35, "green": 32,
    "yellow": 33, "blue": 34, "red": 31,
    "bright_white": 97, "bright_cyan": 96, "bright_magenta": 95,
    "bright_green": 92, "bright_yellow": 93, "bright_blue": 94,
    "bright_red": 91,
}


class ModalResult(enum.Enum):
    CONTINUE = "continue"
    COMMIT = "commit"
    ABORT = "abort"


class ModalChatInput:
    """Modal chat-input state machine. The wrapper's stdin pump activates
    one of these on `@` at column 0 and routes every subsequent keystroke
    through `process_byte` until the modal returns COMMIT or ABORT.

    State stages:
      "target" — building the recipient name. Alphanumeric + `_-` accepted.
      "body"   — locked target, accumulating message body. Set on first space.

    The modal renders ANSI-colored prompt directly to stdout (under
    _render_lock). pwsh / PSReadLine never see these bytes — input is
    intercepted upstream — so ESC color escapes pass through to the user's
    terminal cleanly, unlike the PTY-rendered chat comments in 1.1.0.
    """

    PRINTABLE_LO = 0x20
    PRINTABLE_HI = 0x7e
    BACKSPACE_BYTES = (0x08, 0x7f)
    ENTER_BYTES = (0x0d, 0x0a)
    ESC_BYTE = 0x1b
    CTRL_C_BYTE = 0x03
    SPACE_BYTE = 0x20

    # CSI swallow states: 0=normal, 1=after ESC (waiting on `[`), 2=in CSI
    # params (waiting on a final byte 0x40-0x7e). When VS Code or pwsh
    # sends a CSI sequence (focus events, arrow keys, win32-input wrappers)
    # we don't want it to abort the modal or land in the buffer — swallow
    # the whole sequence silently.
    _ESC_NONE = 0
    _ESC_AFTER_ESC = 1
    _ESC_IN_CSI = 2

    def __init__(
        self,
        stdout: IO[bytes],
        render_lock: threading.Lock,
        sender: Participant,
        participants: Mapping[str, Participant],
    ) -> None:
        self._stdout = stdout
        self._render_lock = render_lock
        self._sender = sender
        self._participants = participants
        self._target_buf = ""
        self._target: str | None = None
        self._buffer = ""
        self._stage: str = "target"
        self._error: str | None = None
        self._first_render = True
        self._esc_state = self._ESC_NONE

    # --- public state inspection (for tests / commit handoff) -------------

    @property
    def target(self) -> str | None:
        return self._target

    @property
    def body(self) -> str:
        return self._buffer

    @property
    def stage(self) -> str:
        return self._stage

    @property
    def error(self) -> str | None:
        return self._error

    # --- byte processing --------------------------------------------------

    def process_byte(self, b: int) -> ModalResult:
        # CSI swallow: focus-in/out, arrow keys, and win32-input wrappers
        # all begin with ESC. If we treated lone-ESC as immediate abort,
        # a focus-out from clicking another pane (\x1b[O) would lose the
        # user's typed buffer. Defer the abort decision: enter
        # _ESC_AFTER_ESC and look at the next byte.
        if self._esc_state == self._ESC_AFTER_ESC:
            if b == 0x5b:  # `[` — start of CSI; swallow until final byte
                self._esc_state = self._ESC_IN_CSI
                return ModalResult.CONTINUE
            # Anything else after a bare ESC means the previous ESC was
            # a lone keypress (the user's intent: abort). The current
            # byte is dropped — acceptable since Esc-then-something is
            # rare and bytes piled up after a deliberate Esc weren't
            # going to be typed cleanly anyway.
            self._esc_state = self._ESC_NONE
            return ModalResult.ABORT
        if self._esc_state == self._ESC_IN_CSI:
            if 0x40 <= b <= 0x7e:
                self._esc_state = self._ESC_NONE
            return ModalResult.CONTINUE
        if b == self.ESC_BYTE:
            self._esc_state = self._ESC_AFTER_ESC
            return ModalResult.CONTINUE
        if b == self.CTRL_C_BYTE:
            return ModalResult.ABORT
        if b in self.ENTER_BYTES:
            return self._on_enter()
        if b in self.BACKSPACE_BYTES:
            return self._on_backspace()
        if b == self.SPACE_BYTE:
            return self._on_space()
        if self.PRINTABLE_LO < b <= self.PRINTABLE_HI:
            return self._on_printable(b)
        return ModalResult.CONTINUE

    def end_of_chunk(self) -> ModalResult:
        """Called by the dispatcher after each input chunk. If the chunk
        ended with a bare ESC (no follow-up byte), that's a lone Esc
        keypress — abort the modal."""
        if self._esc_state == self._ESC_AFTER_ESC:
            self._esc_state = self._ESC_NONE
            return ModalResult.ABORT
        return ModalResult.CONTINUE

    def _on_printable(self, b: int) -> ModalResult:
        ch = chr(b)
        if self._stage == "target":
            if ch.isalnum() or ch in "_-":
                self._target_buf += ch
                self._error = None
        else:
            self._buffer += ch
        return ModalResult.CONTINUE

    def _on_space(self) -> ModalResult:
        if self._stage == "target":
            if not self._target_buf:
                return ModalResult.CONTINUE
            self._lock_target()
        else:
            self._buffer += " "
        return ModalResult.CONTINUE

    def _lock_target(self) -> None:
        target = self._target_buf
        if target == "all" or target in self._participants:
            self._target = target
            self._error = None
        else:
            self._target = None
            self._error = f"unknown @{target}"
        self._stage = "body"

    def _on_backspace(self) -> ModalResult:
        if self._stage == "body":
            if self._buffer:
                self._buffer = self._buffer[:-1]
                return ModalResult.CONTINUE
            # Body empty — pop the implicit space and revert to target stage.
            if self._target_buf:
                self._target_buf = self._target_buf[:-1]
                self._target = None
                self._error = None
                self._stage = "target"
                return ModalResult.CONTINUE
            return ModalResult.ABORT
        # target stage
        if self._target_buf:
            self._target_buf = self._target_buf[:-1]
            self._error = None
            return ModalResult.CONTINUE
        # Nothing left — backspace over the conceptual `@`.
        return ModalResult.ABORT

    def _on_enter(self) -> ModalResult:
        if self._stage == "target":
            # User pressed Enter without typing a space — they likely meant
            # to abort. Nothing to commit (no body).
            return ModalResult.ABORT
        if self._error or self._target is None:
            # Unknown target, can't commit. Stay in modal.
            return ModalResult.CONTINUE
        if not self._buffer.strip():
            return ModalResult.ABORT
        return ModalResult.COMMIT

    # --- rendering --------------------------------------------------------

    def render(self) -> None:
        with self._render_lock:
            self._write_render_unlocked()

    def render_locked(self) -> None:
        """Render assuming the caller already holds _render_lock. Used when
        the session needs to wipe + redraw the modal under a single lock
        acquisition (e.g., concurrent chat-line rendering)."""
        self._write_render_unlocked()

    def _write_render_unlocked(self) -> None:
        if self._first_render:
            # First time: drop to a fresh line below pwsh's prompt.
            self._stdout.write(b"\r\n")
            self._first_render = False
        self._stdout.write(b"\r\x1b[K")
        line = self._build_prompt().encode("utf-8")
        self._stdout.write(line)
        self._stdout.flush()

    def wipe(self) -> None:
        """Clear the modal line. Caller must hold _render_lock."""
        self._stdout.write(b"\r\x1b[K")
        self._stdout.flush()

    def _build_prompt(self) -> str:
        target_display = self._target_buf if self._target_buf else "-"
        suffix = f" ({self._error})" if self._error else ""
        code = _ANSI_FG.get(self._sender.color, _ANSI_FG["white"])
        return (
            f"\x1b[{code}m[chat -> @{target_display}{suffix}]: \x1b[0m"
            f"{self._buffer}"
        )
