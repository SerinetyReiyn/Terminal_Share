from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .chat_store import ChatStore
from .chat_tools import make_chat_tools
from .pty_session import PtySession
from .tools import make_tools


_DESCRIPTIONS = {
    "ps_send": (
        "Inject a command into the wrapped pwsh, preceded by a colored "
        "provenance comment showing who ran it. Provenance + command are "
        "atomic relative to other writers. Returns immediately."
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
        "project DB and renders a colored # comment line into the wrapped "
        "pane so all three actors see it in scrollback."
    ),
    "chat_inbox": (
        "Return up to `max` unread messages for `reader` (direct + broadcasts), "
        "oldest-first, and atomically mark them read. `remaining` is the "
        "unread count after this batch."
    ),
    "chat_history": (
        "Return the last `limit` messages regardless of read state, "
        "oldest-first within the window. No side effects."
    ),
    "chat_participants": (
        "Return the configured participants (role, display, color) plus the "
        "broadcast keyword, so callers can discover valid sender / to values."
    ),
}


def build_server(
    session: PtySession,
    store: ChatStore,
    host: str,
    port: int,
    log_level: str = "WARNING",
) -> FastMCP:
    server = FastMCP("terminal-share", host=host, port=port, log_level=log_level)
    for name, fn in make_tools(session).items():
        server.add_tool(fn, name=name, description=_DESCRIPTIONS[name])
    for name, fn in make_chat_tools(session, store).items():
        server.add_tool(fn, name=name, description=_DESCRIPTIONS[name])
    return server
