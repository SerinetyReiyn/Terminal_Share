from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional

from .config import load_config
from .pty_session import PtySession
from .server import build_server


def _silence_server_logs() -> None:
    """Suppress uvicorn / mcp INFO logs so they don't interleave with the
    PTY's stdout stream and corrupt PSReadLine's screen repaints."""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _check_port_available(host: str, port: int) -> Optional[str]:
    """Pre-flight bind check. Returns an error message string on failure,
    or None if the port is available. Avoids spawning the PTY only to find
    out a moment later that uvicorn can't bind."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        return f"cannot bind {host}:{port} — {e}"
    finally:
        s.close()
    return None


# Win32 console flags
_STD_INPUT_HANDLE = -10
_ENABLE_PROCESSED_INPUT = 0x0001
_ENABLE_LINE_INPUT = 0x0002
_ENABLE_ECHO_INPUT = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200


def _enable_raw_console_mode() -> Optional[int]:
    """Disable line/echo/processed-input on the Windows console so keystrokes
    flow byte-at-a-time to the PTY (and Ctrl-C reaches the wrapped pwsh as
    \\x03 instead of raising KeyboardInterrupt in the wrapper).

    Returns the previous mode for restoration. None on non-Windows or failure.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    kernel32 = ctypes.windll.kernel32
    h = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    old = wintypes.DWORD()
    if not kernel32.GetConsoleMode(h, ctypes.byref(old)):
        return None
    new_mode = (
        old.value
        & ~_ENABLE_LINE_INPUT
        & ~_ENABLE_ECHO_INPUT
        & ~_ENABLE_PROCESSED_INPUT
    ) | _ENABLE_VIRTUAL_TERMINAL_INPUT
    if not kernel32.SetConsoleMode(h, new_mode):
        return None
    return old.value


def _restore_console_mode(old: Optional[int]) -> None:
    if old is None or os.name != "nt":
        return
    try:
        import ctypes
    except Exception:
        return
    kernel32 = ctypes.windll.kernel32
    h = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
    kernel32.SetConsoleMode(h, old)


def _stdin_pump(session: PtySession, stop_evt: threading.Event) -> None:
    while not stop_evt.is_set():
        try:
            data = sys.stdin.buffer.read1(1024)
        except (OSError, ValueError):
            return
        if not data:
            return
        try:
            session.send(data)
        except Exception:
            return


def _pty_pump(session: PtySession, stop_evt: threading.Event) -> None:
    out = sys.stdout.buffer
    while not stop_evt.is_set():
        if not session.alive:
            return
        data = session.read_pty_output(4096)
        if not data:
            if not session.alive:
                return
            time.sleep(0.005)
            continue
        try:
            out.write(data)
            out.flush()
        except Exception:
            pass
        session.append_output(data)


def main() -> int:
    parser = argparse.ArgumentParser(prog="terminal_share")
    parser.add_argument(
        "--config",
        default=None,
        help="Path to terminal_share.toml (default: ./terminal_share.toml or $TERMINAL_SHARE_CONFIG)",
    )
    args = parser.parse_args()

    config_path = (
        args.config
        or os.environ.get("TERMINAL_SHARE_CONFIG")
        or "terminal_share.toml"
    )
    cfg = load_config(config_path)
    _silence_server_logs()

    bind_error = _check_port_available(cfg.server.host, cfg.server.port)
    if bind_error:
        print(f"terminal_share: {bind_error}", file=sys.stderr)
        print(
            "  another wrapper or process is using the port. "
            "Find it with: Get-NetTCPConnection -LocalPort "
            f"{cfg.server.port} -State Listen",
            file=sys.stderr,
        )
        return 1

    # Switch to the alternate screen buffer so PTY (row 1, col 1) aligns with
    # viewport (row 1, col 1) — without this offset, PSReadLine's absolute
    # positioning paints in the wrong rows. Restored in finally below.
    sys.stdout.write("\x1b[?1049h\x1b[H")
    sys.stdout.flush()

    session = PtySession(command="pwsh.exe")
    server = build_server(session, host=cfg.server.host, port=cfg.server.port)

    old_mode = _enable_raw_console_mode()
    stop_evt = threading.Event()

    threading.Thread(
        target=_pty_pump, args=(session, stop_evt),
        name="pty-pump", daemon=True,
    ).start()
    threading.Thread(
        target=_stdin_pump, args=(session, stop_evt),
        name="stdin-pump", daemon=True,
    ).start()
    threading.Thread(
        target=lambda: server.run(transport="streamable-http"),
        name="mcp-server", daemon=True,
    ).start()

    try:
        while session.alive:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        stop_evt.set()
        session.close()
        _restore_console_mode(old_mode)
        sys.stdout.write("\x1b[?1049l")
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    sys.exit(main())
