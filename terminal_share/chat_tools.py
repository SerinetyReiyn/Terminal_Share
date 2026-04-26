from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Callable

from .chat_store import ChatStore
from .config import Heartbeat
from .pty_session import PtySession


def _compute_status(last_seen: float | None, heartbeat: Heartbeat) -> str:
    if last_seen is None:
        return "offline"
    age = time.time() - last_seen
    if age <= heartbeat.online_seconds:
        return "online"
    if age <= heartbeat.stale_seconds:
        return "stale"
    return "offline"


def _iso_utc(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def make_chat_tools(
    session: PtySession,
    store: ChatStore,
    heartbeat: Heartbeat,
) -> dict[str, Callable]:
    """Build the chat MCP tool callables.

    1.2 additions over 1.1.x:
      - chat_inbox grows wait_seconds long-poll
      - chat_participants exposes last_seen_at + computed status
      - chat_send renders an offline-participant warning to the pane when
        the recipient (or any @all recipient) is stale/offline
      - agent_stop is a new control-plane tool (does not route through
        chat_send; bypasses participant-validation and PTY render)

    Validation rule for chat_send sender + to: sender must be a known
    participant key, never the literal "all"; to may be a known key OR
    "all". Unknown values reject before any DB write or PTY render.
    """

    def _is_known(key: str) -> bool:
        return key in session.participants

    def _participant_status(name: str) -> str:
        return _compute_status(store.last_seen_at(name), heartbeat)

    def _offline_recipients(to: str) -> list[str]:
        """Return participant names whose status is not 'online' for the
        given chat_send recipient. For 'all', expand to every participant
        except the sender (we don't know who that is here, so return all
        non-online and let the caller filter).

        Human-role participants are skipped: they're the terminal owner,
        always available by virtue of being at the wrapper pane. The
        listener / heartbeat concept only applies to LLM agents that
        poll chat_inbox. (Modal commits also call ChatStore.mark_active
        as a belt-and-suspenders refresh, see pty_session._commit_modal.)
        """
        if to == "all":
            return [
                name for name, p in session.participants.items()
                if p.role != "human" and _participant_status(name) != "online"
            ]
        target = session.participants.get(to)
        if target is None or target.role == "human":
            return []
        return [to] if _participant_status(to) != "online" else []

    def _emit_offline_warning(recipients: list[str]) -> None:
        if not recipients:
            return
        names = ", ".join(f"@{name}" for name in recipients)
        plural = "are" if len(recipients) > 1 else "is"
        session.render_system_comment([
            f"{names} {plural} not currently listening — message queued.",
            "  They'll see it when they next start a session.",
        ])

    def chat_send(sender: str, text: str, to: str = "all") -> dict:
        if not _is_known(sender) or sender == "all":
            return {"ok": False, "error": "unknown_sender", "name": sender}
        if to != "all" and not _is_known(to):
            return {"ok": False, "error": "unknown_recipient", "name": to}
        msg_id, ts = store.insert_message(sender=sender, recipient=to, text=text)
        session.render_chat_line(sender_key=sender, recipient_key=to, text=text)
        offline = [r for r in _offline_recipients(to) if r != sender]
        if offline:
            _emit_offline_warning(offline)
        return {"ok": True, "id": msg_id, "ts": ts}

    async def chat_inbox(
        reader: str,
        wait_seconds: float = 0.0,
        max: int = 20,
        mark_read: bool = True,
    ) -> dict:
        # Async + asyncio.to_thread because store.inbox does a blocking
        # threading.Event.wait() on the long-poll path. FastMCP runs sync
        # tool functions inline in the asyncio event loop, so a sync
        # implementation here would block uvicorn from accepting any
        # other HTTP request — including the chat_send that's supposed
        # to wake the long-poll. Offload to a thread so the loop stays
        # free to multiplex.
        if not _is_known(reader):
            return {"ok": False, "error": "unknown_reader", "name": reader}
        messages, remaining = await asyncio.to_thread(
            store.inbox,
            reader=reader,
            wait_seconds=wait_seconds,
            max_count=max,
            mark_read=mark_read,
        )
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
                key: {
                    "role": p.role,
                    "display": p.display,
                    "color": p.color,
                    "last_seen_at": _iso_utc(store.last_seen_at(key)),
                    "status": _participant_status(key),
                }
                for key, p in session.participants.items()
            },
            "broadcast_keyword": "all",
        }

    def agent_stop(participant: str, reason: str = "explicit") -> dict:
        """Insert a synthetic /exit message addressed to `participant` from
        sender 'system'. The receiving agent's loop sees it on its next
        chat_inbox and exits via the same magic-command parser that
        handles user-typed `@<name> /exit`.

        Bypasses chat_send (which would reject sender='system' as unknown)
        and skips both the PTY render path and the offline-participant
        warning — the synthetic /exit is silent control, not conversation.
        Returns was_online so callers know whether the stop arrived at a
        live agent or queued for whenever the participant next listens.
        """
        if not _is_known(participant):
            return {"ok": False, "error": "unknown_participant", "name": participant}
        was_online = _participant_status(participant) == "online"
        store.insert_message(sender="system", recipient=participant, text="/exit")
        return {"ok": True, "participant": participant, "was_online": was_online}

    return {
        "chat_send": chat_send,
        "chat_inbox": chat_inbox,
        "chat_history": chat_history,
        "chat_participants": chat_participants,
        "agent_stop": agent_stop,
    }
