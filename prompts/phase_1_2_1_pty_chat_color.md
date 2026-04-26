# Phase 1.2.1 — Per-sender PTY chat color

**Target version:** `1.2.1`
**Builds on:** 1.2.0 (live agent loop)
**Status:** Ready to build. Self-contained. ~30 LoC per Code's 1.1.1 estimate.
**Reviewer chain:** Code (primary), Serinety (final)
**Risk class:** UX polish. No new behavior, no new threat surface.

---

## 1. Context

1.1.0 originally tried to render chat lines with ANSI per-sender color via the PTY path. PSReadLine intercepted the leading `ESC` byte as a keybinding prefix, leaking the rest (`[35m...`) into pwsh's prompt as malformed array syntax → ParserError visible to Serinety (notebook entry #110).

1.1.0 fix (Option A): drop per-sender ANSI color from PTY rendering entirely. Plain `# [name HH:MM:SS] body` comments only. Color metadata preserved in `[participants]` config and structured `chat_inbox`/`chat_history` returns, but invisible in the pwsh pane.

1.1.1 introduced a **stdout-bypass mechanism** for modal output: writes directly to stdout (under `_render_lock`) instead of routing through the PTY/PSReadLine path. That's how the modal can show colored prompt while typing — the bytes never touch PSReadLine.

**1.2.1 combines those:** route chat lines through the same stdout-bypass mechanism, with per-sender color restored. Provenance comments on `ps_send` still go through the PTY path (audit-trail invariant; pwsh sees the comment in its history).

## 2. Design

### 2.1 Architectural separation

Two render paths from `chat_send`, chosen by message kind:

| Message kind | Path | Color | Reason |
|---|---|---|---|
| Chat line (user → user, agent → user, agent → agent) | **stdout-bypass** | Sender's color from `[participants]` | Visual differentiation, never goes through PSReadLine |
| `ps_send` provenance comment | **PTY** (existing 1.1.0 behavior) | Plain text, no ANSI | pwsh history sees the comment; it's part of the command audit trail |
| System message (offline warning from 1.2 §3.4, future system events) | **stdout-bypass** | System color (default `dim` / `bright_black`) | Distinguishable from participant messages |

The split runs inside `chat_send` (chat_tools.py) — same function that 1.2 §3.4 uses for the offline warning. The render-path decision is made per-message based on a new `kind` parameter (or sender identity check; see §2.3).

### 2.2 Output format

Stdout-bypass chat line:

```
\x1b[<sender_color>m# [<display> HH:MM:SS] <body>\x1b[0m\r\n
```

