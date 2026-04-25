# Phase 1.0.0 — Bootstrap

**Project:** Terminal_Share **Phase:** 1.0.0 (initial release) **Author:** Claudia (Desktop), drafted for Serinety → Claude Code **Date:** 2026-04-25

## Goal

Build the smallest end-to-end loop that proves the architecture: Serinety launches a wrapper inside her VS Code integrated terminal, the wrapper owns a single persistent `pwsh.exe` session via ConPTY, and an MCP client (Claudia) can read from and write to that same session over localhost HTTP.

When this phase ships, Serinety should be able to:

1. Open her VS Code terminal in `C:\Users\Serin\Desktop\ClaudeCode\projects\Terminal_Share`
2. Run `python -m terminal_share`
3. Use that terminal as a normal pwsh prompt — type, see output, run scripts
4. From a separate Claude Desktop session, have Claudia call `ps_send("Get-Date")`and see the command appear in Serinety's terminal with its output
5. From Claudia, call `ps_read(since_seq=N)` and get back accumulated output bytes

That's it for 1.0. No multi-client concurrency, no Code integration yet, no provenance comments, no prompt-sentinel detection, no audit log. Those are 1.1 / 1.2 / 2.0.

## Versioning rule

`terminal_share/__init__.py` MUST contain `__version__ = "1.0.0"`. Phase number on disk must equal the version string. Patch bump = .z, feature = .y, architectural shift = .x. (This is Serinety's standing convention from MAS_Trader — keep to it.)

## Dependencies

- `pywinpty>=2.0` — ConPTY-backed pseudo-console on Windows
- `mcp>=1.0` — official Anthropic MCP Python SDK (use `FastMCP` for ergonomics)
- Python 3.11+

Pin in `pyproject.toml`. Do NOT add anything else for 1.0 — keep the dep graph minimal.

## File layout

```
Terminal_Share/
├── README.md
├── pyproject.toml
├── terminal_share.toml              # runtime config (server + participants)
├── prompts/
│   └── phase_1_0_0_bootstrap.md     (this file)
├── terminal_share/
│   ├── __init__.py                  # __version__ = "1.0.0"
│   ├── __main__.py                  # entrypoint, parses --config flag
│   ├── config.py                    # TOML loader + schema validation
│   ├── pty_session.py               # PTY + output buffer
│   ├── server.py                    # MCP server (FastMCP, Streamable HTTP)
│   └── tools.py                     # ps_send / ps_read / ps_status / ps_signal
└── tests/
    ├── test_config.py
    └── test_pty_session.py
```

No `.vscode/settings.json` yet — Serinety will launch the wrapper manually in 1.0. Auto-shell-replacement comes later.

## Configuration

The wrapper reads `terminal_share.toml` from the CWD at startup. Override with `--config <path>` flag or `TERMINAL_SHARE_CONFIG` env var (flag wins over env var). Use `tomllib` (Python 3.11+ stdlib, no extra dep).

Schema:

```toml
[server]
host = "127.0.0.1"
port = 8765

# Reserved by the wrapper as a broadcast keyword: @all
# A participant named "all" must be rejected at config load time.
[participants.serinety]
role    = "human"           # human | claude_desktop | claude_code | other
display = "Serinety"
color   = "cyan"            # named ANSI color (cyan, magenta, green, yellow,
                            # blue, red, white, bright_*); default "white"

[participants.claudia]
role    = "claude_desktop"
display = "Claudia"
color   = "magenta"

[participants.code]
role    = "claude_code"
display = "Claude Code"
color   = "green"
```

**1.0 only consumes** `[server]`**.** The `[participants.*]` tables are read and validated but otherwise unused until 1.1 (chat / `@mention` channel).

Validation rules `config.py` must enforce at load time:

- `[server].host` is a string, `[server].port` is an int in 1..65535; missing values fall back to `127.0.0.1` and `8765`.
- Each `[participants.<name>]` table requires `role` (one of the four enum values) and `display` (non-empty string). `color` is optional; default `"white"`.
- A participant named `all` (case-insensitive) is rejected with a clear error — it collides with the `@all` broadcast keyword.
- Exactly **one** participant with `role = "human"` must exist if any participants are defined. Zero or two-plus humans is a hard error.
- Unknown roles or unknown color names are hard errors. Don't be lenient — silent typos cost more than they save.

Ship the example block above as the actual `terminal_share.toml` in the repo root. New users edit the names in place; no auto-create logic.

## Architecture

Three concurrent things happen inside the wrapper process:

1. **User I/O pump.** Read bytes from `sys.stdin` (the user's keystrokes) and write them to the PTY's stdin. Read bytes from the PTY's stdout and write them to `sys.stdout` AND append them to the shared output buffer.
2. **MCP server.** FastMCP over Streamable HTTP, bound to the host/port from `[server]` in the config (default `127.0.0.1:8765`). Exposes the four tools below. When a tool writes to PTY stdin, the bytes appear in the user's terminal naturally because the user I/O pump is already echoing PTY stdout to `sys.stdout`.
3. **Output buffer.** A monotonically-increasing sequence of `(seq, ts, bytes)`chunks, capped at \~10 MB total. Older chunks evicted FIFO. `ps_read`returns everything with seq strictly greater than the caller's `since_seq`.

Single PtyProcess instance owned by `pty_session.PtySession`. Both the user I/O pump and the MCP tools call `session.send(text)` and `session.read_since(seq)` — the session class owns the locks.

### Threading / async model

Recommended: asyncio for the I/O pumps, run FastMCP in the same event loop. pywinpty's `read` is blocking — wrap it in `loop.run_in_executor`or use a dedicated reader thread that pushes into an `asyncio.Queue`. sys.stdin reading on Windows is also blocking; same treatment.

Acceptable alternative: two threads (stdin pump, PTY-stdout pump) + asyncio MCP server, sharing the `PtySession` which uses `threading.Lock`internally. Whichever Code prefers — the abstraction line is at `PtySession`, the concurrency model behind it is an implementation detail.

## MCP tool surface

All tools served from `terminal_share/tools.py`, registered with FastMCP in `server.py`.

### `ps_send(text: str) -> dict`

Append `text` to PTY stdin. Always appends a trailing `\r` if `text` doesn't end with one. Returns immediately — does NOT wait for command completion (that's a 1.1+ feature with prompt sentinels).

Returns: `{"ok": true, "bytes_written": int, "next_seq_hint": int}` where `next_seq_hint` is the seq of the most recent buffered output at call time (useful for the caller to use as `since_seq` on a subsequent `ps_read`).

### `ps_read(since_seq: int = 0, max_bytes: int = 65536) -> dict`

Return all buffered output with seq &gt; `since_seq`, up to `max_bytes` total (truncate from the END if exceeded — caller can paginate with another call). Decode as UTF-8 with `errors="replace"`. Do NOT strip ANSI escape codes in 1.0.

Returns: `{"data": str, "last_seq": int, "truncated": bool, "buffer_head_seq": int}`where `buffer_head_seq` is the oldest seq still in the buffer (so the caller can detect if they've fallen behind and lost data).

### `ps_status() -> dict`

Returns:

```json
{
  "alive": true,
  "pid": 12345,
  "version": "1.0.0",
  "buffer_head_seq": 0,
  "buffer_tail_seq": 1842,
  "buffer_bytes": 524288,
  "uptime_seconds": 312.4
}
```

### `ps_signal(name: str) -> dict`

For 1.0, only `name="ctrl_c"` is supported. Send `\x03` to PTY stdin. Anything else returns `{"ok": false, "error": "unsupported signal"}`.

## Non-goals (defer)

- Concurrent-writer locking between multiple MCP clients
- Echo provenance comments (`# [claudia 08:31] running:`)
- Prompt-sentinel detection / "command finished" semantics
- ANSI stripping for LLM consumers
- Audit DB / SQLite logging
- Reconnect after pwsh crash (just exit cleanly for now)
- Auth on the MCP endpoint (localhost-only is the threat model for 1.0; document the risk in README)
- Setting wrapper as VS Code default shell
- Windows resize event forwarding (PTY stays at default 80×24 for 1.0; this is OK for typical use, can be addressed in 1.1)

## Acceptance criteria

All five must pass before 1.0.0 is considered complete.

1. **Local shell works.** From a fresh VS Code terminal in the project root, `python -m terminal_share` launches and presents a normal pwsh prompt. Serinety can type `Get-ChildItem`, see the listing, run multi-line scripts, use tab completion (whatever pwsh natively supports). Ctrl-C interrupts a running command. Exiting the wrapper (Ctrl-D / `exit`) cleanly terminates pwsh and returns to the parent shell with exit code 0.

2. **MCP server reachable.** While the wrapper is running, an HTTP GET on the configured `host:port` (default `http://127.0.0.1:8765/`) returns the FastMCP server descriptor. `ps_status` returns sensible values.

3. **Config respected.** Changing `[server].port` in `terminal_share.toml`to a different value (e.g. `8800`) and relaunching the wrapper makes the MCP endpoint reachable on that port and not on the default. A malformed config (e.g. a participant named `all`, or two `human` roles) prevents startup with a clear error message.

4. **Round-trip command injection.** With Serinety idle at her prompt, an MCP client calls `ps_send("Get-Date\r")`. Within 500ms, the command appears in Serinety's terminal, executes, and the date prints. A subsequent `ps_read(since_seq=hint)` returns the captured output.

5. **Buffer survives quiet periods.** Run a long-output command (e.g. `Get-Process | Format-Table`), wait 30 seconds, then `ps_read(since_seq=0)`returns the full output (or a truncated tail if it exceeded `max_bytes`). `buffer_head_seq` and `buffer_tail_seq` are consistent.

## Smoke test script

Include `tests/smoke_mcp.py` — a minimal script that uses the `mcp` SDK's client to connect to `127.0.0.1:8765` and exercises all four tools in sequence. Should be runnable as `python tests/smoke_mcp.py` while the wrapper is running in another terminal. Output one PASS or FAIL line per tool call.

## README contents (minimum)

- One-line description
- Install: `pip install -e .`
- Run: `python -m terminal_share` (uses `./terminal_share.toml` by default)
- Override config path: `python -m terminal_share --config path\to\other.toml`
- Schema reference: point to the `[server]` and `[participants.*]` blocks in the shipped `terminal_share.toml`
- Smoke test: `python tests/smoke_mcp.py` (in a second terminal)
- MCP client config snippets for both Claude Desktop (`claude_desktop_config.json`) and Claude Code (`claude mcp add` command)
- Security note: 1.0 has no auth; localhost-bound only; any local process can connect

## Open questions to flag, not solve

If you hit any of these during implementation, surface them to Serinety rather than picking silently:

- pywinpty's exact API for sending raw control bytes (some versions need `write(b'\x03')` on the underlying handle, not the high-level write)
- Whether FastMCP supports running inside an existing asyncio loop or needs to own its own (affects threading model)
- Whether VS Code's terminal forwards Ctrl-C to the wrapper or intercepts it for its own use (affects how `ps_signal` round-trips work)

## When done

1. Bump `__version__` to `"1.0.0"` in `terminal_share/__init__.py`
2. Tag commit `v1.0.0`
3. Send a message back via the chat bridge: "phase_1_0_0_bootstrap complete"
