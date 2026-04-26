# Phase 1.1.1 — Wrapped-pane chat input

**Project:** Terminal_Share
**Phase:** 1.1.1 (follow-up to 1.1.0 chat plumbing)
**Author:** Claudia (Desktop), drafted for Serinety -> Claude Code
**Date:** 2026-04-25
**Depends on:** 1.1.0 shipped (verified, tagged `v1.1.0` at `b753911`)

## Goal

Make `@<name> message body` work as input from inside the wrapped pane. Today, Serinety typing `@code hi` produces a pwsh ParserError because `@` is splatting syntax. After 1.1.1 ships, that exact input is intercepted by the wrapper before it reaches pwsh, treated as a chat message, and routed through the same `chat_send` path the LLM clients already use.

When this phase ships:

1. Typing `@<participant> body` at column 0 of a fresh pwsh input enters a modal chat-input mode managed by the wrapper. pwsh never sees the `@` or any subsequent keystrokes until commit or abort.
2. Modal mode renders a colored, sender-styled input prompt below pwsh's prompt: `[chat -> @code]: hi |` (cursor at end). The user can edit with backspace.
3. Pressing Enter commits the message via the existing `chat_send` path. The chat comment line renders into the wrapped pane (same plain-text format as 1.1.0). Modal mode exits; pwsh prompt is restored.
4. Pressing Esc or Ctrl-C aborts modal mode without sending. pwsh prompt is restored, no chat message persisted.
5. Backspace of the leading `@` (when the buffer is empty after backspace) exits modal mode the same way as Esc.
6. LLM-side `chat_send` calls that arrive WHILE the user is in modal mode render their chat lines above the modal prompt without corrupting the user's current typing.

**Out of scope for 1.1.1:**
- Restoring per-sender color for inbound chat lines from LLMs. Code observed during 1.1.0 wrap that the same stdout-bypass mechanism 1.1.1 builds for input could also carry colored chat output. Real, but expanding scope. Deferred to 1.2.
- Arrow keys, history recall, multi-line input, tab completion of participant names. 1.1.1 supports: typing characters, backspace, Enter, Esc, Ctrl-C. Anything else: ignore (don't crash, don't forward to PTY).

## Versioning

`terminal_share/__init__.py` -> `__version__ = "1.1.1"`. Tag `v1.1.1` when ACs pass.

## Dependencies

No new third-party deps.

## File layout deltas vs 1.1.0

- **NEW:** `terminal_share/modal.py` — `ModalChatInput` class encapsulates modal state, keystroke processing, and rendering.
- **MODIFIED:** `pty_session.py` — adds `_render_lock`, `_modal: ModalChatInput | None`, `_chars_since_enter: int`. Stdin pump grows a modal-aware branch.
- **MODIFIED:** `server.py` and `tools.py` — no changes to MCP tool surface; chat tools route through `chat_store` exactly as before.
- **NEW:** `tests/test_modal.py` — unit tests for modal state machine.
- **NEW:** `tests/smoke_modal.py` — manual smoke covering the user flows that need a real terminal (mostly verified by Serinety in-pane).

No DB schema changes.

## Architecture

### Trigger logic

The wrapper's stdin pump tracks `_chars_since_enter: int`, reset to 0 on every `\r` or `\n` byte the user types AND on wrapper startup. Increment on every other byte forwarded to PTY.

When the user types `@` AND `_chars_since_enter == 0`:
- Do NOT forward the `@` to PTY.
- Instantiate `ModalChatInput(stdout, render_lock, chat_store, participants, sender_self)`.
- Set `self._modal = modal`. The modal renders its initial prompt.
- All subsequent keystrokes route to `modal.process_byte(b)` until the modal signals exit.

`sender_self` is the participant-key for "the human at this wrapper." It comes from the [participants] config — the (only) participant with `role = "human"`. Validated at config load.

### Modal state machine

