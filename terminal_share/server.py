from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .chat_store import ChatStore
from .chat_tools import make_chat_tools
from .config import Heartbeat
from .pty_session import PtySession
from .tools import make_tools


_DESCRIPTIONS = {
    "ps_send": (
        "Inject a command into the wrapped pwsh, preceded by a provenance "
        "comment showing who ran it. Provenance + command are atomic "
        "relative to other writers. Returns immediately."
    ),
    "ps_read": (
        "Return buffered PTY output with seq > since_seq, up to max_bytes. "
        "Pass strip_ansi=True for clean text (CSI sequences removed). "
        "Caller paginates with another call using the returned last_seq."
    ),
    "ps_status": (
        "Return wrapper liveness, PID, version, output buffer head/tail seq, "
        "buffer size in bytes, and process uptime."
    ),
    "ps_signal": (
        "Send a control signal to the PTY. Currently only name='ctrl_c' "
        "is supported."
    ),
    "chat_send": (
        "Send a chat message to a participant or to 'all'. Persists to the "
        "project DB and renders a # comment line into the wrapped pane so "
        "all actors see it in scrollback. If the recipient is offline a "
        "system warning is rendered as well."
    ),
    "chat_inbox": (
        "Return up to `max` unread messages for `reader`. Pass wait_seconds "
        "(0..25) to long-poll: blocks server-side until a new message "
        "arrives or the timeout fires. Calling this also refreshes the "
        "reader's heartbeat — see chat_participants for status."
    ),
    "chat_history": (
        "Return the last `limit` messages regardless of read state, "
        "oldest-first within the window. No side effects."
    ),
    "chat_participants": (
        "Return the configured participants with role / display / color "
        "plus per-participant last_seen_at and computed status "
        "(online | stale | offline). Heartbeat thresholds come from the "
        "[heartbeat] section of terminal_share.toml."
    ),
    "agent_stop": (
        "Inject a synthetic /exit control message addressed to "
        "`participant` from sender 'system'. The receiving agent's loop "
        "exits cleanly on its next chat_inbox. Bypasses normal chat_send "
        "validation; does NOT render to the PTY. Returns was_online so "
        "the caller knows whether the stop arrived at a live agent or "
        "queued for whenever the participant next listens."
    ),
}


def build_server(
    session: PtySession,
    store: ChatStore,
    host: str,
    port: int,
    heartbeat: Heartbeat,
    log_level: str = "WARNING",
) -> FastMCP:
    server = FastMCP("terminal-share", host=host, port=port, log_level=log_level)
    for name, fn in make_tools(session).items():
        server.add_tool(fn, name=name, description=_DESCRIPTIONS[name])
    for name, fn in make_chat_tools(session, store, heartbeat).items():
        server.add_tool(fn, name=name, description=_DESCRIPTIONS[name])
    return server
