# Phase 1.3 — Win32-input parsing for arrow keys (STUB)

**Target version:** `1.3.0`**Builds on:** 1.2.x **Status:** **Stub spec — needs design work before implementation.** Captures the problem, the proposed approach from Code's 1.1.1 ship report, and the open design questions that need answers before this is build-ready. **Reviewer chain:** Code (primary, since this is deeply in his terminal-handshake territory), Serinety (final) **Risk class:** Significant refactor of the stdin pump. Higher than 1.2.x. Wants its own design pass before turning into a build prompt.

---

## 1. Problem

1.1.1 introduced three CSI noise filters to keep pwsh's terminal-init handshake from leaking into the prompt as literal text. One of those filters — stripping the CSI 9001 (win32-input mode) enable from pwsh's output — has a known cost flagged in Code's 1.1.1 ship message:

> Arrow keys produce noise, same root cause: PSReadLine relies on win32-input mode for arrow-key handling on Windows; with that filtered out, `\x1b[A` etc. flow as raw VT and PSReadLine leaks `[A` into the prompt.

In practice: while typing in the wrapped pane, pressing arrow up to recall a history entry leaks `[A` into the visible prompt instead of triggering history recall. Same for left/right cursor movement (`[D`/`[C`), down arrow (`[B`), and likely Home/End/Delete/PageUp/PageDown/F-keys (all of which are special keys in win32-input mode).

This makes the wrapped pane meaningfully degraded vs. native pwsh for any keyboard-heavy work. The 1.0–1.2 line is shippable as-is — printables, Enter, Backspace, Esc work — but full keyboard fidelity wants this fix.

## 2. Proposed architecture (from Code's 1.1.1 note)

> Fixable by parsing win32-input ourselves in the stdin pump (extract the unicode char from each CSI 9001 packet, treat that as the keystroke for trigger detection, forward the original packet to PTY so PSReadLine handles it natively).

Concretely, the change set:

1. **Stop stripping the CSI 9001 enable** from pwsh's output. Let it pass through to the outer terminal (Windows Terminal, VS Code integrated terminal). Outer terminal goes into win32-input mode, encodes ALL keystrokes as CSI 9001 packets going stdin → wrapper.
2. **Stdin pump becomes a CSI 9001 parser.** Each packet shape is `\x1b[<vkey>;<scan>;<unicode>;<dwc>;<flags>_`. Extract the `<unicode>` field per packet to know what character/key was pressed.
3. **Modal trigger detection migrates from byte-watching to packet-parsing.** The 1.1.1 trigger ("byte 0x40 = `@` at column 0 with `_chars_since_enter == 0`") becomes "CSI 9001 packet with unicode field == 0x40 AND `_chars_since_enter == 0`."
4. **After detection logic runs, forward the original CSI 9001 packet to PTY.** PSReadLine receives win32-input packets in its native format and handles arrow keys, special keys, and modifier combos correctly.

## 3. Open design questions (NEEDS RESOLUTION BEFORE BUILD)

### 3.1 Disambiguation in the stdin parser

The pump currently filters two CSI noise sources from the PTY-input direction (1.1.1's modal CSI swallow):
- **DA1 reply** (`\x1b[?61;4;...c`) — re-entry from pwsh's terminal-identification query
- **Focus events** (`\x1b[I`, `\x1b[O`) — pane focus changes

Under 1.3, the pump is intentionally letting CSI 9001 packets through in much higher volume (every keystroke). The parser needs to discriminate:

| Packet shape | Behavior |
|---|---|
| `\x1b[<digits>;<digits>;<digits>;<digits>;<digits>_` (final byte `_` / 0x5f) | CSI 9001 — parse, run trigger detection, forward to PTY |
| `\x1b[?<digits>;...c` (final byte `c` after `?`) | DA1 reply — strip (1.1.1 behavior) |
| `\x1b[I` or `\x1b[O` (final byte `I` or `O` after `[`) | Focus event — strip (1.1.1 behavior) |
| `\x1b[<other>` | Currently undefined — needs decision |

**Open question 3.1.a:** what's the catch-all behavior for unrecognized CSI sequences in the new parser? Strip (safer, matches 1.1.1's defensive posture) or forward (more permissive, might let new functionality work)? My weak lean is **strip with logging** so we can see what we're losing during testing, then revisit. Code's call.

### 3.2 What about terminals that don't support CSI 9001?

If the outer terminal doesn't support win32-input mode (older terminals, non-Windows-Terminal emulators, SSH'd remote sessions), pwsh's enable request goes through to the outer terminal but the terminal ignores it. Result:
- Outer terminal stays in plain VT
- Keystrokes flow as raw VT bytes (`@` is `0x40`, arrow up is `\x1b[A`)
- Wrapper's CSI 9001 parser sees no 9001 packets, but DOES see raw VT bytes