Where:
- `sender_color` is the ANSI code from `[participants.<name>].color` (1.0 already validates these as known color names; map name → ANSI code at config-load time and cache).
- `display` is `[participants.<name>].display` (already used by 1.1.0).
- `HH:MM:SS` is local-time wall clock at insert (Serinety's TZ; consistent with existing rendering).
- `body` is the raw message text. No additional escaping needed — the body is already-validated UTF-8 from `chat_send` input.

System messages use a fixed format with `dim`/`bright_black` as the default color:

```
\x1b[90m# [system HH:MM:SS] <body>\x1b[0m\r\n
```

System color overridable via a new `[system]` section in `terminal_share.toml`:

```toml
[system]
color = "bright_black"
```

### 2.3 `chat_send` signature change

Add an internal `kind` discriminator. Two acceptable shapes:

**Option A (preferred): infer from sender.**
- `sender == "system"` → system path
- `sender in [participants]` → chat-line path
- No external API change; `kind` is a private function.

**Option B: explicit param.**
- Add `kind: Literal["chat", "system"] = "chat"` to `chat_send`.
- Caller sets `kind="system"` for the offline warning (1.2 §3.4) and the `agent_stop` synthetic `/exit` (1.2 §3.5 — wait, that one bypasses chat_send entirely per the rev-2 spec, so this doesn't apply).

**My lean: Option A.** No API change, `system` sender is already a special case in 1.2 (it bypasses validation in `agent_stop`'s `ChatStore.insert_message` direct call). Inferring from sender keeps the contract clean. Code's call if he prefers explicitness.

### 2.4 Provenance path (unchanged from 1.1.0)

`ps_send(text, sender)` continues to render its provenance comment through the PTY path:

```
# [<sender> HH:MM:SS] running:
<command>
```

Plain text, no ANSI. PSReadLine sees this as comment + command, both behave naturally in pwsh's history. **Do NOT touch this path in 1.2.1.** The provenance is part of the command audit trail; it has to be in pwsh's `Get-History` to be useful.

### 2.5 Lock discipline

The stdout-bypass writes acquire `_render_lock` (1.1.1's invariant: render → stdin → buffer ordering). 1.2 §3.4 already established this for the offline warning; the chat-line path uses the same lock. No new locks introduced.

Concurrent scenarios to verify under lock:
1. Modal active + incoming chat line → modal stays intact, chat line renders above
2. Two chat lines arriving back-to-back → both render in order, no interleaving
3. Chat line + provenance comment + pwsh output all flowing simultaneously → render order preserved (chat via bypass, provenance via PTY pump, both serialized through the lock)

### 2.6 Color name → ANSI code mapping

Resolved at config-load time in 1.0's participant validation, cached on the participant struct:

| Name | ANSI fg code |
|---|---|
| `red` | `31` |
| `green` | `32` |
| `yellow` | `33` |
| `blue` | `34` |
| `magenta` | `35` |
| `cyan` | `36` |
| `white` | `37` |
| `bright_red` | `91` |
| `bright_green` | `92` |
| `bright_yellow` | `93` |
| `bright_blue` | `94` |
| `bright_magenta` | `95` |
| `bright_cyan` | `96` |
| `bright_white` | `97` |
| `bright_black` | `90` (system default) |

Unknown names rejected at config load (already 1.0 behavior — this just nails down the canonical list).

## 3. Acceptance criteria

1. **AC#1 — 1.2.0 still passes.** All existing smoke tests + unit tests green after rebase.
2. **AC#2 — Chat lines render with sender color.** When Claudia (configured `color = "magenta"`) sends a message via `chat_send`, the bytes written to stdout match `\x1b[35m# [Claudia HH:MM:SS] <body>\x1b[0m\r\n`. Verified by capturing stdout in a test harness or by visual inspection in the wrapped pane.
3. **AC#3 — PSReadLine ParserError still NOT triggered.** The 1.1.0 ANSI-via-PTY problem stays solved. Run a chat-heavy scenario in the wrapper, then run any pwsh command — no `ParserError` artifacts in the prompt or output.
4. **AC#4 — Modal input intact during colored chat.** With modal open (mid-`@code body`), an incoming chat line from the other agent renders above the modal without corrupting the modal's input state. (Same invariant as 1.1.1 AC#7, re-verified under colored output.)
5. **AC#5 — Provenance path unchanged.** `ps_send(text="Get-ChildItem", sender="serinety")` produces the existing 1.1.0 provenance comment in the PTY (plain text, visible in `Get-History`). No color, no stdout-bypass for this path.
6. **AC#6 — System messages distinguishable.** The offline-participant warning (1.2 §3.4) renders in `bright_black` (gray/dim) rather than any participant color. Visually distinct from chat lines.
7. **AC#7 — Concurrent render integrity.** Two chat lines + one provenance comment + one pwsh command output all delivered within ~100ms. Render order preserved, no interleaving, no missing bytes. (Stress test for the `_render_lock` invariant under the new bypass path.)

## 4. Implementation order

1. Add color-name → ANSI-code map and cache it on participant load (chat_tools.py or wherever participant config is parsed). (~10 min)
2. Add `[system]` section + default color to config schema. (~10 min)
3. Refactor `chat_send` to dispatch by sender kind: chat-line → stdout-bypass with color, system → stdout-bypass with system color, provenance unchanged. (~20 min)
4. Verify `_render_lock` is acquired on the new path. (~5 min — should already be true if §2.5 is followed.)
5. Update smoke_chat.py to assert ANSI bytes in the rendered output for AC#2/#5/#6. (~30 min)
6. Manual verification of AC#3/#4/#7 via wrapper session. (~15 min)
7. Tag `v1.2.1`, push.

Total estimate: ~1.5 hours, mostly testing.

## 5. Out of scope

- **Color in `chat_inbox` / `chat_history` MCP returns.** Those returns already include `color` as a structured field per 1.1.0; consumers (Claudia's loop, future tools) format it themselves. 1.2.1 only addresses the wrapper-pane rendering.
- **Per-participant color customization at runtime.** Edit `terminal_share.toml`, restart wrapper. Live config reload is out of scope for the whole 1.x line.
- **Theming / dark-vs-light terminal awareness.** Colors are absolute ANSI codes; user's terminal background may make some choices unreadable. Document, don't auto-adjust.

---

🌻 — Claudia, 2026-04-26
