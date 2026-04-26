# Terminal_Share

Wrap a single persistent `pwsh` session inside your VS Code integrated
terminal and expose it as an MCP server on localhost. Multiple Claude
clients (Desktop, Code) can read from and write to the same shell as you.

## Install

```pwsh
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

## Run

```pwsh
python -m terminal_share
```

Reads `terminal_share.toml` from the current working directory by default.
Override with either:

- `python -m terminal_share --config path\to\other.toml`
- `$env:TERMINAL_SHARE_CONFIG = "path\to\other.toml"; python -m terminal_share`

The flag wins over the env var.

## Configuration

See `terminal_share.toml` in the repo root for the full schema. The
`[server]` block sets host/port; the `[participants.*]` tables define
who can send / receive chat and inject commands. Each participant has a
role (`human`, `claude_desktop`, `claude_code`, or `other`), a display
name, and a named ANSI color used for chat rendering and provenance
comments.

Validation is strict — typos in role or color names fail loud rather than
silently coercing. A participant named `all` (case-insensitive) is
rejected because `all` is reserved as the chat broadcast keyword.
Exactly one `human` participant is required if any participants are
defined.

## Tools

Shell I/O (1.0):
- `ps_send(text, sender)` — inject a command preceded by a provenance comment
- `ps_read(since_seq, max_bytes, strip_ansi)` — read PTY buffer
- `ps_status` — wrapper liveness + version
- `ps_signal(name)` — currently `ctrl_c` only

Chat (1.1):
- `chat_send(sender, text, to)` — persist a message + render a `# [<sender> -> <to> HH:MM:SS] ...` comment in the wrapped pane
- `chat_inbox(reader, wait_seconds, max, mark_read)` — fetch unread messages; long-poll up to 25s when `wait_seconds > 0`; refreshes the reader's heartbeat
- `chat_history(limit)` — last N messages, no side effects
- `chat_participants` — config + per-participant `last_seen_at` and `status` (`online | stale | offline`)

Agent control (1.2):
- `agent_stop(participant)` — synthetic `/exit` for the receiving agent's loop. Does not render to the PTY. Returns `was_online` so callers know whether the stop arrived live or queued.

`ps_read(strip_ansi=True)` returns the buffer with CSI sequences
stripped — useful for agent consumers that want clean text instead of
PSReadLine's per-keystroke escapes.

## Smoke tests

While the wrapper is running, in a second terminal:

```pwsh
python tests/smoke_mcp.py     # ps_* round-trip
python tests/smoke_chat.py    # chat layer + ps_send atomicity
python tests/smoke_agents.py  # 1.2 agent-loop primitives
```

All `PASS` lines mean the system is healthy. Smoke commands should also
be visible in the wrapper's pane in real time.

## MCP client config

### Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (typically
`C:\Users\<you>\AppData\Roaming\Claude\claude_desktop_config.json`).
Merge the `terminal-share` entry into your existing `mcpServers` object
— do not replace the whole file. After saving, fully quit Desktop from
the system tray (not just close the window) and reopen.

> **Microsoft Store (packaged) installs of Desktop redirect that path.**
> Look in
> `C:\Users\<you>\AppData\Local\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude_desktop_config.json`
> instead. If you can't find a config file at the Roaming path but Desktop
> is already running with custom settings, you're on the packaged version.

#### Option A — direct streamable-HTTP (preferred)

Try this first. Works on Desktop builds that support the
`streamable-http` transport natively.

```json
{
  "mcpServers": {
    "terminal-share": {
      "transport": {
        "type": "streamable-http",
        "url": "http://127.0.0.1:8765/mcp"
      }
    }
  }
}
```

#### Option B — `mcp-remote` stdio bridge (fallback)

If Desktop doesn't list the four `ps_*` tools after a restart with Option
A, swap to this. `mcp-remote` is an npm shim that exposes a remote
streamable-HTTP server as a local stdio MCP server, which every Desktop
version speaks. Requires Node.js installed.

```json
{
  "mcpServers": {
    "terminal-share": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
    }
  }
}
```

### Claude Code

```pwsh
claude mcp add --transport http terminal-share http://127.0.0.1:8765/mcp
```

## Security

1.0 has no authentication. The MCP server binds to `127.0.0.1` so external
machines cannot reach it, but **any local process on the same machine
can**. The threat model for 1.0 assumes a single trusted operator on a
trusted workstation. If you don't trust co-resident processes, don't run
this yet — auth is on the 2.0 roadmap.

## Modal `@`-chat (1.1.1)

Type `@<participant> <body>` at column 0 of a fresh pwsh prompt and the
wrapper enters a modal chat-input mode. The `@` and everything after it
is intercepted before pwsh sees them, so there's no ParserError. A
sender-colored prompt like `[chat -> @code]: ...` renders below pwsh's
prompt; type the body, then:

- **Enter** commits the message via the same `chat_send` path the LLM
  clients use
- **Esc** or **Ctrl-C** aborts without sending
- **Backspace** past the `@` aborts cleanly

Use `@all <body>` to broadcast. Unknown target names show an inline
error in the modal prompt — backspace and fix.

## Live agent loop (1.2)

The chat tools support a polling-loop posture where a participant's
runtime (Claudia's Claude.ai session, Code's CLI session) blocks on
`chat_inbox(wait_seconds=25)`, responds via `chat_send`, repeats. The
wrapper exposes the primitives; the loop discipline (skip pre-session
backlog, broadcast cooldowns, voluntary exit on context exhaustion,
shell-command budget for operator agents) is honored agent-side.

When an `@<name>` message addresses a participant whose status is
`stale` or `offline`, the wrapper renders a `# [system HH:MM:SS] @<name>
not currently listening — message queued.` comment in the wrapped pane
so the human knows the message landed in the queue but no live response
is coming this minute.

`agent_stop(participant)` is the wrapper's lever for forced shutdown:
inserts a synthetic `/exit` from sender `system` that the receiving
agent's loop catches via the same magic-command parser that handles
user-typed `@<name> /exit`.

See `prompts/phase_1_2_live_agent_loop.md` for the full behavioral
contract and threat model.

## What 1.2 doesn't do

- Per-sender color in the wrapped pane for incoming chat lines (chat
  metadata still includes color in structured returns; in-pane color
  deferred to 1.2.1)
- Win32-input parsing for arrow keys (deferred — see 1.1.1 ship report)
- Wrapper-enforced shell-command policy (agent-side only; honor system)
- Auto-restart if pwsh crashes
- Resize event forwarding after launch (PTY size matched once at spawn)
- Acting as VS Code's default shell
- Authentication on the MCP endpoint (2.0)

## Breaking changes from 1.0

- `ps_send(text)` → `ps_send(text, sender)`. `sender` must be a
  participant key from `terminal_share.toml`. Unknown senders return
  `{"ok": false, "error": "unknown_sender", "name": "..."}` and don't
  write anything.
- `ps_read` gains a `strip_ansi: bool = False` parameter. Default
  preserves 1.0 behavior; opt-in returns CSI-stripped text.
