from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .pty_session import PtySession
from .tools import make_tools


_DESCRIPTIONS = {
    "ps_send": (
        "Append text to the wrapped pwsh's stdin. Adds a trailing CR if absent. "
        "Returns immediately; does not wait for command completion."
    ),
    "ps_read": (
        "Return buffered PTY output with seq > since_seq, up to max_bytes. "
        "Caller paginates with another call using the returned last_seq."
    ),
    "ps_status": (
        "Return wrapper liveness, PID, version, output buffer head/tail seq, "
        "buffer size in bytes, and process uptime."
    ),
    "ps_signal": (
        "Send a control signal to the PTY. Currently only name='ctrl_c' is supported."
    ),
}


def build_server(
    session: PtySession,
    host: str,
    port: int,
    log_level: str = "WARNING",
) -> FastMCP:
    server = FastMCP("terminal-share", host=host, port=port, log_level=log_level)
    for name, fn in make_tools(session).items():
        server.add_tool(fn, name=name, description=_DESCRIPTIONS[name])
    return server
