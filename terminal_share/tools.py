from __future__ import annotations

from typing import Callable

from . import __version__
from .pty_session import PtySession


def make_tools(session: PtySession) -> dict[str, Callable]:
    """Build the four MCP tool callables, bound to the given session."""

    def ps_send(text: str) -> dict:
        if not text.endswith("\r"):
            text = text + "\r"
        bytes_written = session.send(text)
        st = session.status()
        return {
            "ok": True,
            "bytes_written": bytes_written,
            "next_seq_hint": st["buffer_tail_seq"],
        }

    def ps_read(since_seq: int = 0, max_bytes: int = 65536) -> dict:
        return session.read_since(since_seq=since_seq, max_bytes=max_bytes)

    def ps_status() -> dict:
        st = session.status()
        st["version"] = __version__
        return st

    def ps_signal(name: str) -> dict:
        if name == "ctrl_c":
            session.signal_ctrl_c()
            return {"ok": True}
        return {"ok": False, "error": "unsupported signal"}

    return {
        "ps_send": ps_send,
        "ps_read": ps_read,
        "ps_status": ps_status,
        "ps_signal": ps_signal,
    }
