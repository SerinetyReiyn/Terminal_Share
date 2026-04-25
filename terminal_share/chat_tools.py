from __future__ import annotations

from typing import Callable

from .chat_store import ChatStore
from .pty_session import PtySession


def make_chat_tools(session: PtySession, store: ChatStore) -> dict[str, Callable]:
    """Build the four chat MCP tool callables.

    Validation rule for both sender and `to`:
      - sender must be a known participant key, never the literal "all"
      - to may be a known participant key OR the literal "all"
    Unknown values reject before any DB write or PTY render.
    """

    def _is_known(key: str) -> bool:
        return key in session.participants

    def chat_send(sender: str, text: str, to: str = "all") -> dict:
        if not _is_known(sender) or sender == "all":
            return {"ok": False, "error": "unknown_sender", "name": sender}
        if to != "all" and not _is_known(to):
            return {"ok": False, "error": "unknown_recipient", "name": to}
        msg_id, ts = store.insert_message(sender=sender, recipient=to, text=text)
        session.render_chat_line(sender_key=sender, recipient_key=to, text=text)
        return {"ok": True, "id": msg_id, "ts": ts}

    def chat_inbox(reader: str, max: int = 20) -> dict:
        if not _is_known(reader):
            return {"ok": False, "error": "unknown_reader", "name": reader}
        messages, remaining = store.inbox(reader=reader, max_count=max)
        return {
            "messages": messages,
            "count": len(messages),
            "remaining": remaining,
        }

    def chat_history(limit: int = 50) -> dict:
        messages = store.history(limit=limit)
        return {"messages": messages, "count": len(messages)}

    def chat_participants() -> dict:
        return {
            "participants": {
                key: {"role": p.role, "display": p.display, "color": p.color}
                for key, p in session.participants.items()
            },
            "broadcast_keyword": "all",
        }

    return {
        "chat_send": chat_send,
        "chat_inbox": chat_inbox,
        "chat_history": chat_history,
        "chat_participants": chat_participants,
    }
