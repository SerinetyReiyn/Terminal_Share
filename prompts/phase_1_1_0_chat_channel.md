# Phase 1.1.0 — Chat channel + provenance comments

**Project:** Terminal_Share
**Phase:** 1.1.0 (first feature increment after 1.0.0 ship)
**Author:** Claudia (Desktop), drafted for Serinety -> Claude Code
**Date:** 2026-04-25 (revised 2026-04-25 14:30 PT after PSReadLine ESC discovery)
**Depends on:** 1.0.0 shipped (verified — three-actor round-trip working)

## Goal

Activate the `[participants.*]` table parked since 1.0 and turn the wrapper into a shared chat channel as well as a shared shell.

When this phase ships:

1. The four chat MCP tools exist and persist messages to a per-project SQLite DB (`./terminal_share.db`).
2. Any LLM client can send/receive messages addressed to a participant name or to `@all`.
3. When an LLM client calls `ps_send`, a provenance comment (`# [Claudia 12:31:42] running:`) is rendered into the wrapped pane immediately above the injected command, so all three actors see in scrollback who ran what.
4. `ps_send` calls are serialized — the `# [...] running:` line and the actual command are atomic from the perspective of any other writer, even with concurrent injections.

**Explicit non-goal for 1.1.0:** human-typed `@code` from inside the wrapped pane. Fixing that requires a modal input layer in the stdin pump; that's phase **1.1.1 — wrapped-pane chat input**. Until 1.1.1 ships, Serinety talks to LLMs through their existing chat surfaces, and LLMs talk to each other through the chat tools below.

## Versioning

`terminal_share/__init__.py` -> `__version__ = "1.1.0"`. Tag `v1.1.0` when ACs pass.

## Dependencies

No new third-party deps. Uses `sqlite3` (stdlib).

## File layout deltas vs 1.0.0

Add `terminal_share/chat_store.py` (NEW), `terminal_share/chat_tools.py` (NEW), modify `pty_session.py` / `server.py` / `tools.py`, add `tests/test_chat_store.py` and `tests/smoke_chat.py`. `terminal_share.db` is created in CWD on first launch; add `*.db` to `.gitignore`.

## Schema (chat_store.py)

```sql
CREATE TABLE messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    text      TEXT NOT NULL
);
CREATE TABLE reads (
    reader    TEXT NOT NULL,
    msg_id    INTEGER NOT NULL,
    PRIMARY KEY (reader, msg_id)
);
CREATE INDEX idx_messages_recipient_ts ON messages(recipient, ts);
```

Inbox semantics: a message is "unread for reader R" iff its recipient is R or `all`, AND `(R, msg.id)` is not in `reads`. `chat_inbox(R)` returns matching messages and inserts `reads` rows in a single transaction. `chat_history` ignores reads.

## MCP tool surface

All four served from `chat_tools.py`, registered with FastMCP in `server.py`.

**`chat_send(sender: str, text: str, to: str = "all") -> dict`** — Persist message; sender and to must be in [participants] (or to may be "all"). Reject unknown participants. Side effect: render plain-text comment line into wrapped pane (see Rendering below). Returns `{"ok": true, "id": int, "ts": float}`.

**`chat_inbox(reader: str, max: int = 20) -> dict`** — Returns up to max unread messages for reader (or all-broadcast); atomically marks them read. Returns `{"messages": [...], "count": int, "remaining": int}`.

**`chat_history(limit: int = 50) -> dict`** — Last N messages, oldest-first, no side effects. Same shape minus remaining.

**`chat_participants() -> dict`** — Returns the loaded [participants] table including display, role, and color metadata. Color is structured-only (see Rendering for why).

## Rendering chat into the wrapped pane

**REVISED from original draft.** Original spec called for sender-colored comment lines via ANSI escapes. Discovered during integration: PSReadLine intercepts ESC bytes from PTY stdin as keybinding prefixes — they don't pass through to pwsh as data. Sending `\x1b[35m# [...]` results in pwsh seeing literal `[35m# [...]` and parsing it as malformed array syntax (ParserError).

**Fix: render plain text only into the PTY.** No per-sender ANSI color in the wrapped pane. pwsh's built-in comment syntax highlighting will still render the line in its default comment color (dim gray), so chat lines remain visually distinct from regular output.

Wire format (one line per message, ending in `\r`, written via PTY stdin under `_stdin_lock`):

- Direct: `# [<sender_display> -> <recipient_display> HH:MM:SS] <text>`
- Broadcast: `# [<sender_display> -> all HH:MM:SS] <text>`
- Provenance: `# [<sender_display> HH:MM:SS] running:` followed by the actual command on the next line

24-hour HH:MM:SS local time. Multi-line text: collapse newlines to literal `\n` so the comment stays a single pwsh-parseable line.

