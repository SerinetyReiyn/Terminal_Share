from __future__ import annotations

import io
import threading

import pytest

from terminal_share.config import Participant
from terminal_share.modal import ModalChatInput, ModalResult


def _participants() -> dict[str, Participant]:
    return {
        "serinety": Participant(name="serinety", role="human", display="Serinety", color="cyan"),
        "claudia": Participant(name="claudia", role="claude_desktop", display="Claudia", color="magenta"),
        "code": Participant(name="code", role="claude_code", display="Claude Code", color="green"),
    }


def _modal(sender: str = "serinety") -> tuple[ModalChatInput, io.BytesIO]:
    parts = _participants()
    stdout = io.BytesIO()
    return ModalChatInput(stdout, threading.Lock(), parts[sender], parts), stdout


def _feed(modal: ModalChatInput, text: str) -> ModalResult:
    """Feed text byte-by-byte; return last non-CONTINUE result if any,
    else CONTINUE."""
    last = ModalResult.CONTINUE
    for ch in text:
        result = modal.process_byte(ord(ch))
        if result is not ModalResult.CONTINUE:
            return result
        last = result
    return last


# --- happy path -----------------------------------------------------------

def test_target_then_body_then_enter_commits() -> None:
    modal, _ = _modal()
    result = _feed(modal, "code hi from serinety\r")
    assert result is ModalResult.COMMIT
    assert modal.target == "code"
    assert modal.body == "hi from serinety"


def test_broadcast_target_all() -> None:
    modal, _ = _modal()
    result = _feed(modal, "all hello everyone\r")
    assert result is ModalResult.COMMIT
    assert modal.target == "all"
    assert modal.body == "hello everyone"


def test_target_with_dash_and_underscore() -> None:
    parts = _participants()
    parts["my-bot_1"] = Participant(name="my-bot_1", role="other", display="MyBot", color="white")
    stdout = io.BytesIO()
    modal = ModalChatInput(stdout, threading.Lock(), parts["serinety"], parts)
    result = _feed(modal, "my-bot_1 hi\r")
    assert result is ModalResult.COMMIT
    assert modal.target == "my-bot_1"


# --- abort paths ----------------------------------------------------------

def test_esc_aborts_at_chunk_end() -> None:
    """Lone Esc keypress: defers abort until end_of_chunk so we can
    distinguish it from the leading byte of a CSI sequence."""
    modal, _ = _modal()
    for ch in "code hi":
        modal.process_byte(ord(ch))
    # First sighting of ESC: defer
    assert modal.process_byte(0x1b) is ModalResult.CONTINUE
    # End of chunk with no follow-up byte → lone Esc → abort
    assert modal.end_of_chunk() is ModalResult.ABORT


def test_esc_with_csi_follow_up_does_not_abort() -> None:
    """Focus-out (\\x1b[O) and similar CSI sequences must not abort the
    modal — they're ambient terminal events, not user intent."""
    modal, _ = _modal()
    _feed(modal, "code hello")
    # \x1b[O — focus-out
    assert modal.process_byte(0x1b) is ModalResult.CONTINUE
    assert modal.process_byte(ord("[")) is ModalResult.CONTINUE
    assert modal.process_byte(ord("O")) is ModalResult.CONTINUE
    assert modal.end_of_chunk() is ModalResult.CONTINUE
    # Body intact
    assert modal.body == "hello"


def test_esc_followed_by_non_bracket_aborts() -> None:
    """ESC followed immediately by a non-`[` byte is treated as a lone
    Esc abort (the next byte is dropped)."""
    modal, _ = _modal()
    _feed(modal, "code hi")
    assert modal.process_byte(0x1b) is ModalResult.CONTINUE
    assert modal.process_byte(ord("a")) is ModalResult.ABORT


def test_ctrl_c_aborts() -> None:
    modal, _ = _modal()
    for ch in "code hi":
        modal.process_byte(ord(ch))
    assert modal.process_byte(0x03) is ModalResult.ABORT


def test_backspace_over_at_aborts() -> None:
    """Backspacing when the buffer is empty (just typed `@`) aborts."""
    modal, _ = _modal()
    assert modal.process_byte(0x08) is ModalResult.ABORT


def test_backspace_past_target_aborts() -> None:
    """Type `@code`, backspace 5 times to clear, sixth backspace aborts."""
    modal, _ = _modal()
    for ch in "code":
        modal.process_byte(ord(ch))
    for _ in range(4):
        assert modal.process_byte(0x08) is ModalResult.CONTINUE
    assert modal.target is None
    assert modal.process_byte(0x08) is ModalResult.ABORT


def test_enter_in_target_stage_aborts() -> None:
    """Enter pressed before space (still in target stage) aborts — there's
    no body to send."""
    modal, _ = _modal()
    for ch in "code":
        modal.process_byte(ord(ch))
    assert modal.process_byte(0x0d) is ModalResult.ABORT