`ModalChatInput` holds:
- `buffer: str` — accumulated body text (post-target)
- `target: str | None` — set when target name is locked in (first space after `@`)
- `stage: Literal["target", "body"]` — `"target"` until first space, then `"body"`
- `cursor: int` — for now == `len(buffer)`; supports basic backspace only

Per-byte processing:
- `stage == "target"` and char in `[a-zA-Z0-9_-]`: append to a target-buffer.
- `stage == "target"` and char is space: lock target; switch to `"body"`; if target unknown, render an inline error and stay in modal so user can backspace and fix.
- `stage == "body"` and char is printable: append to `buffer`.
- `\b` (0x08) or DEL (0x7f): pop last char from buffer (or target-buffer if empty). If both empty AND user backspaces over the conceptual `@`, exit modal as ABORT.
- `\r` or `\n`: COMMIT.
- `\x1b` (ESC): ABORT.
- `\x03` (Ctrl-C): ABORT.
- Anything else: ignore silently.

After every keystroke that mutates state, redraw the line under `_render_lock`.

### Rendering strategy

Modal owns one screen line. Rendering protocol on every redraw:

1. Acquire `_render_lock`.
2. Write `\r\x1b[K` to wipe the current line.
3. Write the colored modal prompt: `\x1b[<sender_color>m[chat -> @<target_or_-> ]: \x1b[0m<buffer><cursor>`.
4. Release `_render_lock`.

The wipe-then-redraw is safe because we own the line for modal duration. PSReadLine's prompt is on a row above; we don't touch it. The modal line lives on the row below pwsh's prompt.

When modal commits or aborts:
1. Acquire `_render_lock`.
2. Wipe modal line: `\r\x1b[K`.
3. On commit only: call `chat_store.send(sender, target, body)` and write the persisted chat-comment line through PTY exactly as `chat_send` does today (plain text, sender display name, recipient, timestamp). Pwsh sees the comment, no-ops, redraws prompt naturally.
4. On abort: send a single `\r` to PTY so PSReadLine paints a fresh empty prompt. The user sees one blank prompt cycle (acceptable hiccup).
5. Clear `self._modal`.
6. Release `_render_lock`.

### Concurrent chat-line render during modal

When an LLM client calls `chat_send` and `self._modal is not None`:

1. Acquire `_render_lock`.
2. Wipe modal line: `\r\x1b[K`.
3. Write the chat comment line as a normal plain-text comment (1.1.0 path), terminated with `\r\n` — this scrolls up in the wrapped pane.
4. Re-render the modal prompt + buffer on the now-current line.
5. Release `_render_lock`.

The user perceives: their typing prompt jumps up by one line; a new chat line appears above; their cursor is still at the end of their buffer. Slight visual jitter, no input loss.

### Lock ordering

Three locks now coexist:
- `_stdin_lock` (1.0): brief, held during PTY-stdin writes from the stdin pump and from `ps_send`.
- `_buffer_lock` (1.0): brief, held during buffer appends/reads.
- `_render_lock` (NEW in 1.1.1): held during ALL stdout writes — modal redraws AND the natural PTY-output pump pass-through.

**The PTY-output pump must acquire `_render_lock` around its `sys.stdout.buffer.write + flush`.** An earlier draft exempted the pump, reasoning that pump output is high-volume; that's wrong. Without a shared lock, modal's `\r\x1b[K` wipe and the pump's pwsh-output bytes interleave at the OS level: pump output can land between wipe and redraw, or modal redraw can land mid-pwsh-output-burst. Both produce visible byte corruption that AC#7 specifically tests for.

Lock duration in practice: pump acquires render for sub-ms per iteration (one `write + flush`); modal acquires render for sub-ms per keystroke redraw. Contention is negligible.

Required lock-acquisition order to avoid deadlock: `_render_lock` before `_stdin_lock` before `_buffer_lock`. Document this in `pty_session.py`. The PTY pump itself only ever takes `_render_lock` and (separately, not nested) `_buffer_lock`, so it doesn't introduce ordering hazards.