The `color` field in [participants] is preserved in config and in `chat_participants` / `chat_inbox` / `chat_history` returns — LLM clients receive it as structured metadata. It is simply not applied to the in-PTY rendering. Future phase (1.2 or 1.1.0 follow-up) may add a separate stdout-bypass path that supports color for chat-only messages while keeping provenance+command on the PTY path; deferred because it requires careful handling of PSReadLine's prompt redraw.

Implementation: `pty_session.py` gets a method `render_chat_line(sender_display, recipient_display_or_all, text)` that builds the plain ASCII comment line, takes `_stdin_lock`, writes it as bytes ending in `\r`.

## Provenance + atomicity for `ps_send`

Modify `tools.ps_send` to require `sender` parameter:

1. Validate `sender` in participants. Unknown -> reject `{"ok": false, "error": "unknown_sender"}`.
2. Acquire `PtySession._stdin_lock` ONCE.
3. Write the provenance comment line AND the actual command bytes — both writes happen while holding the lock.
4. Release `_stdin_lock`.

**Important: do NOT release `_stdin_lock` between provenance and command.** A draft used a separate `_send_lock` wrapping two brief `_stdin_lock` cycles; that allowed the human stdin pump to slip a keystroke between provenance and command (pwsh would see `Get-Datx` instead of `Get-Date`). One lock, held across both writes, is correct.

Lock held a few ms total; human keystrokes contend briefly as in 1.0. Two concurrent `ps_send` calls serialize: one full pair completes before the next starts. AC#5 tests this.

Updated signature: `ps_send(text: str, sender: str) -> dict`. **Breaking change to 1.0**. Update `smoke_mcp.py` and README.

## `ps_read` adds an ANSI-strip flag

Modify: `ps_read(since_seq: int = 0, max_bytes: int = 65536, strip_ansi: bool = False) -> dict`

When `strip_ansi=True`, strip CSI sequences from returned `data` using stdlib `re` with pattern `r'\x1b\[[0-?]*[ -/]*[@-~]'` (covers CSI, which is essentially all PSReadLine emits). Code comment that we strip CSI only.

Default `False` preserves 1.0 behavior. Buffer stores raw bytes; stripping happens on read path only, per-call.

## Wrapper-level boot config

After config validation, before pwsh spawn:
1. Open / create `terminal_share.db` in CWD.
2. Pragmas at connection open: `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;`.
3. `CREATE TABLE IF NOT EXISTS` for both tables + index.
4. Bind `chat_store` instance into `PtySession` so chat tools and `ps_send` both reach it.

## Acceptance criteria

All six must pass.

1. **Existing 1.0 ACs still pass.** Smoke test (updated for new `ps_send` signature) plus normal pwsh in wrapped pane works.
2. **Chat tools registered.** Both Claudia (Desktop) and Code (CLI) see all four chat tools after MCP discovery.
3. **LLM-to-LLM round trip.** Claudia calls `chat_send(sender="claudia", to="code", text="hi from claudia")`. In Serinety's wrapped pane, a plain-text `# [Claudia -> Claude Code 12:34:56] hi from claudia` line appears (rendered in pwsh's default comment color). Code calls `chat_inbox(reader="code")` and gets the message; subsequent `chat_inbox` returns 0.
4. **Broadcast.** `chat_send(sender="code", to="all", text="team check")` renders one `# [Claude Code -> all ...] team check` line. Both Claudia and Serinety inbox it independently.
5. **Provenance + atomicity.** Claudia and Code each call `ps_send` simultaneously (two concurrent threads in smoke harness). Wrapped pane shows two complete `# [<sender> ...] running:` + command pairs, never interleaved. Verified by parsing buffer.
6. **Validation.** `chat_send(sender="bob", ...)` returns `{"ok": false, "error": "unknown_sender"}`, no DB write, no render. Same for unknown `to`. Same for `to="all"` AS sender (you can send to all, you can't BE all).

## Smoke test additions

Update `tests/smoke_mcp.py` for new `ps_send` signature. Add `tests/smoke_chat.py`: chat_participants, chat_send direct + broadcast, chat_inbox retrieves+marks-read, chat_history regardless of reads, concurrent ps_send atomicity check, unknown sender/recipient rejection. PASS/FAIL per assertion.

## Open questions to flag

1. **Comment line length cap: 4096 chars**, append `... (truncated)`, log full text to DB regardless. Surface if you'd prefer different.
2. **Buffer poisoning — RESOLVED via `ps_read(strip_ansi=True)`.**
3. **Provenance for human typing — intentionally absent.** Absence of comment is meaningful information ("human typed this"). Don't paper over with fake prefix.
4. **Color in PTY rendering — RESOLVED via plain-text fallback** (see Rendering). Color metadata still flows through structured returns; future phase may restore in-pane color via a stdout-bypass path.

## When done

1. Bump `__version__` to `"1.1.0"` in `terminal_share/__init__.py`
2. Commit + tag `v1.1.0`
3. Send chat-bridge message: "phase_1_1_0_chat_channel complete"
4. Note: existing `Claude Chat (Desktop side)` MCP is unrelated to new `chat_*` tools. Don't migrate.