def test_enter_with_empty_body_aborts() -> None:
    modal, _ = _modal()
    for ch in "code   ":
        modal.process_byte(ord(ch))
    # body is "  " (whitespace) — treat as abort
    assert modal.process_byte(0x0d) is ModalResult.ABORT


# --- backspace behavior ---------------------------------------------------

def test_backspace_pops_body_first() -> None:
    modal, _ = _modal()
    _feed(modal, "code hi")
    modal.process_byte(0x08)
    assert modal.body == "h"
    assert modal.stage == "body"


def test_backspace_at_body_empty_pops_target() -> None:
    """Body empty, backspace pops target buf, reverts to target stage."""
    modal, _ = _modal()
    _feed(modal, "code ")  # stage now body, body empty
    assert modal.stage == "body"
    modal.process_byte(0x08)
    assert modal.stage == "target"
    assert modal.target is None  # un-locked
    # After backspacing the implicit space, target buf is "cod"
    modal.process_byte(0x08)
    # Sanity: still in target stage with two chars left
    assert modal.stage == "target"


def test_backspace_clears_inline_error() -> None:
    """Type unknown target + space, error should be set; backspace clears."""
    modal, _ = _modal()
    _feed(modal, "bob ")
    assert modal.error is not None
    # Backspace from body-empty-but-error pops target, reverts
    modal.process_byte(0x08)
    assert modal.error is None


def test_del_treated_as_backspace() -> None:
    modal, _ = _modal()
    _feed(modal, "code hi")
    modal.process_byte(0x7f)
    assert modal.body == "h"


# --- target validation ----------------------------------------------------

def test_unknown_target_marks_error_no_lock() -> None:
    modal, _ = _modal()
    _feed(modal, "bob ")
    assert modal.target is None
    assert modal.error == "unknown @bob"
    assert modal.stage == "body"


def test_enter_with_unknown_target_stays_in_modal() -> None:
    """Per spec: stay in modal so user can backspace and fix."""
    modal, _ = _modal()
    _feed(modal, "bob hi")
    # Enter while target invalid — should NOT commit, NOT abort
    assert modal.process_byte(0x0d) is ModalResult.CONTINUE
    assert modal.error is not None


# --- input filtering ------------------------------------------------------

def test_non_alphanum_in_target_ignored() -> None:
    """Punctuation in target stage shouldn't end up in the target buffer."""
    modal, _ = _modal()
    for ch in "co!de":
        modal.process_byte(ord(ch))
    # Lock target
    modal.process_byte(0x20)
    assert modal.target == "code"


def test_punctuation_in_body_accepted() -> None:
    modal, _ = _modal()
    _feed(modal, "code hi! how's it going?\r")
    assert modal.target == "code"
    assert modal.body == "hi! how's it going?"


def test_leading_space_in_target_ignored() -> None:
    modal, _ = _modal()
    modal.process_byte(0x20)  # space before any target chars
    assert modal.stage == "target"
    assert modal.target is None


def test_high_bytes_silently_ignored() -> None:
    modal, _ = _modal()
    # >0x7e bytes (e.g. start of UTF-8 multibyte) — silently ignored,
    # don't crash, don't lock the modal.
    modal.process_byte(0xe9)  # é first byte
    assert modal.process_byte(ord("c")) is ModalResult.CONTINUE


# --- rendering ------------------------------------------------------------

def test_render_writes_color_and_prompt() -> None:
    modal, stdout = _modal()
    modal.render()
    out = stdout.getvalue().decode("utf-8", errors="replace")
    # First render prefixed with \r\n to drop below pwsh prompt
    assert out.startswith("\r\n")
    # Cyan opener for the human's color
    assert "\x1b[36m" in out
    assert "[chat -> @-]:" in out  # placeholder for empty target
    assert out.endswith("\x1b[0m")


def test_render_after_typing_includes_target_and_buffer() -> None:
    modal, stdout = _modal()
    _feed(modal, "code hi")
    stdout.truncate(0)
    stdout.seek(0)
    modal.render()
    out = stdout.getvalue().decode("utf-8")
    assert "[chat -> @code]:" in out
    assert "hi" in out


def test_render_shows_error_for_unknown_target() -> None:
    modal, stdout = _modal()
    _feed(modal, "bob ")
    stdout.truncate(0)
    stdout.seek(0)
    modal.render()
    out = stdout.getvalue().decode("utf-8")
    assert "unknown @bob" in out


def test_render_uses_wipe_after_first() -> None:
    modal, stdout = _modal()
    modal.render()
    stdout.truncate(0)
    stdout.seek(0)
    modal.render()
    out = stdout.getvalue()
    # Subsequent renders use \r\x1b[K (no leading newline)
    assert not out.startswith(b"\r\n")
    assert b"\r\x1b[K" in out