## Validation rules

- Target name must match a key in `[participants]` OR be the literal `all`. Unknown targets: render inline error in the modal prompt (e.g., red `[chat -> @bob: unknown participant]`) but do NOT exit modal — let the user backspace and fix.
- Empty body (Enter pressed before any body characters): treat as abort (no message sent, no error). Better than persisting empty messages.
- `sender_self` is determined at wrapper startup from the participant with `role = "human"`. If no human role exists in config, the modal feature is disabled and the wrapper logs a warning at startup.

## Acceptance criteria

All seven must pass.

1. **Existing 1.1.0 ACs still pass.** smoke_mcp.py, smoke_chat.py, all unit tests still green.
2. **Modal entry + commit.** Serinety types `@code hi from serinety` + Enter at her wrapper. Code's `chat_inbox(reader="code")` returns a message with sender=serinety, recipient=code, text="hi from serinety". A plain-text `# [Serinety -> Claude Code HH:MM:SS] hi from serinety` line appears in the wrapped pane scrollback. pwsh sees no parser errors.
3. **Broadcast.** Serinety types `@all everybody hello` + Enter. Both Claudia and Code can inbox the message. The chat comment line shows `-> all` in scrollback.
4. **Esc abort.** Serinety types `@code hi` + Esc. No message persisted (verified via `chat_history` showing no new entry). Wrapper returns to pwsh prompt; she can immediately type a normal pwsh command.
5. **Ctrl-C abort.** Same as #4 but with Ctrl-C instead of Esc.
6. **Backspace-to-empty abort.** Serinety types `@code` then 5x backspace. Modal exits cleanly, pwsh prompt returns. `@` is NOT forwarded to PTY (no ParserError from a stray `@`).
7. **Concurrent LLM chat during modal.** Serinety types `@code partial messa` (no Enter). Claudia calls `chat_send(sender="claudia", to="all", text="incoming")`. The wrapped pane shows the new chat line scroll up; Serinety's modal prompt is intact with `partial messa` still in the buffer. She finishes typing `ge` + Enter; her message commits correctly.

## Smoke tests

`tests/test_modal.py` (unit, fully automatable):
- State machine transitions for each input class
- Target validation (known / unknown / `all` / empty)
- Backspace pops correctly across stage boundary
- Commit produces correct `chat_send` arguments
- Abort produces no DB write

`tests/smoke_modal.py` (manual / observational):
- Walks Serinety through the seven AC scenarios with PASS/FAIL prompts
- Note in README: this one requires a real wrapper + a real terminal; not fully automatable

## Open questions to flag

1. **What if the human types `@` mid-line?** Current spec: only triggers at column 0 (`_chars_since_enter == 0`). Mid-line `@` flows to PTY as normal — useful for actual splatting in PowerShell. Confirm this is the desired behavior.

2. **Visual placement of the modal prompt.** Spec says "below pwsh's prompt." This relies on pwsh leaving the cursor at end-of-prompt and our modal taking the next row. If PSReadLine has redrawn the prompt at a different row (e.g., after a long previous output), the modal might land in a non-obvious place. Verify visually during AC testing; if it's awkward, consider explicit `\n` before modal entry to force a clean new line.

3. **Auto-broadcast on missing target.** If user types `@` then immediately space (empty target) then body, treat as `@all` broadcast? Spec currently rejects empty target. Surface if you'd prefer the broadcast fallback.

4. **Participant name autocomplete.** Tab-completion on target name is a clear ergonomic win but adds nontrivial code. Out of scope for 1.1.1; flag as 1.2 candidate if Serinety wants it.

## When done

1. Bump `__version__` to `"1.1.1"` in `terminal_share/__init__.py`.
2. Commit + tag `v1.1.1`.
3. Send chat-bridge message: "phase_1_1_1_modal_chat_input complete" with notes-from-implementation.