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

`ps_send`, `ps_read`, `ps_status`, `ps_signal` (shell I/O) plus
`chat_send`, `chat_inbox`, `chat_history`, `chat_participants` (chat
layer, persisted to `./terminal_share.db`).

When an LLM client calls `ps_send(text, sender)`, a colored
`# [<sender_display> HH:MM:SS] running:` provenance comment is rendered
into the wrapped pane immediately above the injected command, atomic
relative to other writers. `chat_send` renders a similar comment line
into the same pane so all three actors see the same scrollback.

`ps_read(strip_ansi=True)` returns the buffer with CSI sequences
stripped — useful for LLM consumers that want clean text instead of
PSReadLine's per-keystroke escapes.

## Smoke tests

While the wrapper is running, in a second terminal:

```pwsh
python tests/smoke_mcp.py     # ps_* round-trip
python tests/smoke_chat.py    # chat layer + ps_send atomicity
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

## What 1.1 doesn't do

- Human-typed `@code hi` from the wrapped pane (modal input layer — 1.1.1)
- "Command finished" detection via prompt sentinels (1.2)
- Auto-restart if pwsh crashes (1.2)
- Resize event forwarding after launch (PTY size is matched once at spawn)
- Acting as VS Code's default shell (1.2+)
- Authentication on the MCP endpoint (2.0)

## Breaking changes from 1.0

- `ps_send(text)` → `ps_send(text, sender)`. `sender` must be a
  participant key from `terminal_share.toml`. Unknown senders return
  `{"ok": false, "error": "unknown_sender", "name": "..."}` and don't
  write anything.
- `ps_read` gains a `strip_ansi: bool = False` parameter. Default
  preserves 1.0 behavior; opt-in returns CSI-stripped text.