**Open question 3.2.a:** does the parser also handle raw VT input as a fallback path? Two sub-questions:
- **Trigger detection:** can the parser detect `@` at column 0 from a raw VT byte AND from a CSI 9001 packet, and behave consistently?
- **Arrow keys:** if outer terminal sends raw `\x1b[A` (because it doesn't support 9001), do we forward it to PTY as-is (and accept that PSReadLine still leaks it like in 1.1.1) or do we re-encode it as a synthetic CSI 9001 packet so PSReadLine can handle it?

The re-encode option is heavier work but gives uniform behavior. The forward-as-is option means 1.3 only fixes arrows in modern terminals — older environments stay degraded. **My lean is forward-as-is for 1.3 with a config flag for re-encode** if the use case appears, but this needs Code's read on the implementation cost.

### 3.3 Interaction with 1.1.1 modal CSI swallow
1.1.1's modal swallows incoming CSI sequences arriving mid-typing so a focus-out doesn't abort the modal with the leading ESC and lose the body. Under 1.3:

- Most CSI sequences mid-typing are now CSI 9001 keystroke packets — explicitly NOT noise, intentional input
- The modal still wants to swallow DA1 replies and focus events (those are still noise)
- The modal needs to FEED CSI 9001 packets into its own input handler (so arrows work inside the modal too — currently they break the same way)

**Open question 3.3.a:** does the modal grow its own win32-input parser, or does it consume the already-parsed unicode-keystroke stream from the stdin pump? Cleaner: pump parses once, emits a structured "keystroke event" to whichever consumer (PTY forwarding, modal input handler), and modal/PTY both subscribe.

This implies a small refactor: pump goes from "byte stream filter" to "event-emitting parser." Worth doing for cleanliness; non-trivial code change.

### 3.4 Special keys beyond arrows

Arrow keys are the headline. The full set of keys that win32-input encodes specially:
- Arrows (Up/Down/Left/Right)
- Home, End, PageUp, PageDown
- Delete, Insert
- Function keys F1-F24
- Modifier combos (Ctrl+A, Alt+B, Ctrl+Shift+Tab, etc.)

PSReadLine binds many of these for line editing (Ctrl+A = beginning of line, Ctrl+E = end, Ctrl+W = delete word, history navigation). All of these are currently broken in the wrapped pane.

**Open question 3.4.a:** does the spec target "all special keys work" or "arrows work and we'll iterate"? Functionally these all use the same CSI 9001 parser — once arrows work, the rest probably work for free. But ACs should be explicit about scope.

### 3.5 Does this break the stdout-bypass / modal rendering (1.1.1 + 1.2.1)?

Stdout-bypass writes go from wrapper → stdout (outer terminal's stdin), not through pwsh/PTY. That path is independent of the win32-input change. **Probably unaffected**, but verify under the new stdin parser — `_render_lock` invariants need re-checking when the stdin pump is doing more work per packet.

## 4. Provisional acceptance criteria (subject to refinement after design pass)

These are sketches, not commitments — assume they'll get rewritten once §3 questions are answered.

1. **AC#1 — 1.2.x still passes.** All existing smoke tests + unit tests green after rebase.
2. **AC#2 — Arrow up recalls history.** In the wrapped pane, `Get-ChildItem` then arrow-up; pwsh shows `Get-ChildItem` in the prompt for editing or re-execution. No `[A` leaks.
3. **AC#3 — Cursor movement works.** Type `Get-ChildItem`, press left-arrow 4 times, type `-Recurse `; pwsh sees `Get-Childi-Recurse tem` (or whatever the correct insertion-position result is). No `[D` leaks.
4. **AC#4 — Modal trigger still fires on @ at column 0.** 1.1.1 AC#2 still passes under the new packet-parsing trigger detection.
5. **AC#5 — Modal accepts arrow keys for body editing.** Inside an open modal, arrow keys allow cursor movement within the typed body (or, if we don't support that, they're silently dropped — no `[A` leak into the modal).
6. **AC#6 — DA1 reply and focus events still filtered.** 1.1.1's other two CSI noise filters still work — pwsh's startup query reply doesn't leak, focus changes don't abort the modal.
7. **AC#7 — Special-key scope per §3.4 resolution.** Either "all PSReadLine bindings work" or "arrows work explicitly, others may or may not." TBD.
8. **AC#8 — Backward compat per §3.2 resolution.** Either "non-9001 terminals behave like 1.2.x (degraded but not worse)" or "non-9001 terminals re-encoded transparently." TBD.

## 5. Implementation order (sketch — needs revision after design)

1. Drop the CSI 9001 enable strip from pwsh's outbound. (~5 min)
2. Build the CSI 9001 packet parser in the stdin pump. Decide event-emission shape per §3.3. (~2 hours)
3. Migrate modal trigger detection from byte-watching to packet-parsing. (~1 hour)
4. Update modal CSI swallow to discriminate 9001 packets (forward) vs. DA1/focus (strip). (~30 min)
5. Decide §3.2 fallback path; implement chosen option. (TBD time)
6. Smoke tests for AC#2-#5. New `smoke_keys.py`. (~1.5 hours)
7. Manual verification of AC#7/#8 across terminal environments (Windows Terminal, VS Code integrated, conhost, possibly remote SSH).
8. Tag `v1.3.0`, push.

Total estimate: ~5-6 hours optimistic, more if §3.2 re-encoding is chosen.

## 6. Pre-build checklist

Before this stub turns into a build prompt, the following need to be answered:

- [ ] §3.1.a — catch-all CSI behavior (strip vs. forward)
- [ ] §3.2.a — non-9001-terminal fallback (forward-as-is vs. re-encode)
- [ ] §3.3.a — modal architecture (own parser vs. consume pump events)
- [ ] §3.4.a — scope (arrows only vs. all special keys)
- [ ] §3.5 verification — stdout-bypass stress test under the new parser

These are best resolved by Code (he owns the terminal-handshake mental model from 1.1.1) with a brief discussion turn, then I'll rev this stub into a real build spec.

## 7. Out of scope (for 1.3 specifically)

- **Mouse input.** Not currently passed through; not adding.
- **Terminal resize handling.** SIGWINCH-equivalent already works via existing pwsh handshake; not touching.
- **Custom keybinding overrides.** PSReadLine's bindings are the target; we don't add our own keybinding layer.
- **Cross-platform support beyond Windows.** The whole win32-input mode is Windows-specific by definition. Linux/macOS have different terminal semantics that aren't addressed by this phase.

---

🌻 — Claudia, 2026-04-26
**STATUS:** Stub. Do not build from this directly. Resolve §6 checklist first, then I'll produce a real build prompt.
